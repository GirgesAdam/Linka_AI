from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.agents.v2.package_compare_composer import deterministic_package_comparison_reply
from app.agents.v2.responder import _deterministic_availability_reference_reply
from app.agents.v2.semantic_context import build_semantic_context
from app.agents.v2.semantic_state_view import (
    presented_availability_semantic_view,
    verified_read_semantic_view,
    with_safe_availability_reference_context,
    with_safe_read_context,
)
from app.agents.v2.turn_contract import (
    DateConstraint,
    EntityReference,
    Selection,
    TiaTurnUnderstanding,
    TurnEntities,
    TurnOperation,
)
from app.agents.v2.turn_interpreter import merge_presented_availability_context
from app.agents.v2.turn_normalization import normalize_semantic_invariants
from app.services.agent_v2.live_chat import (
    _availability_reference_context_from_turn,
    _verified_read_context_from_turn,
)
from app.services.agent_v2.orchestrator import V2OrchestratedTurn, V2RuntimeStepTrace
from app.services.agent_v2.outcome import TurnOutcome
from app.services.agent_v2.outcome_builder import build_step_outcome
from app.services.agent_v2.planner import (
    PlannerContext,
    PlanStep,
    ReadRequest,
    TurnPlan,
    plan_turn,
)
from app.services.agent_v2.read_executor import ReadExecutionBundle, ReadResult
from app.services.agent_v2.reference_resolution import (
    build_availability_reference_options,
    resolve_verified_availability_reference,
)

NOW = datetime(2026, 10, 9, 21, 0, tzinfo=UTC)
SERVICE_ID = "service-hydra"
DOCTOR_ID = "doctor-yusuf"
DEVICE_KEY = "candela"


def _semantic():
    return build_semantic_context(
        {
            "services": [
                {"id": SERVICE_ID, "name": "HydraFacial"},
                {"id": "service-laser", "name": "ليزر إزالة الشعر - إبط"},
            ],
            "doctors": [
                {
                    "id": DOCTOR_ID,
                    "name": "يوسف",
                    "service_ids": [SERVICE_ID],
                }
            ],
            "laser_devices": [
                {"device_key": DEVICE_KEY, "device_name": "Candela"},
            ],
        }
    )


def _slot(time_value: str, *, end: str | None = None) -> dict[str, object]:
    hour, minute = [int(part) for part in time_value.split(":")]
    if end is None:
        total = hour * 60 + minute + 30
        end_time = f"{(total // 60) % 24:02d}:{total % 60:02d}"
    else:
        end_time = end
    return {
        "branch_id": "branch-1",
        "service_id": SERVICE_ID,
        "service_name": "HydraFacial",
        "doctor_id": DOCTOR_ID,
        "doctor_name": "يوسف",
        "laser_device_key": DEVICE_KEY,
        "laser_device_name": "Candela",
        "start_at": f"2026-10-10T{time_value}:00+03:00",
        "end_at": f"2026-10-10T{end_time}:00+03:00",
        "start_local": f"2026-10-10T{time_value}:00+03:00",
        "end_local": f"2026-10-10T{end_time}:00+03:00",
        "start_time_24h": time_value,
        "end_time_24h": end_time,
        "duration_minutes": 30,
    }


def _window(slot: dict[str, object]) -> dict[str, object]:
    # Customer-facing single-slot windows represent one verified bookable start,
    # so their start/end are the same clock even though the appointment has duration.
    return {
        "start_local": slot["start_local"],
        "end_local": slot["start_local"],
        "start_time_24h": slot["start_time_24h"],
        "end_time_24h": slot["start_time_24h"],
        "doctor_name": slot["doctor_name"],
        "laser_device_name": slot["laser_device_name"],
    }


def _reference_context(*, anchor: int | None = None) -> dict[str, object]:
    slots = [_slot("12:00"), _slot("14:00"), _slot("15:30", end="16:00")]
    options = build_availability_reference_options(
        displayed_windows=[_window(slot) for slot in slots],
        verified_slots=slots,
    )
    context: dict[str, object] = {
        "operation_type": "availability",
        "service_id": SERVICE_ID,
        "doctor_id": DOCTOR_ID,
        "device_key": DEVICE_KEY,
        "date": {"mode": "exact", "start_date": "2026-10-10", "end_date": None},
        "availability_reference_options": options,
    }
    if anchor is not None:
        context["availability_reference_anchor_index"] = anchor
    return context


