"""Doctor/day/device eligibility integration, disposable localhost PostgreSQL 17.6 only."""
from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.api.dependencies.security import WorkspaceAccess, get_workspace_access
from app.api.routes import doctor_laser_weekdays
from app.database.base import Base
from app.database.session import get_db
from app.integrations.clinic.base import AvailabilityRequest
from app.integrations.clinic.tia_database_laser import TiaDatabaseLaserClinicAdapter
from app.models import (
    Appointment,
    Branch,
    BranchWorkingHour,
    ClinicLaserDevice,
    DoctorBranch,
    DoctorService,
    DoctorServiceCategory,
    DoctorWorkingHour,
    Service,
    ServiceDevicePrice,
)
from app.services.booking import BookingRuleError, calculate_availability, find_exact_slot

pytest_plugins = ("test_existing_package_additional_service_pg17",)


@pytest.fixture(scope="module", autouse=True)
def install_weekday_tables(engine):
    tables = {
        "doctor_branches", "doctor_working_hours", "branch_working_hours",
        "doctor_service_categories", "doctor_laser_device_weekday_exclusions",
        "booking_settings", "availability_blocks", "availability_block_targets",
        "availability_block_services", "doctor_availability_windows",
        "appointment_status_history",
    }
    while True:
        previous = len(tables)
        for name in tuple(tables):
            tables.update(fk.column.table.name for fk in Base.metadata.tables[name].foreign_keys)
        if len(tables) == previous:
            break
    Base.metadata.create_all(engine, tables=[Base.metadata.tables[name] for name in tables])


@pytest.fixture
def laser(ctx, engine, install_weekday_tables):
    now = datetime.now(UTC)
    # Date and weekday reflect the local branch clock, not UTC.
    day = now.astimezone(ZoneInfo("Africa/Cairo")).date() + timedelta(days=8)
    weekday = day.weekday()
    sessions = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with sessions() as db:
        branch = db.scalar(select(Branch).where(
            Branch.workspace_id == ctx["workspace"].id
        ))
        branch.timezone = "Africa/Cairo"
        db.add(DoctorBranch(
            workspace_id=ctx["workspace"].id, doctor_id=ctx["doctor"].id,
            branch_id=branch.id, is_primary=True, is_active=True,
        ))
        db.add(DoctorServiceCategory(
            workspace_id=ctx["workspace"].id,
            doctor_id=ctx["doctor"].id, category="laser",
        ))
        for index in range(7):
            db.add(BranchWorkingHour(
                workspace_id=ctx["workspace"].id, branch_id=branch.id,
                weekday=index, start_time=time(9), end_time=time(18),
            ))
            db.add(DoctorWorkingHour(
                workspace_id=ctx["workspace"].id, branch_id=branch.id,
                doctor_id=ctx["doctor"].id, weekday=index,
                start_time=time(9), end_time=time(18),
            ))
        db.add(ServiceDevicePrice(
            workspace_id=ctx["workspace"].id,
            service_id=ctx["primary"].id,
            device_key="other", device_name="Other",
            duration_minutes=30, price_minor=50000, currency="EGP",
        ))
        skin = Service(
            workspace_id=ctx["workspace"].id,
            name="Synthetic Skin",
            slug="synthetic-skin-" + uuid4().hex[:10],
            operational_category="dermatology",
            requires_laser_device=False,
            duration_minutes=30, price_minor=30000, currency="EGP",
        )
        db.add(skin)
        db.flush()
        db.add(DoctorService(
            workspace_id=ctx["workspace"].id,
            doctor_id=ctx["doctor"].id, service_id=skin.id,
        ))
        db.commit()
    app = FastAPI()
    app.include_router(doctor_laser_weekdays.router, prefix="/clinic")
    access = WorkspaceAccess(
        user=ctx["user"], workspace=ctx["workspace"], membership=ctx["member"]
    )
    app.dependency_overrides[get_workspace_access] = lambda: access

    def override_db():
        with sessions() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    with TestClient(app) as client:
        yield {
            **ctx, "client": client, "sessions": sessions, "branch": branch,
            "day": day, "weekday": weekday, "skin": skin, "now": now,
        }


