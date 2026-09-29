from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from app.services.agent_v2.state import ActiveTaskState
from app.services.agent_v2.stateful_test_harness import run_v2_stateful_fixture_turn

TZ = "Africa/Cairo"


def _state_view(active_task: ActiveTaskState | None) -> dict[str, object] | None:
    if active_task is None:
        return None
    if active_task.task_type == "booking":
        constraints = active_task.constraints.model_dump(mode="json")
        target: dict[str, object] | None = None
    else:
        constraints = active_task.replacement.model_dump(mode="json")
        target = {
            "appointment_id": active_task.target.appointment_id,
            "service_id": active_task.target.service_id,
            "doctor_id": active_task.target.doctor_id,
            "device_key": active_task.target.device_key,
            "start_local": active_task.target.start_local,
        }
    snapshot = active_task.option_snapshot
    return {
        "task_type": active_task.task_type,
        "status": active_task.status,
        "version": active_task.version,
        "constraints": constraints,
        "target": target,
        "option_snapshot": (
            {
                "snapshot_id": snapshot.snapshot_id,
                "purpose": snapshot.purpose,
                "task_version": snapshot.task_version,
                "expires_at": snapshot.expires_at.isoformat(),
                "options": [
                    {
                        "ref": option.ref,
                        "label": option.label,
                    }
                    for option in snapshot.options
                ],
            }
            if snapshot is not None
            else None
        ),
    }


def _print_trace(result) -> None:
    print(
        "V2 TRACE:",
        json.dumps(
            [
                {
                    "operation": trace.operation_type,
                    "before": trace.disposition_before,
                    "after": trace.disposition_after,
                    "reads": list(trace.read_kinds),
                    "simulated_write": trace.simulated_write,
                    "outcome": trace.outcome.status,
                    "goal": trace.outcome.response_goal,
                }
                for trace in result.traces
            ],
            ensure_ascii=False,
            indent=2,
            default=str,
        ),
    )
    simulated = [trace.simulated_write for trace in result.traces if trace.simulated_write]
    if simulated:
        print(
            "TEST NOTE: simulated write only ->",
            ", ".join(str(item) for item in simulated),
            "| no database mutation occurred.",
        )


def main() -> None:
    history: list[BaseMessage] = []
    active_task: ActiveTaskState | None = None
    turn_number = 0

    print("Linka Agent Core V2 — STATEFUL TEST CHAT")
    print("Real V2 interpreter/responder. Fixture reads. Writes are simulated only.")
    print("Commands: /state  /reset  /quit")

    while True:
        try:
            raw = input("\nYOU> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nClosed.")
            return
        if not raw:
            continue
        command = raw.casefold()
        if command == "/quit":
            print("Closed.")
            return
        if command == "/reset":
            history = []
            active_task = None
            turn_number = 0
            print("Test conversation and in-memory V2 state reset.")
            continue
        if command == "/state":
            print(json.dumps(_state_view(active_task), ensure_ascii=False, indent=2, default=str))
            continue

        turn_number += 1
        history.append(HumanMessage(content=raw))
        now = datetime.now(ZoneInfo(TZ))
        result = run_v2_stateful_fixture_turn(
            history=history,
            local_now=now,
            timezone_name=TZ,
            active_task=active_task,
            simulate_writes=True,
            turn_id=f"interactive-{turn_number}",
        )
        active_task = result.active_task
        print(f"Linka V2> {result.reply}")
        print(f"MODEL> {result.responder_model}")
        _print_trace(result)
        history.append(AIMessage(content=result.reply))


if __name__ == "__main__":
    main()
