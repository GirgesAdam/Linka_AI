from __future__ import annotations

from datetime import date

from langchain_core.messages import BaseMessage, HumanMessage

from app.services.agent_v2.response_contract import (
    CustomerResponseContract,
    OwnedPulsePackInfo,
    PulseBalanceInfo,
    PulseOfferInfo,
    PulseOverageInfo,
    is_pure_supported_pulse_contract,
)


class PulseComposerValidationError(RuntimeError):
    pass


_STATUS_AR = {
    "active": "نشطة",
    "expired": "منتهية",
    "exhausted": "مستخدمة بالكامل",
    "cancelled": "ملغاة",
}
_STATUS_EN = {
    "active": "active",
    "expired": "expired",
    "exhausted": "fully used",
    "cancelled": "cancelled",
}


def _message_text(message: BaseMessage, *, limit: int = 1000) -> str:
    if not isinstance(message.content, str) or not message.content.strip():
        return ""
    text = " ".join(message.content.strip().split())
    return text[:limit] + ("…" if len(text) > limit else "")


def _latest_customer_is_arabic(history: list[BaseMessage]) -> bool:
    for message in reversed(history):
        if not isinstance(message, HumanMessage):
            continue
        text = _message_text(message)
        if text:
            return any("\u0600" <= char <= "\u06ff" for char in text)
    return False


def _expiry_text(value: str | None, *, arabic: bool) -> str:
    if not value:
        return ""
    try:
        rendered = date.fromisoformat(value[:10]).isoformat()
    except ValueError:
        rendered = value
    return f"؛ صالحة لحد {rendered}" if arabic else f"; expires {rendered}"


def _balance_line(item: PulseBalanceInfo, *, arabic: bool) -> str:
    if arabic:
        return f"{item.device_name}: {item.pulses_remaining} Pulse متاح."
    return f"{item.device_name}: {item.pulses_remaining} Pulses available."


def _owned_line(item: OwnedPulsePackInfo, *, arabic: bool) -> str:
    if arabic:
        status = _STATUS_AR.get(item.effective_status, item.effective_status)
        return (
            f"{item.device_name}: الباقة الأصلية {item.pulses_purchased} Pulse، "
            f"المستخدم حتى الآن {item.pulses_consumed}، المتبقي {item.pulses_remaining}، "
            f"الحالة {status}{_expiry_text(item.expires_at, arabic=True)}."
        )
    status = _STATUS_EN.get(item.effective_status, item.effective_status)
    return (
        f"{item.device_name}: pack size {item.pulses_purchased} Pulses, "
        f"used so far {item.pulses_consumed}, remaining {item.pulses_remaining}, "
        f"status {status}{_expiry_text(item.expires_at, arabic=False)}."
    )


def _offer_line(item: PulseOfferInfo, *, arabic: bool) -> str:
    if arabic:
        return (
            f"{item.pulses_count} Pulse على {item.device_name} "
            f"بسعر {item.price}."
        )
    return (
        f"{item.pulses_count} Pulses for {item.device_name} "
        f"for {item.price}."
    )


def _overage_line(item: PulseOverageInfo, *, arabic: bool) -> str:
    if item.unit_price is None:
        return (
            f"سعر الـPulse الإضافية على {item.device_name} غير متاح في البيانات الحالية."
            if arabic
            else f"The extra-Pulse price for {item.device_name} is not available in the current data."
        )
    if (
        item.requested_pulse_count is not None
        and item.total_price is not None
    ):
        if arabic:
            return (
                f"{item.device_name}: سعر الـPulse الإضافية {item.unit_price} لكل Pulse؛ "
                f"{item.requested_pulse_count} Pulse = {item.total_price}."
            )
        return (
            f"{item.device_name}: extra Pulses cost {item.unit_price} each; "
            f"{item.requested_pulse_count} Pulses = {item.total_price}."
        )
    if arabic:
        return f"{item.device_name}: سعر الـPulse الإضافية {item.unit_price} لكل Pulse."
    return f"{item.device_name}: extra Pulses cost {item.unit_price} each."


def deterministic_pulse_contract_reply(
    contract: CustomerResponseContract,
    *,
    arabic: bool,
) -> str:
    if not is_pure_supported_pulse_contract(contract):
        raise PulseComposerValidationError(
            "Pulse composer requires a pure supported Pulse-information contract."
        )

    chunks: list[str] = []
    for unit in contract.units:
        truth = unit.pulse_truth
        if truth is None:
            raise PulseComposerValidationError("Pulse truth is missing.")

        if "balance" in truth.requested_details:
            if not truth.balances:
                chunks.append(
                    "مفيش رصيد Pulses متاح في بياناتك الحالية."
                    if arabic
                    else "No available Pulse balance is present in your current verified record."
                )
            else:
                header = "رصيد الـPulses الحالي:" if arabic else "Current Pulse balance:"
                chunks.append(
                    "\n".join(
                        [header, *[_balance_line(item, arabic=arabic) for item in truth.balances]]
                    )
                )

        if "owned_packs" in truth.requested_details:
            if not truth.owned_packs:
                chunks.append(
                    "مفيش باقات Pulses مملوكة ظاهرة في بياناتك الحالية."
                    if arabic
                    else "No owned Pulse packs are present in your current verified record."
                )
            else:
                header = "باقات الـPulses اللي عندك:" if arabic else "Your owned Pulse packs:"
                chunks.append(
                    "\n".join(
                        [header, *[_owned_line(item, arabic=arabic) for item in truth.owned_packs]]
                    )
                )

        if "offers" in truth.requested_details:
            if not truth.available_offers:
                chunks.append(
                    "مفيش عروض باقات Pulses مطابقة ومتاحة حاليًا."
                    if arabic
                    else "No matching Pulse-pack offers are currently verified as available."
                )
            else:
                header = (
                    "عروض باقات الـPulses المتاحة للشراء:"
                    if arabic
                    else "Pulse-pack offers currently available to purchase:"
                )
                chunks.append(
                    "\n".join(
                        [header, *[_offer_line(item, arabic=arabic) for item in truth.available_offers]]
                    )
                )

        if "overage_price" in truth.requested_details:
            if not truth.overage_options:
                chunks.append(
                    "سعر الـPulse الإضافية غير متاح في البيانات الحالية."
                    if arabic
                    else "Extra-Pulse pricing is not available in the current verified data."
                )
            else:
                header = "سعر الـPulse الإضافية:" if arabic else "Extra-Pulse pricing:"
                chunks.append(
                    "\n".join(
                        [header, *[_overage_line(item, arabic=arabic) for item in truth.overage_options]]
                    )
                )

    return "\n".join(chunks).strip()


def compose_pulse_contract_reply(
    *,
    history: list[BaseMessage],
    contract: CustomerResponseContract,
) -> tuple[str, str]:
    """Render read-only Pulse information without model-authored Pulse facts."""
    arabic = _latest_customer_is_arabic(history)
    return (
        deterministic_pulse_contract_reply(contract, arabic=arabic),
        "deterministic:pulse-contract",
    )
