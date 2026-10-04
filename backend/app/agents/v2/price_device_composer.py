from __future__ import annotations

import json
import logging
from decimal import Decimal, InvalidOperation
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
    CommercialPriceOption,
    CustomerResponseContract,
    CustomerResponseUnit,
)

logger = logging.getLogger(__name__)

PriceDeviceStyle = Literal["plain", "warm", "friendly"]
PriceDevicePresentation = Literal["sentence", "list"]
PriceDeviceTransition = Literal["sentence", "and", "then"]
CommercialReference = Literal["unit_commercial"]


class StrictPriceDeviceComposerModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PriceDeviceComposerUnitDraft(StrictPriceDeviceComposerModel):
    unit_index: int
    commercial_ref: CommercialReference
    style: PriceDeviceStyle
    option_refs: list[str]
    presentation: PriceDevicePresentation
    transition: PriceDeviceTransition


class PriceDeviceComposerDraft(StrictPriceDeviceComposerModel):
    units: list[PriceDeviceComposerUnitDraft]


class PriceDeviceComposerValidationError(RuntimeError):
    pass


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


def _option_refs(unit_index: int, unit: CustomerResponseUnit) -> list[str]:
    truth = unit.commercial_truth
    if truth is None:
        return []
    return [
        f"unit_{unit_index}_price_option_{index}"
        for index, _option in enumerate(truth.options)
    ]


def _contract_view(contract: CustomerResponseContract) -> list[dict[str, object]]:
    units: list[dict[str, object]] = []
    for index, unit in enumerate(contract.units):
        truth = unit.commercial_truth
        units.append(
            {
                "unit_index": index,
                "response_goal": unit.response_goal,
                "commercial_kind": truth.kind if truth is not None else None,
                "price_option_refs": _option_refs(index, unit),
                "option_count": len(truth.options) if truth is not None else 0,
                "complete_set": bool(truth.complete_set) if truth is not None else False,
            }
        )
    return units


