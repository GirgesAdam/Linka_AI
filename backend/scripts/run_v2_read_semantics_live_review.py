from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

import scripts.run_live_agent_ux_review as base
from app.core.config import settings
from app.models.appointment import Appointment
from app.models.service import Service
from app.models.workspace import Workspace
from app.services.agent_v2.live_chat import run_agent_chat as run_agent_chat_v2


def _appointment_count(db: Session, workspace: Workspace, patient_id: UUID) -> int:
    return int(
        db.scalar(
            select(func.count(Appointment.id)).where(
                Appointment.workspace_id == workspace.id,
                Appointment.patient_id == patient_id,
            )
        )
        or 0
    )


def _send(db: Session, workspace: Workspace, patient_id: UUID, message: str):
    return run_agent_chat_v2(
        db=db,
        workspace=workspace,
        payload=base._payload(patient_id, message, None),
    )


def _case_service_price(db: Session, workspace: Workspace):
    patient = base._base_patient(db, workspace)
    service = db.scalar(
        select(Service)
        .where(
            Service.workspace_id == workspace.id,
            Service.is_active.is_(True),
            Service.requires_laser_device.is_(False),
            Service.price_minor > 0,
        )
        .order_by(Service.name.asc())
        .limit(1)
    )
    if service is None:
        raise RuntimeError("No active non-device priced service found.")
    prompt = f"خدمة {service.name} بتعمل إيه وسعرها كام؟ أنا بس بسأل."
    expected = {
        "service": service.name,
        "price_minor": service.price_minor,
        "currency": service.currency,
    }
    return patient, prompt, expected


def _booking_case_context(db: Session, workspace: Workspace):
    patient = base._base_patient(db, workspace)
    _catalog, service, _doctor, _branch_id, day, available = base._booking_context(db, workspace)
    expected = {
        "service": str(service.get("name") or ""),
        "date": day.isoformat(),
        "first_start_local": available.slots[0].start_at.astimezone(
            ZoneInfo(available.timezone)
        ).isoformat(),
    }
    return patient, service, day, expected


def _case_service_availability(db: Session, workspace: Workspace):
    patient, service, day, expected = _booking_case_context(db, workspace)
    prompt = (
        f"خدمة {service.get('name')} بتعمل إيه، وإيه المواعيد المتاحة ليها "
        f"يوم {day.isoformat()}؟ أنا بس بسأل."
    )
    return patient, prompt, expected


def _case_clinic_availability(db: Session, workspace: Workspace):
    patient, service, day, expected = _booking_case_context(db, workspace)
    prompt = (
        f"رقم العيادة كام، وإيه المواعيد المتاحة لخدمة {service.get('name')} "
        f"يوم {day.isoformat()}؟ أنا بس بسأل."
    )
    return patient, prompt, expected


def _case_appointment_service(db: Session, workspace: Workspace):
    patient = base._base_patient(db, workspace)
    rows = base._seed_upcoming(db, workspace, patient, 1)
    row = rows[0]
    service = db.get(Service, row.service_id)
    if service is None:
        raise RuntimeError("Seeded appointment service missing.")
    prompt = (
        f"ميعادي الجاي إمتى، وقولي كمان خدمة {service.name} بتعمل إيه؟ "
        "أنا بس بسأل ومش عايز أغير حاجة."
    )
    expected = {
        "service": service.name,
        "appointment_start": row.start_at.astimezone(
            ZoneInfo(workspace.timezone)
        ).isoformat(),
        "appointment_status": row.status,
    }
    return patient, prompt, expected


def _case_package_service(db: Session, workspace: Workspace):
    selected = base._package_patient(db, workspace)
    if selected is None:
        raise RuntimeError("No usable package patient found.")
    patient, package = selected
    service = db.get(Service, package.service_id)
    if service is None:
        raise RuntimeError("Package service missing.")
    prompt = (
        f"الباكيدج اللي عندي لخدمة {service.name} فاضل فيها كام جلسة، "
        "وقولي كمان الخدمة دي بتعمل إيه؟"
    )
    expected = {"service": service.name, "package_status": package.status}
    return patient, prompt, expected


CASES = (
    ("service_price", _case_service_price),
    ("service_availability", _case_service_availability),
    ("clinic_availability", _case_clinic_availability),
    ("appointment_service", _case_appointment_service),
    ("package_service", _case_package_service),
)


def _execute(engine, workspace_slug: str, name: str, builder):
    connection = engine.connect()
    outer = connection.begin()
    db = Session(bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint")
    result = {"name": name, "ok": False}
    try:
        workspace = db.scalar(select(Workspace).where(Workspace.slug == workspace_slug))
        if workspace is None:
            raise RuntimeError("Workspace not found.")
        patient, prompt, expected = builder(db, workspace)
        db.flush()
        before = _appointment_count(db, workspace, patient.id)
        response = _send(db, workspace, patient.id, prompt)
        after = _appointment_count(db, workspace, patient.id)
        model = response.model or ""
        reply = response.reply or ""
        result.update(
            {
                "prompt": prompt,
                "reply": reply,
                "model": model,
                "expected": expected,
                "appointment_delta": after - before,
                "handoff_required": response.handoff_required,
                "ok": (
                    model == "deterministic:mixed-typed-contract"
                    and after == before
                    and not response.handoff_required
                    and bool(reply.strip())
                ),
            }
        )
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        db.close()
        if outer.is_active:
            outer.rollback()
        connection.close()
    return result


def main() -> int:
    if not settings.agent_v2_live_enabled:
        raise RuntimeError("AGENT_V2_LIVE_ENABLED must be true.")
    engine = create_engine(settings.database_url, pool_pre_ping=True)
    results = []
    try:
        for index, (name, builder) in enumerate(CASES, 1):
            result = _execute(engine, "tia", name, builder)
            results.append(result)
            print(
                f"[{index}/{len(CASES)}] "
                + json.dumps(result, ensure_ascii=False, separators=(",", ":")),
                flush=True,
            )
    finally:
        engine.dispose()
    payload = {
        "git_sha": "0dac966870ffcd4428e139c687787c5319b5f5f5",
        "started_at": datetime.now(UTC).isoformat(),
        "scenario_count": len(results),
        "database_writes_persisted": False,
        "results": results,
    }
    report = Path("artifacts/v2-read-semantics-live-review.json")
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    passed = sum(bool(item.get("ok")) for item in results)
    print(f"Summary: {passed}/{len(results)} passed", flush=True)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
