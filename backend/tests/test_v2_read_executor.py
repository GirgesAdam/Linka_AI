from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

import pytest

from app.integrations.clinic.base import (
    AppointmentReadResult,
    AppointmentRecord,
    AvailabilityResult,
    AvailabilitySlot,
)
from app.services.agent_v2.planner import PlanStep, ReadRequest, WriteIntent
from app.services.agent_v2.read_executor import (
    ReadExecutionContext,
    ReadExecutionError,
    execute_step_reads,
)
from app.services.booking import BookingCompatibilityError, BookingRuleError

NOW = datetime(2026, 9, 11, 15, 0, tzinfo=UTC)
WORKSPACE_ID = UUID("11111111-1111-4111-8111-111111111111")
PATIENT_ID = UUID("22222222-2222-4222-8222-222222222222")
BRANCH_ID = UUID("33333333-3333-4333-8333-333333333333")
SERVICE_ID = UUID("44444444-4444-4444-8444-444444444444")
PACKAGE_ID = UUID("55555555-5555-4555-8555-555555555555")
OFFER_ID = UUID("66666666-6666-4666-8666-666666666666")


class FakeAdapter:
    def __init__(self, *, availability=None, appointments=None) -> None:
        self.availability = availability
        self.appointments = tuple(appointments or ())
        self.availability_requests = []
        self.appointment_requests = []
        self.required_capabilities = []

    def require_capability(self, capability) -> None:
        self.required_capabilities.append(capability)

    def get_availability(self, request):
        self.availability_requests.append(request)
        if callable(self.availability):
            return self.availability(request)
        return self.availability

    def get_patient_appointments(self, request):
        self.appointment_requests.append(request)
        return AppointmentReadResult(appointments=self.appointments)


def _workspace():
    return SimpleNamespace(
        id=WORKSPACE_ID,
        name="Tia Clinic",
        timezone="Africa/Cairo",
        primary_branch_id=BRANCH_ID,
    )


def _patient():
    return SimpleNamespace(
        id=PATIENT_ID,
        first_name="Mona",
        last_name="Ali",
        phone="01000000000",
        preferred_language="ar",
        status="active",
    )


def _catalog():
    return {
        "services": [
            {
                "id": str(SERVICE_ID),
                "name": "ليزر إبط",
                "price_minor": 50000,
                "currency": "EGP",
                "doctor_ids": ["doctor-maryam"],
                "laser_devices": [
                    {
                        "device_key": "candela_gentle",
                        "device_name": "Candela Gentle",
                        "configured": True,
                    },
                    {
                        "device_key": "prime_lase",
                        "device_name": "Prime Lase",
                        "configured": False,
                    },
                ],
            }
        ],
        "doctors": [
            {
                "id": "doctor-maryam",
                "name": "مريم",
                "service_ids": [str(SERVICE_ID)],
            },
            {
                "id": "doctor-other",
                "name": "سارة",
                "service_ids": ["other-service"],
            },
        ],
        "branches": [
            {
                "id": str(BRANCH_ID),
                "name": "Tia Clinic",
                "phone": "0200000000",
                "city": "Cairo",
            }
        ],
    }


def _context(adapter: FakeAdapter | None = None):
    return ReadExecutionContext(
        db=SimpleNamespace(),
        workspace=_workspace(),
        patient=_patient(),
        now=NOW,
        catalog=_catalog(),
        adapter=adapter,
    )


def _slot(*, doctor_id: str, start_hour_utc: int) -> AvailabilitySlot:
    start = datetime(2026, 9, 17, start_hour_utc, 0, tzinfo=UTC)
    return AvailabilitySlot(
        branch_id=str(BRANCH_ID),
        branch_name="Tia Clinic",
        doctor_id=doctor_id,
        doctor_name="مريم" if doctor_id == "doctor-maryam" else "سارة",
        service_id=str(SERVICE_ID),
        service_name="ليزر إبط",
        start_at=start,
        end_at=start + timedelta(minutes=30),
        duration_minutes=30,
        price_minor=50000,
        currency="EGP",
        laser_device_key="candela_gentle",
        laser_device_name="Candela Gentle",
    )


def _availability(slots) -> AvailabilityResult:
    return AvailabilityResult(
        timezone="Africa/Cairo",
        branch_id=str(BRANCH_ID),
        branch_name="Tia Clinic",
        service_id=str(SERVICE_ID),
        service_name="ليزر إبط",
        service_duration_minutes=30,
        service_price_minor=50000,
        service_currency="EGP",
        slots=tuple(slots),
    )


