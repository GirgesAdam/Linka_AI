
from datetime import UTC, datetime

from app.agents.v2.semantic_context import build_semantic_context
from app.agents.v2.semantic_state_view import with_safe_action_context
from app.agents.v2.turn_contract import (
    AppointmentSelector,
    EntityReference,
    TiaTurnUnderstanding,
    TurnEntities,
    TurnOperation,
)
from app.agents.v2.turn_interpreter import (
    _interpreter_system_prompt,
    merge_verified_action_context,
)

SERVICE_ID = "123e4567-e89b-12d3-a456-426614174001"
DOCTOR_ID = "123e4567-e89b-12d3-a456-426614174002"
RECENT_APPOINTMENT_ID = "123e4567-e89b-12d3-a456-426614174003"
OTHER_APPOINTMENT_ID = "123e4567-e89b-12d3-a456-426614174004"


def _context():
    context = build_semantic_context(
        {
            "services": [{"id": SERVICE_ID, "name": "هيدرافيشل"}],
            "doctors": [
                {"id": DOCTOR_ID, "name": "د. مريم", "service_ids": [SERVICE_ID]}
            ],
            "appointments": [
                {
                    "appointment_id": RECENT_APPOINTMENT_ID,
                    "service_id": SERVICE_ID,
                    "doctor_id": DOCTOR_ID,
                    "status": "confirmed",
                    "start_local": "2026-10-08T10:00:00+03:00",
                },
                {
                    "appointment_id": OTHER_APPOINTMENT_ID,
                    "service_id": SERVICE_ID,
                    "doctor_id": DOCTOR_ID,
                    "status": "confirmed",
                    "start_local": "2026-10-09T10:00:00+03:00",
                },
            ],
        }
    )
    return with_safe_action_context(
        context,
        action_context={
            "operation_type": "book",
            "appointment_id": RECENT_APPOINTMENT_ID,
            "service_id": SERVICE_ID,
            "doctor_id": DOCTOR_ID,
            "start_at": "2026-10-08T07:00:00+00:00",
            "status": "confirmed",
            "package_usage": "unspecified",
        },
    )


def _cancel(*, continues_previous: bool, source: AppointmentSelector | None = None):
    return TurnOperation(
        type="cancel_appointment",
        entities=TurnEntities(),
        source_appointment=source,
        execution_intent="execute",
        continues_previous=continues_previous,
    )


def test_interpreter_contract_explicitly_supports_immediate_booking_revocation() -> None:
    prompt = _interpreter_system_prompt(
        timezone_name="Africa/Cairo",
        local_now=datetime(2026, 10, 2, 10, 0, tzinfo=UTC),
    )

    assert "just-completed booking" in prompt
    assert "emit cancel_appointment" in prompt
    assert "cancel_appointment first" in prompt
    assert "Do not infer cancellation from general dissatisfaction" in prompt


def test_recent_booking_continuation_binds_cancel_to_verified_recent_appointment() -> None:
    turn = TiaTurnUnderstanding(
        operations=[
            _cancel(continues_previous=True),
            TurnOperation(
                type="availability",
                entities=TurnEntities(),
                execution_intent="informational",
            ),
        ]
    )

    merged = merge_verified_action_context(turn, _context())

    cancellation = merged.operations[0]
    assert cancellation.type == "cancel_appointment"
    assert cancellation.source_appointment is not None
    assert cancellation.source_appointment.appointment is not None
    assert cancellation.source_appointment.appointment.ref == "A1"
    assert merged.operations[1].type == "availability"


def test_recent_booking_does_not_create_or_bind_cancellation_without_semantic_continuation() -> None:
    informational = TiaTurnUnderstanding(
        operations=[
            TurnOperation(
                type="availability",
                entities=TurnEntities(),
                execution_intent="informational",
            )
        ]
    )
    assert merge_verified_action_context(informational, _context()) == informational

    unrelated_cancel = TiaTurnUnderstanding(
        operations=[_cancel(continues_previous=False)]
    )
    assert merge_verified_action_context(unrelated_cancel, _context()) == unrelated_cancel




def test_recent_booking_binding_adds_verified_appointment_ref_alongside_reconstructed_scope() -> None:
    turn = TiaTurnUnderstanding(
        operations=[
            _cancel(
                continues_previous=True,
                source=AppointmentSelector(
                    service=EntityReference(text="هيدرافيشل", ref="S1", candidate_refs=[]),
                ),
            )
        ]
    )

    merged = merge_verified_action_context(turn, _context())

    source = merged.operations[0].source_appointment
    assert source is not None
    assert source.appointment is not None
    assert source.appointment.ref == "A1"
    assert source.service is not None
    assert source.service.ref == "S1"


def test_explicit_different_appointment_target_is_never_overwritten_by_recent_booking() -> None:
    turn = TiaTurnUnderstanding(
        operations=[
            _cancel(
                continues_previous=True,
                source=AppointmentSelector(
                    appointment=EntityReference(
                        text=None,
                        ref="A2",
                        candidate_refs=[],
                    )
                ),
            )
        ]
    )

    merged = merge_verified_action_context(turn, _context())

    source = merged.operations[0].source_appointment
    assert source is not None
    assert source.appointment is not None
    assert source.appointment.ref == "A2"


def test_ambiguous_current_target_is_not_silently_replaced_by_recent_booking() -> None:
    turn = TiaTurnUnderstanding(
        operations=[
            _cancel(
                continues_previous=True,
                source=AppointmentSelector(
                    appointment=EntityReference(
                        text="المعاد التاني",
                        ref=None,
                        candidate_refs=["A1", "A2"],
                    )
                ),
            )
        ]
    )

    merged = merge_verified_action_context(turn, _context())

    source = merged.operations[0].source_appointment
    assert source is not None
    assert source.appointment is not None
    assert source.appointment.ref is None
    assert source.appointment.candidate_refs == ["A1", "A2"]
