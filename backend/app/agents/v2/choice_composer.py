from __future__ import annotations

from langchain_core.messages import BaseMessage, HumanMessage

from app.agents.v2.terminal_composer import format_customer_datetime
from app.services.agent_v2.outcome import TurnOutcome
from app.services.agent_v2.response_contract import (
    CustomerResponseContract,
    CustomerResponseUnit,
)

_SUPPORTED_CHOICE_GOALS = frozenset(
    {
        "ask_service_choice",
        "ask_device_choice",
        "ask_time_choice",
        "ask_appointment_choice",
        "ask_package_choice",
    }
)

_INTRO_AR = {
    "ask_service_choice": "تقصد أنهي خدمة من دول؟",
    "ask_device_choice": "تقصد أنهي جهاز من دول؟",
    "ask_time_choice": "أنهي ميعاد تقصد؟",
    "ask_appointment_choice": "تقصد أنهي موعد؟",
    "ask_package_choice": "تقصد أنهي باكدج؟",
}
_INTRO_EN = {
    "ask_service_choice": "Which service do you mean?",
    "ask_device_choice": "Which device do you mean?",
    "ask_time_choice": "Which time do you mean?",
    "ask_appointment_choice": "Which appointment do you mean?",
    "ask_package_choice": "Which package do you mean?",
}


class VerifiedChoicePresentationError(RuntimeError):
    pass


def _message_text(message: BaseMessage, *, limit: int = 1000) -> str:
    if not isinstance(message.content, str) or not message.content.strip():
        return ""
    text = " ".join(message.content.strip().split())
    return text[:limit] + ("…" if len(text) > limit else "")


def latest_customer_is_arabic(history: list[BaseMessage]) -> bool:
    for message in reversed(history):
        if not isinstance(message, HumanMessage):
            continue
        text = _message_text(message)
        if text:
            return any("\u0600" <= char <= "\u06ff" for char in text)
    return False


def _fact_keys(unit: CustomerResponseUnit) -> set[str]:
    return {fact.key for fact in unit.facts}


def _choice_fact_keys(unit: CustomerResponseUnit) -> set[str]:
    return {
        fact.key
        for choice in unit.choices
        for fact in choice.facts
    }


def is_refund_package_choice(unit: CustomerResponseUnit) -> bool:
    """Keep refund-specific package choice presentation inside the Financial/Refund boundary."""
    if unit.response_goal != "ask_package_choice":
        return False
    if "available_quote_count" in _fact_keys(unit):
        return True
    return bool(
        _choice_fact_keys(unit)
        & {
            "refund_amount",
            "refund_amount_minor",
            "refundable_amount",
            "refundable_amount_minor",
        }
    )


def _effective_choice_goal(unit: CustomerResponseUnit) -> str | None:
    if unit.response_goal in _SUPPORTED_CHOICE_GOALS:
        return unit.response_goal
    if unit.response_goal != "clarification" or unit.commercial_truth is not None:
        return None

    needed = next(
        (
            str(fact.value)
            for fact in unit.facts
            if fact.key == "needed" and fact.value not in (None, "")
        ),
        "",
    )
    return {
        "service": "ask_service_choice",
        "device": "ask_device_choice",
        "time": "ask_time_choice",
        "appointment": "ask_appointment_choice",
        "package": "ask_package_choice",
    }.get(needed)


def _outcome_choice_goal(outcome: TurnOutcome) -> str | None:
    if outcome.response_goal in _SUPPORTED_CHOICE_GOALS:
        return outcome.response_goal
    if outcome.response_goal != "clarification":
        return None
    needed = str(outcome.facts.get("needed") or "")
    return {
        "service": "ask_service_choice",
        "device": "ask_device_choice",
        "time": "ask_time_choice",
        "appointment": "ask_appointment_choice",
        "package": "ask_package_choice",
    }.get(needed)


def deduplicate_equivalent_choice_outcomes(
    outcomes: list[TurnOutcome],
) -> list[TurnOutcome]:
    """Drop only repeated presentations of the same canonical verified choice set."""
    seen: set[tuple[str, tuple[tuple[str, str], ...]]] = set()
    deduped: list[TurnOutcome] = []
    for outcome in outcomes:
        goal = _outcome_choice_goal(outcome)
        if outcome.status != "needs_input" or goal is None or not outcome.choices:
            deduped.append(outcome)
            continue
        signature = (
            goal,
            tuple(
                sorted(
                    (
                        str(choice.ref),
                        _normalized_label(choice.label),
                    )
                    for choice in outcome.choices
                )
            ),
        )
        if signature in seen:
            continue
        seen.add(signature)
        deduped.append(outcome)
    return deduped


