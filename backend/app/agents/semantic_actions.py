from __future__ import annotations

from datetime import datetime
from typing import Any

from app.agents.availability_presentation import format_availability_windows_reply
from app.services.laser_booking_context import set_laser_device_key
from app.services.laser_slot_metadata import decode_laser_slot_branch


def select_slot_from_structured_selection(
    booking_output: dict[str, Any] | None,
    *,
    selection_index: int | None,
    selection_time: str | None,
    doctor_id: str | None = None,
) -> dict[str, Any] | None:
    if not booking_output or booking_output.get("ok") is not True:
        return None

    slots = booking_output.get("slots")
    if not isinstance(slots, list) or not slots:
        return None

    if selection_index is not None:
        index = selection_index - 1
        if 0 <= index < len(slots) and isinstance(slots[index], dict):
            return slots[index]

    if selection_time:
        normalized = selection_time.strip()
        if len(normalized) == 4 and normalized[1] == ":":
            normalized = "0" + normalized
        matches = [
            slot
            for slot in slots
            if isinstance(slot, dict)
            and slot.get("start_time_24h") == normalized
            and (doctor_id is None or str(slot.get("doctor_id") or "") == str(doctor_id))
        ]
        if len(matches) == 1:
            return matches[0]

    return None


def _slot_device(slot: dict[str, Any]) -> tuple[str | None, str | None]:
    key = str(slot.get("laser_device_key") or "").strip() or None
    name = str(slot.get("laser_device_name") or "").strip() or None
    if key:
        return key, name
    metadata = decode_laser_slot_branch(slot.get("branch_name"))
    return metadata.device_key, metadata.device_name


def booking_tool_args(slot: dict[str, Any]) -> dict[str, str]:
    device_key, _device_name = _slot_device(slot)
    set_laser_device_key(device_key)
    return {
        "branch_id": str(slot["branch_id"]),
        "service_id": str(slot["service_id"]),
        "doctor_id": str(slot["doctor_id"]),
        "start_at": str(slot["start_local"]),
        "customer_note": "",
    }


def _appointment_device_name(appointment: dict[str, Any]) -> str | None:
    explicit = str(appointment.get("laser_device_name") or "").strip()
    if explicit:
        return explicit
    return decode_laser_slot_branch(appointment.get("branch")).device_name


def format_booking_success(appointment: dict[str, Any]) -> str:
    date_text = ""
    time_text = ""
    try:
        start = datetime.fromisoformat(str(appointment.get("start_local")))
        date_text = start.strftime("%d/%m/%Y")
        time_text = start.strftime("%H:%M")
    except Exception:
        pass

    status = appointment.get("status")
    if status == "pending":
        opening = "تمام، تم حجز موعدك"
    elif status == "confirmed":
        opening = "تمام، تم تأكيد موعدك"
    else:
        opening = "تمام، الحجز اتسجل"

    details = [
        appointment.get("service"),
        _appointment_device_name(appointment),
        appointment.get("doctor"),
        " ".join(part for part in (date_text, time_text) if part) or None,
        appointment.get("price"),
    ]
    suffix = "، ".join(str(item) for item in details if item)
    return opening + (f": {suffix}." if suffix else ".")


def _clock_ar(value: datetime) -> str:
    suffix = "ص" if value.hour < 12 else "م"
    hour = value.hour % 12 or 12
    if value.minute == 0:
        return f"{hour} {suffix}"
    return f"{hour}:{value.minute:02d} {suffix}"


def format_reschedule_success(appointment: dict[str, Any]) -> str:
    start: datetime | None = None
    try:
        start = datetime.fromisoformat(str(appointment.get("start_local")))
    except (TypeError, ValueError):
        pass

    doctor = str(appointment.get("doctor") or appointment.get("doctor_name") or "").strip()
    service = str(appointment.get("service") or appointment.get("service_name") or "").strip()
    device = _appointment_device_name(appointment) or ""
    details: list[str] = []
    if service:
        details.append(f"لـ{service}")
    if device:
        details.append(f"على {device}")
    if start is not None:
        details.append(f"ليوم {start.strftime('%d/%m/%Y')} الساعة {_clock_ar(start)}")
    if doctor:
        details.append(f"مع {doctor}")
    if not details:
        return "تمام، الموعد اتغيّر للميعاد الجديد بنجاح."
    return "تمام، الموعد اتغيّر " + " ".join(details) + "."


def format_handoff_reply(category: str) -> str:
    if category == "medical":
        return "الموضوع ده محتاج تقييم من الفريق الطبي، فحوّلت المحادثة لفريق العيادة للمراجعة."
    if category == "complaint":
        return "حوّلت المحادثة لفريق العيادة عشان يتابعوا الشكوى معاك."
    if category == "payment":
        return "حوّلت الموضوع لفريق العيادة عشان يراجعوا مسألة الدفع معاك."
    return "حوّلت المحادثة لفريق العيادة عشان يكملوا معاك مباشرة."