def availability(laser, *, device="candela", day=None, doctor=True, service=None):
    with laser["sessions"]() as db:
        return calculate_availability(
            db, laser["workspace"], laser["branch"].id,
            service or laser["primary"].id,
            day or laser["day"],
            doctor_id=laser["doctor"].id if doctor else None,
            laser_device_key=device,
            now=laser["now"],
        )[1]


def update(laser, keys, *, weekday=None, branch_id=None, doctor_id=None):
    return laser["client"].put(
        f"/clinic/doctor-laser-weekdays/{doctor_id or laser['doctor'].id}/"
        f"{branch_id or laser['branch'].id}/{weekday if weekday is not None else laser['weekday']}",
        json={"excluded_device_keys": keys},
    )


def test_default_on_save_reload_and_actual_booking_rejection(laser):
    assert availability(laser)
    before = laser["client"].get("/clinic/doctor-laser-weekdays")
    assert before.status_code == 200
    rows = [row for row in before.json() if row["weekday"] == laser["weekday"]]
    assert len(rows) == 1
    assert rows[0]["excluded_device_keys"] == []

    response = update(laser, ["candela"])
    assert response.status_code == 200, response.text
    loaded = laser["client"].get("/clinic/doctor-laser-weekdays")
    assert loaded.status_code == 200
    assert [row for row in loaded.json() if row["weekday"] == laser["weekday"]][0]["excluded_device_keys"] == ["candela"]

    assert availability(laser, device="candela", doctor=False) == []
    with pytest.raises(BookingRuleError):
        availability(laser, device="candela")
    assert availability(laser, device="other")
    assert availability(laser, device="candela", day=laser["day"] + timedelta(days=1))
    assert availability(laser, device=None, service=laser["skin"].id)

    with laser["sessions"]() as db:
        candidate = calculate_availability(
            db, laser["workspace"], laser["branch"].id,
            laser["primary"].id, laser["day"], doctor_id=laser["doctor"].id,
            laser_device_key="other", now=laser["now"],
        )[1][0]
        with pytest.raises(BookingRuleError):
            find_exact_slot(
                db, laser["workspace"], laser["branch"].id,
                laser["primary"].id, laser["doctor"].id,
                candidate.start_at, laser_device_key="candela",
                minimum_notice_minutes_override=0,
            )


def test_all_off_and_new_device_default_on(laser):
    assert update(laser, ["candela", "other"]).status_code == 200
    assert availability(laser, device="candela", doctor=False) == []
    assert availability(laser, device="other", doctor=False) == []
    assert availability(laser, device=None, service=laser["skin"].id)
    with laser["sessions"]() as db:
        device = ClinicLaserDevice(
            workspace_id=laser["workspace"].id,
            device_key="new-device", name="New Device",
        )
        db.add(device)
        db.flush()
        db.add(ServiceDevicePrice(
            workspace_id=laser["workspace"].id,
            service_id=laser["primary"].id,
            device_key="new-device", device_name="New Device",
            duration_minutes=30, price_minor=50000,
        ))
        db.commit()
    assert availability(laser, device="new-device")


def test_invalid_device_wrong_scope_and_unscheduled_weekday_rejected(laser):
    assert update(laser, ["not-registered"]).status_code == 422
    assert update(laser, ["candela", "candela"]).status_code == 422
    assert update(laser, ["candela"], doctor_id=uuid4()).status_code == 404
    assert update(laser, ["candela"], branch_id=uuid4()).status_code == 404
    assert update(laser, [], weekday=7).status_code == 422
    with laser["sessions"]() as db:
        rows = list(db.scalars(select(DoctorWorkingHour).where(
            DoctorWorkingHour.workspace_id == laser["workspace"].id,
            DoctorWorkingHour.doctor_id == laser["doctor"].id,
            DoctorWorkingHour.weekday == laser["weekday"],
        )))
        for row in rows:
            db.delete(row)
        db.commit()
    assert update(laser, ["candela"]).status_code == 409


