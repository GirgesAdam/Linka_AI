from __future__ import annotations

"""Automated Linka conversational regression matrix.

This runner exercises the real FastAPI service-layer agent without WhatsApp or n8n.
It uses the real Gemini runtime and the real PostgreSQL clinic data, but by default
runs inside an external database transaction and rolls the whole suite back at the
end. Internal ``Session.commit()`` calls are isolated as savepoints through
``join_transaction_mode='create_savepoint'``.
Examples:
    python scripts/run_agent_e2e_matrix.py --workspace-slug tia --profile smoke
    python scripts/run_agent_e2e_matrix.py --workspace-slug tia --profile full
    python scripts/run_agent_e2e_matrix.py --workspace-slug tia --profile semantic

The live profiles make Gemini API calls and therefore have normal model usage cost.
No WhatsApp messages are sent.
"""
import argparse
import json
import sys
import traceback
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from time import perf_counter
from typing import Any, Callable
from uuid import UUID

from langchain_core.messages import HumanMessage
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.agents.clinic_grounding import build_clinic_catalog
from app.agents.turn_interpreter import interpret_customer_turn
from app.core.config import settings
from app.integrations.clinic.base import AvailabilityRequest, ClinicCapability
from app.integrations.clinic.registry import get_clinic_adapter
from app.models.appointment import Appointment
from app.models.patient import Patient
from app.models.workspace import Workspace
from app.schemas.agent import AgentChatRequest
from app.services.agent_chat import AgentChatError, run_agent_chat

FIXTURE_VERSION = "realistic-aesthetic-clinic-v1"


@dataclass
class CheckResult:
    name: str
    category: str
    status: str
    duration_ms: int
    details: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


@dataclass
class SuiteReport:
    started_at: str
    workspace_id: str
    workspace_slug: str
    profile: str
    rollback: bool
    results: list[CheckResult] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        out = {"PASS": 0, "WARN": 0, "FAIL": 0, "SKIP": 0}
        for result in self.results:
            out[result.status] = out.get(result.status, 0) + 1
        return out


@dataclass(frozen=True)
class SemanticCase:
    name: str
    category: str
    message: str
    check: Callable[[Any, dict[str, Any]], tuple[bool, str]]


def _norm(value: object) -> str:
    return str(value or "").strip().lower()


def _catalog_row(catalog, collection, name):
    """Resolve a known fixture row for matrix setup only.

    Customer-language understanding is never done here. The matrix sends the
    original natural-language message through the real agent runtime. This helper
    only finds the deterministic fixture row used to build expected assertions.
    Exact catalog display names win. A unique whole-name suffix is accepted so
    presentation prefixes such as a doctor title or branch label do not make the
    test harness depend on cosmetic fixture formatting.
    """
    rows = [row for row in catalog.get(collection, []) if isinstance(row, dict)]
    exact = [row for row in rows if str(row.get("name") or "").strip() == str(name).strip()]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise RuntimeError(
            f"Fixture catalog lookup is ambiguous: {collection} / {name!r}"
        )
    expected = str(name).strip()
    suffix = [
        row
        for row in rows
        if expected
        and str(row.get("name") or "").strip().endswith(" " + expected)
    ]
    if len(suffix) == 1:
        return suffix[0]
    if len(suffix) > 1:
        raise RuntimeError(
            f"Fixture catalog suffix lookup is ambiguous: {collection} / {name!r}"
        )
    available = [str(row.get("name") or "") for row in rows]
    raise RuntimeError(
        f"Required fixture catalog row not found: {collection} / {name}. "
        f"Available rows: {available}"
    )



def _has_capability(name: str) -> Callable[[Any, dict[str, Any]], tuple[bool, str]]:
    def check(decision: Any, _: dict[str, Any]) -> tuple[bool, str]:
        ok = name in set(decision.capabilities or [])
        return ok, f"capabilities={decision.capabilities}"

    return check

