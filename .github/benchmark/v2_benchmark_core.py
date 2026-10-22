from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import perf_counter
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

import app.services.agent_v2.live_chat as live_chat
import scripts.run_live_agent_ux_review as base
from app.agents.clinic_grounding import build_clinic_catalog
from app.core.config import settings
from app.integrations.clinic.base import AvailabilityRequest
from app.integrations.clinic.registry import get_clinic_adapter
from app.models.appointment import ACTIVE_APPOINTMENT_STATUSES, Appointment
from app.models.patient import Patient
from app.models.patient_package import PackageUsage, PatientPackage
from app.models.payment_transaction import PaymentTransaction
from app.models.pulse_billing import AppointmentPulseSettlement
from app.models.service import Service
from app.models.workspace import Workspace
from app.services.patient_packages import list_patient_packages
from langchain_openai import ChatOpenAI
from sqlalchemy import create_engine, exists, func, select, text
from sqlalchemy.orm import Session

WORKSPACE_SLUG = "tia"
REPORT = Path("artifacts/v2-realistic-benchmark.json")


@dataclass
class TurnRecord:
    customer: str
    assistant: str | None
    model: str | None
    duration_ms: int
    token_usage: dict[str, Any]
    understanding: dict[str, Any] | None
    plan: dict[str, Any] | None
    traces: list[dict[str, Any]]
    outcomes: list[dict[str, Any]]
    state_after: dict[str, Any]
    active_task: dict[str, Any] | None = None
    pending_write: dict[str, Any] | None = None
    verified_action_context: dict[str, Any] | None = None
    pending_choice: dict[str, Any] | None = None


@dataclass
class PreparedScenario:
    patient: Patient
    messages: list[str]
    observations: list[str] = field(default_factory=list)
    post: Any = None


@dataclass
class ScenarioResult:
    name: str
    topic: str
    turns: list[TurnRecord] = field(default_factory=list)
    db_before: dict[str, Any] = field(default_factory=dict)
    db_after: dict[str, Any] = field(default_factory=dict)
    db_delta: dict[str, Any] = field(default_factory=dict)
    observations: list[str] = field(default_factory=list)
    error: str | None = None


_TOKEN_BUCKET: list[dict[str, Any]] = []
_CAPTURED_TURN: list[Any] = []
_ORIGINAL_GENERATE = ChatOpenAI._generate
_ORIGINAL_ORCHESTRATE = live_chat.orchestrate_v2_turn


def _capture_generate(self, *args, **kwargs):
    result = _ORIGINAL_GENERATE(self, *args, **kwargs)
    usage: dict[str, Any] = {}
    llm_output = getattr(result, "llm_output", None)
    if isinstance(llm_output, dict) and isinstance(llm_output.get("token_usage"), dict):
        usage = dict(llm_output["token_usage"])
    if not usage:
        try:
            message = result.generations[0].message
            raw = getattr(message, "usage_metadata", None)
            if isinstance(raw, dict):
                usage = dict(raw)
        except Exception:
            pass

    def pick(*keys: str) -> int:
        for key in keys:
            value = usage.get(key)
            if isinstance(value, int):
                return value
        return 0

    input_tokens = pick("prompt_tokens", "input_tokens", "input_token_count")
    output_tokens = pick("completion_tokens", "output_tokens", "output_token_count")
    total_tokens = pick("total_tokens", "total_token_count") or input_tokens + output_tokens
    model_name = getattr(self, "model_name", None) or getattr(self, "model", None)
    _TOKEN_BUCKET.append(
        {
            "model": str(model_name) if model_name else None,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
        }
    )
    return result


def _capture_orchestrate(*args, **kwargs):
    turn = _ORIGINAL_ORCHESTRATE(*args, **kwargs)
    _CAPTURED_TURN[:] = [turn]
    return turn


ChatOpenAI._generate = _capture_generate
live_chat.orchestrate_v2_turn = _capture_orchestrate


def jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (UUID, datetime)):
        return str(value)
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if is_dataclass(value):
        return {item.name: jsonable(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [jsonable(item) for item in value]
    return str(value)


def token_summary() -> dict[str, Any]:
    return {
        "input_tokens": sum(int(row.get("input_tokens") or 0) for row in _TOKEN_BUCKET),
        "output_tokens": sum(int(row.get("output_tokens") or 0) for row in _TOKEN_BUCKET),
        "total_tokens": sum(int(row.get("total_tokens") or 0) for row in _TOKEN_BUCKET),
        "calls": [dict(row) for row in _TOKEN_BUCKET],
    }


def state(db: Session, workspace: Workspace, patient: Patient) -> dict[str, int]:
    appointments = int(
        db.scalar(
            select(func.count(Appointment.id)).where(
                Appointment.workspace_id == workspace.id,
                Appointment.patient_id == patient.id,
            )
        )
        or 0
    )
    packages = int(
        db.scalar(
            select(func.count(PatientPackage.id)).where(
                PatientPackage.workspace_id == workspace.id,
                PatientPackage.patient_id == patient.id,
            )
        )
        or 0
    )
    usages = int(
        db.scalar(
            select(func.count(PackageUsage.id))
            .join(
                PatientPackage,
                (PatientPackage.workspace_id == PackageUsage.workspace_id)
                & (PatientPackage.id == PackageUsage.patient_package_id),
            )
            .where(
                PackageUsage.workspace_id == workspace.id,
                PatientPackage.patient_id == patient.id,
            )
        )
        or 0
    )
    active_packages = int(
        db.scalar(
            select(func.count(PatientPackage.id)).where(
                PatientPackage.workspace_id == workspace.id,
                PatientPackage.patient_id == patient.id,
                PatientPackage.status == "active",
            )
        )
        or 0
    )
    cancelled_packages = int(
        db.scalar(
            select(func.count(PatientPackage.id)).where(
                PatientPackage.workspace_id == workspace.id,
                PatientPackage.patient_id == patient.id,
                PatientPackage.status == "cancelled",
            )
        )
        or 0
    )
    payments = int(
        db.scalar(
            select(func.count(PaymentTransaction.id)).where(
                PaymentTransaction.workspace_id == workspace.id,
                PaymentTransaction.patient_id == patient.id,
                PaymentTransaction.transaction_type == "payment",
            )
        )
        or 0
    )
    refunds = int(
        db.scalar(
            select(func.count(PaymentTransaction.id)).where(
                PaymentTransaction.workspace_id == workspace.id,
                PaymentTransaction.patient_id == patient.id,
                PaymentTransaction.transaction_type == "refund",
            )
        )
        or 0
    )
    settlement_transactions = int(
        db.scalar(
            select(func.count(PaymentTransaction.id)).where(
                PaymentTransaction.workspace_id == workspace.id,
                PaymentTransaction.patient_id == patient.id,
                PaymentTransaction.reason.ilike("%settlement%"),
            )
        )
        or 0
    )
    pulse_settlements = int(
        db.scalar(
            select(func.count(AppointmentPulseSettlement.id))
            .join(
                Appointment,
                (Appointment.workspace_id == AppointmentPulseSettlement.workspace_id)
                & (Appointment.id == AppointmentPulseSettlement.appointment_id),
            )
            .where(
                AppointmentPulseSettlement.workspace_id == workspace.id,
                Appointment.patient_id == patient.id,
            )
        )
        or 0
    )
    return {
        "appointments": appointments,
        "patient_packages": packages,
        "patient_packages_active": active_packages,
        "patient_packages_cancelled": cancelled_packages,
        "package_usages": usages,
        "payments": payments,
        "refunds": refunds,
        "settlement_transactions": settlement_transactions,
        "settlements": pulse_settlements,
    }


def delta(before: dict[str, int], after: dict[str, int]) -> dict[str, int]:
    return {key: int(after.get(key, 0)) - int(before.get(key, 0)) for key in set(before) | set(after)}


def send(db: Session, workspace: Workspace, patient: Patient, message: str, conversation_id: UUID | None):
    _TOKEN_BUCKET.clear()
    _CAPTURED_TURN.clear()
    started = perf_counter()
    response = live_chat.run_agent_chat(
        db=db,
        workspace=workspace,
        payload=base._payload(patient.id, message, conversation_id),
    )
    duration_ms = int((perf_counter() - started) * 1000)
    turn = _CAPTURED_TURN[-1] if _CAPTURED_TURN else None
    traces = jsonable(getattr(turn, "traces", ())) if turn is not None else []
    outcomes = jsonable(getattr(turn, "outcomes", ())) if turn is not None else []
    record = TurnRecord(
        customer=message,
        assistant=response.reply,
        model=response.model,
        duration_ms=duration_ms,
        token_usage=token_summary(),
        understanding=jsonable(getattr(turn, "understanding", None)) if turn is not None else None,
        plan=jsonable(getattr(turn, "plan", None)) if turn is not None else None,
        traces=traces if isinstance(traces, list) else [],
        outcomes=outcomes if isinstance(outcomes, list) else [],
        state_after=state(db, workspace, patient),
        active_task=jsonable(getattr(turn, "active_task", None)) if turn is not None else None,
        pending_write=jsonable(getattr(turn, "pending_write", None)) if turn is not None else None,
        verified_action_context=(
            jsonable(getattr(turn, "verified_action_context", None)) if turn is not None else None
        ),
        pending_choice=jsonable(getattr(turn, "pending_choice", None)) if turn is not None else None,
    )
    return record, response.conversation_id


def run_messages(db: Session, workspace: Workspace, patient: Patient, messages: list[str]) -> list[TurnRecord]:
    turns: list[TurnRecord] = []
    conversation_id: UUID | None = None
    for message in messages:
        record, conversation_id = send(db, workspace, patient, message, conversation_id)
        turns.append(record)
    return turns


def patient_without_future(db: Session, workspace: Workspace) -> Patient:
    now = datetime.now(UTC)
    future = exists().where(
        Appointment.workspace_id == workspace.id,
        Appointment.patient_id == Patient.id,
        Appointment.status.in_(ACTIVE_APPOINTMENT_STATUSES),
        Appointment.start_at >= now,
    )
    patient = db.scalar(
        select(Patient)
        .where(Patient.workspace_id == workspace.id, Patient.status != "blocked", ~future)
        .order_by(Patient.created_at.asc())
        .limit(1)
    )
    return patient or base._base_patient(db, workspace)


def service_by_name(db: Session, workspace: Workspace, name: str) -> Service:
    service = db.scalar(
        select(Service).where(
            Service.workspace_id == workspace.id,
            Service.is_active.is_(True),
            Service.name == name,
        )
    )
    if service is None:
        raise RuntimeError(f"Active service not found: {name}")
    return service


def doctor_name(catalog: dict[str, Any], doctor_id: str | UUID) -> str:
    target = str(doctor_id)
    for row in catalog.get("doctors", []):
        if isinstance(row, dict) and str(row.get("id")) == target:
            return str(row.get("name") or target)
    return target


def availability(
    db: Session,
    workspace: Workspace,
    *,
    service_id: UUID | str,
    doctor_id: UUID | str | None = None,
    device_key: str | None = None,
    days: int = 35,
):
    catalog = build_clinic_catalog(db, workspace)
    target_service = str(service_id)
    doctors = [
        row
        for row in catalog.get("doctors", [])
        if isinstance(row, dict)
        and row.get("id")
        and target_service in {str(value) for value in (row.get("service_ids") or [])}
    ]
    if doctor_id is not None:
        doctors = [row for row in doctors if str(row.get("id")) == str(doctor_id)]
    adapter = get_clinic_adapter(db=db, workspace=workspace)
    branch_id = str(workspace.primary_branch_id)
    today = datetime.now(UTC).date()
    for offset in range(1, days + 1):
        day = today + timedelta(days=offset)
        for doctor in doctors:
            result = adapter.get_availability(
                AvailabilityRequest(
                    branch_id=branch_id,
                    service_id=target_service,
                    booking_date=day,
                    doctor_id=str(doctor["id"]),
                    laser_device_key=device_key,
                )
            )
            if result.slots:
                return catalog, doctor, day, result
    raise RuntimeError(f"No availability for service={service_id} doctor={doctor_id} device={device_key}")


def two_doctors_same_day(db: Session, workspace: Workspace):
    catalog = build_clinic_catalog(db, workspace)
    adapter = get_clinic_adapter(db=db, workspace=workspace)
    branch_id = str(workspace.primary_branch_id)
    today = datetime.now(UTC).date()
    for service in catalog.get("services", []):
        if not isinstance(service, dict) or not service.get("id") or service.get("requires_laser_device"):
            continue
        service_id = str(service["id"])
        doctors = [
            row
            for row in catalog.get("doctors", [])
            if isinstance(row, dict)
            and row.get("id")
            and service_id in {str(value) for value in (row.get("service_ids") or [])}
        ]
        if len(doctors) < 2:
            continue
        for offset in range(1, 21):
            day = today + timedelta(days=offset)
            choices = []
            for doctor in doctors:
                response = adapter.get_availability(
                    AvailabilityRequest(
                        branch_id=branch_id,
                        service_id=service_id,
                        booking_date=day,
                        doctor_id=str(doctor["id"]),
                    )
                )
                if response.slots:
                    choices.append((doctor, response))
            if len(choices) >= 2:
                return service, day, choices[0], choices[1]
    raise RuntimeError("No service with two doctors available on the same day")


def package_patient(db: Session, workspace: Workspace):
    selected = base._package_patient(db, workspace)
    if selected is None:
        raise RuntimeError("No usable package patient")
    patient, package = selected
    reads = list_patient_packages(
        db,
        workspace_id=workspace.id,
        patient_id=patient.id,
        service_id=package.service_id,
        usable_only=True,
    )
    if not reads:
        raise RuntimeError("Selected package is not usable")
    read = next((row for row in reads if row.id == package.id), reads[0])
    return patient, package, read


def package_offer(
    db: Session,
    workspace: Workspace,
    *,
    service_name: str,
    sessions: int,
    device_key: str,
) -> dict[str, Any]:
    row = db.execute(
        text(
            """
            select spo.id, spo.sessions_count, spo.price_minor, spo.currency,
                   spo.device_key, spo.device_name, s.id as service_id, s.name as service_name
            from service_package_offers spo
            join services s on s.id = spo.service_id and s.workspace_id = spo.workspace_id
            where spo.workspace_id = :workspace_id
              and spo.is_active = true
              and s.is_active = true
              and s.name = :service_name
              and spo.sessions_count = :sessions
              and spo.device_key = :device_key
            order by spo.price_minor asc
            limit 1
            """
        ),
        {
            "workspace_id": str(workspace.id),
            "service_name": service_name,
            "sessions": sessions,
            "device_key": device_key,
        },
    ).mappings().first()
    if row is None:
        raise RuntimeError(f"Package offer not found for {service_name}/{sessions}/{device_key}")
    return dict(row)


def replacement_slot(db: Session, workspace: Workspace, appointment: Appointment):
    adapter = get_clinic_adapter(db=db, workspace=workspace)
    tz = ZoneInfo(workspace.timezone)
    start_day = appointment.start_at.astimezone(tz).date()
    for offset in range(1, 21):
        day = start_day + timedelta(days=offset)
        response = adapter.get_availability(
            AvailabilityRequest(
                branch_id=str(appointment.branch_id),
                service_id=str(appointment.service_id),
                booking_date=day,
                doctor_id=str(appointment.doctor_id),
                exclude_appointment_id=str(appointment.id),
                laser_device_key=appointment.laser_device_key,
            )
        )
        if response.slots:
            return day, response
    raise RuntimeError("No replacement slot found")


def appointment_ids(db: Session, workspace: Workspace, patient: Patient) -> set[UUID]:
    return set(
        db.scalars(
            select(Appointment.id).where(
                Appointment.workspace_id == workspace.id,
                Appointment.patient_id == patient.id,
            )
        )
    )


def package_ids(db: Session, workspace: Workspace, patient: Patient) -> set[UUID]:
    return set(
        db.scalars(
            select(PatientPackage.id).where(
                PatientPackage.workspace_id == workspace.id,
                PatientPackage.patient_id == patient.id,
            )
        )
    )


def append_new_rows(
    db: Session,
    workspace: Workspace,
    patient: Patient,
    before_appointments: set[UUID],
    before_packages: set[UUID],
    observations: list[str],
) -> None:
    new_appointments = list(
        db.scalars(
            select(Appointment).where(
                Appointment.workspace_id == workspace.id,
                Appointment.patient_id == patient.id,
                Appointment.id.notin_(before_appointments),
            )
        )
    )
    for row in new_appointments:
        observations.append(
            "new_appointment="
            + json.dumps(
                {
                    "id": str(row.id),
                    "service_id": str(row.service_id),
                    "doctor_id": str(row.doctor_id),
                    "start_at": row.start_at.isoformat(),
                    "end_at": row.end_at.isoformat(),
                    "status": row.status,
                    "billing_context": row.billing_context,
                    "patient_package_id": str(row.patient_package_id) if row.patient_package_id else None,
                    "laser_device_key": row.laser_device_key,
                    "price_minor": row.price_minor,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
    new_packages = list(
        db.scalars(
            select(PatientPackage).where(
                PatientPackage.workspace_id == workspace.id,
                PatientPackage.patient_id == patient.id,
                PatientPackage.id.notin_(before_packages),
            )
        )
    )
    for row in new_packages:
        observations.append(
            "new_package="
            + json.dumps(
                {
                    "id": str(row.id),
                    "service_id": str(row.service_id),
                    "name": row.name,
                    "sessions_purchased": row.sessions_purchased,
                    "status": row.status,
                    "laser_device_key": row.laser_device_key,
                    "sale_price_minor": row.sale_price_minor,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )


def generic_context(db: Session, workspace: Workspace, patient: Patient | None = None):
    patient = patient or patient_without_future(db, workspace)
    catalog, service, doctor, branch_id, day, available = base._booking_context(db, workspace)
    tz = ZoneInfo(available.timezone)
    return patient, catalog, service, doctor, branch_id, day, available, tz, available.slots[0]


def execute_one(engine, name: str, topic: str) -> ScenarioResult:
    from v2_benchmark_scenarios import prepare_scenario

    connection = engine.connect()
    outer = connection.begin()
    db = Session(bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint")
    result = ScenarioResult(name=name, topic=topic)
    workspace: Workspace | None = None
    prepared: PreparedScenario | None = None
    before: dict[str, Any] = {}
    before_appointments: set[UUID] = set()
    before_packages: set[UUID] = set()
    try:
        workspace = db.scalar(select(Workspace).where(Workspace.slug == WORKSPACE_SLUG))
        if workspace is None:
            raise RuntimeError("Workspace tia not found")
        prepared = prepare_scenario(db, workspace, name)
        before = state(db, workspace, prepared.patient)
        before_appointments = appointment_ids(db, workspace, prepared.patient)
        before_packages = package_ids(db, workspace, prepared.patient)
        result.db_before = dict(before)
        conversation_id: UUID | None = None
        for message in prepared.messages:
            record, conversation_id = send(db, workspace, prepared.patient, message, conversation_id)
            result.turns.append(record)
        if prepared.post is not None:
            prepared.post()
    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}"
    finally:
        if workspace is not None and prepared is not None:
            try:
                after = state(db, workspace, prepared.patient)
                result.db_after = dict(after)
                result.db_delta = delta(before, after)
                observations = list(prepared.observations)
                append_new_rows(
                    db,
                    workspace,
                    prepared.patient,
                    before_appointments,
                    before_packages,
                    observations,
                )
                result.observations = observations
            except Exception as evidence_exc:
                if result.error is None:
                    result.error = f"{type(evidence_exc).__name__}: {evidence_exc}"
                else:
                    result.observations.append(
                        f"evidence_capture_error={type(evidence_exc).__name__}: {evidence_exc}"
                    )
        db.close()
        if outer.is_active:
            outer.rollback()
        connection.close()
    return result


def main() -> int:
    from v2_benchmark_scenarios import SCENARIOS, TOPICS

    if not settings.agent_v2_live_enabled:
        raise RuntimeError("Benchmark requires AGENT_V2_LIVE_ENABLED=true; refusing V1 fallback.")

    engine = create_engine(settings.database_url, pool_pre_ping=True)
    results: list[ScenarioResult] = []
    started = datetime.now(UTC)
    try:
        for index, name in enumerate(SCENARIOS, start=1):
            print(f"[{index:02d}/{len(SCENARIOS)}] {name}", flush=True)
            result = execute_one(engine, name, TOPICS[name])
            results.append(result)
            compact = {
                "name": result.name,
                "error": result.error,
                "db_delta": result.db_delta,
                "turns": [
                    {
                        "customer": turn.customer,
                        "assistant": turn.assistant,
                        "model": turn.model,
                        "duration_ms": turn.duration_ms,
                        "tokens": turn.token_usage.get("total_tokens", 0),
                    }
                    for turn in result.turns
                ],
            }
            print(json.dumps(compact, ensure_ascii=False, separators=(",", ":")), flush=True)
    finally:
        engine.dispose()

    summary = {
        "runtime_errors": sum(1 for item in results if item.error),
        "total_turns": sum(len(item.turns) for item in results),
        "total_duration_ms": sum(turn.duration_ms for item in results for turn in item.turns),
        "total_input_tokens": sum(
            int(turn.token_usage.get("input_tokens") or 0) for item in results for turn in item.turns
        ),
        "total_output_tokens": sum(
            int(turn.token_usage.get("output_tokens") or 0) for item in results for turn in item.turns
        ),
        "total_tokens": sum(
            int(turn.token_usage.get("total_tokens") or 0) for item in results for turn in item.turns
        ),
    }
    payload = {
        "started_at": started.isoformat(),
        "finished_at": datetime.now(UTC).isoformat(),
        "workspace_slug": WORKSPACE_SLUG,
        "runtime": "v2",
        "scenario_count": len(results),
        "database_writes_persisted": False,
        "whatsapp_or_n8n_used": False,
        "scenario_names": list(SCENARIOS),
        "results": [asdict(item) for item in results],
        "summary": summary,
    }
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print("BENCHMARK_SUMMARY=" + json.dumps(summary, ensure_ascii=False, separators=(",", ":")), flush=True)
    print(f"REPORT={REPORT}", flush=True)
    return 1 if summary["runtime_errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
