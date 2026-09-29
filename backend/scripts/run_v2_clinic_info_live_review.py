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


def _service_name(catalog: dict[str, object]) -> str:
    for row in catalog.get("services", []):
        if isinstance(row, dict) and row.get("name"):
            return str(row["name"])
    raise RuntimeError("No service available in catalog")


def _validate(
    *,
    name: str,
    response: str,
    model: str,
    service_name: str,
) -> str | None:
    expected_by_case = {
        "clinic_name": ("Tia",),
        "address": ("Staging regression address",),
        "contact": ("+200000100001", "regression-main@tia.example"),
        "working_hours": ("10:00", "22:00"),
        "open_now": ("10:00", "22:00"),
        "general_info": ("PHASE3I VERIFIED POLICY",),
        "mixed_service": ("+200000100001", service_name),
    }
    for expected in expected_by_case[name]:
        if expected not in response:
            return f"missing expected verified value: {expected}"

    pure = {
        "clinic_name",
        "address",
        "contact",
        "working_hours",
        "open_now",
        "general_info",
    }
    if name in pure and model != "deterministic:clinic-information-contract":
        return f"pure clinic response used unexpected model: {model}"
    if name == "open_now":
        safe_markers = ("ما أقدرش أؤكد", "cannot confirm")
        if not any(marker in response for marker in safe_markers):
            return "open-now reply made no explicit live-state limitation"
    for forbidden in (
        "+200000100002",
        "regression-new-cairo@tia.example",
        "Staging regression New Cairo",
        "access_token",
        "api_key",
        "webhook",
        "phone_number_id",
        "workspace_id",
        "branch_id",
    ):
        if forbidden.casefold() in response.casefold():
            return f"forbidden clinic/internal value leaked: {forbidden}"
    return None


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
        service_name = _service_name(catalog)
        message = message_builder(service_name)
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
        validation_error = _validate(
            name=name,
            response=reply.reply,
            model=reply.model,
            service_name=service_name,
        )
        if validation_error:
            result.classification = "ROBOTIC/MATERIAL"
            result.error = validation_error
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
        ("clinic_name", lambda _s: "اسم العيادة إيه؟"),
        ("address", lambda _s: "عنوان العيادة فين؟"),
        ("contact", lambda _s: "رقم العيادة والإيميل المسجلين إيه؟"),
        ("working_hours", lambda _s: "مواعيد عمل العيادة إيه؟"),
        ("open_now", lambda _s: "العيادة مفتوحة دلوقتي؟"),
        ("general_info", lambda _s: "إيه المعلومات العامة المحفوظة عن العيادة؟"),
        ("mixed_service", lambda _s: f"رقم العيادة وعندكم خدمة {_s}؟"),
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
    path = Path("artifacts/v2-clinic-info-live-review.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return 1 if any(item.error for item in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
