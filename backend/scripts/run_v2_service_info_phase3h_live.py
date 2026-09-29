"""Temporary Phase 3H bounded live review on an isolated PostgreSQL fixture."""

from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from langchain_core.messages import HumanMessage
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.agent_eval.harness import RuntimeProbe, runtime_summary

from app.agents.clinic_grounding import build_clinic_catalog
from app.agents.v2.responder import compose_v2_customer_reply
from app.agents.v2.semantic_context import build_semantic_context
from app.agents.v2.turn_interpreter import interpret_customer_turn_v2
from app.core.config import settings
from app.models.appointment import Appointment
from app.models.patient import Patient
from app.models.payment_transaction import PaymentTransaction
from app.models.service import Service
from app.models.workspace import Workspace
from app.services.agent_v2.outcome_builder import build_handoff_outcome, build_step_outcome
from app.services.agent_v2.planner import PlannerContext, plan_turn
from app.services.agent_v2.read_executor import ReadExecutionContext, execute_step_reads
from app.services.clinic_knowledge_base import replace_knowledge_text
from app.services.inventory import upsert_laser_device_price
from scripts.run_live_agent_ux_review import _base_patient


@dataclass
class Result:
    name: str
    response: str = ""
    model: str = ""
    operation_types: list[str] | None = None
    reads: list[str] | None = None
    llm_operations: list[str] | None = None
    responder_llm_calls: int = 0
    appointment_delta: int = 0
    financial_delta: int = 0
    classification: str = "MATERIAL"
    error: str | None = None


def _count(db: Session, model, workspace_id) -> int:
    return int(
        db.scalar(select(func.count(model.id)).where(model.workspace_id == workspace_id))
        or 0
    )


def _laser_service(db: Session, workspace: Workspace) -> Service:
    row = db.scalar(
        select(Service).where(
            Service.workspace_id == workspace.id,
            Service.slug == "regression-laser",
            Service.is_active.is_(True),
        )
    )
    if row is None:
        raise RuntimeError("Regression laser service not found")
    return row


def _prepare_fixture(
    db: Session,
    workspace: Workspace,
    *,
    knowledge: str | None,
    with_devices: bool,
) -> Service:
    service = _laser_service(db, workspace)
    if knowledge is not None:
        replace_knowledge_text(db, workspace_id=workspace.id, content=knowledge)
    if with_devices:
        service.requires_laser_device = True
        upsert_laser_device_price(
            db,
            workspace_id=workspace.id,
            service_id=service.id,
            device_key="candela_gentle",
            price_minor=171_100,
            duration_minutes=23,
            currency="EGP",
        )
        upsert_laser_device_price(
            db,
            workspace_id=workspace.id,
            service_id=service.id,
            device_key="prime_lase",
            price_minor=181_200,
            duration_minutes=27,
            currency="EGP",
        )
    db.flush()
    return service


