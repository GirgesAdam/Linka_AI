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


_WEEKDAYS_EN = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)
_WEEKDAYS_AR = (
    "الاثنين",
    "الثلاثاء",
    "الأربعاء",
    "الخميس",
    "الجمعة",
    "السبت",
    "الأحد",
)


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


def _address_text(location: ClinicLocationInfo, *, arabic: bool) -> str | None:
    if location.address:
        return location.address
    parts: list[str] = []
    for value in (
        location.address_line1,
        location.address_line2,
        location.city,
        location.state,
        location.country_code,
    ):
        if value and value not in parts:
            parts.append(value)
    if not parts:
        return None
    return ("، " if arabic else ", ").join(parts)


def _working_hours_text(truth: ClinicTruth, *, arabic: bool) -> str | None:
    location = truth.location
    if location is None or not location.working_hours:
        return None
    grouped: dict[int, list[str]] = {}
    for row in location.working_hours:
        grouped.setdefault(row.weekday, []).append(f"{row.start}–{row.end}")
    names = _WEEKDAYS_AR if arabic else _WEEKDAYS_EN
    separator = "؛ " if arabic else "; "
    rendered = separator.join(
        f"{names[weekday]}: {', '.join(periods)}"
        for weekday, periods in sorted(grouped.items())
    )
    timezone = location.timezone or truth.timezone
    if timezone:
        rendered = f"{rendered} ({timezone})"
    return rendered


def _detail_reply(truth: ClinicTruth, *, arabic: bool) -> str:
    parts: list[str] = []
    location = truth.location

    for detail in truth.requested_details:
        if detail == "name":
            parts.append(
                f"اسم العيادة: {truth.clinic_name}."
                if arabic
                else f"Clinic name: {truth.clinic_name}."
            )
        elif detail == "address":
            address = _address_text(location, arabic=arabic) if location else None
            location_name = location.name if location else None
            if address:
                if arabic:
                    prefix = f"{location_name}: " if location_name else ""
                    parts.append(f"العنوان المسجل: {prefix}{address}.")
                else:
                    prefix = f"{location_name}: " if location_name else ""
                    parts.append(f"Registered address: {prefix}{address}.")
            else:
                parts.append(
                    "مفيش عنوان عميل مسجل في بيانات العيادة الحالية."
                    if arabic
                    else "No customer-facing address is stored in the current clinic data."
                )
        elif detail == "contact":
            contact_parts: list[str] = []
            if location and location.phone:
                contact_parts.append(
                    f"رقم التواصل: {location.phone}."
                    if arabic
                    else f"Contact phone: {location.phone}."
                )
            if contact_parts:
                parts.extend(contact_parts)
            else:
                parts.append(
                    "مفيش بيانات تواصل عميل مسجلة في بيانات العيادة الحالية."
                    if arabic
                    else "No customer-facing contact details are stored in the current clinic data."
                )
        elif detail == "working_hours":
            hours = _working_hours_text(truth, arabic=arabic)
            parts.append(
                (
                    f"ساعات العمل الأسبوعية المسجلة: {hours}."
                    if hours
                    else "مفيش ساعات عمل أسبوعية مسجلة في بيانات العيادة الحالية."
                )
                if arabic
                else (
                    f"Registered weekly working hours: {hours}."
                    if hours
                    else "No weekly working hours are stored in the current clinic data."
                )
            )
        elif detail == "general_info":
            if truth.knowledge:
                parts.append(truth.knowledge)
            else:
                parts.append(
                    "مفيش معلومات عامة إضافية محفوظة للعيادة حاليًا."
                    if arabic
                    else "No additional clinic-authored general information is stored right now."
                )
        elif detail == "open_now":
            hours = _working_hours_text(truth, arabic=arabic)
            if arabic:
                if hours:
                    parts.append(f"ساعات العمل الأسبوعية المسجلة: {hours}.")
                parts.append(
                    "البيانات الحالية ما فيهاش حالة فتح/إغلاق لحظية أو استثناءات اليوم، "
                    "فما أقدرش أؤكد إن العيادة مفتوحة دلوقتي."
                )
            else:
                if hours:
                    parts.append(f"Registered weekly working hours: {hours}.")
                parts.append(
                    "The current data does not include a verified live open/closed state or today's "
                    "exceptions, so I cannot confirm that the clinic is open right now."
                )

    if not parts:
        raise ClinicComposerValidationError("Clinic truth has no supported requested details.")
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
        chunks.append(_detail_reply(truth, arabic=arabic))
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