def _lacks_capability(name: str) -> Callable[[Any, dict[str, Any]], tuple[bool, str]]:
    def check(decision: Any, _: dict[str, Any]) -> tuple[bool, str]:
        ok = name not in set(decision.capabilities or [])
        return ok, f"capabilities={decision.capabilities}"

    return check

def _package_intent(expected_intent: str) -> Callable[[Any, dict[str, Any]], tuple[bool, str]]:
    def check(decision: Any, _: dict[str, Any]) -> tuple[bool, str]:
        actual = str(getattr(decision, "package_intent", "none"))
        return actual == expected_intent, (
            f"package_intent={actual} expected={expected_intent}"
        )

    return check


def _canonical_entity(field: str, expected_key: str) -> Callable[[Any, dict[str, Any]], tuple[bool, str]]:
    def check(decision: Any, expected: dict[str, Any]) -> tuple[bool, str]:
        actual = getattr(decision.entity_hints, field)
        wanted = expected[expected_key]
        return str(actual or "") == str(wanted), f"{field}={actual} expected={wanted}"

    return check


def _all(*checks: Callable[[Any, dict[str, Any]], tuple[bool, str]]):
    def combined(decision: Any, expected: dict[str, Any]) -> tuple[bool, str]:
        messages: list[str] = []
        for check in checks:
            ok, message = check(decision, expected)
            messages.append(message)
            if not ok:
                return False, " | ".join(messages)
        return True, " | ".join(messages)

    return combined


def _exact_time(expected_time: str):
    def check(decision: Any, _: dict[str, Any]) -> tuple[bool, str]:
        hints = decision.entity_hints
        ok = (
            hints.requested_start_time == expected_time
            and hints.not_before_time is None
            and hints.not_after_time is None
        )
        return ok, (
            f"start={hints.requested_start_time} before={hints.not_before_time} "
            f"after={hints.not_after_time}"
        )

    return check


def _time_window(*, before: str | None = None, after: str | None = None):
    def check(decision: Any, _: dict[str, Any]) -> tuple[bool, str]:
        hints = decision.entity_hints
        ok = hints.not_before_time == after and hints.not_after_time == before
        return ok, f"not_before={hints.not_before_time} not_after={hints.not_after_time}"

    return check


def _medical_handoff(decision: Any, _: dict[str, Any]) -> tuple[bool, str]:
    risk_flags = set(decision.risk_flags or [])
    category = str(decision.recommended_handoff_category or "")
    ok = category == "medical" or "medical" in risk_flags
    return ok, f"risk_flags={decision.risk_flags} handoff={category}"


def _ids_are_catalog_grounded(decision: Any, expected: dict[str, Any]) -> tuple[bool, str]:
    allowed = expected["allowed_ids"]
    hints = decision.entity_hints
    values: list[str] = []
    for field_name in (
        "service_id",
        "branch_id",
        "doctor_id",
    ):
        value = getattr(hints, field_name, None)
        if value:
            values.append(str(value))
    for field_name in (
        "service_candidate_ids",
        "branch_candidate_ids",
        "doctor_candidate_ids",
    ):
        values.extend(str(x) for x in (getattr(hints, field_name, None) or []))
    unknown = [value for value in values if value not in allowed]
    return not unknown, f"unknown_ids={unknown}"


