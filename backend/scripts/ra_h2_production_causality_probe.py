from __future__ import annotations

import json
import os
from datetime import datetime
from uuid import uuid4
from zoneinfo import ZoneInfo

from langchain_core.messages import AIMessage, HumanMessage
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

import app.agents.v2.turn_interpreter as ti
from app.core.config import settings
from app.models.patient import Patient
from app.models.service import Service
from app.models.workspace import Workspace
from app.services.agent_v2.orchestrator import orchestrate_v2_turn
from app.services.agent_v2.state import BookingTaskState

LABEL = os.environ["PROBE_LABEL"]
SHA = os.environ["PROBE_SHA"]
FIRST = "عايزة أحجز ليزر إبط وليزر بكيني مع بعض في نفس الزيارة، اختار دكتور واحد يعمل الاتنين ووريني أقرب ميعاد مناسب قبل ما تحجز"
SECOND = "السعر الإجمالي كام؟"
TARGET_NAMES = {"ليزر إبط", "ليزر بكيني"}


def dump(value):
    if value is None:
        return None
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return str(value)


raw_attempts: list[dict[str, object]] = []
original_invoke = ti.invoke_typed_structured_output


def capturing_invoke(*args, **kwargs):
    model = kwargs.get("model") if "model" in kwargs else (args[0] if args else None)
    model_name = getattr(model, "model_name", None) or getattr(model, "model", None) or type(model).__name__
    try:
        value = original_invoke(*args, **kwargs)
    except Exception as exc:
        raw_attempts.append({"status": "error", "model": str(model_name), "error_type": type(exc).__name__, "error": str(exc)})
        raise
    raw_attempts.append({"status": "ok", "model": str(model_name), "structured": dump(value)})
    return value


ti.invoke_typed_structured_output = capturing_invoke


def main() -> int:
    engine = create_engine(settings.database_url, pool_pre_ping=True)
    connection = engine.connect()
    outer = connection.begin()
    db = Session(bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint")
    try:
        workspace = None
        target_services: list[Service] = []
        for candidate in db.scalars(select(Workspace).order_by(Workspace.id.asc())):
            rows = list(db.scalars(select(Service).where(Service.workspace_id == candidate.id, Service.name.in_(TARGET_NAMES))))
            if {row.name for row in rows} == TARGET_NAMES:
                workspace = candidate
                target_services = rows
                break
        if workspace is None:
            raise RuntimeError("No staging workspace contains both target services.")
        patient = db.scalar(select(Patient).where(Patient.workspace_id == workspace.id).order_by(Patient.created_at.asc(), Patient.id.asc()))
        if patient is None:
            raise RuntimeError("No staging patient available for selected workspace.")
        service_ids = {row.name: str(row.id) for row in target_services}
        expected_ids = set(service_ids.values())
        local_now = datetime(2026, 9, 11, 20, 0, tzinfo=ZoneInfo(workspace.timezone))
        fixture = {"revision": LABEL, "sha": SHA, "workspace_id": str(workspace.id), "workspace_slug": workspace.slug, "patient_id": str(patient.id), "timezone": workspace.timezone, "service_ids": service_ids}
        print("PRODUCTION_FIXTURE=" + json.dumps(fixture, ensure_ascii=False, separators=(",", ":")), flush=True)

        grouped = first_clarify = second_clarify = structured_errors = 0
        samples: list[dict[str, object]] = []
        for sample in range(1, 6):
            conversation_id = uuid4()
            history = [HumanMessage(content=FIRST)]
            raw_attempts.clear()
            first = orchestrate_v2_turn(db=db, workspace=workspace, patient=patient, conversation_id=conversation_id, run_id=uuid4(), history=history, local_now=local_now, timezone_name=workspace.timezone, clinic_name=workspace.name)
            raw_first = list(raw_attempts)
            first_ids = list(first.active_task.requested_service_ids) if isinstance(first.active_task, BookingTaskState) else []
            first_grouped = isinstance(first.active_task, BookingTaskState) and set(first_ids) == expected_ids
            grouped += int(first_grouped)
            first_clarify += int(any(step.disposition == "clarify" and step.clarification_field == "service" for step in first.plan.steps))

            history.extend([AIMessage(content=first.reply), HumanMessage(content=SECOND)])
            raw_attempts.clear()
            second = orchestrate_v2_turn(db=db, workspace=workspace, patient=patient, conversation_id=conversation_id, run_id=uuid4(), history=history, local_now=local_now, timezone_name=workspace.timezone, clinic_name=workspace.name)
            raw_second = list(raw_attempts)
            second_ids = list(second.active_task.requested_service_ids) if isinstance(second.active_task, BookingTaskState) else []
            second_clarify += int(any(step.disposition == "clarify" and step.clarification_field == "service" for step in second.plan.steps))
            structured_errors += sum(1 for item in [*raw_first, *raw_second] if item.get("status") == "error")

            trace = {
                "revision": LABEL,
                "sha": SHA,
                "sample": sample,
                "first": {"raw": raw_first, "understanding": dump(first.understanding), "plan": dump(first.plan), "active_task": dump(first.active_task), "requested_service_ids": first_ids, "reads": [kind for trace in first.traces for kind in trace.read_kinds], "reply": first.reply, "pending_write": first.pending_write is not None},
                "second": {"raw": raw_second, "understanding": dump(second.understanding), "plan": dump(second.plan), "active_task": dump(second.active_task), "requested_service_ids": second_ids, "reads": [kind for trace in second.traces for kind in trace.read_kinds], "reply": second.reply, "pending_write": second.pending_write is not None},
            }
            print("PRODUCTION_CAUSALITY_TRACE=" + json.dumps(trace, ensure_ascii=False, separators=(",", ":"), default=str), flush=True)
            samples.append({"sample": sample, "first_grouped": first_grouped, "first_ops": [op.type for op in first.understanding.operations], "first_ids": first_ids, "first_reply": first.reply, "second_ops": [op.type for op in second.understanding.operations], "second_ids": second_ids, "second_reads": trace["second"]["reads"], "second_reply": second.reply})

        summary = {"revision": LABEL, "sha": SHA, "runs": 5, "grouped_success_count": grouped, "grouped_success_rate": grouped / 5, "first_service_clarifications": first_clarify, "second_service_clarifications": second_clarify, "structured_output_failures": structured_errors, "samples": samples}
        print("PRODUCTION_CAUSALITY_SUMMARY=" + json.dumps(summary, ensure_ascii=False, separators=(",", ":")), flush=True)
        return 0
    finally:
        db.close()
        if outer.is_active:
            outer.rollback()
        connection.close()
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
