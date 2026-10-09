from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import date, datetime
from typing import Literal

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from pydantic import BaseModel, ConfigDict

from app.agents.doctor_names import format_doctor_name
from app.agents.llm_runtime import LLMProviderError, invoke_with_model_chain
from app.agents.model_provider import (
    build_realtime_composer_fallback_model,
    build_realtime_composer_model,
    model_label,
)
from app.agents.structured_output import StructuredOutputError, invoke_typed_structured_output
from app.agents.v2.availability_pagination import select_availability_window_page
from app.agents.v2.customer_datetime import format_customer_date
from app.core.config import settings
from app.services.agent_v2.response_contract import (
    AVAILABILITY_STATE_BY_GOAL,
    CustomerResponseContract,
    CustomerResponseUnit,
    ResponseFact,
)

logger = logging.getLogger(__name__)

AvailabilityStyle = Literal["plain", "warm", "friendly"]
AvailabilityPresentationMode = Literal["compact", "detailed"]
AvailabilityClosingAction = Literal[
    "ask_selection",
    "offer_other_time",
    "offer_other_scope",
    "none",
]
AvailabilityTransition = Literal["sentence", "and", "then"]
AvailabilityReference = Literal["unit_availability"]


class StrictAvailabilityComposerModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AvailabilityComposerUnitDraft(StrictAvailabilityComposerModel):
    unit_index: int
    availability_ref: AvailabilityReference
    style: AvailabilityStyle
    window_refs: list[str]
    optional_fact_keys: list[str]
    presentation_mode: AvailabilityPresentationMode
    closing_action: AvailabilityClosingAction
    transition: AvailabilityTransition


class AvailabilityComposerDraft(StrictAvailabilityComposerModel):
    units: list[AvailabilityComposerUnitDraft]


class AvailabilityComposerPresentationDraft(StrictAvailabilityComposerModel):
    """LLM-owned presentation choices only; all structural truth stays backend-owned."""

    style: AvailabilityStyle
    presentation_mode: AvailabilityPresentationMode


class AvailabilityComposerValidationError(RuntimeError):
    def __init__(self, message: str, *, reason: str = "invalid_contract") -> None:
        super().__init__(message)
        self.reason = reason


_OPTIONAL_FACT_KEYS = frozenset({"service_name"})

_ALLOWED_CLOSING_BY_STATE: dict[str, frozenset[str]] = {
    "options_available": frozenset({"ask_selection", "none"}),
    "requested_time_unavailable": frozenset({"offer_other_time", "none"}),
    "no_availability": frozenset({"offer_other_scope", "none"}),
}

_AR_WEEKDAYS = (
    "الاثنين",
    "الثلاثاء",
    "الأربعاء",
    "الخميس",
    "الجمعة",
    "السبت",
    "الأحد",
)
_EN_WEEKDAYS = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)
_AR_MONTHS = {
    1: "يناير",
    2: "فبراير",
    3: "مارس",
    4: "أبريل",
    5: "مايو",
    6: "يونيو",
    7: "يوليو",
    8: "أغسطس",
    9: "سبتمبر",
    10: "أكتوبر",
    11: "نوفمبر",
    12: "ديسمبر",
}


def _message_text(message: BaseMessage, *, limit: int = 1000) -> str:
    if not isinstance(message.content, str) or not message.content.strip():
        return ""
    text = " ".join(message.content.strip().split())
    return text[:limit] + ("…" if len(text) > limit else "")


def _latest_customer_index(history: list[BaseMessage]) -> int | None:
    for index in range(len(history) - 1, -1, -1):
        if isinstance(history[index], HumanMessage) and _message_text(history[index]):
            return index
    return None


def _latest_customer_is_arabic(history: list[BaseMessage]) -> bool:
    index = _latest_customer_index(history)
    text = _message_text(history[index]) if index is not None else ""
    return any("\u0600" <= char <= "\u06ff" for char in text)


def _fact_map(unit: CustomerResponseUnit) -> dict[str, ResponseFact]:
    return {fact.key: fact for fact in unit.facts}


def _window_fact(unit: CustomerResponseUnit) -> ResponseFact | None:
    fact = _fact_map(unit).get("availability_windows")
    if fact is None or not isinstance(fact.value, list):
        return None
    return fact


def _window_refs(unit_index: int, unit: CustomerResponseUnit) -> list[str]:
    fact = _window_fact(unit)
    if fact is None:
        return []
    return [
        f"unit_{unit_index}_window_{index}"
        for index, value in enumerate(fact.value)
        if isinstance(value, dict)
    ]


