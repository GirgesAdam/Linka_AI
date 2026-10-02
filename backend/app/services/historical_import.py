from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import re
from collections import Counter, defaultdict
from collections.abc import Iterable
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Border, Font, Side
from openpyxl.worksheet.datavalidation import DataValidation
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.models.agent_action import AgentAction
from app.models.appointment import Appointment
from app.models.automation_job import AutomationJob
from app.models.clinic_inventory import ServiceDevicePrice
from app.models.crm_campaign_conversion import CRMCampaignConversion
from app.models.doctor import Doctor
from app.models.doctor_branch import DoctorBranch
from app.models.doctor_service import DoctorService
from app.models.historical_import import (
    ClinicHistoricalImportBatch,
    ClinicHistoricalImportLink,
    ClinicHistoricalImportRow,
)
from app.models.patient import Patient
from app.models.patient_package import PackageUsage, PatientPackage
from app.models.payment_transaction import PaymentAllocation, PaymentTransaction
from app.models.service import Service
from app.models.staff import Staff
from app.models.workspace import Workspace
from app.schemas.crm import normalize_patient_identity_phone
from app.schemas.historical_import import (
    HistoricalImportBatchRead,
    HistoricalImportDocument,
    HistoricalImportIssueGroup,
    HistoricalImportPreviewResponse,
)
from app.services.activity import record_activity_event

SCHEMA_VERSION = "tia_history_v1"
MAX_DOCUMENT_BYTES = 25 * 1024 * 1024
MAX_UPLOAD_BYTES = 60 * 1024 * 1024
MAX_IMPORT_ROWS = 250_000
RECOGNIZED_SHEETS = {
    "patients": "patient",
    "appointments": "appointment",
    "payments": "payment",
    "payment_allocations": "payment_allocation",
    "packages": "package",
    "active_packages": "package",
}
ACTIVE_PACKAGE_CONTRACT = "active_packages_v2"
ACTIVE_PACKAGE_HEADER_ALIASES = {
    "full_name": "full_name",
    "phone": "phone",
    "service_name": "service_name",
    "sessions_total": "sessions_total",
    "sessions_remaining": "sessions_remaining",
    "amount_paid": "amount_paid",
    "purchased_at": "purchased_at",
    "package_price": "package_price",
    "laser_device_name": "laser_device_name",
    "device_name": "laser_device_name",
    "اسم_العميل": "full_name",
    "رقم_الموبايل": "phone",
    "رقم_الهاتف": "phone",
    "اسم_الخدمة": "service_name",
    "عدد_الجلسات_الكلي": "sessions_total",
    "إجمالي_الجلسات": "sessions_total",
    "عدد_الجلسات_المتبقي": "sessions_remaining",
    "الجلسات_المتبقية": "sessions_remaining",
    "المبلغ_المدفوع": "amount_paid",
    "تاريخ_الشراء": "purchased_at",
    "سعر_الباقة": "package_price",
    "اسم_الجهاز": "laser_device_name",
}
EGYPT_TZ = ZoneInfo("Africa/Cairo")
VALID_APPOINTMENT_STATUSES = {
    "pending",
    "confirmed",
    "checked_in",
    "in_progress",
    "completed",
    "cancelled",
    "no_show",
    "rescheduled",
}
VALID_PAYMENT_METHODS = {"unknown", "cash", "card", "bank_transfer", "wallet", "online", "other"}
VALID_PACKAGE_STATUSES = {"active", "expired", "cancelled"}
VALID_PATIENT_SOURCES = {
    "whatsapp", "instagram", "facebook", "website", "referral", "walk_in",
    "campaign", "phone", "other",
}


class HistoricalImportError(ValueError):
    pass


class HistoricalImportConflictError(HistoricalImportError):
    pass


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    text = str(value).strip()
    return text or None


def _header(value: Any) -> str:
    text = _clean(value) or ""
    text = text.casefold().replace("-", "_").replace(" ", "_")
    text = re.sub(r"_+", "_", text)
    return text.strip("_")


def _header_for_sheet(sheet_key: str, value: Any) -> str:
    normalized = _header(value)
    if sheet_key == "active_packages":
        return ACTIVE_PACKAGE_HEADER_ALIASES.get(normalized, normalized)
    return normalized


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _payload_hash(payload: dict[str, Any]) -> str:
    return _digest(json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str, separators=(",", ":")))


def _decode(document: HistoricalImportDocument) -> bytes:
    try:
        return base64.b64decode(document.content_base64, validate=True)
    except Exception as exc:  # pragma: no cover - defensive boundary
        raise HistoricalImportError(f"{document.name}: invalid file encoding.") from exc




def _validate_documents(documents: list[HistoricalImportDocument]) -> None:
    seen_names: set[str] = set()
    total = 0
    for document in documents:
        key = document.name.casefold()
        if key in seen_names:
            raise HistoricalImportError(f"Duplicate upload filename: {document.name}.")
        seen_names.add(key)
        raw = _decode(document)
        size = len(raw)
        if size > MAX_DOCUMENT_BYTES:
            raise HistoricalImportError(f"{document.name}: file is too large for historical import.")
        total += size
        if total > MAX_UPLOAD_BYTES:
            raise HistoricalImportError("Historical import files are too large in total.")

def _source_fingerprint(documents: list[HistoricalImportDocument]) -> str:
    h = hashlib.sha256()
    for document in sorted(documents, key=lambda item: item.name.casefold()):
        raw = _decode(document)
        h.update(document.name.encode("utf-8"))
        h.update(document.format.encode("ascii"))
        h.update(raw)
    return h.hexdigest()


def _rows_from_xlsx(document: HistoricalImportDocument) -> Iterable[tuple[str, int, dict[str, Any]]]:
    raw = _decode(document)
    try:
        workbook = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    except Exception as exc:
        raise HistoricalImportError(f"{document.name}: unreadable Excel workbook.") from exc
    try:
        for sheet in workbook.worksheets:
            key = _header(sheet.title)
            if key not in RECOGNIZED_SHEETS:
                continue
            iterator = sheet.iter_rows(values_only=True)
            try:
                headers = [_header_for_sheet(key, cell) for cell in next(iterator)]
            except StopIteration:
                continue
            if not any(headers):
                continue
            for row_number, values in enumerate(iterator, start=2):
                if not any(_clean(value) is not None for value in values):
                    continue
                row = {headers[index]: values[index] for index in range(min(len(headers), len(values))) if headers[index]}
                yield key, row_number, row
    finally:
        workbook.close()


def _rows_from_csv(document: HistoricalImportDocument) -> Iterable[tuple[str, int, dict[str, Any]]]:
    key = _header(Path(document.name).stem)
    if key not in RECOGNIZED_SHEETS:
        raise HistoricalImportError(
            f"{document.name}: CSV filename must be one of {', '.join(sorted(RECOGNIZED_SHEETS))}."
        )
    raw = _decode(document)
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise HistoricalImportError(f"{document.name}: CSV must be UTF-8.") from exc
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return
    headers = {
        _header_for_sheet(key, name): name
        for name in reader.fieldnames
        if _header_for_sheet(key, name)
    }
    for row_number, raw_row in enumerate(reader, start=2):
        row = {key_name: raw_row.get(original) for key_name, original in headers.items()}
        if any(_clean(value) is not None for value in row.values()):
            yield key, row_number, row


def _iter_rows(documents: list[HistoricalImportDocument]) -> Iterable[tuple[str, str, int, dict[str, Any]]]:
    recognized = False
    for document in documents:
        iterator = _rows_from_xlsx(document) if document.format == "xlsx" else _rows_from_csv(document)
        for sheet, row_number, row in iterator:
            recognized = True
            yield document.name, sheet, row_number, row
    if not recognized:
        raise HistoricalImportError(
            "No recognized historical-data sheet was found. Use the Linka Active Packages Import template or a supported legacy historical sheet."
        )


def _parse_uuid(value: Any) -> UUID | None:
    text = _clean(value)
    if not text:
        return None
    try:
        return UUID(text)
    except ValueError:
        return None


def _parse_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = _clean(value)
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        return None


