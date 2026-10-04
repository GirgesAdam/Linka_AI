from __future__ import annotations

from datetime import date

from app.agents.v2.customer_datetime import format_customer_date
from app.agents.v2.turn_contract import TiaTurnUnderstanding, TurnEntities, TurnOperation
from app.services.agent_v2.orchestrator import _terminal_no_reply_allowed
from app.services.agent_v2.state import BookingTaskState, WriteAuthorization


def _social(*, automation_relationship: str = "none") -> TurnOperation:
    return TurnOperation(
        type="social",
        entities=TurnEntities(),
        execution_intent="informational",
        automation_context_relationship=automation_relationship,
    )


def test_same_year_customer_date_omits_year_and_includes_weekday() -> None:
    assert format_customer_date(
        "2026-10-04",
        arabic=True,
        reference_date=date(2026, 10, 1),
    ) == "الأحد 4 أكتوبر"


def test_different_year_customer_date_includes_year() -> None:
    assert format_customer_date(
        "2027-10-04",
        arabic=True,
        reference_date=date(2026, 10, 1),
    ) == "الاثنين 4 أكتوبر 2027"


def test_terminal_empty_semantic_closing_can_be_no_reply() -> None:
    understanding = TiaTurnUnderstanding(
        operations=[],
        safety_signals=[],
        response_disposition="no_reply",
    )

    assert _terminal_no_reply_allowed(
        understanding,
        active_task=None,
        pending_choice=None,
    ) is True


def test_no_reply_fails_closed_with_active_booking() -> None:
    understanding = TiaTurnUnderstanding(
        operations=[_social()],
        safety_signals=[],
        response_disposition="no_reply",
    )
    task = BookingTaskState(
        write_authorization=WriteAuthorization(operation="booking"),
    )

    assert _terminal_no_reply_allowed(
        understanding,
        active_task=task,
        pending_choice=None,
    ) is False


def test_automation_acknowledgement_is_not_silenced() -> None:
    understanding = TiaTurnUnderstanding(
        operations=[_social(automation_relationship="acknowledge")],
        safety_signals=[],
        response_disposition="no_reply",
    )

    assert _terminal_no_reply_allowed(
        understanding,
        active_task=None,
        pending_choice=None,
    ) is False
