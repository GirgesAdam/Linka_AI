from __future__ import annotations

from langchain_core.messages import BaseMessage, HumanMessage

from app.services.agent_v2.response_contract import (
    ClinicLocationInfo,
    ClinicTruth,
    CustomerResponseContract,
    is_pure_supported_clinic_contract,
)


class ClinicComposerValidationError(RuntimeError):
    pass


def _message_text(message: BaseMessage, *, limit: int = 1000) -> str:
    if not isinstance(message.content, str) or not message.content.strip():
        return ""
    return " ".join(message.content.strip().split())[:limit]


def _latest_customer_is_arabic(history: list[BaseMessage]) -> bool:
    for message in reversed(history):
        if isinstance(message, HumanMessage):
            text = _message_text(message)
            if text:
                return any("\u0600" <= char <= "\u06ff" for char in text)
    return False


def _address_text(location: ClinicLocationInfo) -> str | None:
    if location.address:
        return location.address
    parts = [
        value
        for value in (
            location.address_line1,
            location.address_line2,
            location.city,
            location.state,
            location.country_code,
        )
        if value
    ]
    return ", ".join(parts) if parts else None


def _render_location(
    truth: ClinicTruth,
    *,
    arabic: bool,
    include_address: bool,
    include_contact: bool,
) -> str:
    if not truth.complete_location_set:
        raise ClinicComposerValidationError("Clinic location scope is not verified complete.")
    if not truth.locations:
        if include_address and include_contact:
            return (
                "لا توجد بيانات عنوان أو تواصل محفوظة للموقع الحالي."
                if arabic
                else "No address or contact details are stored for the current clinic location."
            )
        if include_address:
            return (
                "لا يوجد عنوان محفوظ للموقع الحالي."
                if arabic
                else "No address is stored for the current clinic location."
            )
        return (
            "لا توجد بيانات تواصل محفوظة للموقع الحالي."
            if arabic
            else "No contact details are stored for the current clinic location."
        )

    location = truth.locations[0]
    parts: list[str] = []
    if location.name:
        parts.append(
            f"الموقع: {location.name}."
            if arabic
            else f"Location: {location.name}."
        )
    if include_address:
        address = _address_text(location)
        parts.append(
            f"العنوان: {address}."
            if arabic and address
            else f"Address: {address}."
            if address
            else "لا يوجد عنوان محفوظ للموقع الحالي."
            if arabic
            else "No address is stored for the current clinic location."
        )
    if include_contact:
        if location.phone:
            parts.append(
                f"الهاتف: {location.phone}."
                if arabic
                else f"Phone: {location.phone}."
            )
        if location.email:
            parts.append(
                f"البريد الإلكتروني: {location.email}."
                if arabic
                else f"Email: {location.email}."
            )
        if not location.phone and not location.email:
            parts.append(
                "لا توجد بيانات تواصل محفوظة للموقع الحالي."
                if arabic
                else "No contact details are stored for the current clinic location."
            )
    return " ".join(parts)


def _render_truth(truth: ClinicTruth, *, arabic: bool) -> str:
    parts: list[str] = []
    requested = truth.requested_details

    if "name" in requested:
        if truth.clinic_name:
            parts.append(
                f"اسم العيادة: {truth.clinic_name}."
                if arabic
                else f"Clinic: {truth.clinic_name}."
            )
        else:
            parts.append(
                "اسم العيادة غير محفوظ في البيانات الحالية."
                if arabic
                else "The clinic name is not stored in the current data."
            )

    include_location = "location" in requested
    include_contact = "contact" in requested
    if include_location or include_contact:
        parts.append(
            _render_location(
                truth,
                arabic=arabic,
                include_address=include_location,
                include_contact=include_contact,
            )
        )

    if "knowledge" in requested:
        parts.append(
            truth.knowledge
            if truth.knowledge
            else (
                "لا توجد معلومات إضافية محفوظة للعيادة حاليًا."
                if arabic
                else "No additional clinic information is currently stored."
            )
        )

    if not parts:
        raise ClinicComposerValidationError("Clinic truth has no renderable requested facts.")
    return " ".join(parts).strip()


def deterministic_clinic_contract_reply(
    contract: CustomerResponseContract,
    *,
    arabic: bool,
) -> str:
    if not is_pure_supported_clinic_contract(contract):
        raise ClinicComposerValidationError(
            "Clinic composer requires a pure supported clinic-information contract."
        )
    chunks: list[str] = []
    for unit in contract.units:
        truth = unit.clinic_truth
        if truth is None:
            raise ClinicComposerValidationError("Clinic truth is missing.")
        chunks.append(_render_truth(truth, arabic=arabic))
    return "\n".join(chunks).strip()


def compose_clinic_contract_reply(
    *,
    history: list[BaseMessage],
    contract: CustomerResponseContract,
) -> tuple[str, str]:
    return (
        deterministic_clinic_contract_reply(
            contract,
            arabic=_latest_customer_is_arabic(history),
        ),
        "deterministic:clinic-information-contract",
    )