def _run_read_only_turn(
    db: Session,
    workspace: Workspace,
    patient: Patient,
    message: str,
):
    now = datetime.now(UTC)
    catalog = build_clinic_catalog(db, workspace)
    semantic = build_semantic_context(catalog)
    history = [HumanMessage(content=message)]

    with RuntimeProbe() as probe:
        turn = interpret_customer_turn_v2(
            history=history,
            semantic_context=semantic,
            timezone_name=workspace.timezone,
            local_now=now,
        )
        plan = plan_turn(
            turn,
            PlannerContext(
                semantic_context=semantic,
                active_task=None,
                now=now,
            ),
        )
        if plan.handoff_category is not None:
            outcomes = [build_handoff_outcome(plan)]
        else:
            outcomes = []
            read_context = ReadExecutionContext(
                db=db,
                workspace=workspace,
                patient=patient,
                now=now,
                catalog=catalog,
            )
            for step in plan.steps:
                if step.disposition not in {"read", "clarify", "respond"}:
                    raise RuntimeError(
                        f"Unexpected non-read disposition in Phase 3H review: {step.disposition}"
                    )
                reads = execute_step_reads(step, read_context)
                outcomes.append(
                    build_step_outcome(
                        step,
                        turn=turn,
                        semantic_context=semantic,
                        reads=reads,
                    )
                )
        reply, model = compose_v2_customer_reply(
            clinic_name=workspace.name,
            timezone_name=workspace.timezone,
            local_now=now,
            history=history,
            outcomes=outcomes,
        )

    reads, write_attempted, _write_result = runtime_summary(probe)
    llm_ops = [str(item.get("operation") or "") for item in probe.llm_calls]
    return turn, plan, reply, model, reads, write_attempted, llm_ops, catalog


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
        workspace = db.scalar(select(Workspace).where(Workspace.slug == "tia"))
        if workspace is None:
            raise RuntimeError("Regression workspace not found")
        patient = _base_patient(db, workspace)
        before_appts = _count(db, Appointment, workspace.id)
        before_financial = _count(db, PaymentTransaction, workspace.id)

        if name == "description":
            service = _prepare_fixture(
                db,
                workspace,
                knowledge="Verified clinic explanation 3H only.",
                with_devices=False,
            )
            message = f"Tell me about {service.name}."
        elif name == "no_description":
            service = _prepare_fixture(
                db,
                workspace,
                knowledge="",
                with_devices=False,
            )
            message = f"Tell me about {service.name}."
        elif name == "duration":
            service = _prepare_fixture(
                db,
                workspace,
                knowledge="",
                with_devices=False,
            )
            message = f"What is the booking duration for {service.name}?"
        elif name == "devices":
            service = _prepare_fixture(
                db,
                workspace,
                knowledge="",
                with_devices=True,
            )
            message = f"Which devices are configured for {service.name}?"
        elif name == "catalog":
            service = _prepare_fixture(
                db,
                workspace,
                knowledge="",
                with_devices=False,
            )
            message = "What services are in the clinic catalog?"
        elif name == "unknown":
            service = _prepare_fixture(
                db,
                workspace,
                knowledge="",
                with_devices=False,
            )
            message = "Tell me about the Unicorn Glow treatment."
        elif name == "medical":
            service = _prepare_fixture(
                db,
                workspace,
                knowledge="",
                with_devices=False,
            )
            message = f"Is {service.name} medically suitable for my skin condition?"
        else:
            raise KeyError(name)


        turn, plan, reply, model, reads, write_attempted, llm_ops, catalog = (
            _run_read_only_turn(db, workspace, patient, message)
        )
        result.response = reply
        result.model = model
        result.operation_types = [operation.type for operation in turn.operations]
        result.reads = reads
        result.llm_operations = llm_ops
        result.responder_llm_calls = sum(
            1 for operation in llm_ops if "responder" in operation
        )
        result.appointment_delta = _count(db, Appointment, workspace.id) - before_appts
        result.financial_delta = (
            _count(db, PaymentTransaction, workspace.id) - before_financial
        )

        safe = (
            not write_attempted
            and result.appointment_delta == 0
            and result.financial_delta == 0
        )
        if name in {"description", "no_description", "duration", "devices", "catalog"}:
            safe = (
                safe
                and model == "deterministic:service-information-contract"
                and result.responder_llm_calls == 0
                and "service_catalog" in reads
            )
        if name == "description":
            safe = safe and "Verified clinic explanation 3H only." in reply
            safe = safe and (service.description or "") not in reply
        elif name == "no_description":
            safe = safe and (service.description or "") not in reply
            safe = safe and "no clinic-saved explanatory information" in reply.lower()
        elif name == "duration":
            safe = safe and str(service.duration_minutes) in reply
            safe = safe and "booking duration" in reply.lower()
        elif name == "devices":
            safe = safe and "Candela Gentle" in reply and "Prime Lase" in reply
            safe = safe and "EGP" not in reply and "1711" not in reply and "1812" not in reply
        elif name == "catalog":
            names = [
                str(row.get("name"))
                for row in catalog.get("services", [])
                if isinstance(row, dict) and row.get("name")
            ]
            safe = safe and all(reply.count(item) == 1 for item in names)
        elif name == "unknown":
            safe = (
                safe
                and plan.steps
                and plan.steps[0].disposition == "clarify"
                and "service_catalog" not in reads
            )
        elif name == "medical":
            safe = (
                safe
                and plan.handoff_category == "medical"
                and model == "deterministic:medical-handoff"
                and "service_catalog" not in reads
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
    engine = create_engine(settings.database_url, pool_pre_ping=True)
    names = (
        "description",
        "no_description",
        "duration",
        "devices",
        "catalog",
        "unknown",
        "medical",
    )
    results: list[Result] = []
    try:
        for name in names:
            print(f"running {name}", flush=True)
            item = _execute(engine, name)
            results.append(item)
            print(json.dumps(asdict(item), ensure_ascii=False), flush=True)
    finally:
        engine.dispose()

    payload = {
        "phase": "3H",
        "database_writes_persisted": False,
        "agent_scenarios_are_read_only": True,
        "results": [asdict(item) for item in results],
    }
    report = Path("artifacts/v2-service-info-phase3h-live.json")
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

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
