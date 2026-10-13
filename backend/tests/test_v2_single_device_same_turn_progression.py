from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from langchain_core.messages import HumanMessage

from app.agents.v2.turn_contract import (
    DateConstraint,
    TiaTurnUnderstanding,
    TimeConstraint,
    TurnEntities,
    TurnOperation,
)
from app.services.agent_v2 import orchestrator as runtime
from app.services.agent_v2.planner import PlanStep, TurnPlan, VerificationFacts
from app.services.agent_v2.read_executor import ReadExecutionBundle, ReadResult
from app.services.agent_v2.state import BookingTaskState, CustomerConstraints, WriteAuthorization
from app.services.agent_v2.state_persistence import PersistedActiveTask

NOW = datetime(2026, 10, 13, 10, 0, tzinfo=UTC)
SERVICE = "svc-bikini"
DOCTOR = "doc-1"
BRANCH = "branch-1"
DEVICE = "candela_gentle"


def _catalog(devices: list[dict[str, object]]) -> dict[str, object]:
    return {
        "services": [{
            "id": SERVICE,
            "name": "Bikini Laser",
            "duration_minutes": 30,
            "requires_laser_device": True,
            "laser_devices": devices,
        }],
        "laser_devices": [
            {"device_key": str(row["device_key"]), "device_name": str(row.get("device_name") or row["device_key"])}
            for row in devices
        ],
        "branches": [{"id": BRANCH, "name": "Clinic"}],
        "doctors": [{"id": DOCTOR, "name": "Doctor", "service_ids": [SERVICE], "branch_ids": [BRANCH]}],
        "appointments": [],
    }


def _device(key: str = DEVICE, *, price: int = 70000) -> dict[str, object]:
    return {
        "device_key": key,
        "device_name": key,
        "configured": True,
        "price_minor": price,
        "currency": "EGP",
        "duration_minutes": 30,
    }


def _task(clock: str = "16:15") -> BookingTaskState:
    return BookingTaskState(
        write_authorization=WriteAuthorization(
            operation="booking", authorized=True, source_turn_id="turn-start", granted_at=NOW
        ),
        constraints=CustomerConstraints(
            service_id=SERVICE,
            doctor_id=DOCTOR,
            branch_id=BRANCH,
            date=DateConstraint(mode="exact", start_date="2026-10-15"),
            time=TimeConstraint(mode="exact", start_time=clock, start_time_ambiguity="none"),
        ),
        requested_service_ids=[],
    )


def _understanding() -> TiaTurnUnderstanding:
    return TiaTurnUnderstanding(
        operations=[TurnOperation(
            type="book",
            entities=TurnEntities(),
            execution_intent="execute",
            continues_previous=True,
            active_task_relationship="continue",
        )]
    )


def _initial_plan() -> TurnPlan:
    return TurnPlan(steps=[PlanStep(
        operation_index=0,
        operation_type="book",
        disposition="state_update",
        state_action="update_active",
        response_goal="clarification",
    )])


def _slot(clock: str) -> dict[str, object]:
    hour, minute = (int(part) for part in clock.split(":"))
    start = datetime(2026, 10, 15, hour, minute, tzinfo=UTC)
    return {
        "start_at": start.isoformat(),
        "end_at": (start + timedelta(minutes=30)).isoformat(),
        "start_local": start.isoformat(),
        "end_local": (start + timedelta(minutes=30)).isoformat(),
        "start_time_24h": clock,
        "end_time_24h": (start + timedelta(minutes=30)).strftime("%H:%M"),
        "service_id": SERVICE,
        "service_name": "Bikini Laser",
        "doctor_id": DOCTOR,
        "doctor_name": "Doctor",
        "branch_id": BRANCH,
        "branch_name": "Clinic",
        "laser_device_key": DEVICE,
        "laser_device_name": "Candela",
        "price_minor": 70000,
        "currency": "EGP",
    }