def _appointment(
    *,
    appointment_id: str = "appointment-1",
    status: str = "confirmed",
    doctor_id: str = "doctor-maryam",
) -> AppointmentRecord:
    start = datetime(2026, 9, 17, 16, 0, tzinfo=UTC)
    return AppointmentRecord(
        appointment_id=appointment_id,
        patient_id=str(PATIENT_ID),
        status=status,
        service_id=str(SERVICE_ID),
        service_name="ليزر إبط",
        branch_id=str(BRANCH_ID),
        branch_name="Tia Clinic",
        doctor_id=doctor_id,
        doctor_name="مريم",
        start_at=start,
        end_at=start + timedelta(minutes=30),
        timezone="Africa/Cairo",
        price_minor=50000,
        currency="EGP",
        payment_status="unpaid",
        billing_context="standard",
        laser_device_key="candela_gentle",
        laser_device_name="Candela Gentle",
    )


def test_service_and_doctor_reads_use_exact_canonical_catalog_relationships() -> None:
    service_step = PlanStep(
        operation_index=0,
        operation_type="pricing",
        disposition="read",
        reads=[ReadRequest(kind="service_catalog", parameters={"service_id": str(SERVICE_ID)})],
        response_goal="answer_price",
    )
    service_bundle = execute_step_reads(service_step, _context())
    assert service_bundle.results[0].ok is True
    assert service_bundle.results[0].payload["service"]["name"] == "ليزر إبط"

    doctor_step = PlanStep(
        operation_index=0,
        operation_type="doctor_info",
        disposition="read",
        reads=[ReadRequest(kind="doctors", parameters={"service_id": str(SERVICE_ID)})],
        response_goal="answer_doctor",
    )
    doctor_bundle = execute_step_reads(doctor_step, _context())
    assert [row["id"] for row in doctor_bundle.results[0].payload["doctors"]] == [
        "doctor-maryam"
    ]


def test_exact_availability_returns_one_verified_slot_without_writing() -> None:
    adapter = FakeAdapter(
        availability=_availability(
            [
                _slot(doctor_id="doctor-maryam", start_hour_utc=16),
                _slot(doctor_id="doctor-maryam", start_hour_utc=17),
            ]
        )
    )
    step = PlanStep(
        operation_index=0,
        operation_type="book",
        disposition="read",
        reads=[
            ReadRequest(
                kind="availability",
                parameters={
                    "service_id": str(SERVICE_ID),
                    "date": {
                        "mode": "exact",
                        "start_date": "2026-09-17",
                        "end_date": None,
                    },
                    "time": {
                        "mode": "exact",
                        "start_time": "19:00",
                        "end_time": None,
                    },
                    "device_key": "candela_gentle",
                },
            )
        ],
        write_intent=WriteIntent(kind="booking", authorized=True, parameters={}),
        response_goal="present_availability",
    )

    bundle = execute_step_reads(step, _context(adapter))
    assert bundle.results[0].payload["matching_slot_count"] == 1
    assert bundle.verification.exact_slot_match_count == 1
    assert bundle.verification.verified_parameters["doctor_id"] == "doctor-maryam"
    assert len(adapter.availability_requests) == 1
    assert adapter.availability_requests[0].branch_id == str(BRANCH_ID)


def test_normal_availability_uses_single_catalog_branch_without_primary() -> None:
    adapter = FakeAdapter(availability=_availability([]))
    workspace = _workspace()
    workspace.primary_branch_id = None
    step = PlanStep(
        operation_index=0,
        operation_type="availability",
        disposition="read",
        reads=[
            ReadRequest(
                kind="availability",
                parameters={
                    "service_id": str(SERVICE_ID),
                    "date": {"mode": "exact", "start_date": "2026-09-17", "end_date": None},
                },
            )
        ],
        response_goal="present_availability",
    )
    context = ReadExecutionContext(
        db=SimpleNamespace(),
        workspace=workspace,
        patient=_patient(),
        now=NOW,
        catalog=_catalog(),
        adapter=adapter,
    )

    execute_step_reads(step, context)

    assert adapter.availability_requests
    assert all(request.branch_id == str(BRANCH_ID) for request in adapter.availability_requests)