def _contract_view(contract: CustomerResponseContract) -> list[dict[str, object]]:
    units: list[dict[str, object]] = []
    for index, unit in enumerate(contract.units):
        truth = unit.availability_truth
        refs = _window_refs(index, unit)
        units.append(
            {
                "unit_index": index,
                "response_goal": unit.response_goal,
                "availability_state": truth.state if truth is not None else None,
                "window_count": len(refs),
                "has_optional_service_name": any(
                    fact.key == "service_name" for fact in unit.facts
                ),
            }
        )
    return units


def _build_availability_composer_messages(
    *,
    history: list[BaseMessage],
    contract: CustomerResponseContract,
) -> list[BaseMessage]:
    latest_index = _latest_customer_index(history)
    if latest_index is None:
        raise AvailabilityComposerValidationError(
            "Availability composer requires a customer message.",
            reason="missing_customer_message",
        )
    arabic = _latest_customer_is_arabic(history)
    language = "Egyptian Arabic" if arabic else "English"
    system = SystemMessage(
        content=(
            "You select presentation structure for verified clinic availability. "
            "You never write customer prose and you never return any slot/date/time/"
            "doctor/device value. The backend owns availability state and every exact value.\n"
            "The schema intentionally lets you choose only two presentation properties: "
            "style and presentation_mode. Do not return unit indexes, refs, facts, transitions, "
            "closing actions, availability states, or any business value; the backend derives "
            "all of those from the contract. Keep the presentation concise and natural. "
            "Do not infer, merge, invent, or rewrite windows.\n"
            f"Customer reply language: {language}."
        )
    )
    structure = SystemMessage(
        content=(
            "AVAILABILITY_RESPONSE_CONTRACT_STRUCTURE (symbolic refs only; no values):\n"
            + json.dumps(
                _contract_view(contract),
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
    )
    return [
        system,
        structure,
        HumanMessage(content="Select the availability presentation structure."),
    ]


def _materialize_availability_composer_draft(
    contract: CustomerResponseContract,
    presentation: AvailabilityComposerPresentationDraft,
) -> AvailabilityComposerDraft:
    """Bind model-owned style choices to backend-owned structure with no LLM validation gap."""
    units: list[AvailabilityComposerUnitDraft] = []
    for index, unit in enumerate(contract.units):
        truth = unit.availability_truth
        if truth is None or truth.state not in _ALLOWED_CLOSING_BY_STATE:
            raise AvailabilityComposerValidationError(
                "Availability composer received invalid backend availability truth.",
                reason="invalid_backend_truth",
            )
        units.append(
            AvailabilityComposerUnitDraft(
                unit_index=index,
                availability_ref="unit_availability",
                style=presentation.style,
                window_refs=_window_refs(index, unit),
                optional_fact_keys=[
                    key for key in ("service_name",) if key in _fact_map(unit)
                ],
                presentation_mode=presentation.presentation_mode,
                closing_action=_fallback_closing(truth.state),
                transition="sentence" if index == 0 else "and",
            )
        )
    return AvailabilityComposerDraft(units=units)


def validate_availability_composer_draft(
    contract: CustomerResponseContract,
    draft: AvailabilityComposerDraft,
) -> None:
    if len(draft.units) != len(contract.units):
        raise AvailabilityComposerValidationError(
            "Availability composer must represent every contract unit exactly once.",
            reason="invalid_transition",
        )

    for expected_index, (unit, draft_unit) in enumerate(
        zip(contract.units, draft.units, strict=True)
    ):
        if draft_unit.unit_index != expected_index:
            raise AvailabilityComposerValidationError(
                "Availability composer changed compound unit ordering.",
                reason="invalid_transition",
            )
        truth = unit.availability_truth
        expected_state = AVAILABILITY_STATE_BY_GOAL.get(unit.response_goal)
        if truth is None or expected_state is None or truth.state != expected_state:
            raise AvailabilityComposerValidationError(
                "Availability composer received mismatched backend availability truth.",
                reason="invalid_backend_truth",
            )

        expected_refs = _window_refs(expected_index, unit)
        if len(draft_unit.window_refs) != len(set(draft_unit.window_refs)):
            raise AvailabilityComposerValidationError(
                "Availability composer repeated a window reference.",
                reason="invalid_window_refs",
            )
        if truth.state == "options_available":
            fact = _window_fact(unit)
            if fact is None or fact.complete_set is not True:
                raise AvailabilityComposerValidationError(
                    "Available options require a complete verified window set.",
                    reason="invalid_window_refs",
                )
            if draft_unit.window_refs != expected_refs:
                raise AvailabilityComposerValidationError(
                    "Availability composer must preserve the complete verified window set.",
                    reason="invalid_window_refs",
                )
        elif draft_unit.window_refs:
            raise AvailabilityComposerValidationError(
                "Unavailable states cannot reference availability windows.",
                reason="invalid_window_refs",
            )

        if len(draft_unit.optional_fact_keys) != len(set(draft_unit.optional_fact_keys)):
            raise AvailabilityComposerValidationError(
                "Availability composer repeated an optional fact reference.",
                reason="invalid_optional_fact",
            )
        available_optional = {
            fact.key for fact in unit.facts if fact.key in _OPTIONAL_FACT_KEYS
        }
        if any(key not in available_optional for key in draft_unit.optional_fact_keys):
            raise AvailabilityComposerValidationError(
                "Availability composer referenced an unavailable optional fact.",
                reason="invalid_optional_fact",
            )

        allowed_closings = _ALLOWED_CLOSING_BY_STATE[truth.state]
        if draft_unit.closing_action not in allowed_closings:
            raise AvailabilityComposerValidationError(
                "Availability composer selected an invalid closing action for this state.",
                reason="invalid_closing_action",
            )
        if expected_index == 0 and draft_unit.transition != "sentence":
            raise AvailabilityComposerValidationError(
                "The first availability unit must start a sentence.",
                reason="invalid_transition",
            )


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip())
    except ValueError:
        return None


def _extract_time(value: object) -> tuple[int, int] | None:
    if isinstance(value, dict):
        raw = value.get("start_time") or value.get("time")
    else:
        raw = value
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    if "T" in text:
        parsed = _parse_datetime(text)
        return (parsed.hour, parsed.minute) if parsed is not None else None
    parts = text.split(":")
    if len(parts) < 2:
        return None
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour, minute


def _clock(value: object, *, arabic: bool) -> str:
    parsed = _extract_time(value)
    if parsed is None:
        return " ".join(str(value).split())
    hour, minute = parsed
    display = hour % 12 or 12
    minute_text = f":{minute:02d}" if minute else ""
    if arabic:
        period = "صباحًا" if hour < 12 else ("ظهرًا" if hour == 12 else "مساءً")
        return f"{display}{minute_text} {period}"
    period = "AM" if hour < 12 else "PM"
    return f"{display}{minute_text} {period}"


_AR_RELATIVE_DATE_LABELS = frozenset({"\u0627\u0644\u0646\u0647\u0627\u0631\u062f\u0647", "\u0628\u0643\u0631\u0629"})


def _date_text(
    value: object,
    *,
    arabic: bool,
    reference_date: date | None = None,
) -> str:
    return format_customer_date(
        value,
        arabic=arabic,
        reference_date=reference_date,
    )


def _arabic_day_phrase(label: str) -> str:
    if label in _AR_RELATIVE_DATE_LABELS:
        return label
    return f"\u064a\u0648\u0645 {label}"


def _checked_dates(unit: CustomerResponseUnit) -> list[str]:
    fact = _fact_map(unit).get("checked_dates")
    if fact is None or not isinstance(fact.value, list):
        return []
    return [str(value) for value in fact.value if value]


def _date_scope_text(
    unit: CustomerResponseUnit,
    *,
    arabic: bool,
    reference_date: date | None = None,
) -> str:
    raw_dates = _checked_dates(unit)
    if not raw_dates:
        date_fact = _fact_map(unit).get("date_constraint")
        constraint = date_fact.value if date_fact is not None else None
        if isinstance(constraint, dict):
            mode = str(constraint.get("mode") or "").strip()
            start = constraint.get("start_date")
            end = constraint.get("end_date")
            if mode == "exact" and start:
                label = _date_text(start, arabic=arabic, reference_date=reference_date)
                return _arabic_day_phrase(label) if arabic else f"on {label}"
            if mode == "range" and start and end:
                first = _date_text(start, arabic=arabic, reference_date=reference_date)
                last = _date_text(end, arabic=arabic, reference_date=reference_date)
                return f"من {first} لحد {last}" if arabic else f"from {first} through {last}"
        return "في اليوم المطلوب" if arabic else "on the requested day"
    parsed: list[date] = []
    for raw in raw_dates:
        try:
            parsed.append(date.fromisoformat(raw[:10]))
        except ValueError:
            parsed = []
            break
    if parsed and len(parsed) > 1 and all(
        (current - previous).days == 1
        for previous, current in zip(parsed, parsed[1:], strict=False)
    ):
        first = _date_text(parsed[0].isoformat(), arabic=arabic, reference_date=reference_date)
        last = _date_text(parsed[-1].isoformat(), arabic=arabic, reference_date=reference_date)
        return (
            f"من {first} لحد {last}"
            if arabic
            else f"from {first} through {last}"
        )
    labels = [_date_text(raw, arabic=arabic, reference_date=reference_date) for raw in raw_dates]
    if len(labels) == 1:
        return (_arabic_day_phrase(labels[0]) if arabic else f"on {labels[0]}")
    joined = "، ".join(labels)
    return (
        f"في الأيام دي: {joined}"
        if arabic
        else f"on these dates: {joined}"
    )


def _time_scope_text(unit: CustomerResponseUnit, *, arabic: bool) -> str:
    fact = _fact_map(unit).get("time_constraint")
    value = fact.value if fact is not None else None
    if not isinstance(value, dict):
        return ""
    mode = str(value.get("mode") or "").strip()
    start = value.get("start_time")
    end = value.get("end_time")
    if mode == "after" and start:
        clock = _clock(start, arabic=arabic)
        return f"بعد الساعة {clock}" if arabic else f"after {clock}"
    if mode == "before" and start:
        clock = _clock(start, arabic=arabic)
        return f"قبل الساعة {clock}" if arabic else f"before {clock}"
    if mode == "range" and start and end:
        first = _clock(start, arabic=arabic)
        last = _clock(end, arabic=arabic)
        return f"من الساعة {first} لحد {last}" if arabic else f"from {first} to {last}"
    return ""


def _availability_scope_text(
    unit: CustomerResponseUnit,
    *,
    arabic: bool,
    reference_date: date | None = None,
) -> str:
    date_scope = _date_scope_text(
        unit, arabic=arabic, reference_date=reference_date
    )
    time_scope = _time_scope_text(unit, arabic=arabic)
    return f"{date_scope} {time_scope}".strip()


def _window_values(unit: CustomerResponseUnit) -> list[dict[str, object]]:
    fact = _window_fact(unit)
    if fact is None:
        return []
    return [dict(value) for value in fact.value if isinstance(value, dict)]


def _alternative_window_values(unit: CustomerResponseUnit) -> list[dict[str, object]]:
    fact = _fact_map(unit).get("nearest_alternative_windows")
    if fact is None or not isinstance(fact.value, list):
        return []
    return [dict(value) for value in fact.value if isinstance(value, dict)]


def _paged_contract(
    contract: CustomerResponseContract,
    *,
    shown_window_keys: set[str] | frozenset[str] | None,
) -> tuple[CustomerResponseContract, list[str], dict[int, bool]]:
    """Project full verified availability into one deterministic presentation page."""
    units: list[CustomerResponseUnit] = []
    selected_keys: list[str] = []
    has_more_by_unit: dict[int, bool] = {}
    for index, unit in enumerate(contract.units):
        truth = unit.availability_truth
        if truth is None or truth.state != "options_available":
            units.append(unit)
            continue

        windows = _window_values(unit)
        service_fact = _fact_map(unit).get("service_name")
        service_name = service_fact.value if service_fact is not None else None
        selected, keys, has_more = select_availability_window_page(
            windows,
            service_name=service_name,
            shown_keys=shown_window_keys,
        )
        facts = tuple(
            fact.model_copy(update={"value": selected})
            if fact.key == "availability_windows"
            else fact
            for fact in unit.facts
        )
        units.append(unit.model_copy(update={"facts": facts}))
        selected_keys.extend(keys)
        has_more_by_unit[index] = has_more

    return (
        contract.model_copy(update={"units": tuple(units)}),
        selected_keys,
        has_more_by_unit,
    )


def _window_time_text(
    window: dict[str, object],
    *,
    arabic: bool,
    reference_date: date | None = None,
) -> tuple[str, str]:
    start = _parse_datetime(window.get("start_local"))
    end = _parse_datetime(window.get("end_local"))
    if start is not None:
        date_label = _date_text(start.date().isoformat(), arabic=arabic, reference_date=reference_date)
        start_label = _clock(start.isoformat(), arabic=arabic)
    else:
        checked = str(window.get("date") or "")
        date_label = _date_text(checked, arabic=arabic, reference_date=reference_date) if checked else ""
        start_label = _clock(window.get("start_time_24h"), arabic=arabic)
    if end is not None:
        end_label = _clock(end.isoformat(), arabic=arabic)
        same = start is not None and start == end
    else:
        end_label = _clock(window.get("end_time_24h"), arabic=arabic)
        same = start_label == end_label

    if same or not end_label:
        time_text = (
            f"الساعة {start_label}"
            if arabic
            else f"at {start_label}"
        )
    else:
        time_text = (
            f"من {start_label} لـ{end_label}"
            if arabic
            else f"from {start_label} to {end_label}"
        )
    return date_label, time_text


def _window_label(window: dict[str, object], *, arabic: bool) -> str:
    doctor = format_doctor_name(window.get("doctor_name"), arabic=arabic)
    device = str(window.get("laser_device_name") or "").strip()
    if arabic:
        if doctor and device:
            return f"{device} مع {doctor}"
        if doctor:
            return f"مع {doctor}"
        return device
    if doctor and device:
        return f"{device} with {doctor}"
    if doctor:
        return f"with {doctor}"
    return device


def _render_window_rows(
    windows: list[dict[str, object]],
    *,
    arabic: bool,
    mode: AvailabilityPresentationMode,
    reference_date: date | None = None,
) -> list[str]:
    if mode == "detailed":
        rows: list[str] = []
        for window in windows:
            label = _window_label(window, arabic=arabic)
            day, time_text = _window_time_text(window, arabic=arabic, reference_date=reference_date)
            if arabic:
                prefix = f"{label}: " if label else ""
                day_part = f" {_arabic_day_phrase(day)}" if day else ""
                rows.append(f"{prefix}{time_text}{day_part}.")
            else:
                prefix = f"{label}: " if label else ""
                day_part = f" on {day}" if day else ""
                rows.append(f"{prefix}{time_text}{day_part}.")
        return rows

    grouped: dict[tuple[str, str], list[str]] = defaultdict(list)
    for window in windows:
        label = _window_label(window, arabic=arabic)
        day, time_text = _window_time_text(window, arabic=arabic, reference_date=reference_date)
        grouped[(label, day)].append(time_text)

    rows = []
    for (label, day), times in grouped.items():
        if arabic:
            subject = label or "المتاح"
            day_part = f" {_arabic_day_phrase(day)}" if day else ""
            rows.append(f"{subject}{day_part}: " + "، و".join(times) + ".")
        else:
            subject = label or "Available"
            day_part = f" on {day}" if day else ""
            rows.append(f"{subject}{day_part}: " + ", ".join(times) + ".")
    return rows


def _render_windows(
    unit: CustomerResponseUnit,
    *,
    arabic: bool,
    mode: AvailabilityPresentationMode,
    reference_date: date | None = None,
) -> list[str]:
    return _render_window_rows(
        _window_values(unit),
        arabic=arabic,
        mode=mode,
        reference_date=reference_date,
    )


def render_embedded_verified_availability_options(
    availability: dict[str, object],
    *,
    arabic: bool,
    reference_date: date | None = None,
) -> str | None:
    """Render one WhatsApp-sized page of already-verified windows for a price reply.

    The caller has already named the selected service/device, so this intentionally
    avoids repeating the device on every availability row. Exact date/time values
    still come only from the verified availability payload.
    """

    raw_windows = availability.get("availability_windows")
    windows = (
        [dict(item) for item in raw_windows if isinstance(item, dict)]
        if isinstance(raw_windows, list)
        else []
    )
    if not windows:
        return None

    selected, _keys, has_more = select_availability_window_page(
        windows,
        service_name=availability.get("service_name"),
    )
    grouped: dict[tuple[str, str], list[str]] = defaultdict(list)
    ordered_days: list[str] = []
    for window in selected:
        doctor = format_doctor_name(window.get("doctor_name"), arabic=arabic)
        day, time_text = _window_time_text(window, arabic=arabic, reference_date=reference_date)
        if day and day not in ordered_days:
            ordered_days.append(day)
        grouped[(doctor, day)].append(time_text)

    single_day = ordered_days[0] if len(ordered_days) == 1 else None
    if arabic:
        intro = "أقرب المواعيد المتاحة" if has_more else "المتاح"
        if single_day:
            intro += f" {_arabic_day_phrase(single_day)}"
    else:
        intro = "Nearest available times" if has_more else "Available times"
        if single_day:
            intro += f" on {single_day}"

    rows: list[str] = []
    for (doctor, day), times in grouped.items():
        if arabic:
            subject = f"مع {doctor}" if doctor else "متاح"
            day_part = f" {_arabic_day_phrase(day)}" if day and not single_day else ""
            rows.append(f"{subject}{day_part}: " + "، و".join(times) + ".")
        else:
            subject = f"with {doctor}" if doctor else "Available"
            day_part = f" on {day}" if day and not single_day else ""
            rows.append(f"{subject}{day_part}: " + ", ".join(times) + ".")

    if not rows:
        return None
    closing = "أنهي وقت أنسب لك؟" if arabic else "Which time works best for you?"
    return "\n".join([f"{intro}:", *[f"• {row}" for row in rows], closing])


def render_embedded_requested_time_unavailable(
    availability: dict[str, object],
    *,
    arabic: bool,
    reference_date: date | None = None,
) -> str:
    raw = availability.get("nearest_alternative_windows")
    alternatives = (
        [dict(item) for item in raw if isinstance(item, dict)]
        if isinstance(raw, list)
        else []
    )
    requested = availability.get("requested_time") or availability.get("requested_start_time")
    requested_text = _clock(requested, arabic=arabic) if requested else ""
    if arabic:
        first = f"الساعة {requested_text} مش متاحة للأسف." if requested_text else "الوقت اللي طلبته مش متاح للأسف."
    else:
        first = f"{requested_text} isn’t available." if requested_text else "The time you requested isn’t available."
    rows = _render_window_rows(
        alternatives,
        arabic=arabic,
        mode="compact",
        reference_date=reference_date,
    )
    if not rows:
        return first + (
            " مفيش وقت تاني متاح هنا. تحب أشوفلك أقرب يوم بعده؟"
            if arabic
            else " There isn’t another available time here. Would you like me to check the next day?"
        )
    intro = "أقرب مواعيد متاحة:" if arabic else "Nearest available times:"
    closing = "أنهي وقت أنسب لك؟" if arabic else "Which time works best for you?"
    return "\n".join([first, intro, *[f"• {row}" for row in rows], closing])


def render_embedded_no_availability(
    availability: dict[str, object],
    *,
    arabic: bool,
    reference_date: date | None = None,
) -> str:
    """Render verified date-scoped no-availability truth for an embedded price reply."""

    raw_dates = availability.get("checked_dates")
    dates = [str(value) for value in raw_dates if value] if isinstance(raw_dates, list) else []
    if len(dates) == 1:
        day = _date_text(dates[0], arabic=arabic, reference_date=reference_date)
        if arabic:
            return f"\u0645\u0641\u064a\u0634 \u0645\u0648\u0627\u0639\u064a\u062f \u0645\u062a\u0627\u062d\u0629 {_arabic_day_phrase(day)}. \u0623\u0642\u062f\u0631 \u0623\u062f\u0648\u0631\u0644\u0643 \u0641\u064a \u064a\u0648\u0645 \u062a\u0627\u0646\u064a \u0644\u0648 \u062a\u062d\u0628."
        return f"There are no available times on {day}. I can check another day if you'd like."
    if arabic:
        return "مفيش مواعيد متاحة في اليوم المطلوب. أقدر أدورلك في يوم تاني لو تحب."
    return "There is no availability on the requested day. I can check another day if you'd like."


def _optional_context(
    unit: CustomerResponseUnit,
    keys: list[str],
    *,
    arabic: bool,
) -> str:
    facts = _fact_map(unit)
    fragments: list[str] = []
    for key in keys:
        fact = facts[key]
        if key == "service_name":
            value = " ".join(str(fact.value).split())
            fragments.append(
                f"لخدمة {value}" if arabic else f"for {value}"
            )
    return "، ".join(fragments) if arabic else ", ".join(fragments)


def _render_present(
    unit: CustomerResponseUnit,
    draft: AvailabilityComposerUnitDraft,
    *,
    arabic: bool,
    has_more: bool = False,
    continuation: bool = False,
    reference_date: date | None = None,
) -> str:
    intro_by_style = (
        {
            "plain": "المواعيد المتاحة",
            "warm": "المتاح عندنا",
            "friendly": "لقيتلك المواعيد دي",
        }
        if arabic
        else {
            "plain": "Available times",
            "warm": "Here’s what’s available",
            "friendly": "I found these times for you",
        }
    )
    intro = (
        ("كمان متاح عندنا" if arabic else "More available times")
        if continuation
        else (
            ("أقرب المواعيد المتاحة" if arabic else "Nearest available times")
            if has_more
            else intro_by_style[draft.style]
        )
    )
    context = _optional_context(
        unit,
        draft.optional_fact_keys,
        arabic=arabic,
    )
    if context:
        intro = f"{intro} {context}"
    rows = _render_windows(
        unit,
        arabic=arabic,
        mode=draft.presentation_mode,
        reference_date=reference_date,
    )
    if not rows:
        if continuation:
            return (
                "مفيش مواعيد إضافية متاحة في الأيام دي."
                if arabic
                else "There are no more available times on these dates."
            )
        raise AvailabilityComposerValidationError(
            "options_available requires renderable verified windows.",
            reason="invalid_window_refs",
        )
    parts = [f"{intro}:", *[f"• {row}" for row in rows]]
    if draft.closing_action == "ask_selection":
        parts.append(
            "أنهي وقت أنسب لك؟"
            if arabic
            else "Which time works best for you?"
        )
    return "\n".join(parts)


def _finish_customer_sentence(text: str) -> str:
    cleaned = text.rstrip(" .")
    return cleaned if cleaned.endswith(("؟", "?", "!")) else cleaned + "."


def _render_requested_miss(
    unit: CustomerResponseUnit,
    draft: AvailabilityComposerUnitDraft,
    *,
    arabic: bool,
    reference_date: date | None = None,
) -> str:
    facts = _fact_map(unit)
    requested = facts.get("requested_time")
    requested_text = _clock(requested.value, arabic=arabic) if requested else ""
    scope = _date_scope_text(unit, arabic=arabic, reference_date=reference_date)
    context = _optional_context(unit, draft.optional_fact_keys, arabic=arabic)
    alternatives = _alternative_window_values(unit)

    if arabic:
        target = f"الساعة {requested_text}" if requested_text else "الوقت اللي طلبته"
        text = f"{target} مش متاحة للأسف {scope}"
        if context:
            text += f" {context}"
    else:
        target = requested_text or "The time you requested"
        text = f"{target} isn’t available {scope}"
        if context:
            text += f" {context}"

    if alternatives:
        rows = _render_window_rows(
            alternatives,
            arabic=arabic,
            mode="compact",
            reference_date=reference_date,
        )
        if rows:
            intro = "أقرب مواعيد متاحة:" if arabic else "Nearest available times:"
            closing = "أنهي وقت أنسب لك؟" if arabic else "Which time works best for you?"
            return "\n".join([text.rstrip(" .") + ".", intro, *[f"• {row}" for row in rows], closing])

    if draft.closing_action == "offer_other_time":
        text += (
            ". مفيش وقت تاني متاح هنا. تحب أشوفلك أقرب يوم بعده؟"
            if arabic
            else ". There isn’t another available time here. Would you like me to check the next day?"
        )
    return _finish_customer_sentence(text)


def _render_no_availability(
    unit: CustomerResponseUnit,
    draft: AvailabilityComposerUnitDraft,
    *,
    arabic: bool,
    reference_date: date | None = None,
) -> str:
    scope = _availability_scope_text(unit, arabic=arabic, reference_date=reference_date)
    context = _optional_context(unit, draft.optional_fact_keys, arabic=arabic)
    if arabic:
        text = f"للأسف مفيش مواعيد متاحة {scope}"
        if context:
            text += f" {context}"
        if draft.closing_action == "offer_other_scope":
            text += ". تحب أشوفلك يوم أو وقت تاني؟"
    else:
        text = f"Unfortunately, there are no available times {scope}"
        if context:
            text += f" {context}"
        if draft.closing_action == "offer_other_scope":
            text += ". Would you like me to check another day or time?"
    return _finish_customer_sentence(text)


def _render_unit(
    unit: CustomerResponseUnit,
    draft: AvailabilityComposerUnitDraft,
    *,
    arabic: bool,
    has_more: bool = False,
    continuation: bool = False,
    reference_date: date | None = None,
) -> str:
    truth = unit.availability_truth
    if truth is None:
        raise AvailabilityComposerValidationError(
            "Availability unit is missing backend truth.",
            reason="invalid_backend_truth",
        )
    if truth.state == "options_available":
        return _render_present(
            unit,
            draft,
            arabic=arabic,
            has_more=has_more,
            continuation=continuation,
            reference_date=reference_date,
        )
    if truth.state == "requested_time_unavailable":
        return _render_requested_miss(
            unit, draft, arabic=arabic, reference_date=reference_date
        )
    if truth.state == "no_availability":
        return _render_no_availability(
            unit, draft, arabic=arabic, reference_date=reference_date
        )
    raise AvailabilityComposerValidationError(
        "Unsupported availability state.", reason="invalid_backend_truth"
    )


def resolve_availability_composer_draft(
    contract: CustomerResponseContract,
    draft: AvailabilityComposerDraft,
    *,
    arabic: bool,
    has_more_by_unit: dict[int, bool] | None = None,
    continuation: bool = False,
    reference_date: date | None = None,
) -> str:
    validate_availability_composer_draft(contract, draft)
    chunks: list[str] = []
    for index, (unit, draft_unit) in enumerate(
        zip(contract.units, draft.units, strict=True)
    ):
        effective_draft = draft_unit
        truth = unit.availability_truth
        later_options_exist = any(
            later.availability_truth is not None
            and later.availability_truth.state == "options_available"
            for later in contract.units[index + 1 :]
        )
        if (
            truth is not None
            and truth.state == "requested_time_unavailable"
            and later_options_exist
            and draft_unit.closing_action == "offer_other_time"
        ):
            effective_draft = draft_unit.model_copy(
                update={"closing_action": "none"}
            )
        rendered = _render_unit(
            unit,
            effective_draft,
            arabic=arabic,
            has_more=bool((has_more_by_unit or {}).get(index)),
            continuation=continuation,
            reference_date=reference_date,
        )
        if index == 0:
            chunks.append(rendered)
            continue
        if draft_unit.transition == "and":
            prefix = "وكمان، " if arabic else "Also, "
        elif draft_unit.transition == "then":
            prefix = "وبعدها، " if arabic else "Then, "
        else:
            prefix = ""
        chunks.append(prefix + rendered)
    return "\n".join(chunks).strip()


def _fallback_closing(state: str) -> AvailabilityClosingAction:
    return {
        "options_available": "ask_selection",
        "requested_time_unavailable": "offer_other_time",
        "no_availability": "offer_other_scope",
    }[state]  # type: ignore[return-value]


def deterministic_availability_fallback(
    contract: CustomerResponseContract,
    *,
    arabic: bool,
    has_more_by_unit: dict[int, bool] | None = None,
    continuation: bool = False,
    reference_date: date | None = None,
) -> str:
    units: list[AvailabilityComposerUnitDraft] = []
    for index, unit in enumerate(contract.units):
        truth = unit.availability_truth
        if truth is None:
            raise AvailabilityComposerValidationError(
                "Availability fallback requires backend truth.",
                reason="invalid_backend_truth",
            )
        optional = [
            key
            for key in ("service_name",)
            if key in _fact_map(unit)
        ]
        units.append(
            AvailabilityComposerUnitDraft(
                unit_index=index,
                availability_ref="unit_availability",
                style="warm" if index == 0 else "plain",
                window_refs=_window_refs(index, unit),
                optional_fact_keys=optional,
                presentation_mode="compact",
                closing_action=_fallback_closing(truth.state),
                transition="sentence" if index == 0 else "and",
            )
        )
    return resolve_availability_composer_draft(
        contract,
        AvailabilityComposerDraft(units=units),
        arabic=arabic,
        has_more_by_unit=has_more_by_unit,
        continuation=continuation,
        reference_date=reference_date,
    )


def compose_availability_contract_reply(
    *,
    history: list[BaseMessage],
    contract: CustomerResponseContract,
    shown_window_keys: set[str] | frozenset[str] | None = None,
    continuation: bool = False,
    reference_date: date | None = None,
) -> tuple[str, str]:
    """Compose one deterministic page from the full verified availability truth."""
    arabic = _latest_customer_is_arabic(history)
    contract, _selected_keys, has_more_by_unit = _paged_contract(
        contract,
        shown_window_keys=shown_window_keys if continuation else None,
    )
    fallback_text = deterministic_availability_fallback(
        contract,
        arabic=arabic,
        has_more_by_unit=has_more_by_unit,
        continuation=continuation,
        reference_date=reference_date,
    )

    try:
        messages = _build_availability_composer_messages(
            history=history,
            contract=contract,
        )
        primary_name = settings.openai_model
        fallback_name = settings.openai_fallback_model
        primary = build_realtime_composer_model()
        fallback_model = None

        def invoke_structured(model) -> AvailabilityComposerPresentationDraft:
            return invoke_typed_structured_output(
                model=model,
                schema=AvailabilityComposerPresentationDraft,
                messages=messages,
            )

        def primary_call() -> AvailabilityComposerPresentationDraft:
            return invoke_structured(primary)

        def fallback_call() -> AvailabilityComposerPresentationDraft:
            nonlocal fallback_model
            if fallback_model is None:
                fallback_model = build_realtime_composer_fallback_model()
            if fallback_model is None:
                raise RuntimeError(
                    "Availability composer fallback model is not configured."
                )
            return invoke_structured(fallback_model)

        model_calls = [(primary_name, primary_call)]
        if fallback_name and fallback_name != primary_name:
            model_calls.append((fallback_name, fallback_call))

        invocation = invoke_with_model_chain(
            model_calls=model_calls,
            operation="v2-availability-contract-composer",
            circuit_breaker_cooldown_seconds=(
                settings.llm_realtime_circuit_breaker_cooldown_seconds
            ),
        )
        resolved_draft = _materialize_availability_composer_draft(
            contract, invocation.value
        )
        text = resolve_availability_composer_draft(
            contract,
            resolved_draft,
            arabic=arabic,
            has_more_by_unit=has_more_by_unit,
            continuation=continuation,
            reference_date=reference_date,
        )
        return text, f"availability-contract:{model_label(invocation.model_name)}"
    except (
        LLMProviderError,
        StructuredOutputError,
        AvailabilityComposerValidationError,
        RuntimeError,
    ) as exc:
        validation_reason = (
            exc.reason
            if isinstance(exc, AvailabilityComposerValidationError)
            else type(exc).__name__
        )
        logger.warning(
            "Availability contract composer used deterministic fallback reason=%s validation_reason=%s",
            type(exc).__name__,
            validation_reason,
        )
        return fallback_text, "deterministic:availability-contract-fallback"