def test_internal_ai_laser_adapter_filters_candidate_doctors(laser):
    adapter_request = AvailabilityRequest(
        branch_id=str(laser["branch"].id),
        service_id=str(laser["primary"].id),
        booking_date=laser["day"],
        laser_device_key="candela",
        now=laser["now"],
    )
    with laser["sessions"]() as db:
        adapter = TiaDatabaseLaserClinicAdapter(db=db, workspace=laser["workspace"])
        before = adapter.get_availability(adapter_request)
        assert before.slots
    assert update(laser, ["candela"]).status_code == 200
    with laser["sessions"]() as db:
        adapter = TiaDatabaseLaserClinicAdapter(db=db, workspace=laser["workspace"])
        after = adapter.get_availability(adapter_request)
        assert after.slots == ()
    # The existing appointment is not cancelled by changing weekday policy.
    with laser["sessions"]() as db:
        appt = db.get(Appointment, laser["appointment"].id)
        assert appt.status == "completed"


def test_reception_without_clinic_operations_cannot_change_devices(laser):
    membership = laser["member"]
    original_role = membership.role
    original_sections = getattr(membership, "section_access", None)
    try:
        membership.role = "member"
        membership.section_access = {"clinic_operations": False, "administration": False}
        read = laser["client"].get("/clinic/doctor-laser-weekdays")
        write = update(laser, ["candela"])
        assert read.status_code == 403
        assert write.status_code == 403
    finally:
        membership.role = original_role
        membership.section_access = original_sections


def test_direct_appointment_creation_rejects_excluded_doctor_and_device(laser):
    from app.services.appointment_creation import create_appointment_operation
    from app.services.appointment_operations import AppointmentOperationError

    with laser["sessions"]() as db:
        candidate = calculate_availability(
            db, laser["workspace"], laser["branch"].id,
            laser["primary"].id, laser["day"],
            doctor_id=laser["doctor"].id, laser_device_key="candela",
            now=laser["now"],
        )[1][0]
        start = candidate.start_at
    assert update(laser, ["candela"]).status_code == 200

    with laser["sessions"]() as db:
        with pytest.raises(AppointmentOperationError, match="not available"):
            create_appointment_operation(
                db,
                workspace=laser["workspace"],
                patient_id=laser["patient"].id,
                branch_id=laser["branch"].id,
                doctor_id=laser["doctor"].id,
                service_id=laser["primary"].id,
                requested_start_at=start,
                created_by_user_id=laser["user"].id,
                laser_device_key="candela",
                idempotency_key="synthetic-forbidden-direct-create",
            )
        db.rollback()
        persisted = db.scalars(select(Appointment).where(
            Appointment.workspace_id == laser["workspace"].id,
            Appointment.idempotency_key == "synthetic-forbidden-direct-create",
        )).all()
        assert not persisted


def test_internal_ai_adapter_write_refuses_excluded_device(laser):
    from app.integrations.clinic.base import CreateAppointmentRequest

    with laser["sessions"]() as db:
        slot = calculate_availability(
            db, laser["workspace"], laser["branch"].id, laser["primary"].id,
            laser["day"], doctor_id=laser["doctor"].id,
            laser_device_key="candela", now=laser["now"],
        )[1][0]
    assert update(laser, ["candela"]).status_code == 200
    request = CreateAppointmentRequest(
        patient_id=str(laser["patient"].id),
        branch_id=str(laser["branch"].id),
        service_id=str(laser["primary"].id),
        doctor_id=str(laser["doctor"].id),
        start_at=slot.start_at,
        operation_id="synthetic-ai-forbidden-write",
        laser_device_key="candela",
    )
    with laser["sessions"]() as db:
        adapter = TiaDatabaseLaserClinicAdapter(db=db, workspace=laser["workspace"])
        with pytest.raises(BookingRuleError, match="not available"):
            adapter.create_appointment(request)
        db.rollback()
        assert not db.scalars(select(Appointment).where(
            Appointment.workspace_id == laser["workspace"].id,
            Appointment.idempotency_key == "synthetic-ai-forbidden-write",
        )).all()