def test_normal_availability_does_not_guess_between_multiple_catalog_branches() -> None:
    adapter = FakeAdapter(availability=_availability([]))
    workspace = _workspace()
    workspace.primary_branch_id = None
    catalog = _catalog()
    catalog["branches"] = [
        *catalog["branches"],
        {
            "id": "77777777-7777-4777-8777-777777777777",
            "name": "Second Branch",
        },
    ]
    step = PlanStep(
        operation_index=0,
        operation_type="availability",
        disposition="read",
        reads=[
            ReadRequest(
                kind="availability",
                parameters={
                    "service_id": str(SERVICE_ID),
                    "date": {"mode": "exact", "start_date": "2026-09-17", "end_date": None},
                },
            )
        ],
        response_goal="present_availability",
    )
    context = ReadExecutionContext(
        db=SimpleNamespace(),
        workspace=workspace,
        patient=_patient(),
        now=NOW,
        catalog=catalog,
        adapter=adapter,
    )

    with pytest.raises(
        ReadExecutionError,
        match="could not be resolved deterministically",
    ):
        execute_step_reads(step, context)

    assert adapter.availability_requests == []


def test_next_available_search_stops_on_first_day_with_matching_slots() -> None:
    def availability_for_day(request):
        if request.booking_date.isoformat() == "2026-09-12":
            return _availability([_slot(doctor_id="doctor-maryam", start_hour_utc=16)])
        return _availability([])

    adapter = FakeAdapter(availability=availability_for_day)
    step = PlanStep(
        operation_index=0,
        operation_type="availability",
        disposition="read",
        reads=[
            ReadRequest(
                kind="availability",
                parameters={
                    "service_id": str(SERVICE_ID),
                    "date": {"mode": "next_available", "start_date": None, "end_date": None},
                },
            )
        ],
        response_goal="present_availability",
    )
    bundle = execute_step_reads(step, _context(adapter))

    assert bundle.results[0].payload["checked_dates"] == ["2026-09-11", "2026-09-12"]
    assert len(adapter.availability_requests) == 2


def test_cancel_verification_only_counts_actionable_current_patient_appointments() -> None:
    adapter = FakeAdapter(
        appointments=[
            _appointment(appointment_id="pending-1", status="pending"),
            _appointment(appointment_id="done-1", status="completed"),
        ]
    )
    step = PlanStep(
        operation_index=0,
        operation_type="cancel_appointment",
        disposition="read",
        reads=[ReadRequest(kind="appointments", parameters={"appointment_id": "pending-1"})],
        write_intent=WriteIntent(kind="cancel_appointment", authorized=True, parameters={}),
        response_goal="cancellation_completed",
    )
    bundle = execute_step_reads(step, _context(adapter))

    assert bundle.verification.appointment_match_count == 1
    assert bundle.verification.verified_parameters["appointment_id"] == "pending-1"
    assert adapter.appointment_requests[0].patient_id == str(PATIENT_ID)


def test_reschedule_inherits_verified_target_identity_before_availability_read() -> None:
    adapter = FakeAdapter(
        appointments=[_appointment(appointment_id="appointment-1", status="confirmed")],
        availability=_availability([_slot(doctor_id="doctor-maryam", start_hour_utc=16)]),
    )
    step = PlanStep(
        operation_index=0,
        operation_type="reschedule",
        disposition="read",
        reads=[
            ReadRequest(kind="appointments", parameters={"appointment_id": "appointment-1"}),
            ReadRequest(
                kind="availability",
                parameters={
                    "date": {
                        "mode": "exact",
                        "start_date": "2026-09-17",
                        "end_date": None,
                    },
                    "time": {
                        "mode": "exact",
                        "start_time": "19:00",
                        "end_time": None,
                    },
                    "reschedule": True,
                },
            ),
        ],
        write_intent=WriteIntent(kind="reschedule", authorized=True, parameters={}),
        response_goal="present_availability",
    )
    bundle = execute_step_reads(step, _context(adapter))

    assert bundle.verification.appointment_match_count == 1
    assert bundle.verification.exact_slot_match_count == 1
    request = adapter.availability_requests[0]
    assert request.service_id == str(SERVICE_ID)
    assert request.branch_id == str(BRANCH_ID)
    assert request.doctor_id == "doctor-maryam"
    assert request.exclude_appointment_id == "appointment-1"


