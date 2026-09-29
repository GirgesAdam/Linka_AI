from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from app.services.agent_v2.state import (
    ActiveTaskState,
    BookingTaskState,
    RescheduleTaskState,
)
from app.services.agent_v2.stateful_test_harness import run_v2_stateful_fixture_turn
from app.services.agent_v2.test_harness import V2FixtureEnvironment

TZ = "Africa/Cairo"
NOW = datetime(2026, 9, 11, 20, 0, tzinfo=ZoneInfo(TZ))


def _print_turn(message: str, result) -> None:
    print("=" * 96)
    print("CUSTOMER:", message)
    print("OPERATIONS:", [operation.type for operation in result.understanding.operations])
    print(
        "TRACE:",
        [
            {
                "operation": trace.operation_type,
                "before": trace.disposition_before,
                "after": trace.disposition_after,
                "reads": trace.read_kinds,
                "simulated_write": trace.simulated_write,
                "outcome": trace.outcome.status,
            }
            for trace in result.traces
        ],
    )
    if result.active_task is None:
        print("ACTIVE_TASK: none")
    else:
        print(
            "ACTIVE_TASK:",
            {
                "type": result.active_task.task_type,
                "status": result.active_task.status,
                "version": result.active_task.version,
                "has_options": result.active_task.option_snapshot is not None,
            },
        )
    print("Linka V2:", result.reply)
    print("MODEL:", result.responder_model)


def _turn(
    *,
    history: list[BaseMessage],
    active_task: ActiveTaskState | None,
    message: str,
    turn_number: int,
    env: V2FixtureEnvironment | None = None,
):
    history.append(HumanMessage(content=message))
    result = run_v2_stateful_fixture_turn(
        history=history,
        local_now=NOW,
        active_task=active_task,
        env=env,
        simulate_writes=True,
        turn_id=f"live-stateful-{turn_number}",
    )
    _print_turn(message, result)
    history.append(AIMessage(content=result.reply))
    return result


def _progressive_booking_with_side_read() -> None:
    print("\nSTATEFUL CONVERSATION A — progressive booking + side read + selection")
    history: list[BaseMessage] = []
    active_task: ActiveTaskState | None = None

    first = _turn(
        history=history,
        active_task=active_task,
        message="عايزة أحجز جلسة ليزر بكرة، المنطقة لسه هحددها",
        turn_number=1,
    )
    active_task = first.active_task
    assert isinstance(active_task, BookingTaskState), "booking intent was not persisted"
    assert active_task.constraints.service_id is None, "ambiguous service was guessed"
    assert active_task.constraints.date is not None, "known booking date was not persisted"

    second = _turn(
        history=history,
        active_task=active_task,
        message="الإبط",
        turn_number=2,
    )
    active_task = second.active_task
    assert isinstance(active_task, BookingTaskState), "booking state disappeared after service choice"
    assert active_task.constraints.service_id == "svc-underarm", "service choice was not applied"
    assert active_task.option_snapshot is not None, "verified availability options were not persisted"
    snapshot_before_side_read = active_task.option_snapshot.snapshot_id
    version_before_side_read = active_task.version

    side_read = _turn(
        history=history,
        active_task=active_task,
        message="وسعر الإبط كام؟",
        turn_number=3,
    )
    active_task = side_read.active_task
    assert isinstance(active_task, BookingTaskState), "side read cancelled the active booking"
    assert active_task.version == version_before_side_read, "side read mutated booking version"
    assert active_task.option_snapshot is not None, "side read cleared the verified choices"
    assert (
        active_task.option_snapshot.snapshot_id == snapshot_before_side_read
    ), "side read replaced the verified choice snapshot"

    filtered = _turn(
        history=history,
        active_task=active_task,
        message="خلي المواعيد بعد 6 مساءً",
        turn_number=4,
    )
    active_task = filtered.active_task
    assert isinstance(active_task, BookingTaskState), "booking state disappeared after time update"
    assert active_task.constraints.time is not None
    assert active_task.constraints.time.mode == "after"
    assert active_task.constraints.time.start_time == "18:00"
    assert active_task.option_snapshot is not None, "filtered availability snapshot was not persisted"
    assert (
        active_task.option_snapshot.snapshot_id != snapshot_before_side_read
    ), "time mutation failed to invalidate stale choices"

    completed = _turn(
        history=history,
        active_task=active_task,
        message="احجزي الساعة 8 مساءً",
        turn_number=5,
    )
    booking_writes = [
        trace
        for trace in completed.traces
        if trace.simulated_write == "booking"
    ]
    assert len(booking_writes) == 1, "final selection did not produce exactly one booking write"
    assert completed.active_task is None, "completed booking task was not cleared"
    assert any(outcome.status == "completed" for outcome in completed.outcomes)


