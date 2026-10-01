from __future__ import annotations

import json
import logging
from datetime import date, datetime
from typing import Literal
from zoneinfo import ZoneInfo

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
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
    TERMINAL_ACTION_BY_GOAL,
    CustomerResponseContract,
    CustomerResponseUnit,
    ResponseFact,
)

logger = logging.getLogger(__name__)

ComposerStyle = Literal["plain", "warm", "friendly"]
ComposerTransition = Literal["sentence", "and", "then"]
ActionReference = Literal["unit_action"]


class StrictTerminalComposerModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TerminalComposerUnitDraft(StrictTerminalComposerModel):
    unit_index: int
    action_ref: ActionReference
    style: ComposerStyle
    fact_keys: list[str]
    transition: ComposerTransition


class TerminalComposerDraft(StrictTerminalComposerModel):
    units: list[TerminalComposerUnitDraft]


class TerminalComposerValidationError(RuntimeError):
    pass


_RENDERABLE_FACT_KEYS = frozenset(
    {
        "service_name",
        "doctor_name",
        "device_name",
        "start_local",
        "date",
        "time",
        "package_used",
        "package_name",
        "pulses_remaining",
        "pulses_count",
        "follow_up_at",
        "marketing_consent",
    }
)

_DEFAULT_FACT_KEYS_BY_GOAL: dict[str, tuple[str, ...]] = {
    "booking_completed": (
        "service_name",
        "start_local",
        "date",
        "time",
        "doctor_name",
        "device_name",
        "package_used",
        "package_name",
    ),
    "reschedule_completed": (
        "service_name",
        "start_local",
        "date",
        "time",
        "doctor_name",
        "device_name",
    ),
    "cancellation_completed": (
        "service_name",
        "start_local",
        "date",
        "time",
    ),
    "appointment_confirmed": (
        "service_name",
        "start_local",
        "date",
        "time",
        "doctor_name",
    ),
    "package_purchased": ("package_name",),
    "pulse_pack_purchased": ("pulses_count", "device_name"),
    "follow_up_created": ("follow_up_at",),
    "marketing_updated": ("marketing_consent",),
}

_ACTION_PHRASES_AR: dict[str, dict[ComposerStyle, str]] = {
    "booking_completed": {
        "plain": "تم تأكيد الحجز",
        "warm": "حجزك اتأكد",
        "friendly": "ثبتنالك الحجز",
    },
    "reschedule_completed": {
        "plain": "تم تغيير الموعد",
        "warm": "ميعادك اتغيّر",
        "friendly": "عدلنالك الميعاد",
    },
    "cancellation_completed": {
        "plain": "تم إلغاء الموعد",
        "warm": "ميعادك اتلغى",
        "friendly": "لغينالك الميعاد",
    },
    "appointment_confirmed": {
        "plain": "تم تأكيد الموعد",
        "warm": "ميعادك اتأكد",
        "friendly": "أكدنالك الميعاد",
    },
    "package_purchased": {
        "plain": "تمت إضافة الباكدج لحسابك",
        "warm": "الباكدج اتضافت لحسابك",
        "friendly": "ضفنالك الباكدج على حسابك",
    },
    "pulse_pack_purchased": {
        "plain": "تمت إضافة باقة الـPulses لحسابك",
        "warm": "باقة الـPulses اتضافت لحسابك",
        "friendly": "ضفنالك باقة الـPulses على حسابك",
    },
    "follow_up_created": {
        "plain": "تم تسجيل المتابعة",
        "warm": "المتابعة اتسجلت",
        "friendly": "سجلنالك المتابعة",
    },
    "marketing_updated": {
        "plain": "تم تحديث تفضيلات التواصل التسويقي",
        "warm": "تفضيلات التواصل التسويقي اتحدثت",
        "friendly": "حدثنالك تفضيلات التواصل التسويقي",
    },
}

