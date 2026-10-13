from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.agents.v2.semantic_context import build_semantic_context
from app.agents.v2.time_resolution import resolve_turn_times_by_clinic_hours
from app.agents.v2.turn_contract import (
    DateConstraint,
    TiaTurnUnderstanding,
    TimeConstraint,
    TurnEntities,
    TurnOperation,
)
from app.agents.v2.turn_interpreter import (
    _preserve_explicit_clock_constraints_v2,
    _validate_explicit_period_provenance_v2,
)
from app.agents.v2.turn_normalization import normalize_semantic_invariants
from app.services.agent_v2.orchestrator import _prepare_booking_proposal
from app.services.agent_v2.planner import PlannerContext, PlanStep, WriteIntent, plan_turn
from app.services.agent_v2.read_executor import ReadExecutionBundle, ReadResult
from app.services.agent_v2.state import (
    BookingTaskState,
    CustomerConstraints,
    WriteAuthorization,
)
from app.services.agent_v2.state_rules import apply_booking_time_change

NOW = datetime(2026, 10, 13, 10, 0, tzinfo=UTC)
SERVICE_ID = "11111111-1111-4111-8111-111111111111"
DOCTOR_ID = "22222222-2222-4222-8222-222222222222"
BRANCH_ID = "33333333-3333-4333-8333-333333333333"
PATIENT_ID = "44444444-4444-4444-8444-444444444444"


def _catalog() -> dict[str, object]:
    return {
        "services": [
            {
                "id": SERVICE_ID,
                "name": "Test service",
                "duration_minutes": 30,
            }
        ],
        "branches": [
            {
                "id": BRANCH_ID,
                "name": "Clinic",
                "working_hours": [
                    {"weekday": weekday, "start": "10:00", "end": "22:00"}
                    for weekday in range(7)
                ],
            }
        ],
        "doctors": [
            {
                "id": DOCTOR_ID,
                "name": "Doctor",
                "service_ids": [SERVICE_ID],
                "branch_ids": [BRANCH_ID],
            }
        ],
    }


def _turn(
    value: str,
    *,
    ambiguity: str = "none",
    period: str = "none",
    operation_type: str = "book",
) -> TiaTurnUnderstanding:
    return TiaTurnUnderstanding(
        operations=[
            TurnOperation(
                type=operation_type,
                time_period_provenance=period,
                entities=TurnEntities(
                    time=TimeConstraint(
                        mode="exact",
                        start_time=value,
                        start_time_ambiguity=ambiguity,
                    )
                ),
            )
        ]
    )


def _pipeline(turn: TiaTurnUnderstanding, *, text: str) -> TiaTurnUnderstanding:
    period_safe = _validate_explicit_period_provenance_v2(turn)
    literal_safe = _preserve_explicit_clock_constraints_v2(
        period_safe,
        latest_customer_text=text,
    )
    normalized = normalize_semantic_invariants(literal_safe)
    return resolve_turn_times_by_clinic_hours(normalized, build_semantic_context(_catalog()))


def _time(turn: TiaTurnUnderstanding) -> TimeConstraint:
    value = turn.operations[0].entities.time
    assert value is not None
    return value


def test_t1_literal_03_remains_exact_and_never_gets_clinic_hours_pm_mutation() -> None:
    result = _pipeline(_turn("03:00", ambiguity="twelve_hour"), text="03:00")
    assert _time(result).start_time == "03:00"
    assert _time(result).start_time_ambiguity == "none"


def test_t2_literal_1615_remains_exact() -> None:
    result = _pipeline(_turn("16:15"), text="16:15")
    assert _time(result).start_time == "16:15"
    assert _time(result).start_time_ambiguity == "none"


def test_t3_explicit_afternoon_semantics_survive_literal_preservation() -> None:
    result = _pipeline(_turn("16:15", period="explicit_pm"), text="4:15 العصر")
    assert _time(result).start_time == "16:15"
    assert result.operations[0].time_period_provenance == "explicit_pm"


def test_t4_explicit_morning_semantics_remain_am() -> None:
    result = _pipeline(_turn("04:15", period="explicit_am"), text="4:15 صباحًا")
    assert _time(result).start_time == "04:15"
    assert result.operations[0].time_period_provenance == "explicit_am"


def test_t5_explicit_evening_semantics_remain_pm() -> None:
    result = _pipeline(_turn("16:15", period="explicit_pm"), text="4:15 مساءً")
    assert _time(result).start_time == "16:15"


@pytest.mark.parametrize(
    ("text", "value", "period", "expected"),
    [
        ("4:15 PM", "16:15", "explicit_pm", "16:15"),
        ("4:15 AM", "04:15", "explicit_am", "04:15"),
    ],
)
def test_english_explicit_period_uses_same_typed_contract(
    text: str,
    value: str,
    period: str,
    expected: str,
) -> None:
    result = _pipeline(_turn(value, period=period), text=text)
    assert _time(result).start_time == expected


def test_t6_bare_colloquial_three_keeps_existing_clinic_hours_resolution() -> None:
    result = _pipeline(_turn("03:00", ambiguity="twelve_hour"), text="الساعة 3")
    assert _time(result).start_time == "15:00"
    assert _time(result).start_time_ambiguity == "none"


