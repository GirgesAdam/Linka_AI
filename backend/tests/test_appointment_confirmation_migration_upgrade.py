from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from alembic import command
from app.core.config import settings
from app.models.appointment import Appointment
from app.models.appointment_status_history import AppointmentStatusHistory
from app.models.branch import Branch
from app.models.doctor import Doctor
from app.models.patient import Patient
from app.models.service import Service
from app.models.staff import Staff
from app.models.user import User
from app.models.workspace import Workspace

BACKEND = Path(__file__).resolve().parent.parent


def _upgrade(url: str, revision: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIGRATION_DATABASE_URL", url)
    config = Config(str(BACKEND / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND / "alembic"))
    command.upgrade(config, revision)


@pytest.fixture
def migration_database(monkeypatch: pytest.MonkeyPatch):
    base = make_url(settings.database_url)
    database_name = f"tia_confirmation_upgrade_{uuid4().hex[:12]}"
    admin_url = base.set(database="postgres")
    admin_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        with admin_engine.connect() as connection:
            connection.exec_driver_sql(f'CREATE DATABASE "{database_name}"')
    except OperationalError:
        admin_engine.dispose()
        pytest.skip("PostgreSQL is not available for populated migration integration test.")

    target_url = base.set(database=database_name)
    try:
        yield target_url.render_as_string(hide_password=False)
    finally:
        admin_engine.dispose()
        cleanup_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
        try:
            with cleanup_engine.connect() as connection:
                connection.exec_driver_sql(
                    f'DROP DATABASE IF EXISTS "{database_name}" WITH (FORCE)'
                )
        finally:
            cleanup_engine.dispose()


def _seed_confirmation_case(
    db: Session,
    *,
    suffix: str,
    workspace_timezone: str,
    branch_timezone: str | None,
    start_at: datetime,
    created_at: datetime,
    initial_status: str,
    final_status: str | None = None,
    manual_confirmed_at: datetime | None = None,
    include_creation_history: bool = True,
) -> UUID:
    workspace = Workspace(
        name=f"Confirmation migration {suffix}",
        slug=f"confirmation-migration-{suffix}-{uuid4().hex[:8]}",
        timezone=workspace_timezone,
    )
    db.add(workspace)
    db.flush()

    branch = Branch(
        workspace_id=workspace.id,
        name="Main",
        code=f"MAIN-{suffix}",
        timezone=branch_timezone,
    )
    patient = Patient(
        workspace_id=workspace.id,
        first_name="Migration",
        phone=f"0100{uuid4().int % 100000000:08d}",
        status="active",
        whatsapp_opt_in=True,
    )
    staff = Staff(
        workspace_id=workspace.id,
        first_name="Migration",
        last_name="Doctor",
    )
    service = Service(
        workspace_id=workspace.id,
        name="Migration service",
        slug=f"migration-service-{suffix}-{uuid4().hex[:8]}",
        duration_minutes=30,
    )
    user = User(
        email=f"migration-{suffix}-{uuid4().hex[:8]}@example.com",
        full_name="Migration Staff",
    )
    db.add_all([branch, patient, staff, service, user])
    db.flush()
    doctor = Doctor(workspace_id=workspace.id, staff_id=staff.id)
    db.add(doctor)
    db.flush()

    resolved_status = final_status or initial_status
    confirmed_at = None
    if resolved_status == "confirmed":
        confirmed_at = manual_confirmed_at or created_at
    appointment = Appointment(
        workspace_id=workspace.id,
        patient_id=patient.id,
        branch_id=branch.id,
        doctor_id=doctor.id,
        service_id=service.id,
        status=resolved_status,
        start_at=start_at,
        end_at=start_at + timedelta(minutes=30),
        busy_start_at=start_at,
        busy_end_at=start_at + timedelta(minutes=30),
        duration_minutes=30,
        created_at=created_at,
        confirmed_at=confirmed_at,
    )
    db.add(appointment)
    db.flush()

    if include_creation_history:
        db.add(
            AppointmentStatusHistory(
                workspace_id=workspace.id,
                appointment_id=appointment.id,
                changed_by_user_id=None,
                from_status=None,
                to_status=initial_status,
                reason="appointment_created",
                created_at=created_at,
            )
        )
    if initial_status == "pending" and resolved_status == "confirmed":
        assert manual_confirmed_at is not None
        db.add(
            AppointmentStatusHistory(
                workspace_id=workspace.id,
                appointment_id=appointment.id,
                changed_by_user_id=user.id,
                from_status="pending",
                to_status="confirmed",
                reason="appointment_confirmed",
                created_at=manual_confirmed_at,
            )
        )

    appointment_id = appointment.id
    db.commit()
    return appointment_id


def test_0092_reconciles_without_removing_legitimate_manual_confirmations(
    migration_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _upgrade(migration_database, "0091_availability_block_scope", monkeypatch)
    engine = create_engine(migration_database, pool_pre_ping=True)
    try:
        with Session(engine) as db:
            far_start = datetime(2026, 10, 20, 10, 0, tzinfo=UTC)
            far_created = datetime(2026, 10, 1, 10, 0, tzinfo=UTC)

            # M1: legacy booking created directly as confirmed, without a later
            # explicit staff pending -> confirmed transition.
            legacy_auto_id = _seed_confirmation_case(
                db,
                suffix="m1-legacy-auto",
                workspace_timezone="Africa/Cairo",
                branch_timezone="Africa/Cairo",
                start_at=far_start,
                created_at=far_created,
                initial_status="confirmed",
            )

            # M2: explicit staff confirmation before the customer window is valid.
            manual_far_at = datetime(2026, 10, 5, 10, 0, tzinfo=UTC)
            manual_far_id = _seed_confirmation_case(
                db,
                suffix="m2-manual-far",
                workspace_timezone="Africa/Cairo",
                branch_timezone="Africa/Cairo",
                start_at=far_start,
                created_at=far_created,
                initial_status="pending",
                final_status="confirmed",
                manual_confirmed_at=manual_far_at,
            )

            # M3: manual confirmation inside the ordinary customer window remains confirmed.
            near_start = datetime(2026, 10, 10, 10, 0, tzinfo=UTC)
            manual_near_at = datetime(2026, 10, 9, 9, 0, tzinfo=UTC)
            manual_near_id = _seed_confirmation_case(
                db,
                suffix="m3-manual-near",
                workspace_timezone="Africa/Cairo",
                branch_timezone="Africa/Cairo",
                start_at=near_start,
                created_at=far_created,
                initial_status="pending",
                final_status="confirmed",
                manual_confirmed_at=manual_near_at,
            )

            # M4: a booking created pending inside the allowed creation window is reconciled up.
            near_created = datetime(2026, 10, 9, 8, 0, tzinfo=UTC)
            automatic_near_id = _seed_confirmation_case(
                db,
                suffix="m4-automatic-near",
                workspace_timezone="Africa/Cairo",
                branch_timezone="Africa/Cairo",
                start_at=near_start,
                created_at=near_created,
                initial_status="pending",
            )

            # M5: invalid branch timezone falls back to the valid workspace timezone,
            # while the explicit staff confirmation remains authoritative.
            invalid_tz_manual_at = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
            invalid_tz_manual_id = _seed_confirmation_case(
                db,
                suffix="m5-invalid-tz-manual",
                workspace_timezone="Africa/Cairo",
                branch_timezone="Invalid/Branch-Timezone",
                start_at=far_start,
                created_at=far_created,
                initial_status="pending",
                final_status="confirmed",
                manual_confirmed_at=invalid_tz_manual_at,
            )

            # Conservative fallback: a far confirmed row with no creation history
            # has ambiguous intent, so migration must not guess and downgrade it.
            ambiguous_id = _seed_confirmation_case(
                db,
                suffix="ambiguous-no-history",
                workspace_timezone="Africa/Cairo",
                branch_timezone="Africa/Cairo",
                start_at=far_start,
                created_at=far_created,
                initial_status="confirmed",
                include_creation_history=False,
            )

        _upgrade(migration_database, "0092_appt_confirmation", monkeypatch)

        with Session(engine) as db:
            legacy_auto = db.get(Appointment, legacy_auto_id)
            manual_far = db.get(Appointment, manual_far_id)
            manual_near = db.get(Appointment, manual_near_id)
            automatic_near = db.get(Appointment, automatic_near_id)
            invalid_tz_manual = db.get(Appointment, invalid_tz_manual_id)
            ambiguous = db.get(Appointment, ambiguous_id)

            assert legacy_auto is not None
            assert legacy_auto.status == "pending"
            assert legacy_auto.confirmed_at is None

            assert manual_far is not None
            assert manual_far.status == "confirmed"
            assert manual_far.confirmed_at == manual_far_at

            assert manual_near is not None
            assert manual_near.status == "confirmed"
            assert manual_near.confirmed_at == manual_near_at

            assert automatic_near is not None
            assert automatic_near.status == "confirmed"
            assert automatic_near.confirmed_at == near_created

            assert invalid_tz_manual is not None
            assert invalid_tz_manual.status == "confirmed"
            assert invalid_tz_manual.confirmed_at == invalid_tz_manual_at

            assert ambiguous is not None
            assert ambiguous.status == "confirmed"
            assert ambiguous.confirmed_at == far_created
    finally:
        engine.dispose()