def test_branch_and_second_doctor_are_independent(laser):
    from app.models import Doctor, Staff

    with laser["sessions"]() as db:
        second_branch = Branch(
            workspace_id=laser["workspace"].id,
            name="Synthetic Secondary Branch",
            code="S" + uuid4().hex[:8],
            timezone="Africa/Cairo",
        )
        second_staff = Staff(
            workspace_id=laser["workspace"].id,
            first_name="Secondary", last_name="Doctor",
        )
        db.add_all([second_branch, second_staff])
        db.flush()
        other_doctor = Doctor(workspace_id=laser["workspace"].id, staff_id=second_staff.id)
        db.add(other_doctor)
        db.flush()
        db.add_all([
            DoctorBranch(
                workspace_id=laser["workspace"].id,
                doctor_id=laser["doctor"].id,
                branch_id=second_branch.id,
                is_active=True,
            ),
            DoctorBranch(
                workspace_id=laser["workspace"].id,
                doctor_id=other_doctor.id,
                branch_id=laser["branch"].id,
                is_active=True,
            ),
            DoctorService(
                workspace_id=laser["workspace"].id,
                doctor_id=other_doctor.id,
                service_id=laser["primary"].id,
            ),
            DoctorServiceCategory(
                workspace_id=laser["workspace"].id,
                doctor_id=other_doctor.id, category="laser",
            ),
            DoctorWorkingHour(
                workspace_id=laser["workspace"].id,
                doctor_id=other_doctor.id,
                branch_id=laser["branch"].id,
                weekday=laser["weekday"], start_time=time(9), end_time=time(18),
            ),
            DoctorWorkingHour(
                workspace_id=laser["workspace"].id,
                doctor_id=laser["doctor"].id,
                branch_id=second_branch.id,
                weekday=laser["weekday"], start_time=time(9), end_time=time(18),
            ),
            BranchWorkingHour(
                workspace_id=laser["workspace"].id,
                branch_id=second_branch.id,
                weekday=laser["weekday"], start_time=time(9), end_time=time(18),
            ),
        ])
        db.commit()

    assert update(laser, ["candela"]).status_code == 200
    first_branch_slots = availability(laser, doctor=False)
    assert first_branch_slots
    assert {slot.doctor_id for slot in first_branch_slots} == {other_doctor.id}

    with laser["sessions"]() as db:
        second_branch_slots = calculate_availability(
            db, laser["workspace"], second_branch.id,
            laser["primary"].id, laser["day"],
            doctor_id=laser["doctor"].id,
            laser_device_key="candela", now=laser["now"],
        )[1]
        assert second_branch_slots
        assert {slot.doctor_id for slot in second_branch_slots} == {laser["doctor"].id}


def test_removing_workday_resets_device_exclusions_to_default_on(laser):
    from app.api.routes.doctor_admin import _replace_doctor_hours
    from app.models.doctor_laser_weekday_exclusion import DoctorLaserWeekdayExclusion
    from app.schemas.clinic import WorkingHoursReplace

    assert update(laser, ["candela"]).status_code == 200
    assert not availability(laser, doctor=False)
    with laser["sessions"]() as db:
        hours = list(db.scalars(select(DoctorWorkingHour).where(
            DoctorWorkingHour.workspace_id == laser["workspace"].id,
            DoctorWorkingHour.doctor_id == laser["doctor"].id,
            DoctorWorkingHour.branch_id == laser["branch"].id,
        )))
        remaining_days = [
            {"weekday": row.weekday, "start_time": row.start_time, "end_time": row.end_time}
            for row in hours if row.weekday != laser["weekday"]
        ]
        _replace_doctor_hours(
            db, workspace=laser["workspace"],
            doctor_id=laser["doctor"].id,
            payload=WorkingHoursReplace(intervals=remaining_days),
        )
        db.commit()
        exclusions = db.scalars(select(DoctorLaserWeekdayExclusion).where(
            DoctorLaserWeekdayExclusion.workspace_id == laser["workspace"].id,
            DoctorLaserWeekdayExclusion.doctor_id == laser["doctor"].id,
            DoctorLaserWeekdayExclusion.branch_id == laser["branch"].id,
            DoctorLaserWeekdayExclusion.weekday == laser["weekday"],
        )).all()
        assert not exclusions
        db.add(DoctorWorkingHour(
            workspace_id=laser["workspace"].id,
            doctor_id=laser["doctor"].id,
            branch_id=laser["branch"].id,
            weekday=laser["weekday"], start_time=time(9), end_time=time(18),
        ))
        db.commit()
    assert availability(laser, device="candela")