_ACTION_PHRASES_EN: dict[str, dict[ComposerStyle, str]] = {
    "booking_completed": {
        "plain": "Your booking is confirmed",
        "warm": "Your booking is all set",
        "friendly": "I’ve confirmed your booking",
    },
    "reschedule_completed": {
        "plain": "Your appointment has been rescheduled",
        "warm": "Your appointment has been moved",
        "friendly": "I’ve updated your appointment time",
    },
    "cancellation_completed": {
        "plain": "Your appointment has been cancelled",
        "warm": "Your appointment is cancelled",
        "friendly": "I’ve cancelled your appointment",
    },
    "appointment_confirmed": {
        "plain": "Your appointment is confirmed",
        "warm": "Your appointment is all confirmed",
        "friendly": "I’ve confirmed your appointment",
    },
    "package_purchased": {
        "plain": "The package has been added to your account",
        "warm": "Your package is now on your account",
        "friendly": "I’ve added the package to your account",
    },
    "pulse_pack_purchased": {
        "plain": "The Pulse pack has been added to your account",
        "warm": "Your Pulse pack is now on your account",
        "friendly": "I’ve added the Pulse pack to your account",
    },
    "follow_up_created": {
        "plain": "The follow-up has been scheduled",
        "warm": "Your follow-up is set",
        "friendly": "I’ve scheduled the follow-up",
    },
    "marketing_updated": {
        "plain": "Your marketing preferences have been updated",
        "warm": "Your marketing preferences are updated",
        "friendly": "I’ve updated your marketing preferences",
    },
}