def _semantic_cases() -> list[SemanticCase]:
    # These are deliberately natural-language examples, not runtime lexical rules.
    return [
        SemanticCase(
            "service_underarm_dialect",
            "entity_resolution",
            "عايز ليزر ابط",
            _canonical_entity("service_id", "underarm_service_id"),
        ),
        SemanticCase(
            "service_underarm_paraphrase",
            "entity_resolution",
            "عايز ازالة شعر الابط بالليزر",
            _canonical_entity("service_id", "underarm_service_id"),
        ),
        SemanticCase(
            "doctor_short_name",
            "entity_resolution",
            "عايز احجز مع د احمد محمود",
            _canonical_entity("doctor_id", "ahmed_doctor_id"),
        ),
        SemanticCase(
            "branch_city_name",
            "entity_resolution",
            "عايز الفرع اللي في مدينة نصر",
            _canonical_entity("branch_id", "nasr_branch_id"),
        ),
        SemanticCase(
            "full_booking_grounding",
            "booking",
            "عايز احجز ليزر ابط مع د احمد في مدينة نصر يوم التلات الساعة ٨ بالليل",
            _all(
                _has_capability("availability_discovery"),
                _has_capability("appointment_creation"),
                _canonical_entity("service_id", "underarm_service_id"),
                _canonical_entity("doctor_id", "ahmed_doctor_id"),
                _canonical_entity("branch_id", "nasr_branch_id"),
                _exact_time("20:00"),
            ),
        ),
        SemanticCase(
            "service_information",
            "information",
            "عايز اعرف الخدمات اللي عندكو",
            _has_capability("service_information"),
        ),
        SemanticCase(
            "pricing",
            "information",
            "جلسة ليزر الابط بكام؟",
            _all(
                _has_capability("pricing"),
                _canonical_entity("service_id", "underarm_service_id"),
            ),
        ),
        SemanticCase(
            "doctor_discovery",
            "information",
            "مين الدكاترة اللي بيعملوا ليزر إزالة الشعر؟",
            _has_capability("doctor_discovery"),
        ),
        SemanticCase(
            "branch_discovery",
            "information",
            "فروعكم فين؟",
            _has_capability("branch_discovery"),
        ),
        SemanticCase(
            "package_purchase_explicit",
            "package_semantics",
            "عايز أشتري باكدج ليزر ابط 6 جلسات",
            _all(
                _package_intent("purchase"),
                _has_capability("package_information"),
                _lacks_capability("appointment_creation"),
            ),
        ),
        SemanticCase(
            "package_purchase_multisession_plan",
            "package_semantics",
            "محتاج أبدأ 3 جلسات ليزر ابط كخطة واحدة",
            _all(
                _package_intent("purchase"),
                _has_capability("package_information"),
                _lacks_capability("appointment_creation"),
            ),
        ),
        SemanticCase(
            "package_inquiry_compare",
            "package_semantics",
            "أنا أول مرة وعايز ليزر ابط، أحجز جلسة واحدة ولا أبدأ كورس جلسات كامل؟",
            _all(
                _package_intent("inquire"),
                _has_capability("package_information"),
                _lacks_capability("appointment_creation"),
            ),
        ),
        SemanticCase(
            "package_use_existing",
            "package_semantics",
            "عندي باكدج ليزر ابط وعايز أحجز الجلسة الجاية منه",
            _all(
                _package_intent("use_existing"),
                _has_capability("package_information"),
                _has_capability("appointment_creation"),
            ),
        ),
        SemanticCase(
            "package_avoid_existing",
            "package_semantics",
            "عندي باكدج ليزر ابط بس المرة دي عايز أحجز جلسة عادية منفصلة ومتحسبهاش من الباكدج",
            _all(
                _package_intent("avoid_existing"),
                _has_capability("appointment_creation"),
            ),
        ),
        SemanticCase(
            "single_session_no_package_intent",
            "package_semantics",
            "عايز أحجز جلسة واحدة بس ليزر ابط",
            _all(
                _package_intent("none"),
                _has_capability("appointment_creation"),
            ),
        ),
        SemanticCase(
            "exact_time_semantics",
            "time_semantics",
            "عايز ميعاد الساعة ٦ بالليل",
            _exact_time("18:00"),
        ),
        SemanticCase(
            "after_time_semantics",
            "time_semantics",
            "عايز ميعاد بعد الساعة ٦ بالليل",
            _time_window(after="18:00"),
        ),
        SemanticCase(
            "before_time_semantics",
            "time_semantics",
            "عايز ميعاد قبل الساعة ٨ بالليل",
            _time_window(before="20:00"),
        ),
        SemanticCase(
            "range_time_semantics",
            "time_semantics",
            "عايز ميعاد من الساعة ٦ لحد ٨ بالليل",
            _time_window(after="18:00", before="20:00"),
        ),
        SemanticCase(
            "relative_date",
            "date_semantics",
            "عايز احجز يوم التلات الجاي",
            lambda decision, _: (
                decision.entity_hints.requested_date is not None,
                f"requested_date={decision.entity_hints.requested_date}",
            ),
        ),
        SemanticCase(
            "medical_suitability",
            "safety",
            "انا حامل، هل ينفع اعمل بوتوكس ولا ايه الأنسب ليا؟",
            _medical_handoff,
        ),
        SemanticCase(
            "mixed_language",
            "robustness",
            "عايز underarm laser مع د Ahmed في Nasr City",
            _all(
                _canonical_entity("service_id", "underarm_service_id"),
                _canonical_entity("doctor_id", "ahmed_doctor_id"),
                _canonical_entity("branch_id", "nasr_branch_id"),
            ),
        ),
        SemanticCase(
            "catalog_id_grounding",
            "safety",
            "عايز احجز خدمة مناسبة مع دكتور مناسب في فرع مناسب",
            _ids_are_catalog_grounded,
        ),
    ]


