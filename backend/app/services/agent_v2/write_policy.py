from __future__ import annotations

from app.services.agent_v2.planner import PlanStep, advance_step_after_verification
from app.services.agent_v2.read_executor import ReadExecutionBundle

_NON_FINANCIAL_PAYMENT_STATUSES = frozenset({"", "unknown", "unpaid", "pending", "none", "not_required"})


def _appointment_has_financial_effect(row: dict[str, object]) -> bool:
    """Fail closed when cancellation could change money or package entitlement."""

    if row.get("patient_package_id") or row.get("package_external_id"):
        return True
    if str(row.get("billing_context") or "").strip().lower() in {
        "package_prepaid",
        "pulse_prepaid",
    }:
        return True
    try:
        if int(row.get("amount_paid_minor") or 0) > 0:
            return True
    except (TypeError, ValueError):
        return True
    payment_status = str(row.get("payment_status") or "unknown").strip().lower()
    return payment_status not in _NON_FINANCIAL_PAYMENT_STATUSES


def paid_appointment_cancellation_requires_human(
    step: PlanStep,
    reads: ReadExecutionBundle,
) -> bool:
    if step.write_intent is None or step.write_intent.kind != "cancel_appointment":
        return False

    matches: list[dict[str, object]] = []
    for result in reads.results:
        if result.kind != "appointments" or not result.ok:
            continue
        rows = result.payload.get("appointments")
        if not isinstance(rows, list):
            continue
        matches.extend(row for row in rows if isinstance(row, dict))

    # The planner only writes after exactly one appointment is verified. If that one appointment has
    # money/package state attached, Linka must stop before mutation and hand the request to clinic staff.
    return len(matches) == 1 and _appointment_has_financial_effect(matches[0])


def advance_step_with_write_policies(
    step: PlanStep,
    reads: ReadExecutionBundle,
) -> PlanStep:
    """Apply deterministic verification, then central pre-write safety policies."""

    advanced = (
        advance_step_after_verification(step, reads.verification)
        if step.write_intent is not None and step.reads
        else step
    )
    if advanced.disposition == "write_ready" and paid_appointment_cancellation_requires_human(
        advanced,
        reads,
    ):
        return advanced.model_copy(
            update={
                "disposition": "handoff",
                "response_goal": "handoff",
                "facts": {
                    **advanced.facts,
                    "category": "paid_cancellation",
                    "priority": "normal",
                    "reason": "financial_cancellation_requires_staff",
                },
            }
        )
    return advanced
