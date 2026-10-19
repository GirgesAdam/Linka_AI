from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from app.agents.v2.price_device_composer import deterministic_price_device_fallback
from app.agents.v2.responder import _deterministic_booking_proposal_reply
from app.agents.v2.semantic_context import build_semantic_context
from app.agents.v2.turn_contract import TiaTurnUnderstanding, TurnEntities, TurnOperation
from app.integrations.clinic.base import AvailabilitySlot
from app.services.agent_v2.outcome import TurnOutcome
from app.services.agent_v2.outcome_builder import build_step_outcome
from app.services.agent_v2.planner import PlannerContext, PlanStep, ReadRequest, plan_turn
from app.services.agent_v2.read_executor import (
    ReadExecutionBundle,
    ReadExecutionContext,
    _read_service_catalog_set,
    _slot_payload,
)
from app.services.agent_v2.response_contract import build_customer_response_contract
from app.services.agent_v2.state import BookingTaskState, CustomerConstraints, WriteAuthorization
from app.services.laser_slot_metadata import encode_laser_slot_branch

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)


def _catalog(*, same_total: bool = True) -> dict[str, object]:
    return {
        "services": [
            {
                "id": "svc-underarm",
                "name": "Underarm",
                "requires_laser_device": True,
                "currency": "EGP",
                "laser_devices": [
                    {
                        "device_key": "candela",
                        "device_name": "Candela",
                        "price_minor": 50_000,
                        "currency": "EGP",
                        "configured": True,
                    },
                    {
                        "device_key": "prime",
                        "device_name": "Prime Lase",
                        "price_minor": 60_000,
                        "currency": "EGP",
                        "configured": True,
                    },
                ],
            },
            {
                "id": "svc-arms",
                "name": "Full Arms",
                "requires_laser_device": True,
                "currency": "EGP",
                "laser_devices": [
                    {
                        "device_key": "candela",
                        "device_name": "Candela",
                        "price_minor": 80_000,
                        "currency": "EGP",
                        "configured": True,
                    },
                    {
                        "device_key": "prime",
                        "device_name": "Prime Lase",
                        "price_minor": 70_000 if same_total else 90_000,
                        "currency": "EGP",
                        "configured": True,
                    },
                ],
            },
            {
                "id": "svc-combo",
                "name": "Laser Combo",
                "requires_laser_device": False,
                "price_minor": 110_000,
                "currency": "EGP",
            },
        ],
        "devices": [
            {"key": "candela", "name": "Candela"},
            {"key": "prime", "name": "Prime Lase"},
        ],
    }


def _read_context(catalog: dict[str, object]) -> ReadExecutionContext:
    return ReadExecutionContext(
        db=SimpleNamespace(),
        workspace=SimpleNamespace(),
        patient=SimpleNamespace(),
        now=NOW,
        catalog=catalog,
        adapter=None,
    )


def _pricing_turn() -> TiaTurnUnderstanding:
    return TiaTurnUnderstanding(
        operations=[
            TurnOperation(
                type="pricing",
                entities=TurnEntities(),
                execution_intent="informational",
                requested_service_details=["price"],
            )
        ]
    )


def _active_task(service_ids: list[str], *, device_key: str | None = None) -> BookingTaskState:
    return BookingTaskState(
        write_authorization=WriteAuthorization(operation="booking", authorized=False),
        constraints=CustomerConstraints(
            service_id=service_ids[0] if service_ids else None,
            device_key=device_key,
        ),
        requested_service_ids=service_ids,
    )


def _plan_for_active(task: BookingTaskState, catalog: dict[str, object] | None = None) -> PlanStep:
    semantic = build_semantic_context(catalog or _catalog())
    return plan_turn(
        _pricing_turn(),
        PlannerContext(semantic_context=semantic, active_task=task, now=NOW),
    ).steps[0]


def _visit_total_reply(*, same_total: bool, device_key: str | None = None) -> tuple[str, TurnOutcome]:
    catalog = _catalog(same_total=same_total)
    request = ReadRequest(
        kind="service_catalog_set",
        parameters={
            "service_ids": ["svc-underarm", "svc-arms"],
            **({"device_key": device_key} if device_key else {}),
        },
    )
    result = _read_service_catalog_set(request, _read_context(catalog))
    step = PlanStep(
        operation_index=0,
        operation_type="pricing",
        disposition="read",
        reads=[request],
        response_goal="answer_price",
        facts={
            "requested_service_ids": ["svc-underarm", "svc-arms"],
            "visit_total_price": True,
            **({"device_key": device_key} if device_key else {}),
        },
    )
    turn = _pricing_turn()
    outcome = build_step_outcome(
        step,
        turn=turn,
        semantic_context=build_semantic_context(catalog),
        reads=ReadExecutionBundle(results=[result]),
    )
    contract = build_customer_response_contract([outcome])
    return deterministic_price_device_fallback(contract, arabic=True), outcome


