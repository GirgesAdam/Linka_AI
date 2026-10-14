from __future__ import annotations

import json
import unicodedata
from copy import deepcopy
from datetime import datetime
from zoneinfo import ZoneInfo

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from app.services.agent_v2.state import ActiveTaskState, BookingTaskState
from app.services.agent_v2.stateful_test_harness import run_v2_stateful_fixture_turn
from app.services.agent_v2.test_harness import V2FixtureEnvironment
from app.services.laser_slot_metadata import encode_laser_slot_branch

TZ = "Africa/Cairo"
NOW = datetime(2026, 9, 11, 20, 0, tzinfo=ZoneInfo(TZ))
CONFIRM_NOW = datetime(2026, 9, 16, 12, 0, tzinfo=ZoneInfo(TZ))


def _ascii_decimal_digits(value: object) -> str:
    normalized: list[str] = []
    for char in str(value):
        try:
            normalized.append(str(unicodedata.decimal(char)))
        except (TypeError, ValueError):
            normalized.append(char)
    return "".join(normalized)


def _task_evidence(task: ActiveTaskState | None) -> dict[str, object] | None:
    if task is None:
        return None
    payload = task.model_dump(mode="json")
    if isinstance(task, BookingTaskState):
        return {
            "task_type": payload.get("task_type"),
            "status": payload.get("status"),
            "version": payload.get("version"),
            "requested_service_ids": payload.get("requested_service_ids"),
            "service_device_keys": payload.get("service_device_keys"),
            "constraints": payload.get("constraints"),
            "grouped": payload.get("grouped"),
            "proposal": payload.get("proposal"),
            "option_snapshot": payload.get("option_snapshot"),
        }
    return payload


def _turn_evidence(result) -> dict[str, object]:
    return {
        "operations": [operation.type for operation in result.understanding.operations],
        "operation_details": [
            {
                "type": operation.type,
                "execution_intent": operation.execution_intent,
                "active_task_relationship": operation.active_task_relationship,
                "fresh_task": operation.fresh_task,
                "grouped_booking_action": operation.grouped_booking_action,
                "service": (
                    operation.entities.service.model_dump(mode="json")
                    if operation.entities.service is not None
                    else None
                ),
            }
            for operation in result.understanding.operations
        ],
        "traces": [
            {
                "operation": trace.operation_type,
                "before": trace.disposition_before,
                "after": trace.disposition_after,
                "reads": list(trace.read_kinds),
                "simulated_write": trace.simulated_write,
                "outcome_status": trace.outcome.status,
                "response_goal": trace.outcome.response_goal,
                "facts": trace.outcome.facts,
            }
            for trace in result.traces
        ],
        "active_task": _task_evidence(result.active_task),
    }


def _run(
    name: str,
    messages: list[str],
    *,
    env: V2FixtureEnvironment | None = None,
    now: datetime = NOW,
) -> tuple[list[dict[str, object]], object]:
    history: list[BaseMessage] = []
    active_task: ActiveTaskState | None = None
    transcript: list[dict[str, object]] = []
    last = None
    for index, message in enumerate(messages, start=1):
        history.append(HumanMessage(content=message))
        result = run_v2_stateful_fixture_turn(
            history=history,
            local_now=now,
            active_task=active_task,
            env=env,
            simulate_writes=True,
            turn_id=f"{name}-{index}",
        )
        transcript.append(
            {
                "customer": message,
                "agent": result.reply,
                "model": result.responder_model,
                "canonical": _turn_evidence(result),
            }
        )
        history.append(AIMessage(content=result.reply))
        active_task = result.active_task
        last = result
    print(
        "RA_RESULT="
        + json.dumps(
            {"scenario": name, "turns": transcript},
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        ),
        flush=True,
    )
    assert last is not None
    return transcript, last