def reschedule_tool_args(*, current_appointment_id: str, slot: dict[str, Any]) -> dict[str, str]:
    device_key, _device_name = _slot_device(slot)
    set_laser_device_key(device_key)
    return {
        "appointment_id": current_appointment_id,
        "start_at": str(slot["start_local"]),
        "branch_id": str(slot.get("branch_id") or ""),
        "doctor_id": str(slot.get("doctor_id") or ""),
        "service_id": str(slot.get("service_id") or ""),
        "reason": "Customer selected a replacement slot in the active workflow.",
    }


def _display_date(value: object) -> str:
    if not value:
        return ""
    try:
        return datetime.fromisoformat(str(value)).strftime("%d/%m/%Y")
    except Exception:
        try:
            return datetime.fromisoformat(f"{value}T00:00:00").strftime("%d/%m/%Y")
        except Exception:
            return str(value)


def _numbered_names(items: object, *keys: str, limit: int = 5) -> list[str]:
    if not isinstance(items, list):
        return []
    values: list[str] = []
    for item in items[:limit]:
        if not isinstance(item, dict):
            continue
        value = next((item.get(key) for key in keys if item.get(key)), None)
        if value:
            values.append(str(value))
    return [f"{index}) {value}" for index, value in enumerate(values, start=1)]


def _format_slot_choices(output: dict[str, Any], *, reschedule: bool) -> str | None:
    return format_availability_windows_reply(
        output,
        reschedule=reschedule,
        booking_authorized=not reschedule,
    )


def format_verified_tool_fallback(tool_name: str, output: dict[str, Any]) -> str | None:
    if tool_name == "create_follow_up_task":
        if output.get("ok") is not True:
            return "معلش، مقدرتش أسجل المتابعة بالوقت ده. قولي وقت واضح تاني في المستقبل."
        due_text = _display_date(output.get("due_at"))
        return f"تمام، سجلت متابعة للفريق يوم {due_text}." if due_text else "تمام، سجلت متابعة للفريق."

    if tool_name not in {"get_booking_options", "get_reschedule_options"}:
        return None

    if output.get("ok") is not True:
        reason = str(output.get("reason") or "")
        messages = {
            "service_not_found": "ملقتش خدمة مطابقة للطلب في بيانات العيادة. قولي اسم الخدمة بشكل تاني.",
            "branch_not_found": "مقدرتش أكمل الحجز بالمعلومات الحالية. قولي الخدمة أو الدكتور اللي تقصده.",
            "doctor_not_found": "ملقتش الدكتور المطلوب متاح للخدمة والفرع دول. ممكن أقولك الدكاترة المتاحين.",
            "appointment_not_found": "ملقتش حجز قادم مطابق أقدر أغيّر ميعاده.",
            "no_active_branch": "الحجز مش متاح حاليًا. ممكن أساعدك في حاجة تانية.",
        }
        return messages.get(reason) or "معلش، مقدرتش أكمل البحث بالمعلومات الحالية. قولي التفاصيل المطلوبة تاني."

    if output.get("needs_service_choice"):
        choices = _numbered_names(output.get("services"), "service_name", "name")
        if choices:
            return "لقيت أكتر من خدمة مطابقة:\n" + "\n".join(choices) + "\nاختار الخدمة اللي تقصدها."

    if output.get("needs_laser_device_choice"):
        choices = _numbered_names(output.get("laser_devices"), "device_name", "name")
        if choices:
            return "اختار جهاز الليزر اللي تفضله:\n" + "\n".join(choices)

    if output.get("needs_branch_choice"):
        return "معلش، مقدرتش أكمل الحجز دلوقتي. جرّب تاني بعد لحظة."

    if output.get("needs_doctor_choice"):
        choices = _numbered_names(output.get("doctors"), "doctor_name", "name")
        if choices:
            return "لقيت أكتر من دكتور مطابق:\n" + "\n".join(choices) + "\nاختار الدكتور اللي تقصده."

    if output.get("needs_appointment_choice"):
        appointments = output.get("appointments")
        if isinstance(appointments, list):
            lines: list[str] = []
            for index, appointment in enumerate(appointments[:5], start=1):
                if not isinstance(appointment, dict):
                    continue
                service = str(appointment.get("service") or "").strip()
                date_text = _display_date(appointment.get("start_local"))
                details = " - ".join(part for part in (service, date_text) if part)
                if details:
                    lines.append(f"{index}) {details}")
            if lines:
                return "لقيت أكتر من حجز قادم:\n" + "\n".join(lines) + "\nاختار الحجز اللي عايز تغيّره."

    return _format_slot_choices(output, reschedule=tool_name == "get_reschedule_options")
