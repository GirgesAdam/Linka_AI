from __future__ import annotations

from types import SimpleNamespace

from app.agents.v2.semantic_context import build_semantic_context
from app.agents.v2.semantic_state_view import with_safe_read_context
from app.services.agent_v2.live_chat import (
    _availability_option_count_for_step,
    _outbound_verified_action_context,
    _recent_verified_action_context_from_outbounds,
    _safe_action_context_passthrough,
    _verified_action_context_from_turn,
)


def test_safe_read_context_exposes_only_verified_availability_summary() -> None:
    context = build_semantic_context(
        {
            "services": [{"id": "service-1", "name": "استشارة"}],
            "doctors": [],
            "branches": [],
        }
    )
    safe = with_safe_read_context(
        context,
        read_context={
            "operation_type": "availability",
            "service_id": "service-1",
            "availability_option_count": 4,
            "availability_presented_window_keys": ["internal-cursor"],
            "availability_presentation_has_more": True,
            "internal_note": "never expose",
        },
    )

    recent = safe.model_input["recent_verified_read"]
    assert recent["service_ref"] == "S1"
    assert recent["availability_option_count"] == 4
    assert recent["availability_found"] is True
    assert "availability_presented_window_keys" not in recent
    assert "availability_presentation_has_more" not in recent
    assert "internal_note" not in recent


def test_zero_verified_options_is_exposed_as_false_condition() -> None:
    context = build_semantic_context(
        {"services": [{"id": "service-1", "name": "استشارة"}], "doctors": [], "branches": []}
    )
    safe = with_safe_read_context(
        context,
        read_context={
            "operation_type": "availability",
            "service_id": "service-1",
            "availability_option_count": 0,
        },
    )

    recent = safe.model_input["recent_verified_read"]
    assert recent["availability_option_count"] == 0
    assert recent["availability_found"] is False


def test_live_context_reads_option_count_from_matching_outcome_only() -> None:
    turn = SimpleNamespace(
        traces=(
            SimpleNamespace(
                operation_index=0,
                outcome=SimpleNamespace(
                    facts={"availability": {"available_option_count": 7}}
                ),
            ),
        )
    )

    assert _availability_option_count_for_step(turn, operation_index=0) == 7
    assert _availability_option_count_for_step(turn, operation_index=1) is None



def test_completed_pulse_purchase_produces_minimal_verified_action_context() -> None:
    turn = SimpleNamespace(
        traces=(
            SimpleNamespace(
                operation_index=0,
                outcome=SimpleNamespace(
                    status="completed",
                    action_result={
                        "ok": True,
                        "action": "buy_pulse_pack",
                    },
                ),
            ),
        ),
        plan=SimpleNamespace(
            steps=(
                SimpleNamespace(
                    operation_index=0,
                    write_intent=SimpleNamespace(
                        kind="buy_pulse_pack",
                        parameters={
                            "device_key": "candela_gentle",
                            "pulse_count": 1000,
                            "pulse_pack_offer_id": "internal-offer",
                        },
                    ),
                ),
            )
        ),
    )

    assert _verified_action_context_from_turn(turn) == {
        "operation_type": "buy_pulse_pack",
        "device_key": "candela_gentle",
        "pulse_count": 1000,
    }


def test_failed_pulse_purchase_does_not_produce_verified_action_context() -> None:
    turn = SimpleNamespace(
        traces=(
            SimpleNamespace(
                operation_index=0,
                outcome=SimpleNamespace(
                    status="blocked",
                    action_result={
                        "ok": False,
                        "action": "buy_pulse_pack",
                    },
                ),
            ),
        ),
        plan=SimpleNamespace(steps=()),
    )

    assert _verified_action_context_from_turn(turn) is None


def test_direct_verified_booking_action_context_is_preserved_for_next_turn() -> None:
    context = {
        "operation_type": "book",
        "appointment_id": "appointment-1",
        "service_id": "service-1",
        "doctor_id": "doctor-1",
        "device_key": "candela_gentle",
        "start_at": "2026-09-25T11:00:00+00:00",
        "status": "confirmed",
        "package_usage": "unspecified",
    }
    turn = SimpleNamespace(
        verified_action_context=context,
        traces=(),
        plan=SimpleNamespace(steps=()),
    )

    assert _verified_action_context_from_turn(turn) == context


def test_direct_verified_cancellation_action_context_is_preserved_for_next_turn() -> None:
    context = {
        "operation_type": "cancel_appointment",
        "appointment_id": "appointment-1",
        "status": "cancelled",
    }
    turn = SimpleNamespace(
        verified_action_context=context,
        traces=(),
        plan=SimpleNamespace(steps=()),
    )

    assert _verified_action_context_from_turn(turn) == context