def _run(
    monkeypatch: pytest.MonkeyPatch,
    *,
    devices: list[dict[str, object]],
    exact_match_count: int | None = 1,
    slots: list[dict[str, object]] | None = None,
    alternatives: list[dict[str, object]] | None = None,
):
    task = _task()
    persisted = PersistedActiveTask(active_task=task, flow_id=uuid4(), flow_version=1)
    monkeypatch.setattr(runtime, "load_active_task", lambda *a, **k: persisted)
    monkeypatch.setattr(runtime, "interpret_customer_turn_v2", lambda **k: _understanding())
    monkeypatch.setattr(runtime, "plan_turn", lambda *a, **k: _initial_plan())
    monkeypatch.setattr(runtime, "resolve_additional_services", lambda *a, **k: [])
    monkeypatch.setattr(runtime, "compose_v2_customer_reply", lambda **k: ("ok", "test"))

    read_calls: list[list[str]] = []
    availability_requests: list[dict[str, object]] = []

    def fake_reads(step, _context):
        kinds = [read.kind for read in step.reads]
        read_calls.append(kinds)
        results: list[ReadResult] = []
        verification = VerificationFacts()
        for read in step.reads:
            if read.kind == "service_catalog":
                results.append(ReadResult(kind="service_catalog", ok=True, payload={
                    "service": {
                        "id": SERVICE,
                        "name": "Bikini Laser",
                        "requires_laser_device": True,
                        "laser_devices": devices,
                    }
                }))
            elif read.kind == "availability":
                availability_requests.append(dict(read.parameters))
                resolved_slots = list(slots or [])
                results.append(ReadResult(kind="availability", ok=True, payload={
                    "service_name": "Bikini Laser",
                    "slots": resolved_slots,
                    "nearest_alternative_slots": list(alternatives or []),
                    "matching_slot_count": len(resolved_slots),
                    "checked_dates": ["2026-10-15"],
                }))
                verified = {
                    "service_id": SERVICE,
                    "doctor_id": DOCTOR,
                    "branch_id": BRANCH,
                    "device_key": DEVICE,
                }
                if exact_match_count == 1 and resolved_slots:
                    verified["start_at"] = str(resolved_slots[0]["start_at"])
                verification = VerificationFacts(
                    exact_slot_match_count=exact_match_count,
                    verified_parameters=verified,
                )
        return ReadExecutionBundle(results=results, verification=verification)

    monkeypatch.setattr(runtime, "execute_step_reads", fake_reads)

    proposal_attaches: list[object] = []
    original_attach = runtime.attach_booking_proposal

    def count_attach(active_task, *, proposal):
        proposal_attaches.append(proposal)
        return original_attach(active_task, proposal=proposal)

    monkeypatch.setattr(runtime, "attach_booking_proposal", count_attach)

    write_calls: list[object] = []
    def write_executor(step):
        write_calls.append(step)
        return {"ok": True}

    saved: list[BookingTaskState] = []
    def save_task(*a, **kwargs):
        active = kwargs["active_task"]
        saved.append(active)
        return PersistedActiveTask(active_task=active, flow_id=persisted.flow_id, flow_version=2)
    monkeypatch.setattr(runtime, "save_active_task", save_task)
    completed: list[dict[str, object]] = []
    monkeypatch.setattr(
        runtime,
        "complete_active_task",
        lambda *a, **kwargs: completed.append(dict(kwargs.get("result") or {})),
    )

    workspace = SimpleNamespace(id=uuid4(), timezone="UTC")
    patient = SimpleNamespace(id=uuid4())
    result = runtime.orchestrate_v2_turn(
        db=SimpleNamespace(), workspace=workspace, patient=patient,
        conversation_id=uuid4(), run_id=uuid4(),
        history=[HumanMessage(content="book")], local_now=NOW,
        timezone_name="UTC", clinic_name="Clinic", catalog=_catalog(devices),
        turn_id="turn-runtime", write_executor=write_executor,
    )
    return result, read_calls, availability_requests, proposal_attaches, write_calls, saved, completed