@pytest.mark.parametrize(
    ("selection", "anchor", "expected_index", "expected_time"),
    [
        (Selection(kind="ref", ref="opt_2"), None, 2, "14:00"),
        (Selection(kind="index", index=2), None, 2, "14:00"),
        (Selection(kind="relative", relative="next"), 1, 2, "14:00"),
        (Selection(kind="relative", relative="previous"), 3, 2, "14:00"),
        (Selection(kind="relative", relative="first"), None, 1, "12:00"),
        (Selection(kind="relative", relative="last"), None, 3, "15:30"),
    ],
)
def test_verified_reference_resolution_uses_server_owned_options(
    selection: Selection,
    anchor: int | None,
    expected_index: int,
    expected_time: str,
) -> None:
    resolved = resolve_verified_availability_reference(
        selection,
        _reference_context(anchor=anchor),
    )
    assert resolved["status"] == "resolved"
    assert resolved["index"] == expected_index
    option = resolved["option"]
    assert isinstance(option, dict)
    slot = option["slot"]
    assert isinstance(slot, dict)
    assert slot["start_time_24h"] == expected_time
    assert slot["doctor_id"] == DOCTOR_ID
    assert slot["laser_device_key"] == DEVICE_KEY


def test_next_without_anchor_clarifies_instead_of_guessing_or_rereading() -> None:
    operation = TurnOperation(
        type="availability",
        continues_previous=True,
        entities=TurnEntities(),
        selection=Selection(kind="relative", relative="next"),
    )
    turn = TiaTurnUnderstanding(operations=[operation])
    context = PlannerContext(
        semantic_context=_semantic(),
        active_task=None,
        now=NOW,
        recent_read_context=_reference_context(),
    )
    step = plan_turn(turn, context).steps[0]
    assert step.disposition == "clarify"
    assert step.reads == []
    assert step.facts["availability_reference_reason"] == "needs_anchor"


def test_second_option_makes_progress_without_identical_availability_replay() -> None:
    operation = TurnOperation(
        type="availability",
        continues_previous=True,
        entities=TurnEntities(),
        selection=Selection(kind="index", index=2),
    )
    turn = TiaTurnUnderstanding(operations=[operation])
    step = plan_turn(
        turn,
        PlannerContext(
            semantic_context=_semantic(),
            active_task=None,
            now=NOW,
            recent_read_context=_reference_context(),
        ),
    ).steps[0]
    assert step.disposition == "respond"
    assert step.reads == []
    assert step.response_goal == "availability_reference"
    selected = step.facts["availability_reference_option"]
    assert isinstance(selected, dict)
    assert selected["start_time_24h"] == "14:00"
    assert selected["doctor_name"] == "يوسف"
    assert selected["laser_device_name"] == "Candela"


def test_compressed_availability_window_never_becomes_invented_slot() -> None:
    first = _slot("14:15", end="14:45")
    second = _slot("14:45", end="15:15")
    compressed = {
        "start_local": first["start_local"],
        "end_local": second["start_local"],
        "start_time_24h": "14:15",
        "end_time_24h": "14:45",
        "doctor_name": "يوسف",
        "laser_device_name": "Candela",
    }
    options = build_availability_reference_options(
        displayed_windows=[compressed],
        verified_slots=[first, second],
    )
    assert options[0]["concrete"] is False
    assert "slot" not in options[0]
    resolved = resolve_verified_availability_reference(
        Selection(kind="index", index=1),
        {"availability_reference_options": options},
    )
    assert resolved["status"] == "window_ambiguous"


def test_new_verified_date_context_replaces_old_reference_options() -> None:
    old = _reference_context(anchor=2)
    new = dict(_reference_context())
    new["date"] = {"mode": "exact", "start_date": "2026-10-11", "end_date": None}
    new_slots = [_slot("10:00"), _slot("11:00")]
    for slot in new_slots:
        slot["start_local"] = str(slot["start_local"]).replace("2026-10-10", "2026-10-11")
        slot["end_local"] = str(slot["end_local"]).replace("2026-10-10", "2026-10-11")
    new["availability_reference_options"] = build_availability_reference_options(
        displayed_windows=[_window(slot) for slot in new_slots],
        verified_slots=new_slots,
    )
    new.pop("availability_reference_anchor_index", None)

    resolved = resolve_verified_availability_reference(
        Selection(kind="index", index=2),
        new,
    )
    assert resolved["status"] == "resolved"
    assert resolved["option"]["slot"]["start_time_24h"] == "11:00"
    assert old["availability_reference_options"] != new["availability_reference_options"]