def _informational_turn(
    *,
    operation_type: str = "pricing",
    active_task=None,
    write_intent=None,
    state_action: str = "none",
    disposition: str = "read",
    execution_intent: str = "informational",
):
    return SimpleNamespace(
        verified_action_context=None,
        traces=(),
        active_task=active_task,
        pending_write=None,
        plan=SimpleNamespace(
            steps=(
                SimpleNamespace(
                    operation_index=0,
                    operation_type=operation_type,
                    disposition=disposition,
                    write_intent=write_intent,
                    state_action=state_action,
                ),
            )
        ),
        understanding=SimpleNamespace(
            operations=(
                SimpleNamespace(
                    type=operation_type,
                    execution_intent=execution_intent,
                ),
            )
        ),
    )


def _recent_booking_context() -> dict[str, object]:
    return {
        "operation_type": "book",
        "appointment_id": "appointment-1",
        "service_id": "service-1",
        "doctor_id": "doctor-1",
        "start_at": "2026-09-28T07:00:00+00:00",
        "status": "confirmed",
        "package_usage": "unspecified",
    }


def _outbound_message(
    *,
    context: dict[str, object] | None = None,
    passthrough: bool = False,
    runtime: str = "v2",
    sender_type: str = "ai",
):
    return SimpleNamespace(
        sender_type=sender_type,
        direction="outbound",
        metadata_json={
            "runtime": runtime,
            "v2_action_context": context,
            "v2_action_context_passthrough": passthrough,
        },
    )


def test_recent_booking_context_recovers_across_safe_passthrough_chain() -> None:
    recent = _recent_booking_context()
    messages = [
        _outbound_message(passthrough=True),
        _outbound_message(passthrough=True),
        _outbound_message(context=recent),
    ]

    assert _recent_verified_action_context_from_outbounds(messages) == recent


def test_recent_booking_context_does_not_cross_non_passthrough_barrier() -> None:
    recent = _recent_booking_context()
    messages = [
        _outbound_message(passthrough=True),
        _outbound_message(passthrough=False),
        _outbound_message(context=recent),
    ]

    assert _recent_verified_action_context_from_outbounds(messages) is None


def test_recent_booking_context_does_not_recover_old_non_booking_action() -> None:
    messages = [
        _outbound_message(passthrough=True),
        _outbound_message(
            context={
                "operation_type": "cancel_appointment",
                "appointment_id": "appointment-1",
                "status": "cancelled",
            }
        ),
    ]

    assert _recent_verified_action_context_from_outbounds(messages) is None


def test_safe_action_context_passthrough_requires_read_only_informational_turn() -> None:
    assert _safe_action_context_passthrough(_informational_turn()) is True
    assert (
        _safe_action_context_passthrough(
            _informational_turn(
                operation_type="book",
                write_intent=SimpleNamespace(kind="booking"),
                state_action="start_booking",
                execution_intent="execute",
            )
        )
        is False
    )
    assert (
        _safe_action_context_passthrough(
            _informational_turn(active_task=SimpleNamespace(task_type="booking"))
        )
        is False
    )


def test_completed_booking_context_survives_informational_detours() -> None:
    recent = _recent_booking_context()

    after_price = _outbound_verified_action_context(
        _informational_turn(operation_type="pricing"),
        recent_action_context=recent,
    )
    assert after_price == recent

    after_clinic_info = _outbound_verified_action_context(
        _informational_turn(operation_type="clinic_info"),
        recent_action_context=after_price,
    )
    assert after_clinic_info == recent

    after_list = _outbound_verified_action_context(
        _informational_turn(operation_type="appointment_list"),
        recent_action_context=after_clinic_info,
    )
    assert after_list == recent


def test_booking_context_is_not_retained_across_new_executable_action() -> None:
    recent = _recent_booking_context()
    turn = _informational_turn(
        operation_type="book",
        write_intent=SimpleNamespace(kind="booking"),
        state_action="start_booking",
        execution_intent="execute",
    )

    assert (
        _outbound_verified_action_context(
            turn,
            recent_action_context=recent,
        )
        is None
    )


def test_booking_context_is_not_retained_when_active_task_exists() -> None:
    recent = _recent_booking_context()

    assert (
        _outbound_verified_action_context(
            _informational_turn(active_task=SimpleNamespace(task_type="booking")),
            recent_action_context=recent,
        )
        is None
    )


def test_new_verified_action_context_replaces_retained_booking_context() -> None:
    recent = _recent_booking_context()
    direct = {
        "operation_type": "cancel_appointment",
        "appointment_id": "appointment-1",
        "status": "cancelled",
    }
    turn = _informational_turn()
    turn.verified_action_context = direct

    assert _outbound_verified_action_context(
        turn,
        recent_action_context=recent,
    ) == direct