def is_supported_verified_choice_unit(unit: CustomerResponseUnit) -> bool:
    return (
        unit.status == "needs_input"
        and _effective_choice_goal(unit) is not None
        and bool(unit.choices)
        and not is_refund_package_choice(unit)
    )


def is_pure_supported_verified_choice_contract(
    contract: CustomerResponseContract,
) -> bool:
    return bool(contract.units) and all(
        is_supported_verified_choice_unit(unit)
        for unit in contract.units
    )


def _normalized_label(label: str) -> str:
    return " ".join(label.split()).casefold()


def _duplicate_visible_labels(unit: CustomerResponseUnit) -> bool:
    labels = [_normalized_label(choice.label) for choice in unit.choices]
    return (
        any(not label for label in labels)
        or len(labels) != len(set(labels))
    )


def _choice_fact(choice, key: str) -> object | None:
    for fact in choice.facts:
        if fact.key == key:
            return fact.value
    return None


def _appointment_choice_label(choice, *, arabic: bool) -> str:
    start_local = _choice_fact(choice, "start_local")
    if start_local in (None, ""):
        return choice.label.strip()

    service_name = str(_choice_fact(choice, "service_name") or "").strip()
    doctor_name = str(_choice_fact(choice, "doctor_name") or "").strip()
    start = format_customer_datetime(start_local, arabic=arabic)

    parts: list[str] = []
    if service_name:
        parts.append(service_name)
    if arabic:
        parts.append(f"يوم {start}")
        if doctor_name:
            parts.append(f"مع {doctor_name}")
        return "، ".join(parts)

    parts.append(start)
    if doctor_name:
        parts.append(f"with {doctor_name}")
    return ", ".join(parts)


def _duplicate_fail_safe(*, arabic: bool) -> str:
    if arabic:
        return (
            "في أكتر من اختيار بنفس البيانات الظاهرة، "
            "فمحتاجين تمييز إضافي من بيانات العيادة قبل ما تختار."
        )
    return (
        "More than one option has the same visible details, "
        "so the clinic data needs an additional distinction before you choose."
    )


def deterministic_verified_choice_unit_reply(
    unit: CustomerResponseUnit,
    *,
    arabic: bool,
) -> str | None:
    if not is_supported_verified_choice_unit(unit):
        return None
    if _duplicate_visible_labels(unit):
        return _duplicate_fail_safe(arabic=arabic)

    effective_goal = _effective_choice_goal(unit)
    intro = (
        _INTRO_AR.get(effective_goal)
        if arabic
        else _INTRO_EN.get(effective_goal)
    )
    if not intro:
        raise VerifiedChoicePresentationError(
            f"Unsupported verified choice goal: {unit.response_goal}"
        )

    labels = [
        (
            _appointment_choice_label(choice, arabic=arabic)
            if effective_goal == "ask_appointment_choice"
            else choice.label.strip()
        )
        for choice in unit.choices
    ]
    return intro + "\n" + "\n".join(f"- {label}" for label in labels)


def deterministic_verified_choice_contract_reply(
    contract: CustomerResponseContract,
    *,
    arabic: bool,
) -> str:
    if not is_pure_supported_verified_choice_contract(contract):
        raise VerifiedChoicePresentationError(
            "Verified choice composer requires a pure supported choice contract."
        )

    chunks: list[str] = []
    for unit in contract.units:
        rendered = deterministic_verified_choice_unit_reply(unit, arabic=arabic)
        if rendered is None:
            raise VerifiedChoicePresentationError(
                "Verified choice unit unexpectedly became unsupported."
            )
        chunks.append(rendered)
    return "\n\n".join(chunks).strip()


def compose_verified_choice_contract_reply(
    *,
    history: list[BaseMessage],
    contract: CustomerResponseContract,
) -> tuple[str, str]:
    return (
        deterministic_verified_choice_contract_reply(
            contract,
            arabic=latest_customer_is_arabic(history),
        ),
        "deterministic:verified-choice-contract",
    )
