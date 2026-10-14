from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage

import app.agents.v2.turn_interpreter as turn_interpreter
from app.agents.v2.semantic_context import build_semantic_context
from app.agents.v2.semantic_state_view import with_safe_task_context

CASES = [
    "03:00",
    "4:15 العصر",
    "4:15 صباحًا",
    "4:15 مساءً",
    "16:15",
    "الساعة 3",
    "4:15 PM",
    "4:15 AM",
]


def _catalog() -> dict[str, object]:
    return {
        "services": [
            {
                "id": "svc-bikini",
                "name": "ليزر بكيني",
                "requires_laser_device": True,
                "laser_devices": [
                    {
                        "device_key": "candela_gentle",
                        "device_name": "Candela Gentle",
                        "configured": True,
                    }
                ],
            }
        ],
        "branches": [
            {
                "id": "branch-main",
                "name": "Clinic",
                "working_hours": [
                    {"weekday": weekday, "start": "10:00", "end": "22:00"}
                    for weekday in range(7)
                ],
            }
        ],
    }


def _context():
    base = build_semantic_context(_catalog())
    return with_safe_task_context(
        base,
        active_task={
            "task_type": "booking",
            "status": "collecting",
            "requested_service_ids": ["svc-bikini"],
            "constraints": {
                "service_id": "svc-bikini",
                "device_key": "candela_gentle",
                "date": {"mode": "exact", "start_date": "2026-10-15", "end_date": None},
            },
        },
    )


def _time_view(turn: Any) -> dict[str, object] | None:
    operations = getattr(turn, "operations", None)
    if not operations:
        return None
    operation = operations[0]
    constraint = operation.entities.time
    if constraint is None:
        return None
    return {
        "operation_type": operation.type,
        "active_task_relationship": operation.active_task_relationship,
        "active_task_explicit_fields": list(operation.active_task_explicit_fields),
        "period_evidence_text": constraint.period_evidence_text,
        "period_status": constraint.period_status,
        "selection_period_status": (
            operation.selection.period_status
            if operation.selection is not None and operation.selection.kind == "time"
            else "none"
        ),
        "mode": constraint.mode,
        "start_time": constraint.start_time,
        "start_time_ambiguity": constraint.start_time_ambiguity,
        "end_time": constraint.end_time,
        "end_time_ambiguity": constraint.end_time_ambiguity,
    }


def _run_case(text: str) -> dict[str, object]:
    stages: dict[str, object] = {}
    original_time_normalize = turn_interpreter._normalize_time_semantics_v2
    original_normalize = turn_interpreter.normalize_semantic_invariants
    original_resolve = turn_interpreter.resolve_turn_times_by_clinic_hours

    def capture_time_normalize(turn, *, latest_customer_text: str):
        stages["raw_interpreter"] = _time_view(turn)
        result = original_time_normalize(turn, latest_customer_text=latest_customer_text)
        stages["after_time_semantic_normalization"] = _time_view(result)
        return result

    def capture_normalize(turn):
        result = original_normalize(turn)
        stages["after_normalize_semantic_invariants"] = _time_view(result)
        return result

    def capture_resolve(turn, context):
        result = original_resolve(turn, context)
        stages["after_resolve_turn_times_by_clinic_hours"] = _time_view(result)
        return result

    turn_interpreter._normalize_time_semantics_v2 = capture_time_normalize
    turn_interpreter.normalize_semantic_invariants = capture_normalize
    turn_interpreter.resolve_turn_times_by_clinic_hours = capture_resolve
    try:
        final = turn_interpreter.interpret_customer_turn_v2(
            history=[HumanMessage(content=text)],
            semantic_context=_context(),
            timezone_name="Africa/Cairo",
            local_now=datetime(2026, 10, 13, 12, 0),
        )
        stages["final_interpreter_result"] = _time_view(final)
    finally:
        turn_interpreter._normalize_time_semantics_v2 = original_time_normalize
        turn_interpreter.normalize_semantic_invariants = original_normalize
        turn_interpreter.resolve_turn_times_by_clinic_hours = original_resolve

    return {"customer": text, **stages}


def main() -> None:
    results = [_run_case(text) for text in CASES]
    payload = {"cases": results}
    output = Path("artifacts/explicit-daypart-diagnostic.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