def test_verified_read_semantic_view_exposes_safe_positions_but_not_slot_ids() -> None:
    context = _semantic()
    raw = _reference_context(anchor=1)
    safe = verified_read_semantic_view(raw, context=context)
    assert safe["availability_reference_anchor_index"] == 1
    assert safe["availability_reference_options"][1]["start_time_24h"] == "14:00"
    assert "slot" not in safe["availability_reference_options"][1]
    assert "doctor_id" not in safe["availability_reference_options"][1]
    with_read = with_safe_read_context(context, read_context=raw)
    assert with_read.model_input["recent_verified_read"] == safe


def test_reference_reply_is_deterministic_and_grounded() -> None:
    outcome = TurnOutcome(
        status="answered",
        response_goal="availability_reference",
        facts={
            "verified_availability_reference": True,
            "availability_reference_index": 2,
            "availability_reference_option": {
                "index": 2,
                "start_time_24h": "14:00",
                "doctor_name": "يوسف",
                "laser_device_name": "Candela",
            },
        },
    )
    reply = _deterministic_availability_reference_reply([outcome])
    assert reply is not None
    assert "14:00" in reply[0]
    assert "يوسف" in reply[0]
    assert "Candela" in reply[0]


def _package_compare_outcome(packages: list[dict[str, object]], *, price: str | None = "1,200 EGP") -> TurnOutcome:
    service: dict[str, object] = {"name": "ليزر إزالة الشعر - إبط"}
    if price is not None:
        service["price"] = price
        service["currency"] = "EGP"
    return TurnOutcome(
        status="answered",
        response_goal="package_comparison",
        facts={
            "customer_packages": {"packages": packages},
            "service_catalog": {"service": service},
        },
    )


def test_package_compare_semantics_are_deterministically_informational() -> None:
    turn = TiaTurnUnderstanding(
        operations=[
            TurnOperation(
                type="package_compare",
                execution_intent="execute",
                package_usage="use_existing",
                entities=TurnEntities(service=EntityReference(ref="S2")),
            )
        ]
    )
    normalized = normalize_semantic_invariants(turn)
    operation = normalized.operations[0]
    assert operation.type == "package_compare"
    assert operation.execution_intent == "informational"
    assert operation.package_usage == "unspecified"


def test_package_compare_plan_reads_owned_package_and_single_session_price_only() -> None:
    semantic = _semantic()
    operation = TurnOperation(
        type="package_compare",
        execution_intent="informational",
        entities=TurnEntities(service=EntityReference(ref="S2")),
    )
    step = plan_turn(
        TiaTurnUnderstanding(operations=[operation]),
        PlannerContext(semantic_context=semantic, active_task=None, now=NOW),
    ).steps[0]
    assert step.disposition == "read"
    assert step.write_intent is None
    assert [read.kind for read in step.reads] == ["customer_packages", "service_catalog"]
    assert "package_offers" not in [read.kind for read in step.reads]
    assert step.response_goal == "package_comparison"


def test_applicable_active_package_answers_actual_comparison() -> None:
    outcome = _package_compare_outcome(
        [
            {
                "name": "Underarm 6",
                "sessions_purchased": 6,
                "sessions_remaining": 3,
                "effective_status": "active",
            }
        ]
    )
    reply = deterministic_package_comparison_reply([outcome])
    assert reply is not None
    assert "1,200 EGP" in reply[0]
    assert "3 جلسة" in reply[0]
    assert "بدل دفع جلسة منفصلة" in reply[0]


def test_no_applicable_package_is_grounded_without_offer_dump() -> None:
    reply = deterministic_package_comparison_reply([_package_compare_outcome([])])
    assert reply is not None
    assert "مش ظاهر عندك" in reply[0]
    assert "باكدج" in reply[0]