def _priced_env() -> V2FixtureEnvironment:
    base = V2FixtureEnvironment()
    catalog = deepcopy(base.catalog)
    catalog["primary_branch_id"] = "single-location"
    catalog["branches"] = [
        {
            "id": "single-location",
            "name": "Main Branch",
            "timezone": TZ,
            "working_hours": [
                {"weekday": weekday, "start": "10:00", "end": "22:00"}
                for weekday in range(7)
            ],
        }
    ]
    for doctor in catalog.get("doctors", []):
        if isinstance(doctor, dict):
            doctor["branch_ids"] = ["single-location"]
    return V2FixtureEnvironment(
        catalog=catalog,
        clinic_info=deepcopy(base.clinic_info),
        profile=deepcopy(base.profile),
        history=deepcopy(base.history),
        slots=deepcopy(base.slots),
        package_offers=deepcopy(base.package_offers),
        refund_quotes=deepcopy(base.refund_quotes),
    )


def _marker_env() -> V2FixtureEnvironment:
    base = _priced_env()
    slots = deepcopy(base.slots)
    for slot in slots:
        if (
            slot.get("service_id") == "svc-underarm"
            and slot.get("laser_device_key") == "candela_gentle"
        ):
            slot["branch_name"] = encode_laser_slot_branch(
                "Main Branch",
                device_key="candela_gentle",
                device_name="Candela Gentle",
            )
    return V2FixtureEnvironment(
        catalog=deepcopy(base.catalog),
        clinic_info=deepcopy(base.clinic_info),
        profile=deepcopy(base.profile),
        history=deepcopy(base.history),
        slots=slots,
        package_offers=deepcopy(base.package_offers),
        refund_quotes=deepcopy(base.refund_quotes),
    )


def _assert_no_marker(transcript: list[dict[str, object]]) -> None:
    text = "\n".join(str(turn["agent"]) for turn in transcript)
    assert "TIA_LASER_DEVICE" not in text
    assert "||" not in text


def ra_h1_marker_leak() -> None:
    transcript, _ = _run(
        "RA-H1",
        [
            "\u0639\u0627\u064a\u0632\u0629 \u0623\u062d\u062c\u0632 \u0644\u064a\u0632\u0631 \u0625\u0628\u0637 \u0639\u0644\u0649 \u062c\u0647\u0627\u0632 Candela Gentle \u0645\u0639 \u062f. \u0645\u0631\u064a\u0645 \u064a\u0648\u0645 \u0627\u0644\u0633\u0628\u062a \u0627\u0644\u0633\u0627\u0639\u0629 8 \u0645\u0633\u0627\u0621"
        ],
        env=_marker_env(),
    )
    _assert_no_marker(transcript)
    task = transcript[-1]["canonical"]["active_task"]
    assert task is not None
    assert task["constraints"]["device_key"] == "candela_gentle"


def ra_h2_multiservice_total() -> None:
    transcript, last = _run(
        "RA-H2",
        [
            "\u0639\u0627\u064a\u0632\u0629 \u0623\u062d\u062c\u0632 \u0644\u064a\u0632\u0631 \u0625\u0628\u0637 \u0648\u0644\u064a\u0632\u0631 \u0628\u0643\u064a\u0646\u064a \u0645\u0639 \u0628\u0639\u0636 \u0641\u064a \u0646\u0641\u0633 \u0627\u0644\u0632\u064a\u0627\u0631\u0629\u060c \u0627\u062e\u062a\u0627\u0631 \u062f\u0643\u062a\u0648\u0631 \u0648\u0627\u062d\u062f \u064a\u0639\u0645\u0644 \u0627\u0644\u0627\u062a\u0646\u064a\u0646 \u0648\u0648\u0631\u064a\u0646\u064a \u0623\u0642\u0631\u0628 \u0645\u064a\u0639\u0627\u062f \u0645\u0646\u0627\u0633\u0628 \u0642\u0628\u0644 \u0645\u0627 \u062a\u062d\u062c\u0632",
            "\u0627\u0644\u0633\u0639\u0631 \u0627\u0644\u0625\u062c\u0645\u0627\u0644\u064a \u0643\u0627\u0645\u061f",
        ],
        env=_priced_env(),
    )
    second = transcript[-1]
    assert any("service_catalog_set" in trace["reads"] for trace in second["canonical"]["traces"])
    assert "1200" in _ascii_decimal_digits(second["agent"])
    assert "\u0623\u0646\u0647\u064a \u062e\u062f\u0645\u0629" not in str(second["agent"])
    task = second["canonical"]["active_task"]
    assert task is not None and task["requested_service_ids"] == ["svc-underarm", "svc-bikini"]
    assert last.active_task is not None