def test_q1_internal_laser_marker_is_decoded_before_customer_boundary() -> None:
    encoded = encode_laser_slot_branch(
        "Main Branch",
        device_key="candela",
        device_name="Candela",
    )
    slot = AvailabilitySlot(
        branch_id="branch-1",
        branch_name=encoded,
        service_id="svc-underarm",
        service_name="Underarm",
        doctor_id="doc-1",
        doctor_name="Mariam",
        start_at=NOW + timedelta(days=1),
        end_at=NOW + timedelta(days=1, minutes=30),
        duration_minutes=30,
        price_minor=50_000,
        currency="EGP",
        laser_device_key=None,
        laser_device_name=None,
    )
    payload = _slot_payload(slot, timezone_name="UTC")
    assert payload["branch_name"] == "Main Branch"
    assert payload["laser_device_key"] == "candela"
    assert payload["laser_device_name"] == "Candela"
    assert "TIA_LASER_DEVICE" not in str(payload)
    assert "||" not in str(payload)

    outcome = TurnOutcome(
        status="answered",
        response_goal="booking_proposal",
        facts={
            "booking_proposal_summary": {
                "services": ["Underarm"],
                "start_local": payload["start_local"],
                "doctor_name": payload["doctor_name"],
                "branch_name": payload["branch_name"],
                "device_name": payload["laser_device_name"],
            }
        },
    )
    unit = build_customer_response_contract([outcome]).units[0]
    text = _deterministic_booking_proposal_reply(unit, arabic=True)
    assert text is not None
    assert "Candela" in text
    assert "TIA_LASER_DEVICE" not in text
    assert "||" not in text


def test_q2_known_single_service_price_uses_active_canonical_service_without_clarification() -> None:
    step = _plan_for_active(_active_task(["svc-underarm"], device_key="candela"))
    assert step.disposition == "read"
    assert step.response_goal == "answer_price"
    assert step.clarification_field is None
    assert step.reads == [
        ReadRequest(kind="service_catalog", parameters={"service_id": "svc-underarm"})
    ]
    assert step.facts["device_key"] == "candela"


def test_booking_proposal_includes_verified_complete_set_total_price() -> None:
    _, pricing_outcome = _visit_total_reply(same_total=False, device_key="candela")
    visit_price = pricing_outcome.facts.get("visit_price")
    assert isinstance(visit_price, dict)
    outcome = TurnOutcome(
        status="answered",
        response_goal="booking_proposal",
        facts={
            **pricing_outcome.facts,
            # Duplicate read batches may carry the same complete-set total more than once.
            "visit_price": [visit_price, visit_price],
            "booking_proposal_summary": {
                "services": ["Underarm", "Full Arms"],
                "start_local": "2026-10-17 18:00",
                "doctor_name": "Mariam",
                "branch_name": "Main Branch",
                "device_name": "Candela",
            },
        },
    )
    unit = build_customer_response_contract([outcome]).units[0]

    text = _deterministic_booking_proposal_reply(unit, arabic=True)

    assert text is not None
    assert "1300" in text
    assert "EGP" in text


def test_q3_multi_service_total_uses_active_canonical_set_and_deterministic_aggregate() -> None:
    step = _plan_for_active(
        _active_task(["svc-underarm", "svc-arms"], device_key="candela")
    )
    assert step.disposition == "read"
    assert step.clarification_field is None
    assert step.reads[0].kind == "service_catalog_set"
    assert step.reads[0].parameters["service_ids"] == ["svc-underarm", "svc-arms"]
    text, outcome = _visit_total_reply(same_total=False, device_key="candela")
    assert outcome.facts["visit_price"]["variants"][0]["total_price"] == "1300.00 EGP"
    assert "1300" in text
    assert "Underarm" in text and "Full Arms" in text
    assert "\u0623\u064a \u062e\u062f\u0645\u0629" not in text


def test_q4_total_after_side_read_still_resolves_from_active_service_set() -> None:
    task = _active_task(["svc-underarm", "svc-arms"], device_key="candela")
    step = _plan_for_active(task)
    assert step.facts["requested_service_ids"] == task.requested_service_ids
    assert step.state_action == "none"