@pytest.mark.parametrize(
    ("package", "expected"),
    [
        ({"name": "P", "sessions_remaining": 0, "effective_status": "exhausted"}, "مفيهاش جلسات متبقية"),
        ({"name": "P", "sessions_remaining": 2, "effective_status": "expired"}, "منتهية الصلاحية"),
    ],
)
def test_exhausted_or_expired_package_answer_is_grounded(package: dict[str, object], expected: str) -> None:
    reply = deterministic_package_comparison_reply([_package_compare_outcome([package])])
    assert reply is not None
    assert expected in reply[0]


def test_single_session_price_unavailable_is_stated_not_invented() -> None:
    outcome = _package_compare_outcome(
        [{"name": "P", "sessions_remaining": 1, "effective_status": "active"}],
        price=None,
    )
    reply = deterministic_package_comparison_reply([outcome])
    assert reply is not None
    assert "مش ظاهر عندي بشكل مؤكد" in reply[0]


def test_multiple_applicable_packages_are_not_silently_selected() -> None:
    packages = [
        {"name": "P1", "sessions_remaining": 1, "effective_status": "active"},
        {"name": "P2", "sessions_remaining": 2, "effective_status": "active"},
    ]
    reply = deterministic_package_comparison_reply([_package_compare_outcome(packages)])
    assert reply is not None
    assert "أكتر من باكدج" in reply[0]
    assert "مش هختار" in reply[0]


def test_package_comparison_outcome_contains_no_write_authority() -> None:
    semantic = _semantic()
    operation = TurnOperation(
        type="package_compare",
        execution_intent="informational",
        entities=TurnEntities(service=EntityReference(ref="S2")),
    )
    step = plan_turn(
        TiaTurnUnderstanding(operations=[operation]),
        PlannerContext(semantic_context=semantic, active_task=None, now=NOW),
    ).steps[0]
    assert step.write_intent is None
    assert all(read.kind not in {"package_offers"} for read in step.reads)


def test_later_explicit_package_booking_is_not_blocked_by_prior_comparison_contract() -> None:
    semantic = _semantic()
    later = TurnOperation(
        type="book",
        execution_intent="execute",
        package_usage="use_existing",
        entities=TurnEntities(
            service=EntityReference(ref="S2"),
            date=DateConstraint(mode="exact", start_date="2026-10-10"),
        ),
    )
    step = plan_turn(
        TiaTurnUnderstanding(operations=[later]),
        PlannerContext(semantic_context=semantic, active_task=None, now=NOW),
    ).steps[0]
    assert step.operation_type == "book"
    assert step.response_goal != "package_comparison"
    # The booking flow is allowed to continue through verification; no sticky informational state exists.
    assert step.disposition in {"read", "clarify"}


def _availability_turn_for_context(*, date_value: str, times: list[str]) -> V2OrchestratedTurn:
    raw_slots = []
    windows = []
    for time_value in times:
        slot = _slot(time_value)
        slot["start_local"] = str(slot["start_local"]).replace("2026-10-10", date_value)
        slot["end_local"] = str(slot["end_local"]).replace("2026-10-10", date_value)
        slot["start_at"] = str(slot["start_at"]).replace("2026-10-10", date_value)
        slot["end_at"] = str(slot["end_at"]).replace("2026-10-10", date_value)
        raw_slots.append(slot)
        windows.append(_window(slot))
    operation = TurnOperation(
        type="availability",
        entities=TurnEntities(
            service=EntityReference(ref="S1"),
            doctor=EntityReference(ref="D1"),
            date=DateConstraint(mode="exact", start_date=date_value),
        ),
    )
    step = PlanStep(
        operation_index=0,
        operation_type="availability",
        disposition="read",
        reads=[
            ReadRequest(
                kind="availability",
                parameters={
                    "service_id": SERVICE_ID,
                    "doctor_id": DOCTOR_ID,
                    "device_key": DEVICE_KEY,
                    "date": {"mode": "exact", "start_date": date_value, "end_date": None},
                },
            )
        ],
        response_goal="present_availability",
        facts={
            "service_id": SERVICE_ID,
            "doctor_id": DOCTOR_ID,
            "device_key": DEVICE_KEY,
            "date": {"mode": "exact", "start_date": date_value, "end_date": None},
        },
    )
    outcome = TurnOutcome(
        status="answered",
        response_goal="present_availability",
        facts={
            "availability": {
                "service_name": "HydraFacial",
                "available_option_count": len(raw_slots),
                "availability_windows": windows,
            }
        },
    )
    trace = V2RuntimeStepTrace(
        operation_index=0,
        operation_type="availability",
        disposition_before="read",
        disposition_after="read",
        read_kinds=("availability",),
        outcome=outcome,
        availability_slots=tuple(raw_slots),
    )
    return V2OrchestratedTurn(
        understanding=TiaTurnUnderstanding(operations=[operation]),
        plan=TurnPlan(steps=[step]),
        traces=(trace,),
        outcomes=(outcome,),
        reply="verified availability",
        responder_model=None,
        active_task=None,
        persisted_task=None,
        pending_write=None,
    )