def test_t7_explicit_period_value_survives_into_booking_task_state() -> None:
    result = _pipeline(_turn("16:15", period="explicit_pm"), text="4:15 العصر")
    task = BookingTaskState(
        write_authorization=WriteAuthorization(operation="booking"),
        constraints=CustomerConstraints(
            service_id=SERVICE_ID,
            date=DateConstraint(mode="exact", start_date="2026-10-15"),
            time=TimeConstraint(mode="exact", start_time="11:00"),
        ),
        requested_service_ids=[SERVICE_ID],
    )
    updated = apply_booking_time_change(task, time=_time(result))
    assert updated.constraints.time is not None
    assert updated.constraints.time.start_time == "16:15"
    assert updated.constraints.time.start_time_ambiguity == "none"


def test_t8_literal_preserver_cannot_replace_explicit_pm_value_with_12_hour_variant() -> None:
    period_safe = _validate_explicit_period_provenance_v2(
        _turn("16:15", period="explicit_pm")
    )
    result = _preserve_explicit_clock_constraints_v2(
        period_safe,
        latest_customer_text="4:15 PM",
    )
    assert _time(result).start_time == "16:15"
    assert result.operations[0].time_period_provenance == "explicit_pm"


def test_t9_hash230_literal_03_protection_remains_intact() -> None:
    result = _preserve_explicit_clock_constraints_v2(
        _turn("15:00", ambiguity="twelve_hour"),
        latest_customer_text="03:00",
    )
    assert _time(result).start_time == "03:00"
    assert _time(result).start_time_ambiguity == "none"


def _local(clock: str) -> datetime:
    hour, minute = (int(part) for part in clock.split(":"))
    return datetime(2026, 10, 15, hour, minute, tzinfo=ZoneInfo("Africa/Cairo"))


def _task(clock: str) -> BookingTaskState:
    return BookingTaskState(
        write_authorization=WriteAuthorization(
            operation="booking",
            authorized=True,
            source_turn_id="turn-1",
            granted_at=NOW,
        ),
        constraints=CustomerConstraints(
            service_id=SERVICE_ID,
            doctor_id=DOCTOR_ID,
            branch_id=BRANCH_ID,
            date=DateConstraint(mode="exact", start_date="2026-10-15"),
            time=TimeConstraint(mode="exact", start_time=clock),
        ),
        requested_service_ids=[SERVICE_ID],
    )


def _step(clock: str) -> PlanStep:
    start = _local(clock)
    return PlanStep(
        operation_index=0,
        operation_type="book",
        disposition="write_ready",
        write_intent=WriteIntent(
            kind="booking",
            authorized=True,
            parameters={
                "service_id": SERVICE_ID,
                "doctor_id": DOCTOR_ID,
                "branch_id": BRANCH_ID,
                "start_at": start.astimezone(UTC).isoformat(),
                "package_usage": "unspecified",
            },
            requires_verification=True,
        ),
        response_goal="booking_completed",
        facts={
            "exact_time_requested": True,
            "requested_service_ids": [SERVICE_ID],
            "booking_proposal_required": True,
        },
    )


def _reads(*clocks: str) -> ReadExecutionBundle:
    slots = []
    for clock in clocks:
        start = _local(clock)
        slots.append(
            {
                "start_at": start.astimezone(UTC).isoformat(),
                "end_at": (start + timedelta(minutes=30)).astimezone(UTC).isoformat(),
                "start_local": start.isoformat(),
                "start_time_24h": clock,
                "doctor_id": DOCTOR_ID,
                "doctor_name": "Doctor",
                "branch_id": BRANCH_ID,
                "branch_name": "Clinic",
            }
        )
    return ReadExecutionBundle(
        results=[ReadResult(kind="availability", ok=True, payload={"slots": slots})]
    )


def _prepare(monkeypatch, requested: str, *available: str):
    monkeypatch.setattr(
        "app.services.agent_v2.orchestrator.resolve_additional_services",
        lambda *_args, **_kwargs: [],
    )
    return _prepare_booking_proposal(
        _step(requested),
        reads=_reads(*available),
        current_task=_task(requested),
        db=SimpleNamespace(),
        workspace=SimpleNamespace(id="workspace-1", timezone="Africa/Cairo"),
        patient=SimpleNamespace(id=PATIENT_ID),
        catalog=_catalog(),
        now=NOW,
        turn_id="turn-prepare",
    )


def test_t10_unavailable_exact_1615_never_creates_proposal_or_write(monkeypatch) -> None:
    advanced, proposal = _prepare(monkeypatch, "16:15", "16:00", "16:30")
    assert proposal is None
    assert advanced.write_intent is None
    assert advanced.response_goal == "requested_time_unavailable"


def test_t11_available_explicit_period_time_builds_exact_matching_proposal(monkeypatch) -> None:
    advanced, proposal = _prepare(monkeypatch, "16:30", "16:30")
    assert advanced.response_goal == "booking_proposal"
    assert proposal is not None
    assert proposal.start_at.astimezone(ZoneInfo("Africa/Cairo")).strftime("%H:%M") == "16:30"


def test_t12_conflicting_typed_period_fails_closed_to_time_clarification() -> None:
    inconsistent = _validate_explicit_period_provenance_v2(
        _turn("04:15", period="explicit_pm")
    )
    operation = inconsistent.operations[0]
    assert operation.time_period_provenance == "conflict"
    plan = plan_turn(
        inconsistent,
        PlannerContext(
            semantic_context=build_semantic_context(_catalog()),
            active_task=None,
            now=NOW,
            explicit_user_time="04:15",
        ),
    )
    step = plan.steps[0]
    assert step.disposition == "clarify"
    assert step.write_intent is None
    assert step.facts["time_period_conflict"] is True