def test_grouped_ai_booking_cannot_use_excluded_laser_device(laser):
    from app.services.agent_v2.multi_service_booking import (
        MultiServiceBookingUnavailable,
        execute_booking_proposal,
    )
    from app.services.agent_v2.state import BookingProposal

    with laser["sessions"]() as db:
        slot = calculate_availability(
            db, laser["workspace"], laser["branch"].id,
            laser["primary"].id, laser["day"],
            doctor_id=laser["doctor"].id,
            laser_device_key="candela", now=laser["now"],
        )[1][0]

    proposal = BookingProposal(
        proposal_id="grouped-device-" + uuid4().hex[:10],
        patient_id=str(laser["patient"].id),
        service_ids=[str(laser["primary"].id), str(laser["face"].id)],
        main_service_id=str(laser["primary"].id),
        additional_service_ids=[str(laser["face"].id)],
        branch_id=str(laser["branch"].id),
        doctor_id=str(laser["doctor"].id),
        start_at=slot.start_at,
        end_at=slot.end_at,
        device_key="candela",
        additional_service_device_keys={str(laser["face"].id): "candela"},
        created_at=laser["now"],
        expires_at=laser["now"] + timedelta(minutes=15),
    )
    assert update(laser, ["candela"]).status_code == 200

    with laser["sessions"]() as db:
        before = db.scalar(select(Appointment.id).where(
            Appointment.workspace_id == laser["workspace"].id,
            Appointment.idempotency_key == "grouped-device-exclusion:main",
        ))
        assert before is None
        with pytest.raises(MultiServiceBookingUnavailable):
            execute_booking_proposal(
                db,
                workspace=laser["workspace"],
                patient=laser["patient"],
                proposal=proposal,
                idempotency_key="grouped-device-exclusion",
            )
        db.rollback()
        after = db.scalar(select(Appointment.id).where(
            Appointment.workspace_id == laser["workspace"].id,
            Appointment.idempotency_key == "grouped-device-exclusion:main",
        ))
        assert after is None


def test_archived_visiting_window_cannot_create_slots_or_weekday_device_choices(laser):
    from app.models import Doctor, DoctorAvailabilityWindow

    local_tz = ZoneInfo("Africa/Cairo")
    local_start = datetime.combine(laser["day"], time(10, 0), tzinfo=local_tz)
    local_end = local_start + timedelta(hours=1)
    with laser["sessions"]() as db:
        doctor = db.get(Doctor, laser["doctor"].id)
        doctor.doctor_type = "visiting"  # Synthetic legacy metadata only.
        hours = db.scalars(select(DoctorWorkingHour).where(
            DoctorWorkingHour.workspace_id == laser["workspace"].id,
            DoctorWorkingHour.doctor_id == doctor.id,
            DoctorWorkingHour.branch_id == laser["branch"].id,
            DoctorWorkingHour.weekday == laser["weekday"],
        )).all()
        for hour in hours:
            db.delete(hour)
        db.add(DoctorAvailabilityWindow(
            workspace_id=laser["workspace"].id,
            doctor_id=doctor.id, branch_id=laser["branch"].id,
            start_at=local_start.astimezone(UTC),
            end_at=local_end.astimezone(UTC),
        ))
        db.commit()

    rows = laser["client"].get("/clinic/doctor-laser-weekdays")
    assert rows.status_code == 200, rows.text
    matching = [row for row in rows.json() if row["weekday"] == laser["weekday"]]
    assert not matching
    assert update(laser, ["candela"]).status_code == 409
    assert availability(laser, device="candela", doctor=False) == []
    assert availability(laser, device="other", doctor=False) == []
    with laser["sessions"]() as db:
        assert db.scalar(select(DoctorAvailabilityWindow.id).where(
            DoctorAvailabilityWindow.workspace_id == laser["workspace"].id,
            DoctorAvailabilityWindow.doctor_id == laser["doctor"].id,
        )) is not None

    # Once weekly hours are explicitly configured, a legacy type is irrelevant.
    with laser["sessions"]() as db:
        db.add(DoctorWorkingHour(
            workspace_id=laser["workspace"].id,
            doctor_id=laser["doctor"].id,
            branch_id=laser["branch"].id,
            weekday=laser["weekday"], start_time=time(9), end_time=time(18),
        ))
        db.commit()
    assert availability(laser, device="candela")
    assert update(laser, ["candela"]).status_code == 200
    assert availability(laser, device="candela", doctor=False) == []
    assert availability(laser, device="other")