def _cancel_unfinished_booking() -> None:
    print("\nSTATEFUL CONVERSATION B — cancel unfinished task only")
    history: list[BaseMessage] = []

    started = _turn(
        history=history,
        active_task=None,
        message="عايزة أحجز ليزر إبط بكرة",
        turn_number=101,
    )
    active_task = started.active_task
    assert isinstance(active_task, BookingTaskState), "booking task did not start"

    cancelled = _turn(
        history=history,
        active_task=active_task,
        message="خلاص الغي طلب الحجز ده، مش الموعد الموجود عندي",
        turn_number=102,
    )
    assert cancelled.active_task is None, "cancel_active did not clear the unfinished task"
    assert not any(trace.simulated_write for trace in cancelled.traces), (
        "cancelling the conversational task executed an appointment write"
    )
    assert any(operation.type == "cancel_active" for operation in cancelled.understanding.operations)
    assert len(cancelled.outcomes) == 1
    assert cancelled.outcomes[0].response_goal == "active_task_cancelled"
    assert cancelled.outcomes[0].facts.get("active_task_cancelled") is True
    assert "؟" not in cancelled.reply and "?" not in cancelled.reply, (
        "responder asked for confirmation after Python had already cleared the active task"
    )


def _progressive_reschedule() -> None:
    print("\nSTATEFUL CONVERSATION C — verified target + replacement revalidation")
    history: list[BaseMessage] = []
    base_env = V2FixtureEnvironment()
    env = V2FixtureEnvironment(
        slots=[
            *base_env.slots,
            {
                "branch_id": "single-location",
                "service_id": "svc-underarm",
                "doctor_id": "doc-maryam",
                "doctor_name": "د. مريم",
                "laser_device_key": "candela_gentle",
                "laser_device_name": "Candela Gentle",
                "start_local": "2026-09-13T20:00:00+03:00",
                "end_local": "2026-09-13T20:30:00+03:00",
                "start_at": "2026-09-13T20:00:00+03:00",
                "price_minor": 50_000,
                "currency": "EGP",
            },
        ]
    )

    started = _turn(
        history=history,
        active_task=None,
        message="عايزة أغير ميعاد ليزر الإبط اللي عندي بكرة، لسه هحدد الوقت الجديد",
        turn_number=201,
        env=env,
    )
    active_task = started.active_task
    assert isinstance(active_task, RescheduleTaskState), "verified reschedule target was not persisted"
    assert active_task.target.appointment_id == "apt-underarm-sat"
    assert active_task.replacement.date is not None
    assert active_task.option_snapshot is not None, "replacement availability was not persisted"

    completed = _turn(
        history=history,
        active_task=active_task,
        message="خليه بعد بكرة الساعة 8 مساءً",
        turn_number=202,
        env=env,
    )
    reschedule_writes = [
        trace
        for trace in completed.traces
        if trace.simulated_write == "reschedule"
    ]
    assert len(reschedule_writes) == 1, "replacement did not produce exactly one reschedule write"
    assert completed.active_task is None, "completed reschedule task was not cleared"
    assert any(outcome.status == "completed" for outcome in completed.outcomes)


def main() -> None:
    _progressive_booking_with_side_read()
    _cancel_unfinished_booking()
    _progressive_reschedule()
    print("\nV2 STATEFUL CONVERSATION SUITE PASSED")


if __name__ == "__main__":
    main()