def ra_h3_long_continuation() -> None:
    transcript, _ = _run(
        "RA-H3",
        [
            "\u0639\u0627\u064a\u0632\u0629 \u0623\u062d\u062c\u0632 \u0644\u064a\u0632\u0631 \u0625\u0628\u0637 \u0648\u0644\u064a\u0632\u0631 \u0628\u0643\u064a\u0646\u064a \u0645\u0639 \u0628\u0639\u0636 \u0641\u064a \u0646\u0641\u0633 \u0627\u0644\u0632\u064a\u0627\u0631\u0629\u060c \u0627\u062e\u062a\u0627\u0631 \u062f\u0643\u062a\u0648\u0631 \u0648\u0627\u062d\u062f \u064a\u0639\u0645\u0644 \u0627\u0644\u0627\u062a\u0646\u064a\u0646 \u0648\u0648\u0631\u064a\u0646\u064a \u0623\u0642\u0631\u0628 \u0645\u064a\u0639\u0627\u062f \u0645\u0646\u0627\u0633\u0628 \u0642\u0628\u0644 \u0645\u0627 \u062a\u062d\u062c\u0632",
            "\u064a\u0648\u0645 \u0627\u0644\u0633\u0628\u062a",
            "\u0645\u064a\u0646 \u0627\u0644\u062f\u0643\u0627\u062a\u0631\u0629 \u0627\u0644\u0644\u064a \u0628\u064a\u0639\u0645\u0644\u0648\u0627 \u0627\u0644\u062e\u062f\u0645\u0627\u062a \u062f\u064a\u061f",
            "\u0645\u0648\u0627\u0639\u064a\u062f \u0627\u0644\u0639\u064a\u0627\u062f\u0629 \u0625\u064a\u0647\u061f",
            "\u0627\u0644\u0633\u0639\u0631 \u0627\u0644\u0625\u062c\u0645\u0627\u0644\u064a \u0643\u0627\u0645\u061f",
            "\u0627\u0644\u0633\u0627\u0639\u0629 8 \u0645\u0633\u0627\u0621 \u0645\u0646\u0627\u0633\u0628\u0629",
        ],
        env=_priced_env(),
    )
    price_turn = transcript[4]
    assert "1200" in _ascii_decimal_digits(price_turn["agent"])
    task = price_turn["canonical"]["active_task"]
    assert task is not None and set(task["requested_service_ids"]) == {"svc-underarm", "svc-bikini"}
    selection_reply = str(transcript[5]["agent"])
    assert "Candela" in selection_reply
    assert "Prime Lase" not in selection_reply
    _assert_no_marker(transcript)


def ra_h4_one_device() -> None:
    transcript, _ = _run(
        "RA-H4",
        [
            "\u0639\u0627\u064a\u0632\u0629 \u0623\u062d\u062c\u0632 \u0644\u064a\u0632\u0631 \u0628\u0643\u064a\u0646\u064a",
            "\u0627\u0644\u0633\u0639\u0631 \u0643\u0627\u0645\u061f",
        ],
        env=_priced_env(),
    )
    joined = "\n".join(str(turn["agent"]) for turn in transcript)
    assert "Candela" in joined
    assert "\u062a\u062d\u0628 \u0623\u064a \u062c\u0647\u0627\u0632" not in joined
    assert "700" in _ascii_decimal_digits(transcript[-1]["agent"])


def ra_h5_two_devices() -> None:
    transcript, _ = _run(
        "RA-H5",
        [
            "\u0639\u0627\u064a\u0632\u0629 \u0623\u062d\u062c\u0632 \u0644\u064a\u0632\u0631 \u0625\u0628\u0637",
            "Candela Gentle",
            "\u0627\u0644\u0633\u0639\u0631 \u0627\u0644\u0625\u062c\u0645\u0627\u0644\u064a \u0643\u0627\u0645\u061f",
        ],
        env=_priced_env(),
    )
    first = str(transcript[0]["agent"])
    assert "Candela" in first and "Prime Lase" in first
    final = str(transcript[-1]["agent"])
    assert "500" in _ascii_decimal_digits(final) and "Candela" in final


