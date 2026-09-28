from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import date, datetime
from typing import Literal

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from pydantic import BaseModel, ConfigDict

from app.agents.llm_runtime import LLMProviderError, invoke_with_model_chain
from app.agents.model_provider import (
    build_realtime_composer_fallback_model,
    build_realtime_composer_model,
    model_label,
)
from app.agents.structured_output import StructuredOutputError, invoke_typed_structured_output
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


class AvailabilityComposerValidationError(RuntimeError):
    pass


_OPTIONAL_FACT_KEYS = frozenset({"service_name"})

_ALLOWED_CLOSING_BY_STATE: dict[str, frozenset[str]] = {
    "options_available": frozenset({"ask_selection", "none"}),
    "requested_time_unavailable": frozenset({"offer_other_time", "none"}),
    "no_availability": frozenset({"offer_other_scope", "none"}),
}

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
                "verified_window_refs": refs,
                "window_count": len(refs),
                "available_optional_fact_keys": [
                    fact.key
                    for fact in unit.facts
                    if fact.key in _OPTIONAL_FACT_KEYS
                ],
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
            "Availability composer requires a customer message."
        )
    arabic = _latest_customer_is_arabic(history)
    language = "Egyptian Arabic" if arabic else "English"
    system = SystemMessage(
        content=(
            "You select presentation structure for verified clinic availability. "
            "You never write customer prose and you never return any slot/date/time/"
            "doctor/device value. The backend owns availability state and every exact value.\n"
            "Return one draft unit per CONTRACT unit in identical order. "
            "availability_ref must be unit_availability. For options_available, "
            "window_refs must contain every verified_window_ref exactly once and in the "
            "same order. For other states window_refs must be empty. Choose only optional "
            "fact keys listed for the same unit. Keep the presentation concise and natural. "
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


def validate_availability_composer_draft(
    contract: CustomerResponseContract,
    draft: AvailabilityComposerDraft,
) -> None:
    if len(draft.units) != len(contract.units):
        raise AvailabilityComposerValidationError(
            "Availability composer must represent every contract unit exactly once."
        )

    for expected_index, (unit, draft_unit) in enumerate(
        zip(contract.units, draft.units, strict=True)
    ):
        if draft_unit.unit_index != expected_index:
            raise AvailabilityComposerValidationError(
                "Availability composer changed compound unit ordering."
            )
        truth = unit.availability_truth
        expected_state = AVAILABILITY_STATE_BY_GOAL.get(unit.response_goal)
        if truth is None or expected_state is None or truth.state != expected_state:
            raise AvailabilityComposerValidationError(
                "Availability composer received mismatched backend availability truth."
            )

        expected_refs = _window_refs(expected_index, unit)
        if len(draft_unit.window_refs) != len(set(draft_unit.window_refs)):
            raise AvailabilityComposerValidationError(
                "Availability composer repeated a window reference."
            )
        if truth.state == "options_available":
            fact = _window_fact(unit)
            if fact is None or fact.complete_set is not True:
                raise AvailabilityComposerValidationError(
                    "Available options require a complete verified window set."
                )
            if draft_unit.window_refs != expected_refs:
                raise AvailabilityComposerValidationError(
                    "Availability composer must preserve the complete verified window set."
                )
        elif draft_unit.window_refs:
            raise AvailabilityComposerValidationError(
                "Unavailable states cannot reference availability windows."
            )

        if len(draft_unit.optional_fact_keys) != len(set(draft_unit.optional_fact_keys)):
            raise AvailabilityComposerValidationError(
                "Availability composer repeated an optional fact reference."
            )
        available_optional = {
            fact.key for fact in unit.facts if fact.key in _OPTIONAL_FACT_KEYS
        }
        if any(key not in available_optional for key in draft_unit.optional_fact_keys):
            raise AvailabilityComposerValidationError(
                "Availability composer referenced an unavailable optional fact."
            )

        allowed_closings = _ALLOWED_CLOSING_BY_STATE[truth.state]
        if draft_unit.closing_action not in allowed_closings:
            raise AvailabilityComposerValidationError(
                "Availability composer selected an invalid closing action for this state."
            )
        if expected_index == 0 and draft_unit.transition != "sentence":
            raise AvailabilityComposerValidationError(
                "The first availability unit must start a sentence."
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


def _date_text(value: object, *, arabic: bool) -> str:
    if isinstance(value, str):
        raw = value[:10]
    else:
        raw = str(value)[:10]
    try:
        parsed = date.fromisoformat(raw)
    except ValueError:
        return str(value)
    if arabic:
        return f"{parsed.day} {_AR_MONTHS[parsed.month]} {parsed.year}"
    return f"{parsed.strftime('%B')} {parsed.day}, {parsed.year}"


def _checked_dates(unit: CustomerResponseUnit) -> list[str]:
    fact = _fact_map(unit).get("checked_dates")
    if fact is None or not isinstance(fact.value, list):
        return []
    return [str(value) for value in fact.value if value]


def _date_scope_text(unit: CustomerResponseUnit, *, arabic: bool) -> str:
    raw_dates = _checked_dates(unit)
    if not raw_dates:
        return "النطاق اللي اتفحص" if arabic else "the checked search scope"
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
        first = _date_text(parsed[0].isoformat(), arabic=arabic)
        last = _date_text(parsed[-1].isoformat(), arabic=arabic)
        return (
            f"من {first} لحد {last}"
            if arabic
            else f"from {first} through {last}"
        )
    labels = [_date_text(raw, arabic=arabic) for raw in raw_dates]
    if len(labels) == 1:
        return (f"يوم {labels[0]}" if arabic else f"on {labels[0]}")
    joined = "، ".join(labels)
    return (
        f"في الأيام اللي اتفحصت: {joined}"
        if arabic
        else f"on the checked dates: {joined}"
    )


def _window_values(unit: CustomerResponseUnit) -> list[dict[str, object]]:
    fact = _window_fact(unit)
    if fact is None:
        return []
    return [dict(value) for value in fact.value if isinstance(value, dict)]


def _window_time_text(window: dict[str, object], *, arabic: bool) -> tuple[str, str]:
    start = _parse_datetime(window.get("start_local"))
    end = _parse_datetime(window.get("end_local"))
    if start is not None:
        date_label = _date_text(start.date().isoformat(), arabic=arabic)
        start_label = _clock(start.isoformat(), arabic=arabic)
    else:
        checked = str(window.get("date") or "")
        date_label = _date_text(checked, arabic=arabic) if checked else ""
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
            f"بدايات حجز من {start_label} لـ{end_label}"
            if arabic
            else f"bookable starts from {start_label} to {end_label}"
        )
    return date_label, time_text


def _window_label(window: dict[str, object], *, arabic: bool) -> str:
    doctor = str(window.get("doctor_name") or "").strip()
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


def _render_windows(
    unit: CustomerResponseUnit,
    *,
    arabic: bool,
    mode: AvailabilityPresentationMode,
) -> list[str]:
    windows = _window_values(unit)
    if mode == "detailed":
        rows: list[str] = []
        for window in windows:
            label = _window_label(window, arabic=arabic)
            day, time_text = _window_time_text(window, arabic=arabic)
            if arabic:
                prefix = f"{label}: " if label else ""
                day_part = f" يوم {day}" if day else ""
                rows.append(f"{prefix}{time_text}{day_part}.")
            else:
                prefix = f"{label}: " if label else ""
                day_part = f" on {day}" if day else ""
                rows.append(f"{prefix}{time_text}{day_part}.")
        return rows

    grouped: dict[tuple[str, str], list[str]] = defaultdict(list)
    for window in windows:
        label = _window_label(window, arabic=arabic)
        day, time_text = _window_time_text(window, arabic=arabic)
        grouped[(label, day)].append(time_text)

    rows = []
    for (label, day), times in grouped.items():
        if arabic:
            subject = label or "المتاح"
            day_part = f" يوم {day}" if day else ""
            rows.append(f"{subject}{day_part}: " + "، و".join(times) + ".")
        else:
            subject = label or "Available"
            day_part = f" on {day}" if day else ""
            rows.append(f"{subject}{day_part}: " + ", ".join(times) + ".")
    return rows


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
    intro = intro_by_style[draft.style]
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
    )
    if not rows:
        raise AvailabilityComposerValidationError(
            "options_available requires renderable verified windows."
        )
    parts = [f"{intro}:", *rows]
    if draft.closing_action == "ask_selection":
        parts.append(
            "أنهي وقت أنسب لك؟"
            if arabic
            else "Which time works best for you?"
        )
    return "\n".join(parts)


def _render_requested_miss(
    unit: CustomerResponseUnit,
    draft: AvailabilityComposerUnitDraft,
    *,
    arabic: bool,
) -> str:
    facts = _fact_map(unit)
    requested = facts.get("requested_time")
    requested_text = _clock(requested.value, arabic=arabic) if requested else ""
    scope = _date_scope_text(unit, arabic=arabic)
    context = _optional_context(unit, draft.optional_fact_keys, arabic=arabic)
    if arabic:
        target = f"ميعاد الساعة {requested_text}" if requested_text else "الوقت المطلوب"
        text = f"{target} مش متاح {scope}"
        if context:
            text += f" {context}"
        if draft.closing_action == "offer_other_time":
            text += ". أقدر أشوفلك وقت تاني في نفس النطاق"
    else:
        target = f"{requested_text}" if requested_text else "The requested time"
        text = f"{target} isn’t available {scope}"
        if context:
            text += f" {context}"
        if draft.closing_action == "offer_other_time":
            text += ". I can check another time in the same scope"
    return text.rstrip(" .") + "."


def _render_no_availability(
    unit: CustomerResponseUnit,
    draft: AvailabilityComposerUnitDraft,
    *,
    arabic: bool,
) -> str:
    scope = _date_scope_text(unit, arabic=arabic)
    context = _optional_context(unit, draft.optional_fact_keys, arabic=arabic)
    truncated_fact = _fact_map(unit).get("search_truncated")
    truncated = truncated_fact is not None and truncated_fact.value is True
    if arabic:
        text = f"مفيش مواعيد متاحة {scope}"
        if context:
            text += f" {context}"
        if truncated:
            text += "، وده بس نطاق البحث اللي اتفحص"
        if draft.closing_action == "offer_other_scope":
            text += ". أقدر أدورلك في نطاق تاني لو تحب"
    else:
        text = f"There are no available times {scope}"
        if context:
            text += f" {context}"
        if truncated:
            text += ", and that is only the scope that was checked"
        if draft.closing_action == "offer_other_scope":
            text += ". I can check another date range if you’d like"
    return text.rstrip(" .") + "."


def _render_unit(
    unit: CustomerResponseUnit,
    draft: AvailabilityComposerUnitDraft,
    *,
    arabic: bool,
) -> str:
    truth = unit.availability_truth
    if truth is None:
        raise AvailabilityComposerValidationError(
            "Availability unit is missing backend truth."
        )
    if truth.state == "options_available":
        return _render_present(unit, draft, arabic=arabic)
    if truth.state == "requested_time_unavailable":
        return _render_requested_miss(unit, draft, arabic=arabic)
    if truth.state == "no_availability":
        return _render_no_availability(unit, draft, arabic=arabic)
    raise AvailabilityComposerValidationError("Unsupported availability state.")


def resolve_availability_composer_draft(
    contract: CustomerResponseContract,
    draft: AvailabilityComposerDraft,
    *,
    arabic: bool,
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
        rendered = _render_unit(unit, effective_draft, arabic=arabic)
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
) -> str:
    units: list[AvailabilityComposerUnitDraft] = []
    for index, unit in enumerate(contract.units):
        truth = unit.availability_truth
        if truth is None:
            raise AvailabilityComposerValidationError(
                "Availability fallback requires backend truth."
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
    )


def compose_availability_contract_reply(
    *,
    history: list[BaseMessage],
    contract: CustomerResponseContract,
) -> tuple[str, str]:
    """Compose pure availability outcomes without model-authored availability facts."""
    arabic = _latest_customer_is_arabic(history)
    fallback_text = deterministic_availability_fallback(
        contract,
        arabic=arabic,
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

        def invoke_structured(model) -> AvailabilityComposerDraft:
            return invoke_typed_structured_output(
                model=model,
                schema=AvailabilityComposerDraft,
                messages=messages,
            )

        def primary_call() -> AvailabilityComposerDraft:
            return invoke_structured(primary)

        def fallback_call() -> AvailabilityComposerDraft:
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
        text = resolve_availability_composer_draft(
            contract,
            invocation.value,
            arabic=arabic,
        )
        return text, f"availability-contract:{model_label(invocation.model_name)}"
    except (
        LLMProviderError,
        StructuredOutputError,
        AvailabilityComposerValidationError,
        RuntimeError,
    ) as exc:
        logger.warning(
            "Availability contract composer used deterministic fallback reason=%s",
            type(exc).__name__,
        )
        return fallback_text, "deterministic:availability-contract-fallback"
