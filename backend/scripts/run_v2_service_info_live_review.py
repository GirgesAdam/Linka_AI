from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

import scripts.run_live_agent_ux_review as base
from app.agents.clinic_grounding import build_clinic_catalog
from app.core.config import settings
from app.models.appointment import Appointment
from app.models.payment_transaction import PaymentTransaction
from app.models.workspace import Workspace
from app.services.agent_v2.live_chat import run_agent_chat as run_agent_chat_v2


@dataclass
class Result:
    name: str
    message: str
    response: str | None = None
    model: str | None = None
    appointment_delta: int = 0
    financial_delta: int = 0
    classification: str = "ACCEPTABLE"
    error: str | None = None

def _count(db: Session, model, workspace_id) -> int:
    return int(
        db.scalar(
            select(func.count(model.id)).where(model.workspace_id == workspace_id)
        )
        or 0
    )


def _service_names(catalog: dict[str, object]) -> tuple[str, str | None]:
    rows = [
        row
        for row in catalog.get("services", [])
        if isinstance(row, dict) and row.get("name")
    ]
    if not rows:
        raise RuntimeError("No services available in catalog")
    first = str(rows[0]["name"])
    laser = next(
        (
            str(row["name"])
            for row in rows
            if isinstance(row.get("laser_devices"), list) and row.get("laser_devices")
        ),
        None,
    )
    return first, laser

def _run_one(engine, name: str, message_builder) -> Result:
    connection = engine.connect()
    outer = connection.begin()
    db = Session(
        bind=connection,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    try:
        workspace = db.scalar(select(Workspace).where(Workspace.slug == "tia"))
        if workspace is None:
            raise RuntimeError("Workspace tia not found")
        patient = base._base_patient(db, workspace)
        catalog = build_clinic_catalog(db, workspace)
        first_service, laser_service = _service_names(catalog)
        message = message_builder(first_service, laser_service)
        before_a = _count(db, Appointment, workspace.id)
        before_f = _count(db, PaymentTransaction, workspace.id)
        reply = run_agent_chat_v2(
            db=db,
            workspace=workspace,
            payload=base._payload(patient.id, message, None),
        )
        after_a = _count(db, Appointment, workspace.id)
        after_f = _count(db, PaymentTransaction, workspace.id)
        result = Result(
            name=name,
            message=message,
            response=reply.reply,
            model=reply.model,
            appointment_delta=after_a - before_a,
            financial_delta=after_f - before_f,
        )
        if result.appointment_delta != 0 or result.financial_delta != 0:
            result.classification = "ROBOTIC/MATERIAL"
            result.error = "Unexpected write delta"
        return result
    except Exception as exc:  # noqa: BLE001
        return Result(
            name=name,
            message="",
            classification="ROBOTIC/MATERIAL",
            error=f"{type(exc).__name__}: {exc}",
        )
    finally:
        db.close()
        if outer.is_active:
            outer.rollback()
        connection.close()


def main() -> int:
    if not settings.agent_v2_live_enabled:
        raise RuntimeError("AGENT_V2_LIVE_ENABLED=true is required")
    cases = [
        ("service_list", lambda _s, _l: "إيه الخدمات الموجودة عندكم؟"),
        ("explicit_service", lambda s, _l: f"قولي عن خدمة {s}"),
        ("duration", lambda s, _l: f"مدة موعد {s} قد إيه؟"),
        ("devices", lambda s, laser: f"خدمة {laser or s} بتشتغل على أجهزة إيه؟"),
        ("mixed_price", lambda s, _l: f"قولي عن {s} وسعرها كام؟"),
        ("unknown_service", lambda _s, _l: "قولي عن خدمة اسمها Quantum Unicorn Therapy"),
        ("medical_suitability", lambda s, _l: f"هل خدمة {s} مناسبة لحالتي ومضمونة؟"),
    ]
    engine = create_engine(settings.database_url, pool_pre_ping=True)
    results: list[Result] = []
    try:
        for name, builder in cases:
            result = _run_one(engine, name, builder)
            results.append(result)
            print(json.dumps(asdict(result), ensure_ascii=False), flush=True)
    finally:
        engine.dispose()

    payload = {
        "started_at": datetime.now(UTC).isoformat(),
        "database_writes_persisted": False,
        "results": [asdict(item) for item in results],
    }
    path = Path("artifacts/v2-service-info-live-review.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return 1 if any(item.error for item in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