def test_q5_one_common_compatible_device_answers_total_without_device_question() -> None:
    catalog = _catalog(same_total=False)
    arms = next(row for row in catalog["services"] if row["id"] == "svc-arms")
    arms["laser_devices"] = [
        row for row in arms["laser_devices"] if row["device_key"] == "candela"
    ]
    request = ReadRequest(
        kind="service_catalog_set",
        parameters={"service_ids": ["svc-underarm", "svc-arms"]},
    )
    result = _read_service_catalog_set(request, _read_context(catalog))
    assert [row["device_name"] for row in result.payload["variants"]] == ["Candela"]

    step = PlanStep(
        operation_index=0,
        operation_type="pricing",
        disposition="read",
        reads=[request],
        response_goal="answer_price",
        facts={
            "requested_service_ids": ["svc-underarm", "svc-arms"],
            "visit_total_price": True,
        },
    )
    outcome = build_step_outcome(
        step,
        turn=_pricing_turn(),
        semantic_context=build_semantic_context(catalog),
        reads=ReadExecutionBundle(results=[result]),
    )
    text = deterministic_price_device_fallback(
        build_customer_response_contract([outcome]),
        arabic=True,
    )
    assert "Candela" in text
    assert "\u062a\u062d\u0628 \u0623\u064a \u062c\u0647\u0627\u0632" not in text


def test_q6_unresolved_devices_with_same_total_can_answer_without_choosing_device() -> None:
    text, outcome = _visit_total_reply(same_total=True)
    variants = outcome.facts["visit_price"]["variants"]
    assert len(variants) == 2
    assert {row["total_price"] for row in variants} == {"1300.00 EGP"}
    assert "1300" in text
    assert "\u062a\u062d\u0628 \u0623\u064a \u062c\u0647\u0627\u0632" not in text


def test_q7_unresolved_devices_with_different_totals_require_grounded_device_choice() -> None:
    text, outcome = _visit_total_reply(same_total=False)
    variants = outcome.facts["visit_price"]["variants"]
    assert {row["total_price"] for row in variants} == {"1300.00 EGP", "1500.00 EGP"}
    assert "Candela" in text and "Prime Lase" in text
    assert "1300" in text and "1500" in text
    assert "\u062a\u062d\u0628 \u0623\u064a \u062c\u0647\u0627\u0632" in text


def test_q8_service_correction_prices_only_current_retained_service_set() -> None:
    corrected = _active_task(["svc-underarm"], device_key="candela")
    step = _plan_for_active(corrected)
    assert step.reads[0].kind == "service_catalog"
    assert step.reads[0].parameters["service_id"] == "svc-underarm"
    assert "svc-arms" not in str(step.reads[0].parameters)


def test_q9_combo_replacement_cannot_double_count_removed_constituents() -> None:
    corrected = _active_task(["svc-combo"])
    step = _plan_for_active(corrected)
    assert step.reads == [ReadRequest(kind="service_catalog", parameters={"service_id": "svc-combo"})]
    assert "svc-underarm" not in str(step.reads)
    assert "svc-arms" not in str(step.reads)


def test_q10_device_specific_multi_service_reply_has_name_and_no_internal_marker() -> None:
    text, _ = _visit_total_reply(same_total=False, device_key="candela")
    assert "Candela" in text
    assert "TIA_LASER_DEVICE" not in text
    assert "||" not in text


def test_q12_q13_q14_status_copy_is_appointment_specific() -> None:
    source = (
        Path(__file__).resolve().parents[2] / "frontend/src/lib/status.ts"
    ).read_text(encoding="utf-8")
    appointment = source.split("export const appointmentLabels", 1)[1].split("};", 1)[0]
    generic = source.split("export const statusLabels", 1)[1].split("};", 1)[0]
    label_function = source.split("export function labelForStatus", 1)[1].split("}", 1)[0]
    assert 'pending: "\u0645\u062d\u062c\u0648\u0632"' in appointment
    assert 'confirmed: "\u0645\u0624\u0643\u062f"' in appointment
    assert 'pending: "\u0642\u064a\u062f \u0627\u0644\u0627\u0646\u062a\u0638\u0627\u0631"' in generic
    assert "appointmentLabels[value]" not in label_function



def test_ra_h3_reviewer_contract_allows_verified_proposal_confirmation_write() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "run_human_conversation_quality_batch1_review.py"
    ).read_text(encoding="utf-8")
    body = source.split("def ra_h3_long_continuation()", 1)[1].split("def ra_h4_one_device()", 1)[0]

    assert "assert all(trace.simulated_write is None for trace in last.traces)" not in body
    assert 'writes == ["booking_proposal"]' in body
    assert "booking_proposal_confirmation" in body
