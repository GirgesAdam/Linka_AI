from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

import scripts.run_live_agent_ux_review as base
from app.core.config import settings
from app.models.appointment import Appointment
from app.models.branch import Branch
from app.models.patient import Patient
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
    return int(db.scalar(select(func.count(model.id)).where(model.workspace_id == workspace_id)) or 0)


def _run_one(engine, name: str, message: str) -> Result:
    connection = engine.connect()
    outer = connection.begin()
    db = Session(bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint")
    try:
        workspace = db.scalar(select(Workspace).where(Workspace.slug == "tia"))
        if workspace is None:
            raise RuntimeError("Workspace tia not found")
        branch = db.scalar(
            select(Branch)
            .where(Branch.workspace_id == workspace.id, Branch.is_active.is_(True))
            .order_by(Branch.created_at.asc())
        )
        if branch is None:
            branch = Branch(
                workspace_id=workspace.id,
                name="Main Regression Branch",
                code="main-regression",
                country_code="EG",
                timezone="Africa/Cairo",
                is_active=True,
            )
            db.add(branch)
            db.flush()
        workspace.name = "Tia Clinic Regression"
        workspace.primary_branch_id = branch.id
        branch.name = "Main Regression Branch"
        branch.phone = "01012345678"
        branch.email = "clinic-regression@example.test"
        branch.address_line1 = "10 Nile Regression St"
        branch.city = "Cairo"
        branch.country_code = "EG"
        db.flush()

        patient = db.scalar(
            select(Patient)
            .where(Patient.workspace_id == workspace.id, Patient.status != "blocked")
            .order_by(Patient.created_at.asc())
            .limit(1)
        )
        if patient is None:
            patient = Patient(
                workspace_id=workspace.id,
                first_name="Clinic",
                last_name="Review",
                phone="01000000000",
                phone_normalized="201000000000",
                preferred_language="ar",
                source="other",
                status="active",
            )
            db.add(patient)
            db.flush()
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

        response = reply.reply or ""
        if name == "clinic_name" and "Tia Clinic Regression" not in response:
            result.error = "Verified clinic name missing"
        elif name == "address" and ("10 Nile Regression St" not in response or "Cairo" not in response):
            result.error = "Verified clinic address missing"
        elif name == "contact" and (
            "01012345678" not in response or "clinic-regression@example.test" not in response
        ):
            result.error = "Verified clinic contact missing"
        elif name == "open_now_control":
            lowered = response.casefold()
            if any(token in lowered for token in ("مفتوح الآن", "مفتوحين دلوقتي", "مغلق الآن", "closed now", "open now")):
                result.error = "Unsupported open/closed claim"
        if result.error:
            result.classification = "ROBOTIC/MATERIAL"
        return result
    except Exception as exc:  # noqa: BLE001
        return Result(
            name=name,
            message=message,
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
        ("clinic_name", "اسم العيادة إيه؟"),
        ("address", "عنوان العيادة فين؟"),
        ("contact", "رقم العيادة والإيميل إيه؟"),
        ("broad_info", "قولي بيانات العيادة عندكم"),
        ("mixed_service", "عنوان العيادة وإيه الخدمات الموجودة؟"),
        ("open_now_control", "العيادة مفتوحة دلوقتي؟"),
    ]
    engine = create_engine(settings.database_url, pool_pre_ping=True)
    results = []
    try:
        for name, message in cases:
            result = _run_one(engine, name, message)
            results.append(result)
            print(json.dumps(asdict(result), ensure_ascii=False), flush=True)
    finally:
        engine.dispose()

    payload = {
        "started_at": datetime.now(UTC).isoformat(),
        "database_writes_persisted": False,
        "results": [asdict(item) for item in results],
    }
    path = Path("artifacts/v2-clinic-info-live-review.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return 1 if any(item.error for item in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