def test_two_work_intervals_share_one_device_choice(laser):
    with laser["sessions"]() as db:
        hour = db.scalar(select(DoctorWorkingHour).where(
            DoctorWorkingHour.workspace_id == laser["workspace"].id,
            DoctorWorkingHour.doctor_id == laser["doctor"].id,
            DoctorWorkingHour.branch_id == laser["branch"].id,
            DoctorWorkingHour.weekday == laser["weekday"],
        ))
        hour.end_time = time(12)
        db.add(DoctorWorkingHour(
            workspace_id=laser["workspace"].id,
            doctor_id=laser["doctor"].id,
            branch_id=laser["branch"].id,
            weekday=laser["weekday"],
            start_time=time(14), end_time=time(17),
        ))
        db.commit()
    response = laser["client"].get("/clinic/doctor-laser-weekdays")
    assert response.status_code == 200
    matches = [row for row in response.json() if row["weekday"] == laser["weekday"]]
    assert len(matches) == 1
    assert update(laser, ["candela"]).status_code == 200
    assert availability(laser, device="candela", doctor=False) == []
    assert availability(laser, device="other")


def test_existing_future_appointment_is_not_cancelled_after_device_change(laser):
    from app.services.appointment_creation import create_appointment_operation

    with laser["sessions"]() as db:
        slot = calculate_availability(
            db, laser["workspace"], laser["branch"].id,
            laser["primary"].id, laser["day"],
            doctor_id=laser["doctor"].id,
            laser_device_key="candela", now=laser["now"],
        )[1][0]
        created = create_appointment_operation(
            db,
            workspace=laser["workspace"],
            patient_id=laser["patient"].id,
            branch_id=laser["branch"].id,
            doctor_id=laser["doctor"].id,
            service_id=laser["primary"].id,
            requested_start_at=slot.start_at,
            created_by_user_id=laser["user"].id,
            laser_device_key="candela",
            idempotency_key="synthetic-preexisting-future-" + uuid4().hex,
        )
        created_id = created.id
        saved_status = created.status
        db.commit()
    assert update(laser, ["candela"]).status_code == 200
    with laser["sessions"]() as db:
        existing = db.get(Appointment, created_id)
        assert existing is not None
        assert existing.status == saved_status
        assert existing.start_at == slot.start_at
        assert existing.laser_device_key == "candela"


def test_legacy_weekday_window_is_not_used_anywhere_in_laser_api(laser):
    from app.api.routes import doctor_laser_weekdays

    assert not hasattr(doctor_laser_weekdays, "_window_weekdays")
    rows = laser["client"].get("/clinic/doctor-laser-weekdays")
    assert rows.status_code == 200
    assert {item["weekday"] for item in rows.json()} == set(range(7))


def test_edit_legacy_visiting_doctor_weekly_hours_keeps_ids_windows_and_history(laser):
    from app.models import Doctor, DoctorAvailabilityWindow
    from app.schemas.clinic_setup_v2 import WorkingHoursUpdateV2
    from app.services.clinic_setup_v2 import (
        build_setup_v2_snapshot,
        replace_regular_doctor_hours_v2,
    )

    target_day = laser["day"]
    with laser["sessions"]() as db:
        doctor = db.get(Doctor, laser["doctor"].id)
        doctor.doctor_type = "visiting"  # synthetic archived discriminator
        old_window = DoctorAvailabilityWindow(
            workspace_id=laser["workspace"].id,
            doctor_id=doctor.id,
            branch_id=laser["branch"].id,
            start_at=datetime.combine(target_day, time(11), tzinfo=ZoneInfo("Africa/Cairo")).astimezone(UTC),
            end_at=datetime.combine(target_day, time(12), tzinfo=ZoneInfo("Africa/Cairo")).astimezone(UTC),
        )
        db.add(old_window)
        db.commit()
        window_id = old_window.id
    with laser["sessions"]() as db:
        # Editing working hours must not flip types implicitly or delete dated history.
        replace_regular_doctor_hours_v2(
            db,
            workspace=laser["workspace"],
            doctor_id=laser["doctor"].id,
            payload=WorkingHoursUpdateV2(intervals=[{
                "weekday": laser["weekday"],
                "start_time": "09:00",
                "end_time": "18:00",
            }]),
        )
        db.commit()
        assert db.get(Doctor, laser["doctor"].id).doctor_type == "visiting"
        assert db.get(DoctorAvailabilityWindow, window_id) is not None
        assert db.get(Appointment, laser["appointment"].id) is not None
        snapshot = build_setup_v2_snapshot(db, workspace=laser["workspace"])
        matching = [row for row in snapshot.doctors if row.id == laser["doctor"].id]
        assert len(matching) == 1
        assert matching[0].doctor_type == "regular"  # compatibility presentation
        assert not hasattr(matching[0], "visiting_windows")
        assert [row.weekday for row in matching[0].weekly_hours] == [laser["weekday"]]
    assert availability(laser, device="candela")


