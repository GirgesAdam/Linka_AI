from __future__ import annotations

from datetime import date

from langchain_core.messages import BaseMessage, HumanMessage

from app.services.agent_v2.response_contract import (
    CustomerResponseContract,
    OwnedPackageInfo,
    PackageOfferInfo,
    is_pure_supported_package_contract,
)


class PackageComposerValidationError(RuntimeError):
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


def _visible_owned_signature(item: OwnedPackageInfo) -> tuple[object, ...]:
    return (
        item.name.casefold(),
        (item.device_name or "").casefold(),
        item.sessions_purchased,
        item.sessions_remaining,
        item.effective_status.casefold(),
        item.expires_at or "",
    )


def _visible_offer_signature(item: PackageOfferInfo) -> tuple[object, ...]:
    return (
        item.service_name.casefold(),
        (item.device_name or "").casefold(),
        item.sessions_count,
    )


def _has_duplicate_visible_identity(values: tuple[object, ...], *, owned: bool) -> bool:
    signatures = [
        _visible_owned_signature(item) if owned else _visible_offer_signature(item)
        for item in values
    ]
    return len(signatures) != len(set(signatures))


def _expiry_text(value: str | None, *, arabic: bool) -> str:
    if not value:
        return ""
    try:
        parsed = date.fromisoformat(value[:10])
        rendered = parsed.isoformat()
    except ValueError:
        rendered = value
    return f"؛ صالحة لحد {rendered}" if arabic else f"; expires {rendered}"


def _owned_line(item: OwnedPackageInfo, *, arabic: bool) -> str:
    if arabic:
        parts = [item.name]
        if item.device_name:
            parts.append(f"الجهاز: {item.device_name}")
        parts.append(f"الجلسات الأصلية: {item.sessions_purchased}")
        parts.append(f"المتبقي: {item.sessions_remaining}")
        status = _STATUS_AR.get(item.effective_status, item.effective_status)
        parts.append(f"الحالة: {status}{_expiry_text(item.expires_at, arabic=True)}")
        return " — ".join(parts) + "."

    parts = [item.name]
    if item.device_name:
        parts.append(f"device: {item.device_name}")
    parts.append(f"purchased sessions: {item.sessions_purchased}")
    parts.append(f"remaining: {item.sessions_remaining}")
    status = _STATUS_EN.get(item.effective_status, item.effective_status)
    parts.append(f"status: {status}{_expiry_text(item.expires_at, arabic=False)}")
    return " — ".join(parts) + "."


def _offer_line(item: PackageOfferInfo, *, arabic: bool) -> str:
    if arabic:
        text = f"باكدج {item.sessions_count} جلسة لخدمة {item.service_name}"
        if item.device_name:
            text += f" على {item.device_name}"
        return text + "."
    text = f"{item.sessions_count}-session package for {item.service_name}"
    if item.device_name:
        text += f" on {item.device_name}"
    return text + "."


def deterministic_package_contract_reply(
    contract: CustomerResponseContract,
    *,
    arabic: bool,
) -> str:
    if not is_pure_supported_package_contract(contract):
        raise PackageComposerValidationError(
            "Package composer requires a pure supported package-information contract."
        )

    chunks: list[str] = []
    for unit in contract.units:
        truth = unit.package_truth
        if truth is None:
            raise PackageComposerValidationError("Package truth is missing.")

        if truth.owned_requested:
            if _has_duplicate_visible_identity(truth.owned_packages, owned=True):
                chunks.append(
                    (
                        "في أكتر من باكدج مملوكة مطابقة بنفس البيانات الظاهرة، "
                        "فمحتاجين تمييز إضافي من بيانات العيادة قبل عرضها بشكل آمن."
                    )
                    if arabic
                    else (
                        "More than one owned package has the same visible details, "
                        "so the clinic data needs an additional distinction before showing it safely."
                    )
                )
            elif not truth.owned_packages:
                chunks.append(
                    "مفيش باكدجات مملوكة ظاهرة في بياناتك الحالية."
                    if arabic
                    else "No owned packages are present in your current verified record."
                )
            else:
                header = "الباكدجات اللي عندك:" if arabic else "Your owned packages:"
                chunks.append(
                    "\n".join(
                        [header, *[_owned_line(item, arabic=arabic) for item in truth.owned_packages]]
                    )
                )

        if truth.offers_requested:
            if _has_duplicate_visible_identity(truth.package_offers, owned=False):
                chunks.append(
                    (
                        "في أكتر من عرض باكدج مطابق بنفس البيانات الظاهرة، "
                        "فمحتاجين تمييز إضافي من بيانات العيادة قبل الاختيار."
                    )
                    if arabic
                    else (
                        "More than one package offer has the same visible details, "
                        "so the clinic data needs an additional distinction before choosing."
                    )
                )
            elif not truth.package_offers:
                chunks.append(
                    "مفيش عروض باكدجات مطابقة ومتاحة حاليًا."
                    if arabic
                    else "No matching package offers are currently verified as available."
                )
            else:
                header = (
                    "عروض الباكدجات المتاحة للشراء:"
                    if arabic
                    else "Package offers currently available to purchase:"
                )
                chunks.append(
                    "\n".join(
                        [header, *[_offer_line(item, arabic=arabic) for item in truth.package_offers]]
                    )
                )

    rendered = "\n".join(chunks).strip()
    for unit in contract.units:
        truth = unit.package_truth
        if truth is None or not any(
            package.effective_status == "active" and package.sessions_remaining > 0
            for package in truth.owned_packages
        ):
            continue
        facts = {fact.key: fact.value for fact in unit.facts}
        if facts.get("booking_commercial_basis_step") is not True:
            continue
        availability = facts.get("availability")
        if not isinstance(availability, dict):
            continue
        count = availability.get("available_option_count")
        exact_time = facts.get("exact_time_requested") is True
        if isinstance(count, int) and count > 0:
            if exact_time:
                rendered += (
                    "\nالجلسة هتتحسب من الباكدج الحالية، والوقت المطلوب متاح. تحب أحجز؟"
                    if arabic
                    else "\nThis session will use your current package, and the requested time is available. Shall I book it?"
                )
            else:
                rendered += (
                    "\nالجلسة هتتحسب من الباكدج الحالية، وفي مواعيد متاحة في اليوم المطلوب."
                    if arabic
                    else "\nThis session will use your current package, and there is verified availability on the requested day."
                )
        elif count == 0:
            rendered += (
                "\nالباكدج الحالية تغطي الجلسة، لكن الوقت المطلوب مش متاح."
                if arabic and exact_time
                else "\nالباكدج الحالية تغطي الجلسة، لكن مفيش مواعيد متاحة في اليوم المطلوب."
                if arabic
                else "\nYour current package covers the session, but the requested time is unavailable."
                if exact_time
                else "\nYour current package covers the session, but there is no availability on the requested day."
            )
    return rendered


def compose_package_contract_reply(
    *,
    history: list[BaseMessage],
    contract: CustomerResponseContract,
) -> tuple[str, str]:
    """Render read-only package information without model-authored package facts."""
    arabic = _latest_customer_is_arabic(history)
    return (
        deterministic_package_contract_reply(contract, arabic=arabic),
        "deterministic:package-contract",
    )
