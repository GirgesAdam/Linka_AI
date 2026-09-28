from __future__ import annotations

from datetime import datetime

from langchain_core.messages import BaseMessage, HumanMessage

from app.services.agent_v2.response_contract import (
    AppointmentInfoTruth,
    AppointmentServiceInfo,
    AppointmentVisitInfo,
    CustomerResponseContract,
    is_pure_supported_appointment_contract,
)


class AppointmentInfoComposerValidationError(RuntimeError):
    pass


_STATUS_AR = {
    "pending": "في انتظار التأكيد",
    "confirmed": "مؤكد",
    "completed": "مكتمل",
    "cancelled": "ملغي",
    "mixed": "بحالات مختلفة",
}
_STATUS_EN = {
    "pending": "pending confirmation",
    "confirmed": "confirmed",
    "completed": "completed",
    "cancelled": "cancelled",
    "mixed": "mixed status",
}
_MONTHS_AR = (
    "",
    "يناير",
    "فبراير",
    "مارس",
    "أبريل",
    "مايو",
    "يونيو",
    "يوليو",
    "أغسطس",
    "سبتمبر",
    "أكتوبر",
    "نوفمبر",
    "ديسمبر",
)
_MONTHS_EN = (
    "",
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


def _message_text(message: BaseMessage, *, limit: int = 1000) -> str:
    if not isinstance(message.content, str) or not message.content.strip():
        return ""
    return " ".join(message.content.strip().split())[:limit]
def _latest_customer_is_arabic(history: list[BaseMessage]) -> bool:
    for message in reversed(history):
        if not isinstance(message, HumanMessage):
            continue
        text = _message_text(message)
        if text:
            return any("\u0600" <= char <= "\u06ff" for char in text)
    return False


def _format_start(value: str, *, arabic: bool) -> str:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return value
    hour = parsed.hour % 12 or 12
    minute = f"{parsed.minute:02d}"
    if arabic:
        period = "صباحًا" if parsed.hour < 12 else "مساءً"
        return (
            f"{parsed.day} {_MONTHS_AR[parsed.month]} {parsed.year} "
            f"الساعة {hour}:{minute} {period}"
        )
    period = "AM" if parsed.hour < 12 else "PM"
    return (
        f"{_MONTHS_EN[parsed.month]} {parsed.day}, {parsed.year} "
        f"at {hour}:{minute} {period}"
    )
def _service_text(service: AppointmentServiceInfo, *, arabic: bool) -> str:
    if service.service_name and service.device_name:
        if arabic:
            return f"{service.service_name} على جهاز {service.device_name}"
        return f"{service.service_name} on {service.device_name}"
    if service.service_name:
        return service.service_name
    if service.device_name:
        return (
            f"على جهاز {service.device_name}"
            if arabic
            else f"on {service.device_name}"
        )
    return ""


def _visit_line(
    visit: AppointmentVisitInfo,
    *,
    arabic: bool,
    index: int | None,
) -> str:
    start = _format_start(visit.start_local, arabic=arabic)
    status = (_STATUS_AR if arabic else _STATUS_EN).get(visit.status, visit.status)
    services = [
        text
        for item in visit.services
        if (text := _service_text(item, arabic=arabic))
    ]
    if arabic:
        parts = [f"يوم {start}"]
        if visit.doctor_name:
            parts.append(f"مع {visit.doctor_name}")
        if services:
            parts.append("الخدمات: " + "، ".join(services))
        parts.append(f"الحالة: {status}")
        prefix = f"{index}) " if index is not None else ""
        return prefix + "، ".join(parts) + "."

    parts = [start]
    if visit.doctor_name:
        parts.append(f"with {visit.doctor_name}")
    if services:
        parts.append("services: " + "; ".join(services))
    parts.append(f"status: {status}")
    prefix = f"{index}) " if index is not None else ""
    return prefix + ", ".join(parts) + "."


def _render_truth(truth: AppointmentInfoTruth, *, arabic: bool) -> str:
    if not truth.visits:
        if truth.complete_set:
            return (
                "مفيش مواعيد جاية في السجل المؤكد الحالي."
                if arabic
                else "There are no upcoming appointments in the current verified schedule."
            )
        return (
            "مفيش مواعيد جاية في النتيجة المتحققة الحالية."
            if arabic
            else "There are no upcoming appointments in the current verified result."
        )

    if len(truth.visits) == 1:
        header = (
            "ميعادك الجاي:"
            if arabic and truth.complete_set
            else "الموعد الجاي المتحقق:"
            if arabic
            else "Your upcoming appointment:"
            if truth.complete_set
            else "Verified upcoming appointment:"
        )
        return header + "\n" + _visit_line(
            truth.visits[0],
            arabic=arabic,
            index=None,
        )

    header = (
        "مواعيدك الجاية:"
        if arabic and truth.complete_set
        else "المواعيد الجاية المتحققة:"
        if arabic
        else "Your upcoming appointments:"
        if truth.complete_set
        else "Verified upcoming appointments:"
    )
    lines = [
        _visit_line(visit, arabic=arabic, index=index)
        for index, visit in enumerate(truth.visits, start=1)
    ]
    return "\n".join([header, *lines])
def deterministic_appointment_info_reply(
    contract: CustomerResponseContract,
    *,
    arabic: bool,
) -> str:
    if not is_pure_supported_appointment_contract(contract):
        raise AppointmentInfoComposerValidationError(
            "Appointment-info composer requires a pure supported appointment contract."
        )

    chunks: list[str] = []
    for unit in contract.units:
        truth = unit.appointment_truth
        if truth is None:
            raise AppointmentInfoComposerValidationError(
                "Appointment information truth is missing."
            )
        chunks.append(_render_truth(truth, arabic=arabic))
    return "\n".join(chunks).strip()


def compose_appointment_info_contract_reply(
    *,
    history: list[BaseMessage],
    contract: CustomerResponseContract,
) -> tuple[str, str]:
    """Render verified upcoming appointments without model-authored bindings."""
    return (
        deterministic_appointment_info_reply(
            contract,
            arabic=_latest_customer_is_arabic(history),
        ),
        "deterministic:appointment-info-contract",
    )