_ARABIC_MONTHS = {
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

_STATUS_AR = {
    "confirmed": "مؤكد",
    "cancelled": "ملغي",
    "canceled": "ملغي",
    "active": "نشط",
    "completed": "مكتمل",
    "pending": "قيد الانتظار",
}
_STATUS_EN = {
    "confirmed": "confirmed",
    "cancelled": "cancelled",
    "canceled": "cancelled",
    "active": "active",
    "completed": "completed",
    "pending": "pending",
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
    latest_index = _latest_customer_index(history)
    latest_text = _message_text(history[latest_index]) if latest_index is not None else ""
    return any("\u0600" <= char <= "\u06ff" for char in latest_text)


def _recent_messages(
    history: list[BaseMessage],
    *,
    latest_customer_index: int,
    limit: int = 6,
) -> list[BaseMessage]:
    result: list[BaseMessage] = []
    for message in history[:latest_customer_index]:
        if not isinstance(message, (HumanMessage, AIMessage)):
            continue
        text = _message_text(message, limit=700)
        if not text:
            continue
        result.append(
            HumanMessage(content=text)
            if isinstance(message, HumanMessage)
            else AIMessage(content=text)
        )
    return result[-limit:]


def _composer_contract_view(contract: CustomerResponseContract) -> list[dict[str, object]]:
    units: list[dict[str, object]] = []
    for index, unit in enumerate(contract.units):
        action = unit.action_truth
        units.append(
            {
                "unit_index": index,
                "response_goal": unit.response_goal,
                "action_truth": (
                    {
                        "action": action.action,
                        "succeeded": action.succeeded,
                    }
                    if action is not None
                    else None
                ),
                "available_facts": [
                    {
                        "key": fact.key,
                        "semantic_type": fact.semantic_type,
                        "requirement": fact.requirement,
                    }
                    for fact in unit.facts
                    if fact.key in _RENDERABLE_FACT_KEYS
                ],
            }
        )
    return units


def _build_terminal_composer_messages(
    *,
    history: list[BaseMessage],
    contract: CustomerResponseContract,
) -> list[BaseMessage]:
    latest_index = _latest_customer_index(history)
    if latest_index is None:
        raise TerminalComposerValidationError(
            "Terminal composer requires a customer message."
        )

    arabic = _latest_customer_is_arabic(history)
    language = "Egyptian Arabic" if arabic else "English"
    contract_view = _composer_contract_view(contract)
    system = SystemMessage(
        content=(
            "You are selecting presentation structure for completed clinic actions. "
            "You do not write customer prose and you never return fact values. "
            "The backend owns every action phrase and exact fact value.\n"
            "Return exactly one draft unit for every CONTRACT unit, in the same order. "
            "unit_index must match its CONTRACT position and action_ref must be unit_action. "
            "Choose a natural style, choose only fact_keys listed for that same unit, "
            "include every fact marked required, and avoid redundant date/time keys when "
            "start_local is available. Use transition=sentence for the first unit; later "
            "units may use sentence, and, or then. Keep the result concise.\n"
            f"Customer reply language: {language}."
        )
    )
    contract_message = SystemMessage(
        content=(
            "TERMINAL_RESPONSE_CONTRACT_STRUCTURE (no fact values):\n"
            + json.dumps(
                contract_view,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
    )
    latest_text = _message_text(history[latest_index])
    return [
        system,
        *_recent_messages(history, latest_customer_index=latest_index),
        contract_message,
        HumanMessage(content=latest_text),
    ]


def _fact_map(unit: CustomerResponseUnit) -> dict[str, ResponseFact]:
    return {
        fact.key: fact
        for fact in unit.facts
        if fact.key in _RENDERABLE_FACT_KEYS
    }


def validate_terminal_composer_draft(
    contract: CustomerResponseContract,
    draft: TerminalComposerDraft,
) -> None:
    if len(draft.units) != len(contract.units):
        raise TerminalComposerValidationError(
            "Terminal composer must represent every contract unit exactly once."
        )

    for expected_index, (unit, draft_unit) in enumerate(
        zip(contract.units, draft.units, strict=True)
    ):
        if draft_unit.unit_index != expected_index:
            raise TerminalComposerValidationError(
                "Terminal composer changed compound unit ordering."
            )
        if unit.action_truth is None or unit.action_truth.succeeded is not True:
            raise TerminalComposerValidationError(
                "Terminal composer received a unit without successful action truth."
            )
        expected_action = TERMINAL_ACTION_BY_GOAL.get(unit.response_goal)
        if expected_action is None or unit.action_truth.action != expected_action:
            raise TerminalComposerValidationError(
                "Terminal composer received mismatched action identity."
            )

        available = _fact_map(unit)
        if len(draft_unit.fact_keys) != len(set(draft_unit.fact_keys)):
            raise TerminalComposerValidationError(
                "Terminal composer repeated a fact reference."
            )
        unsupported = [
            key for key in draft_unit.fact_keys if key not in available
        ]
        if unsupported:
            raise TerminalComposerValidationError(
                "Terminal composer referenced an unavailable fact."
            )

        required = {
            key
            for key, fact in available.items()
            if fact.requirement == "required"
        }
        if not required.issubset(set(draft_unit.fact_keys)):
            raise TerminalComposerValidationError(
                "Terminal composer omitted a required response fact."
            )

        if expected_index == 0 and draft_unit.transition != "sentence":
            raise TerminalComposerValidationError(
                "The first terminal unit must start a sentence."
            )


def _extract_date(value: object) -> date | None:
    if isinstance(value, dict):
        raw = value.get("start_date") or value.get("date")
    else:
        raw = value
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return date.fromisoformat(raw.strip()[:10])
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
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        return parsed.hour, parsed.minute
    pieces = text.split(":")
    if len(pieces) < 2:
        return None
    try:
        hour = int(pieces[0])
        minute = int(pieces[1])
    except ValueError:
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour, minute


def _format_date(value: object, *, arabic: bool) -> str:
    parsed = _extract_date(value)
    if parsed is None:
        return str(value)
    if arabic:
        return f"{parsed.day} {_ARABIC_MONTHS[parsed.month]} {parsed.year}"
    return f"{parsed.strftime('%B')} {parsed.day}, {parsed.year}"


def _format_clock(value: object, *, arabic: bool) -> str:
    parsed = _extract_time(value)
    if parsed is None:
        return str(value)
    hour, minute = parsed
    display_hour = hour % 12 or 12
    minute_text = f":{minute:02d}" if minute else ""
    if arabic:
        if hour < 12:
            period = "صباحًا"
        elif hour == 12:
            period = "ظهرًا"
        else:
            period = "مساءً"
        return f"{display_hour}{minute_text} {period}"
    period = "AM" if hour < 12 else "PM"
    return f"{display_hour}{minute_text} {period}"


def format_customer_datetime(
    value: object,
    *,
    arabic: bool,
    timezone_name: str | None = None,
) -> str:
    """Format a verified datetime for customer presentation without changing its identity."""
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.strip())
        except ValueError:
            parsed = None
        if parsed is not None:
            if timezone_name and parsed.tzinfo is not None:
                parsed = parsed.astimezone(ZoneInfo(timezone_name))
            if arabic:
                day = f"{parsed.day} {_ARABIC_MONTHS[parsed.month]} {parsed.year}"
                clock = _format_clock(
                    f"{parsed.hour:02d}:{parsed.minute:02d}",
                    arabic=True,
                )
                return f"{day} الساعة {clock}"
            day = f"{parsed.strftime('%B')} {parsed.day}, {parsed.year}"
            clock = _format_clock(
                f"{parsed.hour:02d}:{parsed.minute:02d}",
                arabic=False,
            )
            return f"{day} at {clock}"
    return str(value)


def _format_datetime(value: object, *, arabic: bool) -> str:
    return format_customer_datetime(value, arabic=arabic)


def _clean_scalar(value: object) -> str:
    return " ".join(str(value).strip().split())


def _render_fact(fact: ResponseFact, *, arabic: bool) -> str | None:
    key = fact.key
    value = fact.value

    if key == "service_name":
        text = _clean_scalar(value)
        return f"لخدمة {text}" if arabic else f"for {text}"
    if key == "doctor_name":
        text = _clean_scalar(value)
        return f"مع {text}" if arabic else f"with {text}"
    if key == "device_name":
        text = _clean_scalar(value)
        return f"على {text}" if arabic else f"on {text}"
    if key == "start_local":
        text = _format_datetime(value, arabic=arabic)
        return f"يوم {text}" if arabic else f"on {text}"
    if key == "date":
        text = _format_date(value, arabic=arabic)
        return f"يوم {text}" if arabic else f"on {text}"
    if key == "time":
        text = _format_clock(value, arabic=arabic)
        return f"الساعة {text}" if arabic else f"at {text}"
    if key == "status":
        raw = _clean_scalar(value).casefold()
        text = (_STATUS_AR if arabic else _STATUS_EN).get(raw, _clean_scalar(value))
        return f"بحالة {text}" if arabic else f"with status {text}"
    if key == "package_used":
        if value is not True:
            return None
        return "باستخدام الباكدج الحالية" if arabic else "using your existing package"
    if key == "package_name":
        text = _clean_scalar(value)
        return f"باكدج {text}" if arabic else f"package {text}"
    if key == "pulses_remaining":
        text = _clean_scalar(value)
        return f"المتبقي {text} Pulse" if arabic else f"{text} Pulses remaining"
    if key == "pulses_count":
        text = _clean_scalar(value)
        return f"بعدد {text} Pulse" if arabic else f"with {text} Pulses"
    if key == "follow_up_at":
        text = _format_datetime(value, arabic=arabic)
        return f"يوم {text}" if arabic else f"for {text}"
    if key == "marketing_consent":
        enabled = value is True
        if arabic:
            return (
                "مع تفعيل التواصل التسويقي"
                if enabled
                else "مع إيقاف التواصل التسويقي"
            )
        return (
            "with marketing messages enabled"
            if enabled
            else "with marketing messages disabled"
        )
    raise TerminalComposerValidationError(
        "Terminal composer attempted to render an unsupported fact."
    )


def _deduplicate_time_facts(keys: list[str]) -> list[str]:
    if "start_local" not in keys:
        return keys
    return [key for key in keys if key not in {"date", "time"}]


def _action_phrase(
    unit: CustomerResponseUnit,
    *,
    style: ComposerStyle,
    arabic: bool,
    first: bool,
) -> str:
    phrases = _ACTION_PHRASES_AR if arabic else _ACTION_PHRASES_EN
    try:
        phrase = phrases[unit.response_goal][style]
    except KeyError as exc:
        raise TerminalComposerValidationError(
            "Unsupported terminal action phrase."
        ) from exc
    if first and style == "warm":
        return f"تمام، {phrase}"
    if first and style == "friendly":
        return f"تمام جدًا، {phrase}" if arabic else f"Great, {phrase}"
    return phrase


def _render_unit(
    unit: CustomerResponseUnit,
    draft: TerminalComposerUnitDraft,
    *,
    arabic: bool,
    first: bool,
) -> str:
    action_text = _action_phrase(
        unit,
        style=draft.style,
        arabic=arabic,
        first=first,
    )
    facts = _fact_map(unit)
    selected = _deduplicate_time_facts(draft.fact_keys)
    rendered = [
        fragment
        for key in selected
        if (fragment := _render_fact(facts[key], arabic=arabic))
    ]
    if not rendered:
        return action_text
    separator = "، " if arabic else " "
    return action_text + separator + separator.join(rendered)


def resolve_terminal_composer_draft(
    contract: CustomerResponseContract,
    draft: TerminalComposerDraft,
    *,
    arabic: bool,
) -> str:
    validate_terminal_composer_draft(contract, draft)

    text = ""
    for index, (unit, draft_unit) in enumerate(
        zip(contract.units, draft.units, strict=True)
    ):
        unit_text = _render_unit(
            unit,
            draft_unit,
            arabic=arabic,
            first=index == 0,
        ).strip()
        if index == 0:
            text = unit_text
            continue

        if draft_unit.transition == "and":
            joiner = "، وكمان " if arabic else ", and "
        elif draft_unit.transition == "then":
            joiner = "، وبعدها " if arabic else ", then "
        else:
            joiner = ". "
        if not arabic and draft_unit.transition != "sentence" and unit_text:
            unit_text = unit_text[0].lower() + unit_text[1:]
        text += joiner + unit_text

    return text.rstrip(" .") + "."


def _fallback_fact_keys(unit: CustomerResponseUnit) -> list[str]:
    available = _fact_map(unit)
    preferred = _DEFAULT_FACT_KEYS_BY_GOAL.get(unit.response_goal, ())
    selected = [key for key in preferred if key in available]
    required = [
        key
        for key, fact in available.items()
        if fact.requirement == "required" and key not in selected
    ]
    return _deduplicate_time_facts([*selected, *required])


def deterministic_terminal_fallback(
    contract: CustomerResponseContract,
    *,
    arabic: bool,
) -> str:
    draft_units = [
        TerminalComposerUnitDraft(
            unit_index=index,
            action_ref="unit_action",
            style="warm" if index == 0 else "plain",
            fact_keys=_fallback_fact_keys(unit),
            transition="sentence" if index == 0 else "and",
        )
        for index, unit in enumerate(contract.units)
    ]
    draft = TerminalComposerDraft(units=draft_units)
    return resolve_terminal_composer_draft(
        contract,
        draft,
        arabic=arabic,
    )


def compose_terminal_contract_reply(
    *,
    history: list[BaseMessage],
    contract: CustomerResponseContract,
) -> tuple[str, str]:
    """Compose supported terminal actions without allowing model-authored facts."""
    arabic = _latest_customer_is_arabic(history)
    fallback_text = deterministic_terminal_fallback(
        contract,
        arabic=arabic,
    )

    try:
        messages = _build_terminal_composer_messages(
            history=history,
            contract=contract,
        )
        primary_name = settings.openai_model
        fallback_name = settings.openai_fallback_model
        primary = build_realtime_composer_model()
        fallback_model = None

        def invoke_structured(model) -> TerminalComposerDraft:
            return invoke_typed_structured_output(
                model=model,
                schema=TerminalComposerDraft,
                messages=messages,
            )

        def primary_call() -> TerminalComposerDraft:
            return invoke_structured(primary)

        def fallback_call() -> TerminalComposerDraft:
            nonlocal fallback_model
            if fallback_model is None:
                fallback_model = build_realtime_composer_fallback_model()
            if fallback_model is None:
                raise RuntimeError(
                    "Terminal composer fallback model is not configured."
                )
            return invoke_structured(fallback_model)

        model_calls = [(primary_name, primary_call)]
        if fallback_name and fallback_name != primary_name:
            model_calls.append((fallback_name, fallback_call))

        invocation = invoke_with_model_chain(
            model_calls=model_calls,
            operation="v2-terminal-contract-composer",
            circuit_breaker_cooldown_seconds=(
                settings.llm_realtime_circuit_breaker_cooldown_seconds
            ),
        )
        text = resolve_terminal_composer_draft(
            contract,
            invocation.value,
            arabic=arabic,
        )
        return (
            text,
            f"contract-composer:{model_label(invocation.model_name)}",
        )
    except (
        LLMProviderError,
        StructuredOutputError,
        TerminalComposerValidationError,
        RuntimeError,
    ) as exc:
        logger.warning(
            "Terminal contract composer used deterministic fallback reason=%s",
            type(exc).__name__,
        )
        return fallback_text, "deterministic:terminal-contract-fallback"