def _parse_time(value: Any) -> time | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.time().replace(tzinfo=None)
    if isinstance(value, time):
        return value.replace(tzinfo=None)
    if isinstance(value, (int, float)):
        fraction = float(value) % 1
        seconds = round(fraction * 24 * 3600)
        return time(hour=(seconds // 3600) % 24, minute=(seconds % 3600) // 60, second=seconds % 60)
    text = (_clean(value) or "").strip()
    if not text:
        return None
    for fmt in ("%H:%M", "%H:%M:%S", "%I:%M %p", "%I:%M%p"):
        try:
            return datetime.strptime(text.upper(), fmt).time()
        except ValueError:
            pass
    return None


def _parse_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        dt = datetime.combine(value, time.min)
    else:
        text = _clean(value)
        if not text:
            return None
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            parsed_date = _parse_date(text)
            if parsed_date is None:
                return None
            dt = datetime.combine(parsed_date, time.min)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=EGYPT_TZ)
    return dt.astimezone(UTC)


def _money_minor(value: Any, *, allow_negative: bool = True) -> int | None:
    text = _clean(value)
    if text is None:
        return None
    cleaned = text.replace(",", "").replace("ج.م", "").replace("EGP", "").strip()
    try:
        amount = Decimal(cleaned)
    except InvalidOperation:
        return None
    if not allow_negative and amount < 0:
        return None
    return int((amount * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _int_value(value: Any) -> int | None:
    text = _clean(value)
    if text is None:
        return None
    try:
        number = Decimal(text)
    except InvalidOperation:
        return None
    if number != number.to_integral_value():
        return None
    return int(number)


def _name_parts(full_name: str | None) -> tuple[str, str | None]:
    text = " ".join((full_name or "عميل مستورد").split()) or "عميل مستورد"
    parts = text.split(" ", 1)
    return parts[0][:120], parts[1][:120] if len(parts) > 1 else None


def _patient_identity(row: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    patient_id = _clean(row.get("patient_id"))
    phone_display, phone_normalized = normalize_patient_identity_phone(_clean(row.get("phone") or row.get("patient_phone")))
    if patient_id:
        return f"id:{patient_id}", phone_display, phone_normalized
    if phone_normalized:
        return f"phone:{_digest(phone_normalized)[:24]}", phone_display, phone_normalized
    return None, phone_display, phone_normalized


def _source_id(entity_type: str, explicit: str | None, fallback: str) -> str:
    if explicit:
        return f"{entity_type}:id:{explicit}"
    return f"{entity_type}:auto:{_digest(fallback)[:32]}"


def _service_catalog(db: Session, workspace_id: UUID) -> tuple[dict[UUID, Service], dict[str, Service]]:
    rows = list(db.scalars(select(Service).where(Service.workspace_id == workspace_id, Service.is_active.is_(True))))
    return {row.id: row for row in rows}, {row.name.strip().casefold(): row for row in rows}


def _active_device_price_catalog(
    db: Session,
    workspace_id: UUID,
) -> dict[UUID, dict[str, ServiceDevicePrice]]:
    rows = list(
        db.scalars(
            select(ServiceDevicePrice).where(
                ServiceDevicePrice.workspace_id == workspace_id,
                ServiceDevicePrice.is_active.is_(True),
                ServiceDevicePrice.price_minor.is_not(None),
            )
        )
    )
    result: dict[UUID, dict[str, ServiceDevicePrice]] = defaultdict(dict)
    for row in rows:
        result[row.service_id][row.device_name.strip().casefold()] = row
    return result


def _resolve_service(row: dict[str, Any], by_id: dict[UUID, Service], by_name: dict[str, Service]) -> Service | None:
    service_uuid = _parse_uuid(row.get("service_id"))
    if service_uuid and service_uuid in by_id:
        return by_id[service_uuid]
    name = _clean(row.get("service_name"))
    if name:
        return by_name.get(name.casefold())
    return None


def _normalize_patient(row: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None, str | None]:
    identity, phone_display, phone_normalized = _patient_identity(row)
    if not identity:
        return None, "patient_identity_missing", "Patient requires patient_id or phone."
    birth = _parse_date(row.get("birth_date") or row.get("date_of_birth"))
    source = (_clean(row.get("source")) or "other").casefold()
    if source not in VALID_PATIENT_SOURCES:
        source = "other"
    payload = {
        "identity": identity,
        "patient_id": _clean(row.get("patient_id")),
        "full_name": _clean(row.get("full_name") or row.get("patient_name")),
        "phone": phone_display,
        "phone_normalized": phone_normalized,
        "birth_date": birth.isoformat() if birth else None,
        "source": source,
    }
    return payload, None, None


def _normalize_appointment(
    row: dict[str, Any],
    *,
    services_by_id: dict[UUID, Service],
    services_by_name: dict[str, Service],
) -> tuple[dict[str, Any] | None, str | None, str | None]:
    identity, phone_display, phone_normalized = _patient_identity(row)
    if not identity:
        return None, "appointment_patient_identity_missing", "Appointment requires patient_id or patient_phone."
    service = _resolve_service(row, services_by_id, services_by_name)
    if service is None:
        return None, "appointment_service_unknown", "Appointment service must match a configured Linka service."
    day = _parse_date(row.get("date") or row.get("appointment_date"))
    start_time = _parse_time(row.get("start_time") or row.get("time"))
    if day is None:
        return None, "appointment_date_invalid", "Appointment date is missing or invalid."
    if start_time is None:
        return None, "appointment_time_invalid", "Appointment start_time is missing or invalid."
    status = (_clean(row.get("status")) or "").casefold().replace(" ", "_")
    if not status:
        local_start = datetime.combine(day, start_time, tzinfo=EGYPT_TZ)
        status = "completed" if local_start <= datetime.now(EGYPT_TZ) else "pending"
    if status not in VALID_APPOINTMENT_STATUSES:
        return None, "appointment_status_invalid", "Appointment status is not a supported Linka status."
    explicit = _clean(row.get("appointment_id"))
    fallback = "|".join([
        identity,
        str(service.id),
        day.isoformat(),
        start_time.isoformat(),
        _clean(row.get("doctor_id")) or _clean(row.get("doctor_name")) or "unassigned",
    ])
    payload = {
        "identity": identity,
        "patient_id": _clean(row.get("patient_id")),
        "patient_phone": phone_display,
        "patient_phone_normalized": phone_normalized,
        "patient_name": _clean(row.get("patient_name") or row.get("full_name")),
        "service_id": str(service.id),
        "service_name": service.name,
        "doctor_id": _clean(row.get("doctor_id")),
        "doctor_name": _clean(row.get("doctor_name")),
        "date": day.isoformat(),
        "start_time": start_time.isoformat(),
        "status": status,
        "package_id": _clean(row.get("package_id")),
        "appointment_id": explicit,
    }
    payload["source_record_id"] = _source_id("appointment", explicit, fallback)
    return payload, None, None


def _normalize_payment(row: dict[str, Any], *, source_file: str, sheet: str, row_number: int) -> tuple[dict[str, Any] | None, str | None, str | None]:
    identity, phone_display, phone_normalized = _patient_identity(row)
    if not identity:
        return None, "payment_patient_identity_missing", "Payment requires patient_id or patient_phone."
    amount_minor = _money_minor(row.get("amount"))
    if amount_minor is None or amount_minor == 0:
        return None, "payment_amount_invalid", "Payment amount must be a non-zero number; refunds use a negative amount."
    paid_at = _parse_datetime(row.get("paid_at") or row.get("date"))
    if paid_at is None:
        return None, "payment_date_invalid", "Payment paid_at is missing or invalid."
    method = (_clean(row.get("payment_method")) or "unknown").casefold().replace(" ", "_")
    if method not in VALID_PAYMENT_METHODS:
        method = "other"
    explicit = _clean(row.get("transaction_id"))
    fallback = f"{source_file}|{sheet}|{row_number}"
    payload = {
        "identity": identity,
        "patient_id": _clean(row.get("patient_id")),
        "patient_phone": phone_display,
        "patient_phone_normalized": phone_normalized,
        "transaction_id": explicit,
        "amount_minor": amount_minor,
        "paid_at": paid_at.isoformat(),
        "payment_method": method,
        "appointment_id": _clean(row.get("appointment_id")),
        "package_id": _clean(row.get("package_id")),
        "reference_transaction_id": _clean(row.get("reference_transaction_id")),
    }
    payload["source_record_id"] = _source_id("payment", explicit, fallback)
    return payload, None, None


def _normalize_allocation(row: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None, str | None]:
    transaction_id = _clean(row.get("transaction_id"))
    appointment_id = _clean(row.get("appointment_id"))
    amount_minor = _money_minor(row.get("amount"), allow_negative=False)
    if not transaction_id or not appointment_id:
        return None, "allocation_reference_missing", "Payment allocation requires transaction_id and appointment_id."
    if amount_minor is None or amount_minor <= 0:
        return None, "allocation_amount_invalid", "Payment allocation amount must be positive."
    return {
        "transaction_id": transaction_id,
        "appointment_id": appointment_id,
        "amount_minor": amount_minor,
        "source_record_id": _source_id("payment_allocation", None, f"{transaction_id}|{appointment_id}"),
    }, None, None


def _normalize_package(
    row: dict[str, Any],
    *,
    services_by_id: dict[UUID, Service],
    services_by_name: dict[str, Service],
) -> tuple[dict[str, Any] | None, str | None, str | None]:
    identity, phone_display, phone_normalized = _patient_identity(row)
    if not identity:
        return None, "package_patient_identity_missing", "Package requires patient_id or patient_phone."
    service = _resolve_service(row, services_by_id, services_by_name)
    if service is None:
        return None, "package_service_unknown", "Package service must match a configured Linka service."
    remaining = _int_value(row.get("sessions_remaining"))
    if remaining is None or remaining < 0:
        return None, "package_remaining_invalid", "Package sessions_remaining must be zero or greater."
    total = _int_value(row.get("sessions_total"))
    if total is not None and (total <= 0 or remaining > total):
        return None, "package_total_invalid", "sessions_total must be positive and not less than sessions_remaining."
    price_minor = _money_minor(row.get("price"), allow_negative=False)
    standalone_minor = _money_minor(row.get("standalone_session_price"), allow_negative=False)
    if _clean(row.get("price")) is not None and price_minor is None:
        return None, "package_price_invalid", "Package price must be a valid non-negative number."
    if _clean(row.get("standalone_session_price")) is not None and standalone_minor is None:
        return None, "package_standalone_price_invalid", "standalone_session_price must be a valid non-negative number."
    purchased_at = _parse_datetime(row.get("purchased_at")) or datetime.now(UTC)
    expires_at = _parse_date(row.get("expires_at"))
    status = (_clean(row.get("status")) or "active").casefold().replace(" ", "_")
    if status not in VALID_PACKAGE_STATUSES:
        return None, "package_status_invalid", "Package status is not supported."
    explicit = _clean(row.get("package_id"))
    fallback = f"{identity}|{service.id}|{_clean(row.get('package_name')) or service.name}|{purchased_at.date().isoformat()}"
    payload = {
        "identity": identity,
        "patient_id": _clean(row.get("patient_id")),
        "patient_phone": phone_display,
        "patient_phone_normalized": phone_normalized,
        "package_id": explicit,
        "package_name": _clean(row.get("package_name")) or f"{service.name} Package",
        "service_id": str(service.id),
        "service_name": service.name,
        "sessions_total": total,
        "sessions_remaining": remaining,
        "price_minor": price_minor or 0,
        "standalone_session_price_minor": standalone_minor,
        "purchased_at": purchased_at.isoformat(),
        "expires_at": expires_at.isoformat() if expires_at else None,
        "status": status,
    }
    payload["source_record_id"] = _source_id("package", explicit, fallback)
    return payload, None, None




def _active_package_payment_source_id(package_source_record_id: str) -> str:
    return f"payment:active_package:{_digest(package_source_record_id)[:32]}"


def _active_package_fact_hash(payload: dict[str, Any]) -> str:
    """Hash package facts without treating CRM presentation name as package identity."""
    stable_payload = dict(payload)
    stable_payload.pop("full_name", None)
    stable_payload.pop("patient_name", None)
    # This snapshot is derived from Linka pricing configuration, not from the
    # clinic's historical workbook. Price changes must not make the same import
    # row look like a different historical fact.
    stable_payload.pop("standalone_session_price_minor", None)
    return _payload_hash(stable_payload)


def _active_package_payment_payload(
    payload: dict[str, Any],
    *,
    package_source_record_id: str,
) -> dict[str, Any]:
    return {
        "identity": payload["identity"],
        "amount_minor": int(payload["historical_payment_amount_minor"]),
        "paid_at": payload["purchased_at"],
        "payment_method": "unknown",
        "package_source_record_id": package_source_record_id,
        "import_contract": ACTIVE_PACKAGE_CONTRACT,
    }


_EGYPTIAN_MOBILE_LOCAL_RE = re.compile(r"^01[0125]\d{8}$")
_EGYPTIAN_MOBILE_WITHOUT_ZERO_RE = re.compile(r"^1[0125]\d{8}$")


def _normalize_active_package_egypt_phone(value: Any) -> tuple[str, str]:
    """Normalize clinic-facing active-package phones to one Egyptian mobile identity.

    Excel commonly converts a value such as 01012345678 into the numeric
    1012345678. That representation is repaired deterministically here.
    """
    if isinstance(value, bool) or value is None:
        raise ValueError("Invalid Egyptian mobile number.")
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not value.is_integer():
            raise ValueError("Invalid Egyptian mobile number.")
        raw = str(int(value))
    else:
        raw = _clean(value) or ""

    compact = re.sub(r"[\s().-]", "", raw)
    if compact.startswith("+"):
        compact = compact[1:]
    if compact.startswith("0020"):
        compact = compact[4:]
    elif compact.startswith("20"):
        compact = compact[2:]

    if _EGYPTIAN_MOBILE_WITHOUT_ZERO_RE.fullmatch(compact):
        compact = f"0{compact}"

    if not _EGYPTIAN_MOBILE_LOCAL_RE.fullmatch(compact):
        raise ValueError("Invalid Egyptian mobile number.")

    return compact, f"+20{compact[1:]}"


def _parse_active_package_purchase_date(value: Any) -> date | None:
    """Parse an Egypt-facing package purchase date without US month/day ambiguity."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float, bool)):
        # Proper Excel date cells are returned by openpyxl as datetime/date.
        # A raw number is ambiguous and should not silently become a date.
        return None

    text = (_clean(value) or "").strip()
    if not text:
        return None

    for fmt in (
        "%Y-%m-%d",
        "%Y/%m/%d",
        "%Y.%m.%d",
        "%d/%m/%Y",
        "%d-%m-%Y",
        "%d.%m.%Y",
    ):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass

    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        return None


def _normalize_active_package(
    row: dict[str, Any],
    *,
    services_by_id: dict[UUID, Service],
    services_by_name: dict[str, Service],
    device_prices_by_service_id: dict[UUID, dict[str, ServiceDevicePrice]] | None = None,
) -> tuple[dict[str, Any] | None, str | None, str | None]:
    device_prices_by_service_id = device_prices_by_service_id or {}
    full_name = _clean(row.get("full_name"))
    if not full_name:
        return None, "active_package_name_missing", "full_name is required."

    try:
        phone_display, phone_normalized = _normalize_active_package_egypt_phone(row.get("phone"))
    except ValueError:
        return (
            None,
            "active_package_phone_invalid",
            "رقم الهاتف يجب أن يكون موبايل مصري صحيح (010/011/012/015). لو Excel حذف الصفر الأول، Linka تصلحه تلقائيًا.",
        )
    identity = f"phone:{_digest(phone_normalized)[:24]}"

    service_name = _clean(row.get("service_name"))
    matching_services = [
        service
        for service in services_by_id.values()
        if service_name
        and _clean(service.name)
        and _clean(service.name).casefold() == service_name.casefold()
    ]
    if not matching_services:
        return (
            None,
            "active_package_service_unknown",
            "Unknown service. Use one of the active Linka service names.",
        )
    if len(matching_services) > 1:
        return (
            None,
            "active_package_service_ambiguous",
            "service_name matches more than one active Linka service. Rename the services before importing.",
        )
    service = matching_services[0]

    device_name = _clean(row.get("laser_device_name"))
    laser_device_key: str | None = None
    laser_device_name: str | None = None
    standalone_session_price_minor = int(service.price_minor)
    if bool(getattr(service, "requires_laser_device", False)):
        configured_devices = device_prices_by_service_id.get(service.id, {})
        if not configured_devices:
            return (
                None,
                "active_package_laser_device_unconfigured",
                "خدمة الليزر دي محتاجة جهاز، لكن مفيش جهاز متسعر ومفعل عليها في Linka.",
            )
        if not device_name:
            return (
                None,
                "active_package_laser_device_missing",
                "اختر اسم الجهاز لخدمة الليزر من القائمة الموجودة في ملف Excel.",
            )
        device_price = configured_devices.get(device_name.casefold())
        if device_price is None:
            return (
                None,
                "active_package_laser_device_invalid",
                "الجهاز المختار غير متاح للخدمة دي. اختر جهازًا من القائمة الخاصة بالخدمة.",
            )
        laser_device_key = device_price.device_key
        laser_device_name = device_price.device_name
        standalone_session_price_minor = int(device_price.price_minor or 0)
    elif device_name:
        return (
            None,
            "active_package_device_not_allowed",
            "اترك اسم الجهاز فارغًا لأن الخدمة المختارة ليست خدمة ليزر.",
        )

    total = _int_value(row.get("sessions_total"))
    if total is None or total <= 0:
        return None, "active_package_total_invalid", "sessions_total must be a positive whole number."
    remaining = _int_value(row.get("sessions_remaining"))
    if remaining is None or remaining < 0:
        return None, "active_package_remaining_invalid", "sessions_remaining must be zero or greater."
    if remaining > total:
        return (
            None,
            "active_package_remaining_exceeds_total",
            "sessions_remaining cannot exceed sessions_total.",
        )

    amount_paid_minor = _money_minor(row.get("amount_paid"), allow_negative=False)
    if amount_paid_minor is None or amount_paid_minor <= 0:
        return None, "active_package_amount_paid_invalid", "amount_paid must be positive."

    package_price_raw = _clean(row.get("package_price"))
    package_price_minor = (
        amount_paid_minor
        if package_price_raw is None
        else _money_minor(row.get("package_price"), allow_negative=False)
    )
    if package_price_minor is None:
        return None, "active_package_price_invalid", "package_price must be a valid non-negative amount."
    if package_price_minor < amount_paid_minor:
        return (
            None,
            "active_package_price_below_paid",
            "package_price cannot be less than amount_paid.",
        )

    purchased_date = _parse_active_package_purchase_date(row.get("purchased_at"))
    if purchased_date is None:
        return (
            None,
            "active_package_purchase_date_invalid",
            "تاريخ الشراء غير صالح. استخدم تاريخ Excel أو DD/MM/YYYY أو YYYY-MM-DD.",
        )
    purchased_at = datetime.combine(purchased_date, time.min, tzinfo=EGYPT_TZ).astimezone(UTC)

    identity_facts = "|".join(
        [
            identity,
            str(service.id),
            laser_device_key or "",
            purchased_date.isoformat(),
            str(total),
            str(remaining),
            str(package_price_minor),
            str(amount_paid_minor),
        ]
    )
    source_record_id = _source_id("package", None, f"active_package|{identity_facts}")
    payload = {
        "identity": identity,
        "patient_id": None,
        "patient_phone": phone_display,
        "patient_phone_normalized": phone_normalized,
        "full_name": full_name,
        "patient_name": full_name,
        "package_id": None,
        "package_name": f"{service.name} Package",
        "service_id": str(service.id),
        "service_name": service.name,
        "laser_device_key": laser_device_key,
        "laser_device_name": laser_device_name,
        "sessions_total": total,
        "sessions_remaining": remaining,
        "price_minor": package_price_minor,
        "standalone_session_price_minor": standalone_session_price_minor,
        "purchased_at": purchased_at.isoformat(),
        "expires_at": None,
        "status": "active",
        "historical_payment_amount_minor": amount_paid_minor,
        "import_contract": ACTIVE_PACKAGE_CONTRACT,
        "source_record_id": source_record_id,
    }
    return payload, None, None


def _normalize_row(
    entity_type: str,
    row: dict[str, Any],
    *,
    source_file: str,
    sheet: str,
    row_number: int,
    services_by_id: dict[UUID, Service],
    services_by_name: dict[str, Service],
    device_prices_by_service_id: dict[UUID, dict[str, ServiceDevicePrice]],
) -> tuple[dict[str, Any] | None, str | None, str | None]:
    if entity_type == "patient":
        payload, code, message = _normalize_patient(row)
        if payload is not None:
            payload["source_record_id"] = _source_id("patient", payload.get("patient_id"), payload["identity"])
        return payload, code, message
    if entity_type == "appointment":
        return _normalize_appointment(row, services_by_id=services_by_id, services_by_name=services_by_name)
    if entity_type == "payment":
        return _normalize_payment(row, source_file=source_file, sheet=sheet, row_number=row_number)
    if entity_type == "payment_allocation":
        return _normalize_allocation(row)
    if entity_type == "package":
        if sheet == "active_packages":
            return _normalize_active_package(
                row,
                services_by_id=services_by_id,
                services_by_name=services_by_name,
                device_prices_by_service_id=device_prices_by_service_id,
            )
        return _normalize_package(row, services_by_id=services_by_id, services_by_name=services_by_name)
    raise HistoricalImportError(f"Unsupported entity type: {entity_type}")


def _batch_read(batch: ClinicHistoricalImportBatch) -> HistoricalImportBatchRead:
    return HistoricalImportBatchRead(
        batch_id=batch.id,
        mode=batch.mode,
        status=batch.status,
        schema_version=batch.schema_version,
        source_name=batch.source_name,
        summary=batch.summary_json or {},
        error_message=batch.error_message,
    )


def preview_historical_import(
    db: Session,
    *,
    workspace: Workspace,
    user_id: UUID,
    documents: list[HistoricalImportDocument],
    mode: str,
) -> HistoricalImportPreviewResponse:
    if workspace.primary_branch_id is None:
        raise HistoricalImportError("Complete Clinic Setup before importing historical data.")
    services_by_id, services_by_name = _service_catalog(db, workspace.id)
    if not services_by_id:
        raise HistoricalImportError("Add at least one service before importing historical data.")
    device_prices_by_service_id = _active_device_price_catalog(db, workspace.id)

    _validate_documents(documents)
    fingerprint = _source_fingerprint(documents)
    batch = ClinicHistoricalImportBatch(
        workspace_id=workspace.id,
        created_by_user_id=user_id,
        mode=mode,
        status="preview_ready",
        schema_version=SCHEMA_VERSION,
        source_name=", ".join(document.name for document in documents)[:255],
        source_fingerprint=fingerprint,
        summary_json={},
    )
    db.add(batch)
    db.flush()

    ready_counts: Counter[str] = Counter()
    rejected_counts: Counter[str] = Counter()
    issue_counts: dict[tuple[str, str, str], dict[str, Any]] = {}
    seen: dict[tuple[str, str], str] = {}
    rows_to_add: list[ClinicHistoricalImportRow] = []

    processed_rows = 0
    for source_file, sheet, row_number, raw_row in _iter_rows(documents):
        processed_rows += 1
        if processed_rows > MAX_IMPORT_ROWS:
            db.rollback()
            raise HistoricalImportError(f"Historical import exceeds the {MAX_IMPORT_ROWS:,}-row limit.")
        entity_type = RECOGNIZED_SHEETS[sheet]
        payload, issue_code, issue_message = _normalize_row(
            entity_type,
            raw_row,
            source_file=source_file,
            sheet=sheet,
            row_number=row_number,
            services_by_id=services_by_id,
            services_by_name=services_by_name,
            device_prices_by_service_id=device_prices_by_service_id,
        )
        if payload is None:
            source_record_id = f"{entity_type}:rejected:{_digest(f'{source_file}|{sheet}|{row_number}')[:32]}"
            payload = {"raw": {key: _clean(value) for key, value in raw_row.items()}}
            row_status = "rejected"
        else:
            source_record_id = str(payload.pop("source_record_id"))
            content_hash = (
                _active_package_fact_hash(payload)
                if payload.get("import_contract") == ACTIVE_PACKAGE_CONTRACT
                else _payload_hash(payload)
            )
            key = (entity_type, source_record_id)
            if key in seen:
                if payload.get("import_contract") == ACTIVE_PACKAGE_CONTRACT:
                    issue_code = "duplicate_historical_package_row"
                    issue_message = "Duplicate historical package row."
                else:
                    issue_code = f"duplicate_{entity_type}_record"
                    issue_message = f"Duplicate {entity_type} identity in the same import file."
                duplicate_source = _digest(source_file.casefold())[:10]
                source_record_id = (
                    f"{source_record_id}:duplicate:{duplicate_source}:{row_number}"
                )
                row_status = "rejected"
            else:
                seen[key] = content_hash
                row_status = "ready"
        payload_hash = (
            _active_package_fact_hash(payload)
            if payload.get("import_contract") == ACTIVE_PACKAGE_CONTRACT
            else _payload_hash(payload)
        )
        if row_status == "ready":
            ready_counts[entity_type] += 1
        else:
            rejected_counts[entity_type] += 1
            issue_key = (entity_type, issue_code or "invalid_row", issue_message or "Invalid row")
            group = issue_counts.setdefault(issue_key, {"count": 0, "rows": []})
            group["count"] += 1
            if len(group["rows"]) < 5:
                group["rows"].append(row_number)
        rows_to_add.append(
            ClinicHistoricalImportRow(
                workspace_id=workspace.id,
                batch_id=batch.id,
                entity_type=entity_type,
                source_sheet=sheet,
                row_number=row_number,
                source_record_id=source_record_id,
                payload_hash=payload_hash,
                row_status=row_status,
                normalized_json=payload,
                issue_code=issue_code,
                issue_message=issue_message,
            )
        )

    # Allocation references must point to explicit transaction/appointment IDs in the same
    # upload or to a previously imported source record. The contract does not guess joins.
    explicit_tx = {
        row.normalized_json.get("transaction_id"): int(row.normalized_json.get("amount_minor") or 0)
        for row in rows_to_add
        if row.entity_type == "payment" and row.row_status == "ready" and row.normalized_json.get("transaction_id")
    }
    explicit_appt = {
        row.normalized_json.get("appointment_id")
        for row in rows_to_add
        if row.entity_type == "appointment" and row.row_status == "ready" and row.normalized_json.get("appointment_id")
    }
    allocation_totals: Counter[str] = Counter()
    for staged in rows_to_add:
        if staged.entity_type != "payment_allocation" or staged.row_status != "ready":
            continue
        tx_id = staged.normalized_json.get("transaction_id")
        appt_id = staged.normalized_json.get("appointment_id")
        issue = None
        if tx_id not in explicit_tx or appt_id not in explicit_appt:
            issue = (
                "allocation_reference_unknown",
                "Allocation references must match explicit transaction_id and appointment_id rows in this upload.",
            )
        elif explicit_tx[tx_id] <= 0:
            issue = (
                "allocation_refund_not_supported",
                "Payment allocations can only allocate positive payment transactions, not refunds.",
            )
        else:
            allocation_totals[tx_id] += int(staged.normalized_json.get("amount_minor") or 0)
            if allocation_totals[tx_id] > explicit_tx[tx_id]:
                issue = (
                    "allocation_exceeds_payment",
                    "Payment allocations cannot exceed the referenced payment amount.",
                )
        if issue is None:
            continue
        staged.row_status = "rejected"
        staged.issue_code, staged.issue_message = issue
        ready_counts["payment_allocation"] -= 1
        rejected_counts["payment_allocation"] += 1
        issue_key = ("payment_allocation", staged.issue_code, staged.issue_message)
        group = issue_counts.setdefault(issue_key, {"count": 0, "rows": []})
        group["count"] += 1
        if len(group["rows"]) < 5:
            group["rows"].append(staged.row_number)

    active_ready_rows = [
        staged
        for staged in rows_to_add
        if staged.entity_type == "package"
        and staged.row_status == "ready"
        and staged.normalized_json.get("import_contract") == ACTIVE_PACKAGE_CONTRACT
    ]
    projected_counts: dict[str, int] = {}
    total_amount_imported_minor = 0
    if active_ready_rows:
        patient_phones = {
            str(staged.normalized_json["patient_phone_normalized"])
            for staged in active_ready_rows
            if staged.normalized_json.get("patient_phone_normalized")
        }
        existing_patient_phones = set(
            db.scalars(
                select(Patient.phone_normalized).where(
                    Patient.workspace_id == workspace.id,
                    Patient.phone_normalized.in_(patient_phones),
                )
            ).all()
        )
        package_source_ids = {staged.source_record_id for staged in active_ready_rows}
        existing_package_sources = set(
            db.scalars(
                select(ClinicHistoricalImportLink.source_record_id).where(
                    ClinicHistoricalImportLink.workspace_id == workspace.id,
                    ClinicHistoricalImportLink.entity_type == "package",
                    ClinicHistoricalImportLink.source_record_id.in_(package_source_ids),
                )
            ).all()
        )
        new_active_rows = [
            staged
            for staged in active_ready_rows
            if staged.source_record_id not in existing_package_sources
        ]
        projected_counts = {
            "patients_to_create": len(patient_phones - existing_patient_phones),
            "existing_patients_reused": len(patient_phones & existing_patient_phones),
            "packages_to_create": len(new_active_rows),
            "historical_payments_to_create": len(new_active_rows),
        }
        total_amount_imported_minor = sum(
            int(staged.normalized_json.get("historical_payment_amount_minor") or 0)
            for staged in new_active_rows
        )

    db.add_all(rows_to_add)
    issue_groups = [
        HistoricalImportIssueGroup(
            entity_type=entity_type,
            code=code,
            message=message,
            occurrence_count=meta["count"],
            example_rows=meta["rows"],
        )
        for (entity_type, code, message), meta in sorted(issue_counts.items(), key=lambda item: (-item[1]["count"], item[0]))
    ]
    batch.summary_json = {
        "ready_counts": dict(ready_counts),
        "rejected_counts": dict(rejected_counts),
        "issues": [group.model_dump() for group in issue_groups],
        "source_fingerprint": fingerprint,
        "projected_counts": projected_counts,
        "total_amount_imported_minor": total_amount_imported_minor,
    }
    db.commit()
    db.refresh(batch)
    return HistoricalImportPreviewResponse(
        batch=_batch_read(batch),
        ready_counts=dict(ready_counts),
        rejected_counts=dict(rejected_counts),
        issue_groups=issue_groups,
        can_import=sum(ready_counts.values()) > 0,
        projected_counts=projected_counts,
        total_amount_imported_minor=total_amount_imported_minor,
    )


def get_historical_import_batch(db: Session, *, workspace_id: UUID, batch_id: UUID) -> ClinicHistoricalImportBatch | None:
    return db.scalar(
        select(ClinicHistoricalImportBatch).where(
            ClinicHistoricalImportBatch.workspace_id == workspace_id,
            ClinicHistoricalImportBatch.id == batch_id,
        )
    )


def list_historical_import_batches(db: Session, *, workspace_id: UUID, limit: int = 20) -> list[ClinicHistoricalImportBatch]:
    return list(
        db.scalars(
            select(ClinicHistoricalImportBatch)
            .where(ClinicHistoricalImportBatch.workspace_id == workspace_id)
            .order_by(ClinicHistoricalImportBatch.created_at.desc())
            .limit(limit)
        )
    )


def start_historical_import(db: Session, *, batch: ClinicHistoricalImportBatch) -> ClinicHistoricalImportBatch:
    if batch.status == "imported":
        return batch
    if batch.status == "importing":
        raise HistoricalImportConflictError("This historical import is already running.")
    if batch.status not in {"preview_ready", "failed"}:
        raise HistoricalImportConflictError("This historical import cannot be started from its current state.")
    batch.status = "importing"
    batch.error_message = None
    record_activity_event(
        db, workspace_id=batch.workspace_id, actor_type="staff", actor_user_id=batch.created_by_user_id,
        action="clinic.history_import_started", entity_type="historical_import", entity_id=batch.id,
        summary="Historical clinic import started.", metadata={"mode": batch.mode}, flush=False,
    )
    db.commit()
    db.refresh(batch)
    return batch


def mark_historical_import_failed(db: Session, *, batch_id: UUID, workspace_id: UUID, message: str) -> None:
    batch = get_historical_import_batch(db, workspace_id=workspace_id, batch_id=batch_id)
    if batch is None:
        return
    batch.status = "failed"
    batch.error_message = message[:1200]
    record_activity_event(
        db, workspace_id=batch.workspace_id, actor_type="system", actor_user_id=None,
        action="clinic.history_import_failed", entity_type="historical_import", entity_id=batch.id,
        summary="Historical clinic import failed.", metadata={"mode": batch.mode}, flush=False,
    )
    db.commit()


def _existing_link_map(db: Session, workspace_id: UUID) -> dict[tuple[str, str], ClinicHistoricalImportLink]:
    return {
        (row.entity_type, row.source_record_id): row
        for row in db.scalars(
            select(ClinicHistoricalImportLink).where(ClinicHistoricalImportLink.workspace_id == workspace_id)
        )
    }


def _assign_patient_phone_if_available(
    db: Session,
    *,
    workspace_id: UUID,
    patient: Patient,
    phone: str | None,
    phone_normalized: str | None,
) -> bool:
    """Assign a canonical patient phone only when it is not owned by another patient.

    Historical exports can disagree about which stable patient id owns a phone number.
    The database unique constraint is the final identity safety boundary, so Append/Replace
    must never steal a normalized phone from another canonical patient.
    """
    if not phone_normalized:
        return False
    owner = db.scalar(
        select(Patient).where(
            Patient.workspace_id == workspace_id,
            Patient.phone_normalized == phone_normalized,
        )
    )
    if owner is not None and owner.id != patient.id:
        return False
    if phone:
        patient.phone = phone
    patient.phone_normalized = phone_normalized
    return True


def _patient_for_identity(
    db: Session,
    *,
    workspace: Workspace,
    identity: str,
    payload: dict[str, Any],
    cache: dict[str, Patient],
) -> Patient:
    if identity in cache:
        patient = cache[identity]
        # Fill a missing phone only when it is not already owned by another canonical patient.
        if not patient.phone_normalized and payload.get("patient_phone_normalized"):
            _assign_patient_phone_if_available(
                db,
                workspace_id=workspace.id,
                patient=patient,
                phone=payload.get("patient_phone"),
                phone_normalized=payload.get("patient_phone_normalized"),
            )
        return patient
    phone_normalized = payload.get("phone_normalized") or payload.get("patient_phone_normalized")
    patient = None
    if phone_normalized:
        patient = db.scalar(
            select(Patient).where(
                Patient.workspace_id == workspace.id,
                Patient.phone_normalized == phone_normalized,
            )
        )
    if patient is None:
        full_name = payload.get("full_name") or payload.get("patient_name")
        first_name, last_name = _name_parts(full_name)
        birth_date = _parse_date(payload.get("birth_date"))
        patient = Patient(
            workspace_id=workspace.id,
            first_name=first_name,
            last_name=last_name,
            phone=payload.get("phone") or payload.get("patient_phone"),
            phone_normalized=phone_normalized,
            gender="female",
            birth_date=birth_date,
            preferred_language="ar",
            preferred_branch_id=workspace.primary_branch_id,
            source=payload.get("source") or "other",
            source_detail=None,
            status="active",
            marketing_consent=False,
        )
        db.add(patient)
        db.flush()
    cache[identity] = patient
    return patient


def _fill_missing_patient_facts(
    patient: Patient,
    payload: dict[str, Any],
    *,
    db: Session | None = None,
    workspace_id: UUID | None = None,
) -> None:
    """Merge non-destructive patient presentation facts for Append imports.

    Patient identity is established by patient_id/normalized phone, never by name.
    Append must not erase or silently replace existing canonical facts when a later
    clinic export is partial. It may only fill fields that are currently missing.
    When a database session is supplied, a phone is filled only if no other canonical
    patient in the workspace already owns that normalized number.
    """
    full_name = _clean(payload.get("full_name") or payload.get("patient_name"))
    if full_name and not (patient.first_name or patient.last_name):
        first_name, last_name = _name_parts(full_name)
        patient.first_name = first_name
        patient.last_name = last_name

    phone = payload.get("phone") or payload.get("patient_phone")
    phone_normalized = payload.get("phone_normalized") or payload.get("patient_phone_normalized")
    if phone_normalized and not patient.phone_normalized:
        if db is not None and workspace_id is not None:
            _assign_patient_phone_if_available(
                db,
                workspace_id=workspace_id,
                patient=patient,
                phone=phone,
                phone_normalized=phone_normalized,
            )
        else:
            if phone:
                patient.phone = phone
            patient.phone_normalized = phone_normalized
    elif phone and not patient.phone and (not phone_normalized or phone_normalized == patient.phone_normalized):
        patient.phone = phone

    birth_date = _parse_date(payload.get("birth_date"))
    if birth_date is not None and patient.birth_date is None:
        patient.birth_date = birth_date

    source = _clean(payload.get("source"))
    if source and source != "other" and (not patient.source or patient.source == "other"):
        patient.source = source


def _doctor_catalog(db: Session, workspace_id: UUID) -> tuple[dict[UUID, Doctor], dict[str, Doctor]]:
    rows = list(
        db.execute(
            select(Doctor, Staff)
            .join(Staff, (Staff.workspace_id == Doctor.workspace_id) & (Staff.id == Doctor.staff_id))
            .where(Doctor.workspace_id == workspace_id, Doctor.is_active.is_(True), Staff.is_active.is_(True))
        ).all()
    )
    return {doctor.id: doctor for doctor, _staff in rows}, {
        f"{staff.first_name} {staff.last_name}".strip().casefold(): doctor for doctor, staff in rows
    }


def _create_visiting_doctor(db: Session, *, workspace: Workspace, full_name: str) -> Doctor:
    if workspace.primary_branch_id is None:
        raise HistoricalImportError("Clinic profile is incomplete.")
    first, last = _name_parts(full_name)
    staff = Staff(
        workspace_id=workspace.id,
        user_id=None,
        first_name=first,
        last_name=last or "",
        email=None,
        phone=None,
        job_title="doctor",
        is_active=True,
    )
    db.add(staff)
    db.flush()
    doctor = Doctor(
        workspace_id=workspace.id,
        staff_id=staff.id,
        doctor_type="visiting",
        specialization=None,
        license_number=None,
        bio=None,
        booking_enabled=False,
        is_active=True,
    )
    db.add(doctor)
    db.flush()
    db.add(
        DoctorBranch(
            workspace_id=workspace.id,
            doctor_id=doctor.id,
            branch_id=workspace.primary_branch_id,
            is_primary=True,
            is_active=True,
        )
    )
    db.flush()
    return doctor


def _unassigned_historical_doctor(db: Session, *, workspace: Workspace, by_name: dict[str, Doctor]) -> Doctor:
    key = "غير محدد (تاريخي)".casefold()
    if key in by_name:
        return by_name[key]
    doctor = _create_visiting_doctor(db, workspace=workspace, full_name="غير محدد (تاريخي)")
    by_name[key] = doctor
    return doctor


def _resolve_or_create_doctor(
    db: Session,
    *,
    workspace: Workspace,
    payload: dict[str, Any],
    by_id: dict[UUID, Doctor],
    by_name: dict[str, Doctor],
) -> tuple[Doctor, bool]:
    doctor_uuid = _parse_uuid(payload.get("doctor_id"))
    if doctor_uuid and doctor_uuid in by_id:
        return by_id[doctor_uuid], True
    name = _clean(payload.get("doctor_name"))
    if name:
        key = name.casefold()
        if key in by_name:
            return by_name[key], True
        doctor = _create_visiting_doctor(db, workspace=workspace, full_name=name)
        by_id[doctor.id] = doctor
        by_name[key] = doctor
        return doctor, True
    return _unassigned_historical_doctor(db, workspace=workspace, by_name=by_name), False


def _safe_remove_previous_imports(
    db: Session,
    *,
    workspace_id: UUID,
    incoming_hashes: dict[tuple[str, str], str],
) -> dict[str, int]:
    links = list(db.scalars(select(ClinicHistoricalImportLink).where(ClinicHistoricalImportLink.workspace_id == workspace_id)))
    # Replace means synchronize the previously imported history with this batch.
    # Missing source records and changed source records are removed; identical records
    # remain idempotent and are reused.
    stale = [
        row for row in links
        if row.entity_type != "patient"
        and (
            (row.entity_type, row.source_record_id) not in incoming_hashes
            or incoming_hashes[(row.entity_type, row.source_record_id)] != row.payload_hash
        )
    ]
    ids_by_type: dict[str, set[UUID]] = defaultdict(set)
    for link in stale:
        ids_by_type[link.entity_type].add(link.canonical_id)

    appointment_ids = ids_by_type.get("appointment", set())
    payment_ids = ids_by_type.get("payment", set())
    package_ids = ids_by_type.get("package", set())

    if appointment_ids:
        campaign_ref = db.scalar(
            select(CRMCampaignConversion.id).where(
                CRMCampaignConversion.workspace_id == workspace_id,
                (CRMCampaignConversion.appointment_id.in_(appointment_ids))
                | (CRMCampaignConversion.original_appointment_id.in_(appointment_ids)),
            ).limit(1)
        )
        if campaign_ref is not None:
            raise HistoricalImportConflictError(
                "Some previously imported appointments now have campaign attribution. Use Append or keep those records."
            )
        runtime_payment_ref = db.scalar(
            select(PaymentTransaction.id).where(
                PaymentTransaction.workspace_id == workspace_id,
                PaymentTransaction.id.not_in(payment_ids or {UUID(int=0)}),
                (PaymentTransaction.appointment_id.in_(appointment_ids))
                | (PaymentTransaction.origin_appointment_id.in_(appointment_ids)),
            ).limit(1)
        )
        if runtime_payment_ref is not None:
            raise HistoricalImportConflictError(
                "Some previously imported appointments have newer Linka payment activity. Use Append instead of Replace."
            )

    if package_ids:
        usage_ref = db.scalar(
            select(PackageUsage.id).where(
                PackageUsage.workspace_id == workspace_id,
                PackageUsage.patient_package_id.in_(package_ids),
            ).limit(1)
        )
        if usage_ref is not None:
            raise HistoricalImportConflictError(
                "Some imported packages were used after migration. Use Append so current package balances are preserved."
            )
        runtime_package_payment = db.scalar(
            select(PaymentTransaction.id).where(
                PaymentTransaction.workspace_id == workspace_id,
                PaymentTransaction.id.not_in(payment_ids or {UUID(int=0)}),
                PaymentTransaction.patient_package_id.in_(package_ids),
            ).limit(1)
        )
        if runtime_package_payment is not None:
            raise HistoricalImportConflictError(
                "Some imported packages have newer Linka payment activity. Use Append instead of Replace."
            )

    if payment_ids:
        referenced = db.scalar(
            select(PaymentTransaction.id).where(
                PaymentTransaction.workspace_id == workspace_id,
                PaymentTransaction.id.not_in(payment_ids),
                PaymentTransaction.reference_transaction_id.in_(payment_ids),
            ).limit(1)
        )
        if referenced is not None:
            raise HistoricalImportConflictError(
                "A newer Linka refund references an imported payment. Use Append instead of Replace."
            )

    # Preserve agent audit history. Only clear the nullable appointment pointer.
    if appointment_ids:
        db.execute(
            update(AgentAction)
            .where(AgentAction.workspace_id == workspace_id, AgentAction.appointment_id.in_(appointment_ids))
            .values(appointment_id=None)
        )
        # Appointment-specific queued/derived automation jobs cannot remain valid after the
        # imported appointment is removed. They are scoped to these historical appointments only.
        db.execute(
            delete(AutomationJob).where(
                AutomationJob.workspace_id == workspace_id,
                AutomationJob.appointment_id.in_(appointment_ids),
            )
        )

    if payment_ids or appointment_ids:
        alloc_filter = PaymentAllocation.workspace_id == workspace_id
        clauses = []
        if payment_ids:
            clauses.append(PaymentAllocation.transaction_id.in_(payment_ids))
        if appointment_ids:
            clauses.append(PaymentAllocation.appointment_id.in_(appointment_ids))
        if clauses:
            from sqlalchemy import or_
            db.execute(delete(PaymentAllocation).where(alloc_filter, or_(*clauses)))

    if appointment_ids:
        db.execute(delete(PackageUsage).where(PackageUsage.workspace_id == workspace_id, PackageUsage.appointment_id.in_(appointment_ids)))
    if payment_ids:
        # Clear self-references and package purchase pointers before deleting the imported
        # financial facts; this keeps FK ordering deterministic.
        db.execute(
            update(PaymentTransaction)
            .where(PaymentTransaction.workspace_id == workspace_id, PaymentTransaction.id.in_(payment_ids))
            .values(reference_transaction_id=None)
        )
        db.execute(
            update(PatientPackage)
            .where(PatientPackage.workspace_id == workspace_id, PatientPackage.purchase_transaction_id.in_(payment_ids))
            .values(purchase_transaction_id=None)
        )
        db.execute(delete(PaymentTransaction).where(PaymentTransaction.workspace_id == workspace_id, PaymentTransaction.id.in_(payment_ids)))
    if appointment_ids:
        db.execute(delete(Appointment).where(Appointment.workspace_id == workspace_id, Appointment.id.in_(appointment_ids)))
    if package_ids:
        db.execute(delete(PatientPackage).where(PatientPackage.workspace_id == workspace_id, PatientPackage.id.in_(package_ids)))

    # Patients deliberately remain: patient identity may have conversations/messages/runtime data.
    stale_ids = [row.id for row in stale]
    if stale_ids:
        db.execute(delete(ClinicHistoricalImportLink).where(ClinicHistoricalImportLink.id.in_(stale_ids)))
    return {entity: len(ids) for entity, ids in ids_by_type.items()}


def apply_historical_import(
    db: Session,
    *,
    workspace: Workspace,
    batch: ClinicHistoricalImportBatch,
) -> dict[str, Any]:
    if batch.status not in {"importing", "preview_ready", "failed"}:
        if batch.status == "imported":
            return batch.summary_json or {}
        raise HistoricalImportConflictError("Historical import is not ready to apply.")
    rows = list(
        db.scalars(
            select(ClinicHistoricalImportRow)
            .where(
                ClinicHistoricalImportRow.workspace_id == workspace.id,
                ClinicHistoricalImportRow.batch_id == batch.id,
                ClinicHistoricalImportRow.row_status == "ready",
            )
            .order_by(ClinicHistoricalImportRow.entity_type, ClinicHistoricalImportRow.row_number)
        )
    )
    if not rows:
        raise HistoricalImportError("Historical import contains no ready rows.")

    existing_links = _existing_link_map(db, workspace.id)
    incoming_hashes = {
        (row.entity_type, row.source_record_id): row.payload_hash
        for row in rows if row.entity_type != "payment_allocation"
    }
    for row in rows:
        payload = row.normalized_json
        if (
            row.entity_type == "package"
            and payload.get("import_contract") == ACTIVE_PACKAGE_CONTRACT
        ):
            payment_source_id = _active_package_payment_source_id(row.source_record_id)
            incoming_hashes[("payment", payment_source_id)] = _payload_hash(
                _active_package_payment_payload(
                    payload,
                    package_source_record_id=row.source_record_id,
                )
            )
    if batch.mode == "append":
        for row in rows:
            if row.entity_type in {"payment_allocation", "patient"}:
                # Patients are identity anchors rather than immutable historical facts.
                # A later export may repeat the same patient with partial presentation
                # fields (or a changed CRM source). Reuse the canonical patient instead
                # of forcing Replace. Appointment/payment/package facts remain strict.
                continue
            link = existing_links.get((row.entity_type, row.source_record_id))
            if link and link.payload_hash != row.payload_hash:
                raise HistoricalImportConflictError(
                    f"{row.entity_type} {row.source_record_id} was imported before with different data. Choose Replace previous imports."
                )
            payload = row.normalized_json
            if (
                row.entity_type == "package"
                and payload.get("import_contract") == ACTIVE_PACKAGE_CONTRACT
            ):
                payment_source_id = _active_package_payment_source_id(row.source_record_id)
                payment_link = existing_links.get(("payment", payment_source_id))
                payment_hash = incoming_hashes[("payment", payment_source_id)]
                if payment_link and payment_link.payload_hash != payment_hash:
                    raise HistoricalImportConflictError(
                        "Historical package payment was imported before with different data. "
                        "Choose Replace previous imports."
                    )
    else:
        _safe_remove_previous_imports(db, workspace_id=workspace.id, incoming_hashes=incoming_hashes)
        existing_links = _existing_link_map(db, workspace.id)

    services_by_id, _services_by_name = _service_catalog(db, workspace.id)
    doctors_by_id, doctors_by_name = _doctor_catalog(db, workspace.id)
    patient_cache: dict[str, Patient] = {}
    imported: Counter[str] = Counter()
    skipped: Counter[str] = Counter()

    # Seed patient facts first, then implicit identities from all other sheets. Patient
    # canonical rows deliberately survive Replace because conversations/runtime CRM may
    # already reference them. A stable historical link is reused instead of creating a
    # second patient when a later export changes presentation fields.
    for row in rows:
        payload = row.normalized_json
        identity = payload.get("identity")
        if not identity:
            continue
        patient_source_id = row.source_record_id if row.entity_type == "patient" else _source_id("patient", payload.get("patient_id"), identity)
        patient_hash = row.payload_hash if row.entity_type == "patient" else _payload_hash({"identity": identity})
        key = ("patient", patient_source_id)
        link = existing_links.get(key)
        patient = db.get(Patient, link.canonical_id) if link is not None else None
        if patient is None:
            patient = _patient_for_identity(db, workspace=workspace, identity=identity, payload=payload, cache=patient_cache)
        else:
            patient_cache[identity] = patient

        if row.entity_type == "patient" and batch.mode == "append":
            _fill_missing_patient_facts(
                patient,
                payload,
                db=db,
                workspace_id=workspace.id,
            )

        if row.entity_type == "patient" and batch.mode == "replace_previous_imports":
            first_name, last_name = _name_parts(payload.get("full_name"))
            if payload.get("full_name"):
                patient.first_name = first_name
                patient.last_name = last_name
            if payload.get("phone_normalized"):
                _assign_patient_phone_if_available(
                    db,
                    workspace_id=workspace.id,
                    patient=patient,
                    phone=payload.get("phone"),
                    phone_normalized=payload.get("phone_normalized"),
                )
            if payload.get("birth_date"):
                patient.birth_date = _parse_date(payload.get("birth_date"))
            if payload.get("source"):
                patient.source = payload.get("source")

        if link is None:
            link = ClinicHistoricalImportLink(
                workspace_id=workspace.id,
                batch_id=batch.id,
                entity_type="patient",
                canonical_id=patient.id,
                source_record_id=patient_source_id,
                payload_hash=patient_hash,
            )
            db.add(link)
            db.flush()
            existing_links[key] = link
        elif batch.mode == "replace_previous_imports" and row.entity_type == "patient":
            link.payload_hash = patient_hash
            link.batch_id = batch.id

    package_by_external: dict[str, PatientPackage] = {}
    for row in [item for item in rows if item.entity_type == "package"]:
        link = existing_links.get(("package", row.source_record_id))
        if link:
            skipped["package"] += 1
            package = db.get(PatientPackage, link.canonical_id) if hasattr(link, "canonical_id") else None
            if package and row.normalized_json.get("package_id"):
                package_by_external[row.normalized_json["package_id"]] = package
            if row.normalized_json.get("import_contract") == ACTIVE_PACKAGE_CONTRACT:
                if package is None:
                    raise HistoricalImportConflictError(
                        "Historical package link points to a missing package. "
                        "Review previous imports before retrying."
                    )
                payment_source_id = _active_package_payment_source_id(row.source_record_id)
                payment_link = existing_links.get(("payment", payment_source_id))
                transaction = (
                    db.get(PaymentTransaction, payment_link.canonical_id)
                    if payment_link is not None
                    else None
                )
                if (
                    transaction is None
                    or transaction.patient_package_id != package.id
                    or transaction.patient_id != package.patient_id
                    or transaction.transaction_type != "payment"
                ):
                    raise HistoricalImportConflictError(
                        "Historical package exists without its correctly linked payment. "
                        "Review previous imports before retrying."
                    )
                if package.purchase_transaction_id is None:
                    package.purchase_transaction_id = transaction.id
                elif package.purchase_transaction_id != transaction.id:
                    raise HistoricalImportConflictError(
                        "Historical package purchase link conflicts with its imported payment."
                    )
                skipped["payment"] += 1
            continue
        payload = row.normalized_json
        patient = patient_cache[payload["identity"]]
        service = services_by_id[UUID(payload["service_id"])]
        total_known = payload.get("sessions_total") is not None
        total = int(payload.get("sessions_total") or max(int(payload["sessions_remaining"]), 1))
        package = PatientPackage(
            workspace_id=workspace.id,
            patient_id=patient.id,
            service_id=service.id,
            purchase_transaction_id=None,
            created_by_user_id=batch.created_by_user_id,
            external_id=payload.get("package_id"),
            name=payload["package_name"],
            sessions_purchased=total,
            opening_sessions_remaining=int(payload["sessions_remaining"]),
            sessions_total_known=total_known,
            sale_price_minor=int(payload.get("price_minor") or 0),
            standalone_session_price_minor_at_purchase=(
                payload.get("standalone_session_price_minor")
                if payload.get("standalone_session_price_minor") is not None
                else int(service.price_minor)
            ),
            laser_device_key=payload.get("laser_device_key"),
            laser_device_name=payload.get("laser_device_name"),
            currency="EGP",
            purchased_at=_parse_datetime(payload["purchased_at"]) or datetime.now(UTC),
            expires_at=_parse_date(payload.get("expires_at")),
            status=payload.get("status") or "active",
            source="integration",
            idempotency_key=f"historical:{row.source_record_id}"[:128],
        )
        db.add(package)
        db.flush()
        db.add(ClinicHistoricalImportLink(
            workspace_id=workspace.id,
            batch_id=batch.id,
            entity_type="package",
            canonical_id=package.id,
            source_record_id=row.source_record_id,
            payload_hash=row.payload_hash,
        ))
        if payload.get("package_id"):
            package_by_external[payload["package_id"]] = package

        if payload.get("import_contract") == ACTIVE_PACKAGE_CONTRACT:
            payment_source_id = _active_package_payment_source_id(row.source_record_id)
            payment_payload = _active_package_payment_payload(
                payload,
                package_source_record_id=row.source_record_id,
            )
            payment_link = existing_links.get(("payment", payment_source_id))
            if payment_link is not None:
                raise HistoricalImportConflictError(
                    "Historical package payment exists without its package link. "
                    "Review previous imports before retrying."
                )
            purchased_at = _parse_datetime(payload.get("purchased_at")) or datetime.now(UTC)
            transaction = PaymentTransaction(
                workspace_id=workspace.id,
                appointment_id=None,
                origin_appointment_id=None,
                patient_id=patient.id,
                created_by_user_id=batch.created_by_user_id,
                reference_transaction_id=None,
                patient_package_id=package.id,
                transaction_type="payment",
                amount_minor=int(payload["historical_payment_amount_minor"]),
                currency="EGP",
                payment_method="unknown",
                source="integration",
                external_reference=None,
                reason=None,
                idempotency_key=f"historical:{payment_source_id}"[:128],
                created_at=purchased_at,
            )
            db.add(transaction)
            db.flush()
            package.purchase_transaction_id = transaction.id
            payment_hash = _payload_hash(payment_payload)
            payment_link = ClinicHistoricalImportLink(
                workspace_id=workspace.id,
                batch_id=batch.id,
                entity_type="payment",
                canonical_id=transaction.id,
                source_record_id=payment_source_id,
                payload_hash=payment_hash,
            )
            db.add(payment_link)
            db.flush()
            existing_links[("payment", payment_source_id)] = payment_link
            imported["payment"] += 1

        imported["package"] += 1

    appointment_by_external: dict[str, Appointment] = {}
    for row in [item for item in rows if item.entity_type == "appointment"]:
        link = existing_links.get(("appointment", row.source_record_id))
        if link:
            skipped["appointment"] += 1
            appointment = db.get(Appointment, link.canonical_id) if hasattr(link, "canonical_id") else None
            if appointment and row.normalized_json.get("appointment_id"):
                appointment_by_external[row.normalized_json["appointment_id"]] = appointment
            continue
        payload = row.normalized_json
        patient = patient_cache[payload["identity"]]
        service = services_by_id[UUID(payload["service_id"])]
        doctor, assignment_known = _resolve_or_create_doctor(
            db, workspace=workspace, payload=payload, by_id=doctors_by_id, by_name=doctors_by_name
        )
        # Give newly discovered visiting doctors the historical service association for semantic context.
        assignment = db.scalar(select(DoctorService.id).where(
            DoctorService.workspace_id == workspace.id,
            DoctorService.doctor_id == doctor.id,
            DoctorService.service_id == service.id,
        ))
        if assignment is None:
            db.add(DoctorService(
                workspace_id=workspace.id,
                doctor_id=doctor.id,
                service_id=service.id,
                custom_duration_minutes=None,
                custom_price_minor=None,
                is_active=True,
            ))
        local_start = datetime.combine(date.fromisoformat(payload["date"]), time.fromisoformat(payload["start_time"]), tzinfo=EGYPT_TZ)
        start_at = local_start.astimezone(UTC)
        end_at = start_at + timedelta(minutes=service.duration_minutes)
        package = package_by_external.get(payload.get("package_id") or "")
        status = payload["status"]
        appointment = Appointment(
            workspace_id=workspace.id,
            patient_id=patient.id,
            branch_id=workspace.primary_branch_id,
            doctor_id=doctor.id,
            doctor_assignment_known=assignment_known,
            service_id=service.id,
            patient_package_id=package.id if package else None,
            lead_id=None,
            created_by_user_id=batch.created_by_user_id,
            rescheduled_from_appointment_id=None,
            status=status,
            source="other",
            start_at=start_at,
            end_at=end_at,
            busy_start_at=start_at,
            busy_end_at=end_at,
            duration_minutes=service.duration_minutes,
            price_minor=service.price_minor,
            currency="EGP",
            payment_status="unknown",
            amount_paid_minor=None,
            payment_method="unknown",
            billing_context="package_prepaid" if package else "standard",
            package_external_id=payload.get("package_id"),
            customer_note=None,
            cancellation_reason=None,
            idempotency_key=f"historical:{row.source_record_id}"[:128],
            completed_at=start_at if status == "completed" else None,
            cancelled_at=start_at if status == "cancelled" else None,
            no_show_at=start_at if status == "no_show" else None,
        )
        db.add(appointment)
        db.flush()
        db.add(ClinicHistoricalImportLink(
            workspace_id=workspace.id,
            batch_id=batch.id,
            entity_type="appointment",
            canonical_id=appointment.id,
            source_record_id=row.source_record_id,
            payload_hash=row.payload_hash,
        ))
        if payload.get("appointment_id"):
            appointment_by_external[payload["appointment_id"]] = appointment
        imported["appointment"] += 1

    payment_by_external: dict[str, PaymentTransaction] = {}
    pending_refs: list[tuple[PaymentTransaction, str]] = []
    for row in [item for item in rows if item.entity_type == "payment"]:
        link = existing_links.get(("payment", row.source_record_id))
        if link:
            skipped["payment"] += 1
            transaction = db.get(PaymentTransaction, link.canonical_id) if hasattr(link, "canonical_id") else None
            if transaction and row.normalized_json.get("transaction_id"):
                payment_by_external[row.normalized_json["transaction_id"]] = transaction
            continue
        payload = row.normalized_json
        patient = patient_cache[payload["identity"]]
        signed_amount = int(payload["amount_minor"])
        transaction = PaymentTransaction(
            workspace_id=workspace.id,
            appointment_id=(appointment_by_external.get(payload.get("appointment_id") or "") or None).id if appointment_by_external.get(payload.get("appointment_id") or "") else None,
            origin_appointment_id=None,
            patient_id=patient.id,
            created_by_user_id=batch.created_by_user_id,
            reference_transaction_id=None,
            patient_package_id=(package_by_external.get(payload.get("package_id") or "") or None).id if package_by_external.get(payload.get("package_id") or "") else None,
            transaction_type="refund" if signed_amount < 0 else "payment",
            amount_minor=abs(signed_amount),
            currency="EGP",
            payment_method=payload.get("payment_method") or "unknown",
            source="integration",
            external_reference=payload.get("transaction_id"),
            reason="Historical import" if signed_amount < 0 else None,
            idempotency_key=f"historical:{row.source_record_id}"[:128],
            created_at=_parse_datetime(payload.get("paid_at")) or datetime.now(UTC),
        )
        db.add(transaction)
        db.flush()
        db.add(ClinicHistoricalImportLink(
            workspace_id=workspace.id,
            batch_id=batch.id,
            entity_type="payment",
            canonical_id=transaction.id,
            source_record_id=row.source_record_id,
            payload_hash=row.payload_hash,
        ))
        if payload.get("transaction_id"):
            payment_by_external[payload["transaction_id"]] = transaction
        if payload.get("reference_transaction_id"):
            pending_refs.append((transaction, payload["reference_transaction_id"]))
        imported["payment"] += 1

    for transaction, external_reference in pending_refs:
        referenced = payment_by_external.get(external_reference)
        if referenced is not None and referenced.patient_id == transaction.patient_id:
            transaction.reference_transaction_id = referenced.id

    allocations_by_transaction: Counter[UUID] = Counter()
    for row in [item for item in rows if item.entity_type == "payment_allocation"]:
        payload = row.normalized_json
        transaction = payment_by_external.get(payload["transaction_id"])
        appointment = appointment_by_external.get(payload["appointment_id"])
        if transaction is None or appointment is None:
            skipped["payment_allocation"] += 1
            continue
        amount_minor = int(payload["amount_minor"])
        allocations_by_transaction[transaction.id] += amount_minor
        if allocations_by_transaction[transaction.id] > transaction.amount_minor:
            raise HistoricalImportError(
                f"Payment allocations exceed transaction amount for {payload['transaction_id']}."
            )
        existing = db.scalar(select(PaymentAllocation.id).where(
            PaymentAllocation.workspace_id == workspace.id,
            PaymentAllocation.transaction_id == transaction.id,
            PaymentAllocation.appointment_id == appointment.id,
        ))
        if existing is None:
            db.add(PaymentAllocation(
                workspace_id=workspace.id,
                transaction_id=transaction.id,
                appointment_id=appointment.id,
                amount_minor=amount_minor,
            ))
            imported["payment_allocation"] += 1
        else:
            skipped["payment_allocation"] += 1

    # Count explicit patient sheet facts separately; implicit patients are still retained.
    imported["patient"] = len({patient.id for patient in patient_cache.values()})
    db.flush()
    batch.status = "imported"
    batch.completed_at = datetime.now(UTC)
    summary = dict(batch.summary_json or {})
    summary.update({
        "imported_counts": dict(imported),
        "skipped_counts": dict(skipped),
        "mode": batch.mode,
    })
    batch.summary_json = summary
    batch.error_message = None
    record_activity_event(
        db, workspace_id=workspace.id, actor_type="system", actor_user_id=batch.created_by_user_id,
        action="clinic.history_imported", entity_type="historical_import", entity_id=batch.id,
        summary="Historical clinic import completed.",
        metadata={"mode": batch.mode, "imported_counts": dict(imported), "skipped_counts": dict(skipped)},
        flush=False,
    )
    db.commit()
    return summary


def build_historical_import_template(
    *,
    service_names: Iterable[str] | None = None,
    service_device_names: dict[str, Iterable[str]] | None = None,
) -> bytes:
    """Return the clinic-facing active-package migration workbook."""
    valid_services = sorted(
        {
            str(name).strip()
            for name in (service_names or [])
            if str(name).strip()
        },
        key=str.casefold,
    )
    device_names_by_service = {
        str(service_name).strip(): tuple(
            sorted(
                {
                    str(device_name).strip()
                    for device_name in device_names
                    if str(device_name).strip()
                },
                key=str.casefold,
            )
        )[:2]
        for service_name, device_names in (service_device_names or {}).items()
        if str(service_name).strip()
    }
    example_service = valid_services[0] if valid_services else "اختر خدمة من القائمة"
    example_devices = device_names_by_service.get(example_service, ())
    example_device = example_devices[0] if example_devices else None

    workbook = Workbook()
    instructions = workbook.active
    instructions.title = "README"
    instructions.sheet_view.rightToLeft = True
    instructions.sheet_view.showGridLines = False

    title_font = Font(bold=True, size=15)
    section_font = Font(bold=True, size=12)
    header_font = Font(bold=True)
    thin_side = Side(style="thin")
    table_border = Border(
        left=thin_side,
        right=thin_side,
        top=thin_side,
        bottom=thin_side,
    )

    instructions.merge_cells("A1:I1")
    instructions["A1"] = "دليل استيراد الباقات النشطة"
    instructions["A1"].font = title_font
    instructions["A1"].alignment = Alignment(horizontal="center", vertical="center")
    instructions.row_dimensions[1].height = 26

    instructions.merge_cells("A3:I3")
    instructions["A3"] = (
        "سعر الباقة اختياري. إذا تركته فارغًا، سيتعامل النظام مع المبلغ المدفوع "
        "على أنه سعر الباقة بالكامل."
    )
    instructions["A3"].alignment = Alignment(horizontal="right", vertical="center", wrap_text=True)
    instructions.row_dimensions[3].height = 28

    arabic_headers = [
        "اسم العميل",
        "رقم الموبايل",
        "اسم الخدمة",
        "اسم الجهاز",
        "عدد الجلسات الكلي",
        "عدد الجلسات المتبقي",
        "المبلغ المدفوع",
        "تاريخ الشراء",
        "سعر الباقة",
    ]

    instructions.merge_cells("A5:I5")
    instructions["A5"] = "مثال صحيح"
    instructions["A5"].font = section_font
    instructions["A5"].alignment = Alignment(horizontal="right")

    example_values = [
        "سارة أحمد",
        "01012345678",
        example_service,
        example_device,
        6,
        3,
        3000,
        "15/02/2026",
        3000,
    ]
    for column, header in enumerate(arabic_headers, start=1):
        header_cell = instructions.cell(row=6, column=column, value=header)
        header_cell.font = header_font
        header_cell.border = table_border
        header_cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        value_cell = instructions.cell(row=7, column=column, value=example_values[column - 1])
        value_cell.border = table_border
        value_cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    instructions["B7"].number_format = "@"

    instructions.merge_cells("A9:I9")
    instructions["A9"] = (
        "الخدمات التالية هي الخدمات النشطة المسجلة حاليًا داخل Linka، "
        "وهي نفسها الخدمات التي يمكن اختيارها للباقة."
    )
    instructions["A9"].font = header_font
    instructions["A9"].alignment = Alignment(horizontal="right", vertical="center", wrap_text=True)

    instructions["A11"] = "اسم الخدمة"
    instructions["A11"].font = header_font
    instructions["A11"].border = table_border
    instructions["A11"].alignment = Alignment(horizontal="center")

    service_start_row: int | None = None
    service_end_row: int | None = None
    if valid_services:
        service_start_row = 12
        for index, name in enumerate(valid_services, start=service_start_row):
            cell = instructions.cell(row=index, column=1, value=name)
            cell.border = table_border
            cell.alignment = Alignment(horizontal="right", vertical="center")
        service_end_row = service_start_row + len(valid_services) - 1
    else:
        instructions["A12"] = "لا توجد خدمات نشطة حاليًا."
        instructions["A12"].border = table_border
        instructions["A12"].alignment = Alignment(horizontal="right")

    readme_widths = {
        "A": 28,
        "B": 18,
        "C": 30,
        "D": 20,
        "E": 22,
        "F": 18,
        "G": 18,
        "H": 18,
        "I": 18,
    }
    for column_letter, width in readme_widths.items():
        instructions.column_dimensions[column_letter].width = width
    instructions.freeze_panes = "A3"

    device_lists = workbook.create_sheet("_lists")
    device_lists.sheet_state = "hidden"
    device_lists["A2"] = None
    device_lists["B2"] = None
    device_lists["C2"] = None
    for row_number, service_name in enumerate(valid_services, start=3):
        devices = device_names_by_service.get(service_name, ())
        device_lists.cell(row=row_number, column=1, value=service_name)
        device_lists.cell(
            row=row_number,
            column=2,
            value=devices[0] if len(devices) >= 1 else None,
        )
        device_lists.cell(
            row=row_number,
            column=3,
            value=devices[1] if len(devices) >= 2 else None,
        )

    sheet = workbook.create_sheet("active_packages")
    sheet.sheet_view.rightToLeft = True
    sheet.sheet_view.showGridLines = False
    sheet.append(arabic_headers)
    sheet.freeze_panes = "A2"

    widths = [24, 18, 32, 18, 20, 22, 18, 18, 18]
    for index, width in enumerate(widths, start=1):
        header_cell = sheet.cell(row=1, column=index)
        header_cell.font = header_font
        header_cell.border = table_border
        header_cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        sheet.column_dimensions[header_cell.column_letter].width = width
    sheet.row_dimensions[1].height = 24

    for row_number in range(2, 5001):
        sheet.cell(row=row_number, column=2).number_format = "@"
        sheet.cell(row=row_number, column=8).number_format = "dd/mm/yyyy"

    if service_start_row is not None and service_end_row is not None:
        formula = (
            'INDIRECT("\'README\'!$A$'
            + str(service_start_row)
            + ':$A$'
            + str(service_end_row)
            + '")'
        )
        validation = DataValidation(
            type="list",
            formula1=formula,
            allow_blank=False,
        )
        validation.error = "اختر خدمة من قائمة الخدمات الموجودة في ورقة التعليمات."
        validation.errorTitle = "خدمة غير صحيحة"
        validation.prompt = "اختر خدمة من القائمة."
        validation.promptTitle = "اسم الخدمة"
        sheet.add_data_validation(validation)
        validation.add("C2:C5000")

        device_list_end_row = len(valid_services) + 2
        device_formula = (
            "OFFSET(INDIRECT(\"'_lists'!$B$2\"),"
            "IFERROR(MATCH($C2,INDIRECT(\"'_lists'!$A$3:$A$"
            + str(device_list_end_row)
            + "\"),0),0),0,1,2)"
        )
        device_validation = DataValidation(
            type="list",
            formula1=device_formula,
            allow_blank=True,
        )
        device_validation.error = (
            "اختر جهازًا من القائمة الخاصة بالخدمة، أو اترك الخانة فارغة للخدمات غير الليزر."
        )
        device_validation.errorTitle = "جهاز غير صحيح"
        device_validation.prompt = "اختر الجهاز إذا كانت الخدمة ليزر."
        device_validation.promptTitle = "اسم الجهاز"
        sheet.add_data_validation(device_validation)
        device_validation.add("D2:D5000")

    stream = io.BytesIO()
    workbook.save(stream)
    return stream.getvalue()


__all__ = [
    "HistoricalImportConflictError",
    "HistoricalImportError",
    "apply_historical_import",
    "build_historical_import_template",
    "get_historical_import_batch",
    "list_historical_import_batches",
    "mark_historical_import_failed",
    "preview_historical_import",
    "start_historical_import",
    "_batch_read",
]
