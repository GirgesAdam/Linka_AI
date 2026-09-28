"""Temporary Phase 3G bounded staging review for upcoming appointment responses.

All fixture mutations and chat persistence run inside one outer SQL transaction per
scenario and are rolled back. Agent turns are informational only.
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.agent_eval.harness import RuntimeProbe, assert_demo_only, runtime_summary

from app.agents.clinic_grounding import build_clinic_catalog
from app.core.config import settings
from app.models.appointment import ACTIVE_APPOINTMENT_STATUSES, Appointment
from app.models.branch import Branch
from app.models.clinic_inventory import ServiceDevicePrice
from app.models.message import Message
from app.models.patient import Patient
from app.models.payment_transaction import PaymentTransaction
from app.models.workspace import Workspace
from app.services.agent_v2.live_chat import run_agent_chat as run_agent_chat_v2
from scripts.run_live_agent_ux_review import _base_patient, _payload


@dataclass
class Result:
    name: str
    response: str = ""
    model: str = ""
    reads: list[str] | None = None
    llm_operations: list[str] | None = None
    appointment_delta: int = 0
    financial_delta: int = 0
    responder_llm_calls: int = 0
    classification: str = "MATERIAL"
    error: str | None = None


def _count(db: Session, model, workspace_id: UUID) -> int:
    return int(
        db.scalar(select(func.count(model.id)).where(model.workspace_id == workspace_id))
        or 0
    )


def _reset_upcoming(db: Session, workspace: Workspace, patient: Patient) -> None:
    now = datetime.now(UTC)
    rows = db.scalars(
        select(Appointment).where(
            Appointment.workspace_id == workspace.id,
            Appointment.patient_id == patient.id,
            Appointment.status.in_(ACTIVE_APPOINTMENT_STATUSES),
            Appointment.start_at >= now - timedelta(hours=6),
        )
    ).all()
    for row in rows:
        row.status = "cancelled"
    db.flush()
def _catalog_context(
    db: Session,
    workspace: Workspace,
    *,
    two_services: bool = False,
    prefer_device: bool = False,
):
    catalog = build_clinic_catalog(db, workspace)
    services = {
        str(row["id"]): row
        for row in catalog.get("services", [])
        if isinstance(row, dict) and row.get("id")
    }
    doctors = [
        row
        for row in catalog.get("doctors", [])
        if isinstance(row, dict) and row.get("id")
    ]
    for doctor in doctors:
        ids = [
            str(value)
            for value in doctor.get("service_ids", [])
            if str(value) in services
        ]
        if prefer_device:
            if ids and not any(services[value].get("laser_devices") for value in ids):
                target = services[ids[0]]
                device_price = ServiceDevicePrice(
                    workspace_id=workspace.id,
                    service_id=UUID(ids[0]),
                    device_key="candela_gentle",
                    device_name="Candela Gentle",
                    price_minor=int(target.get("price_minor") or 0),
                    duration_minutes=int(target.get("duration_minutes") or 30),
                    currency=str(target.get("currency") or "EGP"),
                    is_active=True,
                )
                db.add(device_price)
                db.flush()
                target["laser_devices"] = [
                    {
                        "device_key": "candela_gentle",
                        "device_name": "Candela Gentle",
                        "price_minor": int(target.get("price_minor") or 0),
                        "currency": str(target.get("currency") or "EGP"),
                        "configured": True,
                    }
                ]
            ids.sort(
                key=lambda value: bool(services[value].get("laser_devices")),
                reverse=True,
            )
        required = 2 if two_services else 1
        if len(ids) >= required:
            selected = [services[value] for value in ids[:required]]
            return selected, doctor
    raise RuntimeError("No compatible service/doctor fixture context")


def _device(service: dict[str, object]) -> tuple[str | None, str | None, int]:
    devices = [
        row
        for row in service.get("laser_devices", [])
        if isinstance(row, dict) and row.get("device_key")
    ]
    if not devices:
        return None, None, int(service.get("price_minor") or 0)
    selected = devices[0]
    return (
        str(selected.get("device_key")),
        str(selected.get("device_name") or selected.get("device_key")),
        int(selected.get("price_minor") or service.get("price_minor") or 0),
    )


def _add_appointment(
    db: Session,
    workspace: Workspace,
    patient: Patient,
    *,
    service: dict[str, object],
    doctor: dict[str, object],
    start_local: datetime,
    visit_group_id: UUID | None = None,
) -> Appointment:
    timezone = ZoneInfo(workspace.timezone)
    start_at = start_local.replace(tzinfo=timezone).astimezone(UTC)
    duration = int(service.get("duration_minutes") or 30)
    end_at = start_at + timedelta(minutes=duration)
    device_key, device_name, price_minor = _device(service)
    branch_id = workspace.primary_branch_id or db.scalar(
        select(Branch.id)
        .where(Branch.workspace_id == workspace.id, Branch.is_active.is_(True))
        .order_by(Branch.created_at.asc())
        .limit(1)
    )
    if branch_id is None:
        raise RuntimeError("Regression workspace has no active branch")
    row = Appointment(
        workspace_id=workspace.id,
        patient_id=patient.id,
        branch_id=branch_id,
        doctor_id=UUID(str(doctor["id"])),
        service_id=UUID(str(service["id"])),
        status="confirmed",
        source="staff",
        start_at=start_at,
        end_at=end_at,
        busy_start_at=start_at,
        busy_end_at=end_at,
        duration_minutes=duration,
        price_minor=price_minor,
        currency=str(service.get("currency") or "EGP"),
        payment_status="paid",
        amount_paid_minor=price_minor,
        payment_method="cash",
        billing_context="standard",
        laser_device_key=device_key,
        laser_device_name=device_name,
        visit_group_id=visit_group_id,
        confirmed_at=datetime.now(UTC),
    )
    db.add(row)
    db.flush()
    return row
def _seed(
    db: Session,
    workspace: Workspace,
    patient: Patient,
    name: str,
) -> tuple[list[Appointment], dict[str, object]]:
    _reset_upcoming(db, workspace, patient)
    base = datetime.now(ZoneInfo(workspace.timezone)).replace(
        hour=18, minute=0, second=0, microsecond=0
    ) + timedelta(days=3)
    if name == "empty":
        return [], {}

    if name == "grouped":
        services, doctor = _catalog_context(db, workspace, two_services=True)
        group = uuid4()
        first = _add_appointment(
            db, workspace, patient,
            service=services[0], doctor=doctor, start_local=base,
            visit_group_id=group,
        )
        second = _add_appointment(
            db, workspace, patient,
            service=services[1], doctor=doctor,
            start_local=base + timedelta(minutes=90),
            visit_group_id=group,
        )
        return [first, second], {
            "service_names": [str(services[0]["name"]), str(services[1]["name"])],
            "doctor_name": str(doctor.get("name") or ""),
        }

    services, doctor = _catalog_context(
        db, workspace, prefer_device=(name == "doctor_device")
    )
    first = _add_appointment(
        db, workspace, patient,
        service=services[0], doctor=doctor, start_local=base,
    )
    rows = [first]
    if name == "multiple":
        rows.append(
            _add_appointment(
                db, workspace, patient,
                service=services[0], doctor=doctor,
                start_local=base + timedelta(days=2),
            )
        )
    device_name = _device(services[0])[1]
    return rows, {
        "service_names": [str(services[0]["name"])],
        "doctor_name": str(doctor.get("name") or ""),
        "device_name": device_name,
    }


def _send(
    db: Session,
    workspace: Workspace,
    patient: Patient,
    message: str,
    conversation_id: UUID | None = None,
):
    with RuntimeProbe() as probe:
        response = run_agent_chat_v2(
            db=db,
            workspace=workspace,
            payload=_payload(patient.id, message, conversation_id),
        )
    reads, write_attempted, _write_result = runtime_summary(probe)
    llm_ops = [str(item.get("operation") or "") for item in probe.llm_calls]
    return response, reads, write_attempted, llm_ops


def _message_for(name: str, service_name: str | None = None) -> str:
    if name == "single":
        return "ميعادي الجاي إمتى؟"
    if name == "multiple":
        return "عندي مواعيد إيه جاية؟"
    if name == "grouped":
        return "إيه الخدمات الموجودة في الميعاد الجاي ومين الدكتور؟"
    if name == "doctor_device":
        return "المعاد الجاي مع مين وعلى جهاز إيه؟"
    if name == "empty":
        return "عندي مواعيد جاية؟"
    if name == "mixed":
        return f"ميعادي الجاي إمتى وكمان قولي معلومات عن خدمة {service_name}؟"
    raise KeyError(name)


def _check_response(
    name: str,
    response: str,
    expected: dict[str, object],
) -> bool:
    if not response.strip():
        return False
    if name == "empty":
        return "مواعيد جاية" in response or "مواعيد" in response
    for service in expected.get("service_names", []):
        if str(service) not in response:
            return False
    doctor = str(expected.get("doctor_name") or "")
    if doctor and doctor not in response:
        return False
    if name == "doctor_device":
        device = str(expected.get("device_name") or "")
        if device and device not in response:
            return False
    return True


def _execute(engine, name: str) -> Result:
    connection = engine.connect()
    outer = connection.begin()
    db = Session(
        bind=connection,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    result = Result(name=name)
    try:
        workspace = db.scalar(select(Workspace).where(Workspace.slug == "tia-regression"))
        if workspace is None:
            raise RuntimeError("Regression workspace not found")
        workspace.is_demo = True
        db.flush()
        assert_demo_only(workspace)
        patient = _base_patient(db, workspace)
        fixture_name = "single" if name in {"stale", "mixed"} else name
        rows, expected = _seed(db, workspace, patient, fixture_name)
        before_appts = _count(db, Appointment, workspace.id)
        before_financial = _count(db, PaymentTransaction, workspace.id)
        if name == "stale":
            first, _reads, first_write, _first_ops = _send(
                db, workspace, patient, "أهلا"
            )
            if first_write:
                raise RuntimeError("Unexpected agent write in stale-context setup")
            outbound = db.get(Message, first.outbound_message_id)
            if outbound is None:
                raise RuntimeError("Could not find setup assistant message")
            outbound.content = "ميعادك الأحد الساعة 5 مع دكتور قديم."
            db.commit()
            response, reads, write_attempted, llm_ops = _send(
                db,
                workspace,
                patient,
                "ميعادي الجاي إمتى؟",
                first.conversation_id,
            )
        else:
            service_name = (
                str(expected.get("service_names", [""])[0])
                if expected.get("service_names")
                else None
            )
            response, reads, write_attempted, llm_ops = _send(
                db,
                workspace,
                patient,
                _message_for(name, service_name),
            )

        after_appts = _count(db, Appointment, workspace.id)
        after_financial = _count(db, PaymentTransaction, workspace.id)
        result.response = response.reply
        result.model = response.model
        result.reads = reads
        result.llm_operations = llm_ops
        result.appointment_delta = after_appts - before_appts
        result.financial_delta = after_financial - before_financial
        result.responder_llm_calls = sum(
            1
            for operation in llm_ops
            if "responder" in operation and "interpreter" not in operation
        )
        safe = (
            not write_attempted
            and result.appointment_delta == 0
            and result.financial_delta == 0
            and "appointments" in reads
            and _check_response(name, response.reply, expected)
        )
        if name != "mixed":
            safe = (
                safe
                and response.model == "deterministic:appointment-info-contract"
                and result.responder_llm_calls == 0
            )
        result.classification = "ACCEPTABLE" if safe else "MATERIAL"
    except Exception as exc:  # noqa: BLE001
        result.error = f"{type(exc).__name__}: {exc}"
    finally:
        db.close()
        if outer.is_active:
            outer.rollback()
        connection.close()
    return result


def main() -> int:
    if not settings.agent_v2_live_enabled:
        raise RuntimeError("AGENT_V2_LIVE_ENABLED must be true")
    names = (
        "single",
        "multiple",
        "grouped",
        "doctor_device",
        "empty",
        "stale",
        "mixed",
    )
    engine = create_engine(settings.database_url, pool_pre_ping=True)
    results = []
    try:
        for name in names:
            print(f"running {name}", flush=True)
            result = _execute(engine, name)
            results.append(result)
            print(json.dumps(asdict(result), ensure_ascii=False), flush=True)
    finally:
        engine.dispose()
    payload = {
        "phase": "3G",
        "database_writes_persisted": False,
        "agent_scenarios_are_read_only": True,
        "results": [asdict(item) for item in results],
    }
    path = Path("artifacts/v2-read-semantics-live-review.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    failed = [
        item.name
        for item in results
        if item.error is not None or item.classification == "MATERIAL"
    ]
    if failed:
        print("FAILED=" + ",".join(failed), flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