def test_live_read_context_persists_only_displayed_verified_concrete_options() -> None:
    turn = _availability_turn_for_context(
        date_value="2026-10-10",
        times=["12:00", "14:00", "15:30"],
    )
    context = _verified_read_context_from_turn(
        None,
        workspace=SimpleNamespace(id="workspace"),
        turn=turn,
    )
    assert context is not None
    assert context["service_id"] == SERVICE_ID
    assert context["doctor_id"] == DOCTOR_ID
    assert context["device_key"] == DEVICE_KEY
    options = context["availability_reference_options"]
    assert [item["slot"]["start_time_24h"] for item in options] == ["12:00", "14:00", "15:30"]


def test_reference_only_turn_carries_snapshot_and_updates_anchor_without_read() -> None:
    previous = _reference_context()
    step = PlanStep(
        operation_index=0,
        operation_type="availability",
        disposition="respond",
        response_goal="availability_reference",
        facts={
            "verified_availability_reference": True,
            "availability_reference_index": 2,
            "availability_reference_option": {"index": 2, "start_time_24h": "14:00"},
        },
    )
    turn = SimpleNamespace(
        plan=TurnPlan(steps=[step]),
        traces=(),
        understanding=TiaTurnUnderstanding(
            operations=[TurnOperation(type="availability", entities=TurnEntities())]
        ),
    )
    context = _verified_read_context_from_turn(
        None,
        workspace=SimpleNamespace(id="workspace"),
        turn=turn,
        previous_read_context=previous,
    )
    assert context is not None
    assert context["availability_reference_anchor_index"] == 2
    assert context["service_id"] == SERVICE_ID
    assert context["doctor_id"] == DOCTOR_ID
    assert context["device_key"] == DEVICE_KEY


def test_new_date_live_read_context_invalidates_old_anchor_and_options() -> None:
    previous = _reference_context(anchor=2)
    turn = _availability_turn_for_context(date_value="2026-10-11", times=["10:00", "11:00"])
    context = _verified_read_context_from_turn(
        None,
        workspace=SimpleNamespace(id="workspace"),
        turn=turn,
        previous_read_context=previous,
    )
    assert context is not None
    assert context["date"]["start_date"] == "2026-10-11"
    assert "availability_reference_anchor_index" not in context
    assert [item["slot"]["start_time_24h"] for item in context["availability_reference_options"]] == ["10:00", "11:00"]


def test_package_comparison_outcome_builder_carries_owned_state_and_single_price() -> None:
    operation = TurnOperation(
        type="package_compare",
        execution_intent="informational",
        entities=TurnEntities(service=EntityReference(ref="S2")),
    )
    semantic = _semantic()
    step = plan_turn(
        TiaTurnUnderstanding(operations=[operation]),
        PlannerContext(semantic_context=semantic, active_task=None, now=NOW),
    ).steps[0]
    reads = ReadExecutionBundle(
        results=[
            ReadResult(
                kind="customer_packages",
                ok=True,
                payload={
                    "packages": [
                        {
                            "name": "Underarm 6",
                            "sessions_purchased": 6,
                            "sessions_remaining": 3,
                            "effective_status": "active",
                        }
                    ]
                },
            ),
            ReadResult(
                kind="service_catalog",
                ok=True,
                payload={
                    "service": {
                        "id": "service-laser",
                        "name": "ليزر إزالة الشعر - إبط",
                        "price_minor": 120000,
                        "currency": "EGP",
                    }
                },
            ),
        ]
    )
    outcome = build_step_outcome(
        step,
        turn=TiaTurnUnderstanding(operations=[operation]),
        semantic_context=semantic,
        reads=reads,
    )
    assert outcome.response_goal == "package_comparison"
    assert outcome.facts["customer_packages"]["packages"][0]["sessions_remaining"] == 3
    assert outcome.facts["service_catalog"]["service"]["price"] == "1200.00 EGP"
    reply = deterministic_package_comparison_reply([outcome])
    assert reply is not None
    assert "1200.00 EGP" in reply[0]
    assert "3 جلسة" in reply[0]


