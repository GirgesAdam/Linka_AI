from __future__ import annotations

from collections.abc import Hashable

from app.agents.v2.turn_contract import (
    DateConstraint,
    EntityReference,
    TiaTurnUnderstanding,
    TurnEntities,
    TurnOperation,
)


def _entity_identity(entity: EntityReference | None) -> Hashable:
    if entity is None:
        return None
    if entity.ref is not None:
        return ("ref", entity.ref)
    if entity.candidate_refs:
        return ("candidates", entity.candidate_mode, tuple(entity.candidate_refs))
    return ("ungrounded_text", entity.text)


def _default_read_availability_date(operation: TurnOperation) -> TurnOperation:
    """Make an undated availability read mean the nearest upcoming availability.

    Availability is intrinsically read-only, so this default can improve useful discovery without
    authorizing or approximating any write. Booking/reschedule operations keep their stricter date
    requirements and are intentionally untouched here.
    """
    if operation.type != "availability" or operation.entities.date is not None:
        return operation
    entities = operation.entities.model_copy(
        update={
            "date": DateConstraint(
                mode="next_available",
                start_date=None,
                end_date=None,
            )
        }
    )
    return operation.model_copy(update={"entities": entities})


def _normalize_pulse_pricing(operation: TurnOperation) -> TurnOperation:
    """Repair only an explicit structured Pulse-price classification contradiction.

    Pulse count alone is intentionally insufficient: "1000 extra Pulses" is overage semantics,
    while "1000-Pulse pack" is offer semantics. The LLM owns that semantic distinction; Python only
    normalizes it when requested_pulse_details already states the intended Pulse price surface.
    """
    if operation.type != "pricing" or operation.entities.service is not None:
        return operation
    details = list(dict.fromkeys(operation.requested_pulse_details))
    if not details or not set(details).issubset({"offers", "overage_price"}):
        return operation
    return operation.model_copy(
        update={
            "type": "pulse_info",
            "requested_service_details": [],
            "requested_pulse_details": details,
            "execution_intent": "informational",
        }
    )


def _normalize_doctor_set_booking_comparison(operation: TurnOperation) -> TurnOperation:
    """Repair one impossible booking shape into its read-only comparison meaning.

    The interpreter has already grounded a requested doctor set and nearest-availability date.
    Python does not inspect customer text; it only prevents an executable booking from targeting
    multiple doctors simultaneously and preserves the grounded comparison scope as availability.
    """
    doctor = operation.entities.doctor
    date = operation.entities.date
    if (
        operation.type != "book"
        or operation.execution_intent != "execute"
        or doctor is None
        or doctor.ref is not None
        or doctor.candidate_mode != "set"
        or len(doctor.candidate_refs) < 2
        or date is None
        or date.mode != "next_available"
    ):
        return operation
    return operation.model_copy(
        update={
            "type": "availability",
            "execution_intent": "informational",
        }
    )


def _split_pulse_financial_ledger(
    operation: TurnOperation,
) -> tuple[TurnOperation | None, bool]:
    """Separate a receptionist-owned Pulse financial concern from Agent-owned Pulse reads.

    The interpreter supplies financial_ledger as structured semantics. Python never inspects raw
    customer text and never turns this marker into a financial read. If safe Pulse details are also
    requested, preserve those details as one informational pulse_info operation and signal that one
    ordinary human_support operation is required for the financial concern.
    """
    details = list(dict.fromkeys(operation.requested_pulse_details))
    if "financial_ledger" not in details:
        return operation, False

    safe_details = [detail for detail in details if detail != "financial_ledger"]
    if not safe_details:
        return None, True

    safe_operation = operation.model_copy(
        update={
            "type": "pulse_info",
            "requested_service_details": [],
            "requested_pulse_details": safe_details,
            "financial_ownership": "none",
            "execution_intent": "informational",
        }
    )
    return safe_operation, True


def _split_generic_financial_ownership(
    operation: TurnOperation | None,
) -> tuple[TurnOperation | None, bool]:
    """Convert the model's typed receptionist-owned financial marker into a safe handoff."""
    if operation is None or operation.financial_ownership != "reception":
        return operation, False
    if operation.type == "human_support":
        return operation, True
    return None, True


def normalize_semantic_invariants(turn: TiaTurnUnderstanding) -> TiaTurnUnderstanding:
    """Repair contradictions using only structured model output, never raw customer text."""
    existing_human_support = any(operation.type == "human_support" for operation in turn.operations)
    requires_financial_handoff = False
    operations: list[TurnOperation] = []

    for operation in turn.operations:
        normalized = _normalize_pulse_pricing(operation)
        normalized = _normalize_doctor_set_booking_comparison(normalized)
        normalized, needs_handoff = _split_pulse_financial_ledger(normalized)
        requires_financial_handoff = requires_financial_handoff or needs_handoff
        normalized, needs_handoff = _split_generic_financial_ownership(normalized)
        requires_financial_handoff = requires_financial_handoff or needs_handoff
        if normalized is not None:
            operations.append(normalized)

    if requires_financial_handoff and not existing_human_support:
        operations.append(
            TurnOperation(
                type="human_support",
                entities=TurnEntities(),
                financial_ownership="reception",
                execution_intent="informational",
            )
        )

    if operations == turn.operations:
        return turn
    return turn.model_copy(update={"operations": operations})


def _operation_identity(operation: TurnOperation) -> Hashable:
    entities = operation.entities
    return (
        operation.type,
        operation.execution_intent,
        operation.financial_ownership,
        operation.continues_previous,
        operation.source_appointment.model_dump_json()
        if operation.source_appointment is not None
        else None,
        _entity_identity(entities.service),
        _entity_identity(entities.doctor),
        _entity_identity(entities.device),
        _entity_identity(entities.appointment),
        _entity_identity(entities.package),
        entities.date.model_dump_json() if entities.date is not None else None,
        entities.time.model_dump_json() if entities.time is not None else None,
        entities.package_sessions,
        entities.pulse_count,
        entities.marketing_consent,
        entities.follow_up_at_local,
        operation.selection.model_dump_json() if operation.selection is not None else None,
        operation.package_usage,
        tuple(operation.requested_service_details),
        tuple(operation.requested_clinic_details),
        tuple(operation.requested_pulse_details),
    )


def dedupe_exact_operations(turn: TiaTurnUnderstanding) -> TiaTurnUnderstanding:
    """Normalize safe read defaults, then drop exact duplicates in customer order.

    This works only on the structured interpreter contract. It never inspects raw customer text and
    never merges operations whose grounded entities, execution intent, or constraints differ.
    """

    seen: set[Hashable] = set()
    operations: list[TurnOperation] = []
    for raw_operation in turn.operations:
        operation = _default_read_availability_date(raw_operation)
        identity = _operation_identity(operation)
        if identity in seen:
            continue
        seen.add(identity)
        operations.append(operation)

    if operations == turn.operations:
        return turn
    return turn.model_copy(update={"operations": operations})