def test_cross_workspace_laser_doctor_scope_is_enforced(laser):
    from app.models import Doctor, Staff, Workspace

    with laser["sessions"]() as db:
        workspace = Workspace(name="Other Synthetic Workspace", slug="other-weekday-" + uuid4().hex[:10])
        db.add(workspace)
        db.flush()
        branch = Branch(workspace_id=workspace.id, name="Other Branch", code="T" + uuid4().hex[:8])
        staff = Staff(workspace_id=workspace.id, first_name="Other", last_name="Doctor")
        db.add_all([branch, staff])
        db.flush()
        doctor = Doctor(workspace_id=workspace.id, staff_id=staff.id)
        db.add(doctor)
        db.flush()
        db.add_all([
            DoctorBranch(
                workspace_id=workspace.id,
                doctor_id=doctor.id,
                branch_id=branch.id, is_active=True,
            ),
            DoctorServiceCategory(
                workspace_id=workspace.id,
                doctor_id=doctor.id, category="laser",
            ),
            DoctorWorkingHour(
                workspace_id=workspace.id,
                doctor_id=doctor.id,
                branch_id=branch.id, weekday=laser["weekday"],
                start_time=time(9), end_time=time(18),
            ),
        ])
        db.commit()
        doctor_id, branch_id = doctor.id, branch.id
    result = laser["client"].get("/clinic/doctor-laser-weekdays")
    assert result.status_code == 200
    assert all(item["doctor_id"] != str(doctor_id) for item in result.json())
    assert update(laser, ["candela"], doctor_id=doctor_id, branch_id=branch_id).status_code == 404


def test_ai_adapter_reschedule_denies_now_excluded_laser_device(laser):
    from app.integrations.clinic.base import RescheduleAppointmentRequest
    from app.services.appointment_creation import create_appointment_operation

    with laser["sessions"]() as db:
        slots = calculate_availability(
            db, laser["workspace"], laser["branch"].id, laser["primary"].id,
            laser["day"], doctor_id=laser["doctor"].id,
            laser_device_key="candela", now=laser["now"],
        )[1]
        assert len(slots) > 3
        created = create_appointment_operation(
            db, workspace=laser["workspace"],
            patient_id=laser["patient"].id,
            branch_id=laser["branch"].id,
            doctor_id=laser["doctor"].id,
            service_id=laser["primary"].id,
            requested_start_at=slots[0].start_at,
            created_by_user_id=laser["user"].id,
            laser_device_key="candela",
            idempotency_key="synthetic-reschedule-" + uuid4().hex,
        )
        appointment_id = created.id
        old_start = created.start_at
        old_status = created.status
        db.commit()

    assert update(laser, ["candela"]).status_code == 200
    request = RescheduleAppointmentRequest(
        patient_id=str(laser["patient"].id),
        appointment_id=str(appointment_id),
        start_at=slots[-1].start_at,
        operation_id="deny-excluded-reschedule-" + uuid4().hex,
        laser_device_key="candela",
    )
    with laser["sessions"]() as db:
        adapter = TiaDatabaseLaserClinicAdapter(db=db, workspace=laser["workspace"])
        with pytest.raises(BookingRuleError):
            adapter.reschedule_appointment(request)
        db.rollback()
        old = db.get(Appointment, appointment_id)
        assert old.status == old_status
        assert old.start_at == old_start
        assert old.laser_device_key == "candela"