def test_opaque_option_ref_is_server_validated_and_invalid_ref_fails_closed() -> None:
    context = _reference_context()
    resolved = resolve_verified_availability_reference(
        Selection(kind="ref", ref="opt_2"),
        context,
    )
    assert resolved["status"] == "resolved"
    assert resolved["option"]["option_ref"] == "opt_2"
    assert resolved["option"]["slot"]["start_time_24h"] == "14:00"

    invalid = resolve_verified_availability_reference(
        Selection(kind="ref", ref="opt_99"),
        context,
    )
    assert invalid == {"status": "unavailable"}


def test_explicit_hhmm_guard_rejects_different_presented_option() -> None:
    context = _reference_context()
    mismatch = resolve_verified_availability_reference(
        Selection(kind="ref", ref="opt_3"),
        context,
        explicit_user_time="03:00",
    )
    assert mismatch["status"] == "explicit_time_mismatch"

    exact = resolve_verified_availability_reference(
        Selection(kind="ref", ref="opt_1"),
        context,
        explicit_user_time="12:00",
    )
    assert exact["status"] == "resolved"


def test_presented_availability_semantic_view_exposes_opaque_refs_not_canonical_slots() -> None:
    semantic = _semantic()
    raw = _reference_context()
    raw["last_selected_option_ref"] = "opt_2"
    safe = presented_availability_semantic_view(raw, context=semantic)
    assert safe["last_selected_option_ref"] == "opt_2"
    assert safe["options"][1]["option_ref"] == "opt_2"
    assert safe["options"][1]["start_time_24h"] == "14:00"
    assert "slot" not in safe["options"][1]
    assert "doctor_id" not in safe["options"][1]

    with_context = with_safe_availability_reference_context(
        semantic,
        availability_context=raw,
    )
    assert with_context.model_input["presented_availability"] == safe


def test_side_question_preserves_last_presented_availability_snapshot() -> None:
    previous = _reference_context()
    previous["last_selected_option_ref"] = "opt_1"
    side_turn = SimpleNamespace(
        plan=TurnPlan(
            steps=[
                PlanStep(
                    operation_index=0,
                    operation_type="pricing",
                    disposition="read",
                    reads=[ReadRequest(kind="service_catalog", parameters={"service_id": SERVICE_ID})],
                    response_goal="answer_price",
                )
            ]
        ),
        outcomes=(TurnOutcome(status="answered", response_goal="answer_price"),),
    )
    carried = _availability_reference_context_from_turn(
        side_turn,
        previous_context=previous,
        verified_read_context={"operation_type": "pricing", "service_id": SERVICE_ID},
    )
    assert carried == previous


def test_reference_selection_updates_only_last_selected_option_ref() -> None:
    previous = _reference_context()
    step = PlanStep(
        operation_index=0,
        operation_type="availability",
        disposition="respond",
        response_goal="availability_reference",
        facts={
            "verified_availability_reference": True,
            "availability_reference_index": 2,
            "availability_reference_option_ref": "opt_2",
            "availability_reference_option": {
                "option_ref": "opt_2",
                "index": 2,
                "start_time_24h": "14:00",
            },
        },
    )
    turn = SimpleNamespace(
        plan=TurnPlan(steps=[step]),
        outcomes=(TurnOutcome(status="answered", response_goal="availability_reference"),),
    )
    updated = _availability_reference_context_from_turn(
        turn,
        previous_context=previous,
        verified_read_context=None,
    )
    assert updated is not None
    assert updated["last_selected_option_ref"] == "opt_2"
    assert updated["availability_reference_options"] == previous["availability_reference_options"]