def test_package_offer_read_returns_deterministic_match_count(monkeypatch) -> None:
    class Offer:
        id = OFFER_ID
        service_id = SERVICE_ID
        device_key = "candela_gentle"
        sessions_count = 6
        price_minor = 250000
        currency = "EGP"

        def model_dump(self, *, mode):
            assert mode == "json"
            return {
                "id": str(self.id),
                "service_id": str(self.service_id),
                "device_key": self.device_key,
                "sessions_count": self.sessions_count,
                "price_minor": self.price_minor,
                "currency": self.currency,
            }

    monkeypatch.setattr(
        "app.services.agent_v2.read_executor.list_package_offers",
        lambda *args, **kwargs: [Offer()],
    )
    step = PlanStep(
        operation_index=0,
        operation_type="buy_package",
        disposition="read",
        reads=[
            ReadRequest(
                kind="package_offers",
                parameters={
                    "service_id": str(SERVICE_ID),
                    "device_key": "candela_gentle",
                    "package_sessions": 6,
                },
            )
        ],
        write_intent=WriteIntent(kind="buy_package", authorized=True, parameters={}),
        response_goal="package_purchased",
    )
    bundle = execute_step_reads(step, _context())

    assert bundle.verification.package_offer_match_count == 1
    assert bundle.verification.verified_parameters["package_offer_id"] == str(OFFER_ID)


def test_refund_quote_read_delegates_to_shared_safe_quote_service(monkeypatch) -> None:
    class Quote:
        def as_dict(self):
            return {
                "package_id": str(PACKAGE_ID),
                "consumed_sessions": 2,
                "refundable_minor": 180000,
            }

    captured = {}

    def quote_reader(*args, **kwargs):
        captured.update(kwargs)
        return [Quote()], []

    monkeypatch.setattr(
        "app.services.agent_v2.read_executor.list_patient_package_refund_quotes",
        quote_reader,
    )
    step = PlanStep(
        operation_index=0,
        operation_type="refund_quote",
        disposition="read",
        reads=[ReadRequest(kind="package_refund_quote", parameters={"package_id": str(PACKAGE_ID)})],
        response_goal="package_refund_quote",
    )
    bundle = execute_step_reads(step, _context())

    assert bundle.results[0].ok is True
    assert bundle.results[0].payload["quotes"][0]["refundable_minor"] == 180000
    assert captured["package_id"] == PACKAGE_ID
    assert captured["patient_id"] == PATIENT_ID


def test_refund_quote_read_surfaces_unsafe_legacy_package(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.services.agent_v2.read_executor.list_patient_package_refund_quotes",
        lambda *args, **kwargs: ([], [PACKAGE_ID]),
    )
    step = PlanStep(
        operation_index=0,
        operation_type="refund_quote",
        disposition="read",
        reads=[ReadRequest(kind="package_refund_quote", parameters={"package_id": str(PACKAGE_ID)})],
        response_goal="package_refund_quote",
    )
    bundle = execute_step_reads(step, _context())

    assert bundle.results[0].ok is False
    assert bundle.results[0].error_code == "refund_quote_requires_staff"
    assert bundle.results[0].payload["unsafe_package_ids"] == [str(PACKAGE_ID)]


def _compatibility_booking_step(*, doctor_id: str, device_key: str) -> PlanStep:
    return PlanStep(
        operation_index=0,
        operation_type="book",
        disposition="read",
        reads=[
            ReadRequest(
                kind="availability",
                parameters={
                    "service_id": str(SERVICE_ID),
                    "doctor_id": doctor_id,
                    "device_key": device_key,
                    "date": {"mode": "exact", "start_date": "2026-09-17"},
                    "time": {"mode": "exact", "start_time": "19:00"},
                },
            )
        ],
        write_intent=WriteIntent(kind="booking", authorized=True, parameters={}),
        response_goal="present_availability",
    )


def test_known_incompatible_doctor_becomes_typed_failed_read() -> None:
    def availability(request):
        if request.doctor_id == "doctor-other":
            raise BookingCompatibilityError(
                "Doctor is not available for this service at this branch.",
                dimension="doctor",
            )
        return _availability([_slot(doctor_id="doctor-maryam", start_hour_utc=16)])

    adapter = FakeAdapter(availability=availability)
    bundle = execute_step_reads(
        _compatibility_booking_step(
            doctor_id="doctor-other",
            device_key="candela_gentle",
        ),
        _context(adapter),
    )

    result = bundle.results[0]
    assert result.ok is False
    assert result.error_code == "doctor_service_incompatible"
    assert result.payload["compatibility_failure"] == {
        "dimension": "doctor",
        "service_name": "ليزر إبط",
        "requested_name": "سارة",
        "compatible_options": ["مريم"],
    }