def _agent_payload(*, patient_id: UUID, message: str, conversation_id: UUID | None) -> AgentChatRequest:
    fields = AgentChatRequest.model_fields
    payload: dict[str, Any] = {"patient_id": patient_id, "message": message}
    if "conversation_id" in fields:
        payload["conversation_id"] = conversation_id
    if "channel" in fields:
        payload["channel"] = "whatsapp"
    missing_required: list[str] = []
    for name, model_field in fields.items():
        if name in payload:
            continue
        if model_field.is_required():
            missing_required.append(name)
    if missing_required:
        raise RuntimeError(
            "AgentChatRequest has new required fields unsupported by the regression runner: "
            + ", ".join(missing_required)
        )
    return AgentChatRequest(**payload)


def _send(
    db: Session,
    workspace: Workspace,
    patient: Patient,
    message: str,
    conversation_id: UUID | None,
):
    payload = _agent_payload(
        patient_id=patient.id,
        message=message,
        conversation_id=conversation_id,
    )
    started = perf_counter()
    response = run_agent_chat(db=db, workspace=workspace, payload=payload)
    return response, int((perf_counter() - started) * 1000)


def _appointments_for(db: Session, workspace: Workspace, patient: Patient) -> list[Appointment]:
    return list(
        db.scalars(
            select(Appointment)
            .where(
                Appointment.workspace_id == workspace.id,
                Appointment.patient_id == patient.id,
            )
            .order_by(Appointment.created_at.asc())
        )
    )


def _fixture_patients(db: Session, workspace: Workspace) -> dict[str, Patient]:
    rows = list(
        db.scalars(
            select(Patient).where(
                Patient.workspace_id == workspace.id,
                Patient.source_detail == FIXTURE_VERSION,
            )
        )
    )
    by_phone = {row.phone_normalized or row.phone: row for row in rows}
    mapping = {
        "busy-evening": "+200000230001",
        "pending-new-cairo": "+200000230002",
        "injectables": "+200000230003",
        "cancelled-slot": "+200000230004",
        "history": "+200000230005",
        "multiple-upcoming": "+200000230006",
        "blocked": "+200000230007",
    }
    result: dict[str, Patient] = {}
    for key, phone in mapping.items():
        patient = by_phone.get(phone)
        if patient is not None:
            result[key] = patient
    return result


def _find_future_slot(
    db: Session,
    workspace: Workspace,
    *,
    branch_id: UUID,
    service_id: UUID,
    doctor_id: UUID,
    days: int = 35,
):
    # Pick the fixture slot from the same source of truth used by the customer
    # agent. Using native Linka availability directly here can disagree with a
    # configured clinic adapter and create a false conversational booking fail.
    adapter = get_clinic_adapter(db=db, workspace=workspace)
    adapter.require_capability(ClinicCapability.AVAILABILITY_READ)
    today = datetime.now(UTC).date()
    for offset in range(1, days + 1):
        booking_date = today + timedelta(days=offset)
        availability = adapter.get_availability(
            AvailabilityRequest(
                branch_id=str(branch_id),
                service_id=str(service_id),
                booking_date=booking_date,
                doctor_id=str(doctor_id),
            )
        )
        slots = list(availability.slots)
        if slots:
            return booking_date, availability.timezone, slots[0]
    raise RuntimeError("Could not find an available fixture slot in the next 35 days.")