def test_new_availability_replaces_snapshot_and_clears_selected_ref() -> None:
    previous = _reference_context()
    previous["last_selected_option_ref"] = "opt_3"
    new_turn = _availability_turn_for_context(
        date_value="2026-10-11",
        times=["10:00", "11:00"],
    )
    verified = _verified_read_context_from_turn(
        None,
        workspace=SimpleNamespace(id="workspace"),
        turn=new_turn,
        previous_read_context=None,
    )
    assert verified is not None
    updated = _availability_reference_context_from_turn(
        new_turn,
        previous_context=previous,
        verified_read_context=verified,
    )
    assert updated is not None
    assert "last_selected_option_ref" not in updated
    assert updated["date"]["start_date"] == "2026-10-11"
    assert [item["option_ref"] for item in updated["availability_reference_options"]] == [
        "opt_1",
        "opt_2",
    ]

def test_full_booking_interpreter_can_bind_last_presented_option_without_stale_prose() -> None:
    raw = _reference_context()
    raw["last_selected_option_ref"] = "opt_2"
    semantic = with_safe_availability_reference_context(
        _semantic(),
        availability_context=raw,
    )
    model_turn = TiaTurnUnderstanding(
        operations=[
            TurnOperation(
                type="book",
                entities=TurnEntities(),
                execution_intent="execute",
                continues_previous=True,
                selection=Selection(kind="ref", ref="opt_2"),
                fresh_task=True,
                fresh_task_explicit_fields=[],
            )
        ]
    )

    bound = merge_presented_availability_context(model_turn, semantic)
    operation = bound.operations[0]
    assert operation.selection == Selection(kind="ref", ref="opt_2")
    assert operation.continues_previous is True
    assert operation.fresh_task is False
    assert operation.entities.date is not None
    assert operation.entities.date.start_date == "2026-10-10"
    assert operation.entities.time is not None
    assert operation.entities.time.start_time == "14:00"
    assert operation.entities.time.start_time_ambiguity == "none"
    assert operation.entities.service is not None
    assert operation.entities.service.ref is not None
    assert operation.entities.doctor is not None
    assert operation.entities.doctor.ref is not None

    step = plan_turn(
        bound,
        PlannerContext(
            semantic_context=semantic,
            active_task=None,
            now=NOW,
            availability_reference_context=raw,
        ),
    ).steps[0]
    assert step.operation_type == "book"
    assert step.state_action == "start_booking"
    assert any(read.kind == "availability" for read in step.reads)
    assert step.facts["time"]["start_time"] == "14:00"


def test_booking_with_invalid_or_compressed_option_ref_cannot_reach_write_path() -> None:
    first = _slot("14:15", end="14:45")
    second = _slot("14:45", end="15:15")
    raw = _reference_context()
    raw["availability_reference_options"] = build_availability_reference_options(
        displayed_windows=[
            {
                "start_local": first["start_local"],
                "end_local": second["end_local"],
                "start_time_24h": "14:15",
                "end_time_24h": "15:15",
                "doctor_name": "Mariam",
                "laser_device_name": "Candela",
            }
        ],
        verified_slots=[first, second],
    )
    semantic = with_safe_availability_reference_context(
        _semantic(),
        availability_context=raw,
    )
    operation = TurnOperation(
        type="book",
        entities=TurnEntities(),
        execution_intent="execute",
        continues_previous=True,
        selection=Selection(kind="ref", ref="opt_1"),
    )
    bound = merge_presented_availability_context(
        TiaTurnUnderstanding(operations=[operation]),
        semantic,
    )
    step = plan_turn(
        bound,
        PlannerContext(
            semantic_context=semantic,
            active_task=None,
            now=NOW,
            availability_reference_context=raw,
        ),
    ).steps[0]
    assert step.disposition == "clarify"
    assert step.clarification_field == "availability_reference"
    assert step.reads == []
    assert step.write_intent is None
    assert step.facts["availability_reference_reason"] == "window_ambiguous"



def test_new_search_semantic_turn_invalidates_stale_presented_snapshot() -> None:
    previous = _reference_context(anchor=2)
    turn = SimpleNamespace(
        plan=TurnPlan(
            steps=[
                PlanStep(
                    operation_index=0,
                    operation_type="availability",
                    disposition="clarify",
                    response_goal="clarification",
                    clarification_field="service",
                )
            ]
        ),
        outcomes=(TurnOutcome(status="needs_input", response_goal="clarification"),),
        reference_semantic_path_used=True,
        reference_action="new_search",
    )
    updated = _availability_reference_context_from_turn(
        turn,
        previous_context=previous,
        verified_read_context=None,
    )
    assert updated is None