def test_d1_single_device_continues_same_turn_to_exact_time_proposal(monkeypatch: pytest.MonkeyPatch) -> None:
    result, read_calls, availability_requests, proposals, writes, _saved, completed = _run(
        monkeypatch, devices=[_device()], slots=[_slot("16:15")], exact_match_count=1
    )
    assert read_calls == [["service_catalog"], ["availability"]]
    assert len(availability_requests) == 1
    assert availability_requests[0]["device_key"] == DEVICE
    assert availability_requests[0]["time"]["start_time"] == "16:15"
    assert result.active_task is None
    assert proposals == []
    assert len(writes) == 1
    assert writes[0].write_intent is not None
    assert writes[0].write_intent.parameters["device_key"] == DEVICE
    assert writes[0].write_intent.parameters["time"]["start_time"] == "16:15"
    assert result.outcomes[-1].status == "completed"
    assert len(completed) == 1


def test_d2_single_device_exact_unavailable_keeps_1615_and_verified_alternatives(monkeypatch: pytest.MonkeyPatch) -> None:
    alternative = _slot("16:30")
    result, read_calls, availability_requests, proposals, writes, _saved, _completed = _run(
        monkeypatch, devices=[_device()], slots=[], alternatives=[alternative], exact_match_count=0
    )
    assert read_calls == [["service_catalog"], ["availability"]]
    assert len(availability_requests) == 1
    assert availability_requests[0]["time"]["start_time"] == "16:15"
    assert isinstance(result.active_task, BookingTaskState)
    assert result.active_task.constraints.time is not None
    assert result.active_task.constraints.time.start_time == "16:15"
    assert result.active_task.constraints.device_key == DEVICE
    assert result.active_task.proposal is None
    assert proposals == []
    assert writes == []
    assert result.outcomes[-1].response_goal == "requested_time_unavailable"
    assert result.outcomes[-1].facts["availability"]["nearest_alternative_windows"]


def test_d3_multi_device_never_auto_selects_or_reaches_availability(monkeypatch: pytest.MonkeyPatch) -> None:
    result, read_calls, availability_requests, proposals, writes, _saved, _completed = _run(
        monkeypatch,
        devices=[_device("candela_gentle", price=70000), _device("deka", price=65000)],
        exact_match_count=None,
    )
    assert read_calls == [["service_catalog"]]
    assert availability_requests == []
    assert isinstance(result.active_task, BookingTaskState)
    assert result.active_task.constraints.device_key is None
    assert result.active_task.proposal is None
    assert proposals == []
    assert writes == []
    assert result.outcomes[-1].response_goal == "answer_price"
    options = result.outcomes[-1].facts["service_catalog"]["service"]["laser_devices"]
    assert {row["device_name"] for row in options} == {"candela_gentle", "deka"}


def test_d4_zero_compatible_device_fails_closed_without_availability_or_write(monkeypatch: pytest.MonkeyPatch) -> None:
    result, read_calls, availability_requests, proposals, writes, _saved, _completed = _run(
        monkeypatch, devices=[], exact_match_count=None
    )
    assert read_calls == []
    assert availability_requests == []
    assert isinstance(result.active_task, BookingTaskState)
    assert result.active_task.constraints.device_key is None
    assert result.active_task.proposal is None
    assert proposals == []
    assert writes == []
    assert result.outcomes[-1].status == "needs_input"
    assert result.outcomes[-1].facts["needed"] == "device"


def test_d5_single_device_progression_does_not_duplicate_availability_proposal_or_write(monkeypatch: pytest.MonkeyPatch) -> None:
    result, read_calls, availability_requests, proposals, writes, _saved, _completed = _run(
        monkeypatch, devices=[_device()], slots=[_slot("16:15")], exact_match_count=1
    )
    assert read_calls.count(["availability"]) == 1
    assert len(availability_requests) == 1
    assert proposals == []
    assert len(writes) == 1
    assert len(_completed) == 1
    assert result.active_task is None
