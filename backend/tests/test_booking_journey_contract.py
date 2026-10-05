from __future__ import annotations

from datetime import UTC, date, datetime

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


def test_customer_date_uses_local_today_label() -> None:
    assert format_customer_date(
        "2026-10-05",
        arabic=True,
        reference_date=date(2026, 10, 5),
    ) == "\u0627\u0644\u0646\u0647\u0627\u0631\u062f\u0647"


def test_customer_date_uses_local_tomorrow_label() -> None:
    assert format_customer_date(
        "2026-10-06",
        arabic=True,
        reference_date=date(2026, 10, 5),
    ) == "\u0628\u0643\u0631\u0629"


def test_customer_date_later_date_keeps_weekday_day_month() -> None:
    assert format_customer_date(
        "2026-10-07",
        arabic=True,
        reference_date=date(2026, 10, 5),
    ) == "\u0627\u0644\u0623\u0631\u0628\u0639\u0627\u0621 7 \u0623\u0643\u062a\u0648\u0628\u0631"


def test_customer_date_uses_workspace_timezone_across_utc_calendar_boundary() -> None:
    assert format_customer_date(
        "2026-10-05",
        arabic=True,
        reference_datetime=datetime(2026, 10, 4, 22, 30, tzinfo=UTC),
        timezone_name="Africa/Cairo",
    ) == "\u0627\u0644\u0646\u0647\u0627\u0631\u062f\u0647"


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


def test_no_reply_fails_closed_when_semantic_marker_is_missing() -> None:
    class LegacyUnderstanding:
        operations = []
        safety_signals = []

    assert _terminal_no_reply_allowed(
        LegacyUnderstanding(),  # type: ignore[arg-type]
        active_task=None,
        pending_choice=None,
    ) is False


def test_automation_context_presence_is_not_silenced_even_if_semantics_misclassify() -> None:
    understanding = TiaTurnUnderstanding(
        operations=[],
        safety_signals=[],
        response_disposition="no_reply",
    )

    assert _terminal_no_reply_allowed(
        understanding,
        active_task=None,
        pending_choice=None,
        automation_context={"source": "automation_engine", "kind": "appointment_reminder"},
    ) is False
    assert _terminal_no_reply_allowed(
        understanding,
        active_task=None,
        pending_choice=None,
        automation_context={},
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