def _format_local_slot(slot, timezone_name: str) -> tuple[str, str]:
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(timezone_name)
    local = slot.start_at.astimezone(tz)
    return local.strftime("%Y-%m-%d"), local.strftime("%H:%M")


def _record(report: SuiteReport, result: CheckResult) -> None:
    report.results.append(result)
    icon = {"PASS": "PASS", "WARN": "WARN", "FAIL": "FAIL", "SKIP": "SKIP"}[result.status]
    print(f"[{icon}] {result.category}/{result.name} ({result.duration_ms} ms)")
    if result.error:
        print(f"       {result.error}")


def run_semantic_matrix(
    *,
    db: Session,
    workspace: Workspace,
    catalog: dict[str, Any],
    report: SuiteReport,
) -> None:
    underarm = _catalog_row(catalog, "services", "ليزر إزالة الشعر - إبط")
    ahmed = _catalog_row(catalog, "doctors", "أحمد محمود")
    nasr = _catalog_row(catalog, "branches", "فرع مدينة نصر")
    allowed_ids = {
        str(row["id"])
        for collection in ("services", "doctors", "branches")
        for row in catalog.get(collection, [])
        if row.get("id")
    }
    expected = {
        "underarm_service_id": str(underarm["id"]),
        "ahmed_doctor_id": str(ahmed["id"]),
        "nasr_branch_id": str(nasr["id"]),
        "allowed_ids": allowed_ids,
    }
    timezone_name = (workspace.timezone or "Africa/Cairo").strip()
    from zoneinfo import ZoneInfo

    local_now = datetime.now(ZoneInfo(timezone_name))
    for case in _semantic_cases():
        started = perf_counter()
        try:
            decision = interpret_customer_turn(
                flow=None,
                history=[HumanMessage(content=case.message)],
                timezone_name=timezone_name,
                local_now=local_now,
                clinic_catalog=catalog,
            )
            ok, message = case.check(decision, expected)
            _record(
                report,
                CheckResult(
                    name=case.name,
                    category=f"semantic:{case.category}",
                    status="PASS" if ok else "FAIL",
                    duration_ms=int((perf_counter() - started) * 1000),
                    details={
                        "message": case.message,
                        "capabilities": list(decision.capabilities or []),
                        "flow_signal": str(decision.flow_signal),
                        "package_intent": str(getattr(decision, "package_intent", "none")),
                        "risk_flags": list(decision.risk_flags or []),
                        "entity_hints": decision.entity_hints.model_dump(mode="json"),
                        "confidence": decision.confidence,
                        "check": message,
                    },
                    error=None if ok else message,
                ),
            )
        except Exception as exc:  # noqa: BLE001 - regression runner must continue
            _record(
                report,
                CheckResult(
                    name=case.name,
                    category=f"semantic:{case.category}",
                    status="FAIL",
                    duration_ms=int((perf_counter() - started) * 1000),
                    details={"message": case.message},
                    error=f"{type(exc).__name__}: {exc}",
                ),
            )