def ra_h6_service_correction() -> None:
    transcript, _ = _run(
        "RA-H6",
        [
            "\u0639\u0627\u064a\u0632\u0629 \u0623\u062d\u062c\u0632 \u0644\u064a\u0632\u0631 \u0625\u0628\u0637 \u0648\u0644\u064a\u0632\u0631 \u0628\u0643\u064a\u0646\u064a \u0645\u0639 \u0628\u0639\u0636 \u0641\u064a \u0646\u0641\u0633 \u0627\u0644\u0632\u064a\u0627\u0631\u0629\u060c \u0627\u062e\u062a\u0627\u0631 \u062f\u0643\u062a\u0648\u0631 \u0648\u0627\u062d\u062f \u064a\u0639\u0645\u0644 \u0627\u0644\u0627\u062a\u0646\u064a\u0646 \u0648\u0648\u0631\u064a\u0646\u064a \u0623\u0642\u0631\u0628 \u0645\u064a\u0639\u0627\u062f \u0645\u0646\u0627\u0633\u0628 \u0642\u0628\u0644 \u0645\u0627 \u062a\u062d\u062c\u0632",
            "\u062e\u0644\u064a\u0647\u0627 \u0644\u064a\u0632\u0631 \u0625\u0628\u0637 \u0628\u0633",
            "\u0627\u0644\u0633\u0639\u0631 \u0627\u0644\u0625\u062c\u0645\u0627\u0644\u064a \u0643\u0627\u0645\u061f",
        ],
        env=_priced_env(),
    )
    corrected = transcript[1]["canonical"]["active_task"]
    assert corrected is not None and corrected["requested_service_ids"] == ["svc-underarm"]
    assert "1200" not in _ascii_decimal_digits(transcript[-1]["agent"])


def ra_h7_booking_completion() -> None:
    transcript, last = _run(
        "RA-H7",
        [
            "\u0639\u0627\u064a\u0632\u0629 \u0623\u062d\u062c\u0632 \u0644\u064a\u0632\u0631 \u0625\u0628\u0637 \u0639\u0644\u0649 Candela Gentle \u0645\u0639 \u062f. \u0645\u0631\u064a\u0645 \u064a\u0648\u0645 \u0627\u0644\u0633\u0628\u062a \u0627\u0644\u0633\u0627\u0639\u0629 8 \u0645\u0633\u0627\u0621",
            "\u0623\u064a\u0648\u0647 \u0623\u0643\u062f \u0627\u0644\u062d\u062c\u0632",
        ],
        env=_priced_env(),
    )
    booking_writes = [
        trace
        for trace in last.traces
        if trace.simulated_write == "booking"
    ]
    assert len(booking_writes) == 1
    final = str(transcript[-1]["agent"])
    forbidden = ["\u062a\u0645 \u062a\u0623\u0643\u064a\u062f \u0627\u0644\u062d\u0636\u0648\u0631", "attendance confirmed"]
    assert not any(text.lower() in final.lower() for text in forbidden)


def ra_h8_attendance_confirmation() -> None:
    transcript, last = _run(
        "RA-H8",
        ["\u0623\u0643\u062f \u062d\u0636\u0648\u0631\u064a \u0644\u0645\u0639\u0627\u062f \u0627\u0644\u0647\u064a\u062f\u0631\u0627\u0641\u064a\u0634\u0644 \u064a\u0648\u0645 \u0627\u0644\u062e\u0645\u064a\u0633"],
        env=_priced_env(),
        now=CONFIRM_NOW,
    )
    confirm_writes = [
        trace
        for trace in last.traces
        if trace.simulated_write == "confirm_appointment"
    ]
    assert len(confirm_writes) == 1
    assert confirm_writes[0].outcome.status == "completed"
    assert last.outcomes[0].status == "completed"
    assert last.outcomes[0].response_goal == "appointment_confirmed"
    assert str(transcript[-1]["agent"]).strip()


def main() -> None:
    ra_h1_marker_leak()
    ra_h2_multiservice_total()
    ra_h3_long_continuation()
    ra_h4_one_device()
    ra_h5_two_devices()
    ra_h6_service_correction()
    ra_h7_booking_completion()
    ra_h8_attendance_confirmation()
    print("HUMAN CONVERSATION QUALITY BATCH 1 REAL AGENT REVIEW PASSED", flush=True)


if __name__ == "__main__":
    main()