def test_known_incompatible_device_becomes_typed_failed_read() -> None:
    def availability(request):
        if request.laser_device_key == "prime_lase":
            raise BookingCompatibilityError(
                "Price and duration for Prime Lase are not configured.",
                dimension="device",
            )
        return _availability([_slot(doctor_id="doctor-maryam", start_hour_utc=16)])

    adapter = FakeAdapter(availability=availability)
    bundle = execute_step_reads(
        _compatibility_booking_step(
            doctor_id="doctor-maryam",
            device_key="prime_lase",
        ),
        _context(adapter),
    )

    result = bundle.results[0]
    assert result.ok is False
    assert result.error_code == "device_service_incompatible"
    assert result.payload["compatibility_failure"] == {
        "dimension": "device",
        "service_name": "ليزر إبط",
        "requested_name": "Prime Lase",
        "compatible_options": ["Candela Gentle"],
    }


def test_noncompatibility_booking_rule_error_still_raises() -> None:
    adapter = FakeAdapter(
        availability=lambda _request: (_ for _ in ()).throw(
            BookingRuleError("Requested date is outside the booking horizon.")
        )
    )
    with pytest.raises(BookingRuleError, match="outside the booking horizon"):
        execute_step_reads(
            _compatibility_booking_step(
                doctor_id="doctor-maryam",
                device_key="candela_gentle",
            ),
            _context(adapter),
        )


def test_unknown_doctor_is_not_reclassified_as_known_incompatibility() -> None:
    adapter = FakeAdapter(
        availability=lambda _request: (_ for _ in ()).throw(
            BookingCompatibilityError(
                "Doctor is not available for this service at this branch.",
                dimension="doctor",
            )
        )
    )
    with pytest.raises(BookingCompatibilityError, match="Doctor is not available"):
        execute_step_reads(
            _compatibility_booking_step(
                doctor_id="unknown-doctor",
                device_key="candela_gentle",
            ),
            _context(adapter),
        )


def test_unknown_device_is_not_reclassified_as_known_incompatibility() -> None:
    adapter = FakeAdapter(
        availability=lambda _request: (_ for _ in ()).throw(
            BookingCompatibilityError(
                "Laser device is not configured.",
                dimension="device",
            )
        )
    )
    with pytest.raises(BookingCompatibilityError, match="not configured"):
        execute_step_reads(
            _compatibility_booking_step(
                doctor_id="doctor-maryam",
                device_key="unknown-device",
            ),
            _context(adapter),
        )


def test_package_offer_read_honors_explicit_package_id(monkeypatch) -> None:
    other_offer_id = UUID("77777777-7777-4777-8777-777777777777")

    class Offer:
        def __init__(self, offer_id: UUID, price_minor: int) -> None:
            self.id = offer_id
            self.service_id = SERVICE_ID
            self.device_key = "candela_gentle"
            self.sessions_count = 6
            self.price_minor = price_minor
            self.currency = "EGP"

        def model_dump(self, *, mode):
            assert mode == "json"
            return {
                "id": str(self.id),
                "service_id": str(self.service_id),
                "device_key": self.device_key,
                "sessions_count": self.sessions_count,
                "price_minor": self.price_minor,
                "currency": self.currency,
            }

    monkeypatch.setattr(
        "app.services.agent_v2.read_executor.list_package_offers",
        lambda *args, **kwargs: [
            Offer(OFFER_ID, 250000),
            Offer(other_offer_id, 999999),
        ],
    )
    step = PlanStep(
        operation_index=0,
        operation_type="pricing",
        disposition="read",
        reads=[
            ReadRequest(
                kind="package_offers",
                parameters={"package_id": str(OFFER_ID)},
            )
        ],
        response_goal="answer_price",
    )

    bundle = execute_step_reads(step, _context())

    assert bundle.verification.package_offer_match_count == 1
    assert bundle.verification.verified_parameters["package_offer_id"] == str(OFFER_ID)
    assert len(bundle.results[0].payload["offers"]) == 1
    assert bundle.results[0].payload["offers"][0]["id"] == str(OFFER_ID)