def _build_price_device_composer_messages(
    *,
    history: list[BaseMessage],
    contract: CustomerResponseContract,
) -> list[BaseMessage]:
    latest_index = _latest_customer_index(history)
    if latest_index is None:
        raise PriceDeviceComposerValidationError(
            "Price/device composer requires a customer message."
        )
    language = (
        "Egyptian Arabic"
        if _latest_customer_is_arabic(history)
        else "English"
    )
    return [
        SystemMessage(
            content=(
                "You select presentation structure for verified clinic pricing. "
                "You never write customer prose and you never return any service name, "
                "device name, amount, currency, session count, or other commercial value. "
                "The backend owns every exact value and every commercial binding.\n"
                "Return one draft unit per CONTRACT unit in identical order. "
                "commercial_ref must be unit_commercial. option_refs must contain every "
                "price_option_ref exactly once and in the same order. Never infer, merge, "
                "drop, duplicate, or rewrite options. Choose only style and sentence/list "
                "presentation.\n"
                f"Customer reply language: {language}."
            )
        ),
        SystemMessage(
            content=(
                "PRICE_DEVICE_CONTRACT_STRUCTURE (symbolic refs only; no values):\n"
                + json.dumps(
                    _contract_view(contract),
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
        ),
        HumanMessage(content="Select the price/device presentation structure."),
    ]


def validate_price_device_composer_draft(
    contract: CustomerResponseContract,
    draft: PriceDeviceComposerDraft,
) -> None:
    if len(draft.units) != len(contract.units):
        raise PriceDeviceComposerValidationError(
            "Price/device composer must represent every contract unit exactly once."
        )

    for expected_index, (unit, draft_unit) in enumerate(
        zip(contract.units, draft.units, strict=True)
    ):
        if draft_unit.unit_index != expected_index:
            raise PriceDeviceComposerValidationError(
                "Price/device composer changed compound unit ordering."
            )
        truth = unit.commercial_truth
        if truth is None:
            raise PriceDeviceComposerValidationError(
                "Price/device composer received a unit without commercial truth."
            )
        expected_refs = _option_refs(expected_index, unit)
        if len(draft_unit.option_refs) != len(set(draft_unit.option_refs)):
            raise PriceDeviceComposerValidationError(
                "Price/device composer repeated a price option reference."
            )
        if draft_unit.option_refs != expected_refs:
            raise PriceDeviceComposerValidationError(
                "Price/device composer must preserve all verified price option references."
            )
        if expected_index == 0 and draft_unit.transition != "sentence":
            raise PriceDeviceComposerValidationError(
                "The first price/device unit must start a sentence."
            )


def _visible_money(option: CommercialPriceOption, *, arabic: bool) -> str:
    try:
        amount = Decimal(option.amount)
        amount_text = format(amount, "f")
        if "." in amount_text:
            amount_text = amount_text.rstrip("0").rstrip(".")
    except InvalidOperation:
        amount_text = option.amount
    currency = option.currency.upper()
    if arabic and currency == "EGP":
        currency = "جنيه"
    return " ".join(part for part in (amount_text, currency) if part)


def _service_option_text(
    option: CommercialPriceOption,
    *,
    arabic: bool,
) -> str:
    price = _visible_money(option, arabic=arabic)
    if option.qualifier == "base":
        if arabic:
            return f"جلسة {option.service_name} سعرها {price}"
        return f"{option.service_name} is {price}"
    if option.qualifier == "device":
        if arabic:
            return (
                f"جلسة {option.service_name} على {option.device_name} "
                f"سعرها {price}"
            )
        return f"{option.service_name} on {option.device_name} is {price}"
    if option.qualifier == "package":
        device = (
            f" على {option.device_name}"
            if arabic and option.device_name
            else (
                f" on {option.device_name}"
                if option.device_name
                else ""
            )
        )
        if arabic:
            return (
                f"باكدج {option.sessions_count} جلسات لـ{option.service_name}"
                f"{device} سعرها {price}"
            )
        return (
            f"The {option.sessions_count}-session {option.service_name} package"
            f"{device} is {price}"
        )
    raise PriceDeviceComposerValidationError("Unsupported commercial qualifier.")


def _render_option_list(
    unit: CustomerResponseUnit,
    *,
    arabic: bool,
    presentation: PriceDevicePresentation,
) -> str:
    truth = unit.commercial_truth
    if truth is None:
        raise PriceDeviceComposerValidationError("Missing commercial truth.")
    options = list(truth.options)
    if not options:
        if truth.kind == "package_price_options":
            return (
                "مفيش عرض باكدج مطابق مؤكد في بيانات العيادة الحالية."
                if arabic
                else "There is no matching verified package offer in the current clinic data."
            )
        service_name = truth.service_name or (
            "الخدمة" if arabic else "the service"
        )
        return (
            f"السعر المؤكد لخدمة {service_name} مش متاح في بيانات العيادة الحالية."
            if arabic
            else f"The verified price for {service_name} is not available in the current clinic data."
        )

    if truth.kind in {"service_base_price", "service_device_price"}:
        rendered = _service_option_text(options[0], arabic=arabic) + "."
        facts = {fact.key: fact.value for fact in unit.facts}
        if facts.get("booking_next_field") == "date":
            return rendered + (" تحب تحجز يوم إيه؟" if arabic else " What day would you like to book?")
        if facts.get("booking_next_field") == "booking":
            return rendered + (" تحب أكمل الحجز على الموعد ده؟" if arabic else " Shall I continue with this booking?")
        return rendered

    if truth.kind in {
        "service_device_price_options",
        "device_price_clarification",
    }:
        if presentation == "list":
            lines = [
                (
                    f"{option.device_name} — {_visible_money(option, arabic=arabic)}"
                )
                for option in options
            ]
            if arabic:
                intro = f"سعر جلسة {options[0].service_name} حسب الجهاز:"
                closing = "تحب أي جهاز؟"
            else:
                intro = f"{options[0].service_name} pricing depends on the device:"
                closing = "Which device would you like?"
            return "\n".join([intro, *lines, closing])

        rendered = [
            (
                f"{option.device_name} بسعر {_visible_money(option, arabic=arabic)}"
                if arabic
                else f"{option.device_name} at {_visible_money(option, arabic=arabic)}"
            )
            for option in options
        ]
        if arabic:
            return "اختار جهاز الليزر: " + "، ".join(rendered) + "."
        return "Choose the laser device: " + "; ".join(rendered) + "."

    if truth.kind == "package_price_options":
        if len(options) == 1:
            return _service_option_text(options[0], arabic=arabic) + "."
        lines = [
            _service_option_text(option, arabic=arabic)
            for option in options
        ]
        if presentation == "list":
            intro = (
                "العروض المطابقة المؤكدة:"
                if arabic
                else "Verified matching package offers:"
            )
            return "\n".join([intro, *[line + "." for line in lines]])
        separator = "، " if arabic else "; "
        return separator.join(lines) + "."

    raise PriceDeviceComposerValidationError(
        f"Unsupported commercial kind: {truth.kind}"
    )


def resolve_price_device_composer_draft(
    contract: CustomerResponseContract,
    draft: PriceDeviceComposerDraft,
    *,
    arabic: bool,
) -> str:
    validate_price_device_composer_draft(contract, draft)
    chunks: list[str] = []
    for index, (unit, draft_unit) in enumerate(
        zip(contract.units, draft.units, strict=True)
    ):
        rendered = _render_option_list(
            unit,
            arabic=arabic,
            presentation=draft_unit.presentation,
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


def _default_draft(
    contract: CustomerResponseContract,
) -> PriceDeviceComposerDraft:
    units = []
    for index, unit in enumerate(contract.units):
        truth = unit.commercial_truth
        if truth is None:
            raise PriceDeviceComposerValidationError(
                "Price/device fallback requires commercial truth."
            )
        units.append(
            PriceDeviceComposerUnitDraft(
                unit_index=index,
                commercial_ref="unit_commercial",
                style="warm" if index == 0 else "plain",
                option_refs=_option_refs(index, unit),
                presentation=(
                    "list"
                    if len(truth.options) > 1
                    else "sentence"
                ),
                transition="sentence" if index == 0 else "and",
            )
        )
    return PriceDeviceComposerDraft(units=units)


def deterministic_price_device_fallback(
    contract: CustomerResponseContract,
    *,
    arabic: bool,
) -> str:
    return resolve_price_device_composer_draft(
        contract,
        _default_draft(contract),
        arabic=arabic,
    )


def _requires_model(contract: CustomerResponseContract) -> bool:
    return any(
        unit.commercial_truth is not None
        and unit.commercial_truth.kind
        in {"package_price_options", "device_price_clarification"}
        and bool(unit.commercial_truth.options)
        for unit in contract.units
    )


def compose_price_device_contract_reply(
    *,
    history: list[BaseMessage],
    contract: CustomerResponseContract,
) -> tuple[str, str]:
    """Compose commercial reads without model-authored amounts or bindings."""
    arabic = _latest_customer_is_arabic(history)
    fallback_text = deterministic_price_device_fallback(
        contract,
        arabic=arabic,
    )
    if not _requires_model(contract):
        return fallback_text, "deterministic:price-device-contract"

    try:
        messages = _build_price_device_composer_messages(
            history=history,
            contract=contract,
        )
        primary_name = settings.openai_model
        fallback_name = settings.openai_fallback_model
        primary = build_realtime_composer_model()
        fallback_model = None

        def invoke_structured(model) -> PriceDeviceComposerDraft:
            return invoke_typed_structured_output(
                model=model,
                schema=PriceDeviceComposerDraft,
                messages=messages,
            )

        def primary_call() -> PriceDeviceComposerDraft:
            return invoke_structured(primary)

        def fallback_call() -> PriceDeviceComposerDraft:
            nonlocal fallback_model
            if fallback_model is None:
                fallback_model = build_realtime_composer_fallback_model()
            if fallback_model is None:
                raise RuntimeError(
                    "Price/device composer fallback model is not configured."
                )
            return invoke_structured(fallback_model)

        model_calls = [(primary_name, primary_call)]
        if fallback_name and fallback_name != primary_name:
            model_calls.append((fallback_name, fallback_call))

        invocation = invoke_with_model_chain(
            model_calls=model_calls,
            operation="v2-price-device-contract-composer",
            circuit_breaker_cooldown_seconds=(
                settings.llm_realtime_circuit_breaker_cooldown_seconds
            ),
        )
        text = resolve_price_device_composer_draft(
            contract,
            invocation.value,
            arabic=arabic,
        )
        return text, f"price-device-contract:{model_label(invocation.model_name)}"
    except (
        LLMProviderError,
        StructuredOutputError,
        PriceDeviceComposerValidationError,
        RuntimeError,
    ) as exc:
        logger.warning(
            "Price/device contract composer used deterministic fallback reason=%s",
            type(exc).__name__,
        )
        return fallback_text, "deterministic:price-device-contract-fallback"