def run_e2e_matrix(
    *,
    db: Session,
    workspace: Workspace,
    catalog: dict[str, Any],
    patients: dict[str, Patient],
    report: SuiteReport,
    smoke: bool,
) -> None:
    active = patients.get("busy-evening")
    if active is None:
        _record(
            report,
            CheckResult(
                name="fixture_patients_present",
                category="setup",
                status="FAIL",
                duration_ms=0,
                error="Realistic fixture patient +200000230001 is missing. Run the realistic seed first.",
            ),
        )
        return

    # Read-only service information turn.
    started = perf_counter()
    try:
        before = {row.id for row in _appointments_for(db, workspace, active)}
        response, duration = _send(db, workspace, active, "عايز اعرف الخدمات اللي عندكو", None)
        after = {row.id for row in _appointments_for(db, workspace, active)}
        ok = bool(response.reply.strip()) and before == after
        _record(
            report,
            CheckResult(
                name="service_information_no_write",
                category="e2e:read",
                status="PASS" if ok else "FAIL",
                duration_ms=duration,
                details={"reply": response.reply, "model": response.model},
                error=None if ok else "Read-only service question unexpectedly changed appointments or returned empty reply.",
            ),
        )
    except Exception as exc:  # noqa: BLE001
        _record(
            report,
            CheckResult(
                name="service_information_no_write",
                category="e2e:read",
                status="FAIL",
                duration_ms=int((perf_counter() - started) * 1000),
                error=f"{type(exc).__name__}: {exc}",
            ),
        )

    underarm = _catalog_row(catalog, "services", "ليزر إزالة الشعر - إبط")
    full_body = _catalog_row(catalog, "services", "ليزر إزالة الشعر - جسم كامل سيدات")
    ahmed = _catalog_row(catalog, "doctors", "أحمد محمود")
    nasr = _catalog_row(catalog, "branches", "فرع مدينة نصر")

    # Grounded happy-path booking using an actually available adapter slot.
    started = perf_counter()
    try:
        booking_date, timezone_name, slot = _find_future_slot(
            db,
            workspace,
            branch_id=UUID(str(nasr["id"])),
            service_id=UUID(str(underarm["id"])),
            doctor_id=UUID(str(ahmed["id"])),
        )
        date_text, time_text = _format_local_slot(slot, timezone_name)
        before_ids = {row.id for row in _appointments_for(db, workspace, active)}
        response, duration1 = _send(
            db,
            workspace,
            active,
            f"عايز احجز ليزر ابط مع د احمد في مدينة نصر يوم {date_text} الساعة {time_text}",
            None,
        )
        conversation_id = response.conversation_id
        response2, duration2 = _send(
            db,
            workspace,
            active,
            "احجز الموعد ده",
            conversation_id,
        )
        after_rows = _appointments_for(db, workspace, active)
        created = [row for row in after_rows if row.id not in before_ids]
        if not created:
            # Some valid conversational paths present the exact slot once more before write.
            response3, duration3 = _send(
                db,
                workspace,
                active,
                "ايوة احجزه",
                conversation_id,
            )
            duration2 += duration3
            response2 = response3
            after_rows = _appointments_for(db, workspace, active)
            created = [row for row in after_rows if row.id not in before_ids]
        newest = created[-1] if created else None
        ok = bool(newest) and newest.service_id == UUID(str(underarm["id"])) and newest.duration_minutes == 15
        _record(
            report,
            CheckResult(
                name="underarm_booking_service_duration",
                category="e2e:booking",
                status="PASS" if ok else "FAIL",
                duration_ms=duration1 + duration2,
                details={
                    "requested_date": str(booking_date),
                    "requested_time": time_text,
                    "first_reply": response.reply,
                    "write_reply": response2.reply,
                    "created_appointment_id": str(newest.id) if newest else None,
                    "duration_minutes": newest.duration_minutes if newest else None,
                    "status": newest.status if newest else None,
                },
                error=None if ok else "The conversational booking did not create a 15-minute underarm appointment.",
            ),
        )
    except Exception as exc:  # noqa: BLE001
        _record(
            report,
            CheckResult(
                name="underarm_booking_service_duration",
                category="e2e:booking",
                status="FAIL",
                duration_ms=int((perf_counter() - started) * 1000),
                error=f"{type(exc).__name__}: {exc}",
            ),
        )

    if smoke:
        return

    # Full-body duration uses exactly Service.duration_minutes (60) for the same doctor.
    second_patient = patients.get("cancelled-slot") or active
    started = perf_counter()
    try:
        booking_date, timezone_name, slot = _find_future_slot(
            db,
            workspace,
            branch_id=UUID(str(nasr["id"])),
            service_id=UUID(str(full_body["id"])),
            doctor_id=UUID(str(ahmed["id"])),
        )
        date_text, time_text = _format_local_slot(slot, timezone_name)
        before_ids = {row.id for row in _appointments_for(db, workspace, second_patient)}
        first, d1 = _send(
            db,
            workspace,
            second_patient,
            f"عايز احجز ليزر جسم كامل سيدات مع د احمد في مدينة نصر يوم {date_text} الساعة {time_text}",
            None,
        )
        second, d2 = _send(db, workspace, second_patient, "احجز الموعد ده", first.conversation_id)
        created = [row for row in _appointments_for(db, workspace, second_patient) if row.id not in before_ids]
        if not created:
            third, d3 = _send(db, workspace, second_patient, "ايوة احجزه", first.conversation_id)
            second = third
            d2 += d3
            created = [row for row in _appointments_for(db, workspace, second_patient) if row.id not in before_ids]
        newest = created[-1] if created else None
        ok = bool(newest) and newest.service_id == UUID(str(full_body["id"])) and newest.duration_minutes == 60
        _record(
            report,
            CheckResult(
                name="full_body_booking_service_duration",
                category="e2e:booking",
                status="PASS" if ok else "FAIL",
                duration_ms=d1 + d2,
                details={
                    "first_reply": first.reply,
                    "write_reply": second.reply,
                    "duration_minutes": newest.duration_minutes if newest else None,
                    "created_appointment_id": str(newest.id) if newest else None,
                },
                error=None if ok else "The conversational booking did not create a 60-minute full-body appointment.",
            ),
        )
    except Exception as exc:  # noqa: BLE001
        _record(
            report,
            CheckResult(
                name="full_body_booking_service_duration",
                category="e2e:booking",
                status="FAIL",
                duration_ms=int((perf_counter() - started) * 1000),
                error=f"{type(exc).__name__}: {exc}",
            ),
        )

    # Existing multiple appointments: the agent should not perform a destructive write from an ambiguous request.
    multiple = patients.get("multiple-upcoming")
    if multiple is not None:
        for name, message in (
            ("ambiguous_cancel_no_write", "الغي معادي"),
            ("ambiguous_reschedule_no_write", "عايز اغير معادي"),
        ):
            started = perf_counter()
            try:
                before = {row.id: row.status for row in _appointments_for(db, workspace, multiple)}
                response, duration = _send(db, workspace, multiple, message, None)
                after = {row.id: row.status for row in _appointments_for(db, workspace, multiple)}
                ok = before == after and bool(response.reply.strip())
                _record(
                    report,
                    CheckResult(
                        name=name,
                        category="e2e:ambiguity",
                        status="PASS" if ok else "FAIL",
                        duration_ms=duration,
                        details={"reply": response.reply},
                        error=None if ok else "Ambiguous appointment-management request changed appointment state.",
                    ),
                )
            except Exception as exc:  # noqa: BLE001
                _record(
                    report,
                    CheckResult(
                        name=name,
                        category="e2e:ambiguity",
                        status="FAIL",
                        duration_ms=int((perf_counter() - started) * 1000),
                        error=f"{type(exc).__name__}: {exc}",
                    ),
                )

    # Blocked patient must never enter the automated agent.
    blocked = patients.get("blocked")
    if blocked is not None:
        started = perf_counter()
        try:
            _send(db, workspace, blocked, "عايز احجز جلسة", None)
            _record(
                report,
                CheckResult(
                    name="blocked_patient_rejected",
                    category="e2e:safety",
                    status="FAIL",
                    duration_ms=int((perf_counter() - started) * 1000),
                    error="Blocked patient unexpectedly reached the automated agent.",
                ),
            )
        except AgentChatError as exc:
            _record(
                report,
                CheckResult(
                    name="blocked_patient_rejected",
                    category="e2e:safety",
                    status="PASS",
                    duration_ms=int((perf_counter() - started) * 1000),
                    details={"error": str(exc)},
                ),
            )
        except Exception as exc:  # noqa: BLE001
            _record(
                report,
                CheckResult(
                    name="blocked_patient_rejected",
                    category="e2e:safety",
                    status="FAIL",
                    duration_ms=int((perf_counter() - started) * 1000),
                    error=f"Unexpected exception type {type(exc).__name__}: {exc}",
                ),
            )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Linka automated semantic + conversational E2E matrix.")
    parser.add_argument("--workspace-slug", default="tia")
    parser.add_argument("--workspace-id", type=UUID, default=None)
    parser.add_argument(
        "--profile",
        choices=("semantic", "smoke", "full"),
        default="full",
        help="semantic = LLM grounding only; smoke = semantic + core E2E; full = full matrix",
    )
    parser.add_argument(
        "--keep-data",
        action="store_true",
        help="Commit test conversations/appointments instead of rolling them back. Not recommended.",
    )
    parser.add_argument(
        "--report",
        default="artifacts/agent-e2e-matrix-report.json",
        help="JSON report path, relative to backend unless absolute.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    environment = str(settings.environment or "").strip().lower()
    if environment == "production":
        print("Refusing to run the conversational regression matrix in production.", file=sys.stderr)
        return 2

    engine = create_engine(settings.database_url, pool_pre_ping=True)
    connection = engine.connect()
    outer = connection.begin()
    # Service code may call commit many times. Each commit remains a savepoint, while
    # the outer connection transaction is owned by this runner and is rolled back.
    db = Session(
        bind=connection,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    report: SuiteReport | None = None
    exit_code = 1
    try:
        if args.workspace_id is not None:
            workspace = db.scalar(select(Workspace).where(Workspace.id == args.workspace_id))
        else:
            workspace = db.scalar(select(Workspace).where(Workspace.slug == args.workspace_slug))
        if workspace is None:
            raise RuntimeError("Workspace not found.")

        report = SuiteReport(
            started_at=datetime.now(UTC).isoformat(),
            workspace_id=str(workspace.id),
            workspace_slug=workspace.slug,
            profile=args.profile,
            rollback=not args.keep_data,
        )
        catalog_started = perf_counter()
        catalog = build_clinic_catalog(db, workspace)
        _record(
            report,
            CheckResult(
                name="active_catalog",
                category="setup",
                status="PASS" if catalog.get("services") and catalog.get("branches") and catalog.get("doctors") else "FAIL",
                duration_ms=int((perf_counter() - catalog_started) * 1000),
                details={
                    "services": len(catalog.get("services", [])),
                    "branches": len(catalog.get("branches", [])),
                    "doctors": len(catalog.get("doctors", [])),
                },
            ),
        )
        run_semantic_matrix(db=db, workspace=workspace, catalog=catalog, report=report)

        if args.profile in {"smoke", "full"}:
            patients = _fixture_patients(db, workspace)
            run_e2e_matrix(
                db=db,
                workspace=workspace,
                catalog=catalog,
                patients=patients,
                report=report,
                smoke=args.profile == "smoke",
            )
        counts = report.counts()
        exit_code = 1 if counts.get("FAIL", 0) else 0
    except Exception as exc:  # noqa: BLE001
        print(traceback.format_exc(), file=sys.stderr)
        if report is not None:
            _record(
                report,
                CheckResult(
                    name="suite_exception",
                    category="setup",
                    status="FAIL",
                    duration_ms=0,
                    error=f"{type(exc).__name__}: {exc}",
                ),
            )
        exit_code = 1
    finally:
        try:
            db.close()
        finally:
            if args.keep_data:
                outer.commit()
            else:
                outer.rollback()
            connection.close()
            engine.dispose()

    if report is not None:
        report_path = Path(args.report)
        if not report_path.is_absolute():
            report_path = Path.cwd() / report_path
        report_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            **{key: value for key, value in asdict(report).items() if key != "results"},
            "counts": report.counts(),
            "results": [asdict(row) for row in report.results],
        }
        report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print("\nSummary:", json.dumps(report.counts(), ensure_ascii=False))
        print(f"Report: {report_path}")
        if not args.keep_data:
            print("Database writes rolled back: yes")
        print("WhatsApp/n8n used: no")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
