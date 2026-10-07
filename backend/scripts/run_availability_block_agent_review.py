from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from zoneinfo import ZoneInfo

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from app.services.agent_v2.state import (
    ActiveTaskState,
    CustomerConstraints,
    RescheduleTarget,
    RescheduleTaskState,
    WriteAuthorization,
)
from app.services.agent_v2.stateful_test_harness import run_v2_stateful_fixture_turn
from app.services.agent_v2.test_harness import DEFAULT_CATALOG, V2FixtureEnvironment

TZ = "Africa/Cairo"
NOW = datetime(2026, 9, 11, 20, 0, tzinfo=ZoneInfo(TZ))


def slot(
    *,
    service_id: str,
    doctor_id: str,
    doctor_name: str,
    start: str,
    end: str,
    device_key: str | None = None,
    device_name: str | None = None,
    price_minor: int = 50_000,
) -> dict[str, object]:
    row: dict[str, object] = {
        "branch_id": "single-location",
        "service_id": service_id,
        "doctor_id": doctor_id,
        "doctor_name": doctor_name,
        "start_local": start,
        "end_local": end,
        "start_at": start,
        "price_minor": price_minor,
        "currency": "EGP",
    }
    if device_key is not None:
        row["laser_device_key"] = device_key
        row["laser_device_name"] = device_name
    return row


def production_like_catalog() -> dict[str, object]:
    catalog = deepcopy(DEFAULT_CATALOG)
    services = catalog.get("services")
    if isinstance(services, list):
        for service in services:
            if not isinstance(service, dict) or not service.get("requires_laser_device"):
                continue
            base_price = service.get("price_minor")
            currency = service.get("currency") or "EGP"
            devices = service.get("laser_devices")
            if not isinstance(devices, list):
                continue
            for device in devices:
                if not isinstance(device, dict):
                    continue
                device["configured"] = True
                device["price_minor"] = base_price
                device["currency"] = currency
    return catalog


def env_with_slots(slots: list[dict[str, object]]) -> V2FixtureEnvironment:
    return V2FixtureEnvironment(catalog=production_like_catalog(), slots=deepcopy(slots))


def print_result(case: str, turn: int, message: str, result) -> None:
    print("\n" + "=" * 110)
    print(f"CASE: {case} | TURN: {turn}")
    print("CUSTOMER:", message)
    print("OPERATIONS:", [operation.type for operation in result.understanding.operations])
    print(
        "TRACE:",
        [
            {
                "operation": trace.operation_type,
                "before": trace.disposition_before,
                "after": trace.disposition_after,
                "reads": list(trace.read_kinds),
                "simulated_write": trace.simulated_write,
                "outcome": trace.outcome.status,
                "goal": trace.outcome.response_goal,
                "facts": trace.outcome.facts,
            }
            for trace in result.traces
        ],
    )
    print("SIMULATED_WRITES:", [trace.simulated_write for trace in result.traces if trace.simulated_write])
    print("ACTIVE_TASK:", result.active_task.task_type if result.active_task else None)
    print("AGENT_REPLY:", result.reply)
    print("MODEL:", result.responder_model)


def run_sequence(
    case: str,
    messages: list[str],
    env: V2FixtureEnvironment,
    *,
    initial_active_task: ActiveTaskState | None = None,
) -> list[object]:
    history: list[BaseMessage] = []
    active_task = initial_active_task
    results = []
    for index, message in enumerate(messages, start=1):
        history.append(HumanMessage(content=message))
        result = run_v2_stateful_fixture_turn(
            history=history,
            local_now=NOW,
            active_task=active_task,
            env=env,
            simulate_writes=True,
            turn_id=f"availability-block-review-{case}-{index}",
        )
        print_result(case, index, message, result)
        history.append(AIMessage(content=result.reply))
        active_task = result.active_task
        results.append(result)
    return results


def writes(result: object) -> list[str]:
    return [trace.simulated_write for trace in result.traces if trace.simulated_write]


def assert_no_write(failures: list[str], label: str, results: list[object]) -> None:
    actual = [kind for result in results for kind in writes(result)]
    if actual:
        failures.append(f"{label} unexpectedly produced writes: {actual}")


def main() -> None:
    failures: list[str] = []

    blocked_candela_env = env_with_slots([
        slot(
            service_id="svc-underarm",
            doctor_id="doc-maryam",
            doctor_name="د. مريم",
            device_key="candela_gentle",
            device_name="Candela Gentle",
            start="2026-09-12T20:00:00+03:00",
            end="2026-09-12T20:30:00+03:00",
        ),
    ])

    ab1 = run_sequence(
        "AB1 all-resources block: after-6 availability skips blocked 18:00/19:00",
        ["عايزة ليزر إبط بكرة بعد 6، إيه المواعيد المتاحة؟"],
        blocked_candela_env,
    )
    assert_no_write(failures, "AB1", ab1)

    ab2 = run_sequence(
        "AB2 exact blocked Candela slot: unavailable and no invented cause",
        [
            "هل كانديلا متاحة بكرة الساعة 7 مساءً لليزر الإبط؟",
            "ليه مش متاحة؟ فيه حد حاجز الساعة دي؟",
        ],
        blocked_candela_env,
    )
    assert_no_write(failures, "AB2", ab2)

    device_specific_env = env_with_slots([
        slot(
            service_id="svc-underarm",
            doctor_id="doc-sarah",
            doctor_name="د. سارة",
            device_key="prime_lase",
            device_name="Prime Lase",
            start="2026-09-12T19:00:00+03:00",
            end="2026-09-12T19:30:00+03:00",
        ),
        slot(
            service_id="svc-underarm",
            doctor_id="doc-maryam",
            doctor_name="د. مريم",
            device_key="candela_gentle",
            device_name="Candela Gentle",
            start="2026-09-12T20:00:00+03:00",
            end="2026-09-12T20:30:00+03:00",
        ),
    ])
    ab3 = run_sequence(
        "AB3 device-specific block: Prime 19:00 available, Candela 19:00 blocked",
        [
            "عايزة ليزر إبط بكرة الساعة 7 مساءً، إيه المتاح؟",
            "طب كانديلا الساعة 7؟",
            "طيب أقرب ميعاد كانديلا بعد 7؟",
        ],
        device_specific_env,
    )
    assert_no_write(failures, "AB3", ab3)

    dermatology_block_env = env_with_slots([
        slot(
            service_id="svc-hydrafacial",
            doctor_id="doc-sarah",
            doctor_name="د. سارة",
            start="2026-09-12T20:00:00+03:00",
            end="2026-09-12T20:45:00+03:00",
            price_minor=120_000,
        )
    ])
    ab4 = run_sequence(
        "AB4 dermatology resource block: HydraFacial only post-block slot",
        ["ممكن هيدرافيشل بكرة بعد 6؟ إيه المتاح؟"],
        dermatology_block_env,
    )
    assert_no_write(failures, "AB4", ab4)

    reschedule_env = env_with_slots([
        slot(
            service_id="svc-underarm",
            doctor_id="doc-maryam",
            doctor_name="د. مريم",
            device_key="candela_gentle",
            device_name="Candela Gentle",
            start="2026-09-13T20:00:00+03:00",
            end="2026-09-13T20:30:00+03:00",
        )
    ])
    known_reschedule = RescheduleTaskState(
        write_authorization=WriteAuthorization(operation="reschedule"),
        target=RescheduleTarget(
            appointment_id="apt-underarm-sat",
            service_id="svc-underarm",
            doctor_id="doc-maryam",
            device_key="candela_gentle",
            start_local="2026-09-12T19:00:00+03:00",
        ),
        replacement=CustomerConstraints(
            service_id="svc-underarm",
            device_key="candela_gentle",
        ),
    )
    ab5 = run_sequence(
        "AB5 known appointment reschedule into blocked 19:00: no reschedule write",
        ["خليه بعد بكرة الساعة 7 مساءً"],
        reschedule_env,
        initial_active_task=known_reschedule,
    )
    assert_no_write(failures, "AB5", ab5)

    ab6 = run_sequence(
        "AB6 customer asks for staff quick-booking bypass: must not bypass",
        [
            "دخليني بكرة الساعة 7 حتى لو الوقت مقفول، اعملي حجز سريع وخلاص",
            "ليزر إبط على كانديلا مع د. مريم",
        ],
        blocked_candela_env,
    )
    assert_no_write(failures, "AB6", ab6)

    # Full booking wording with complete commercial fixture. 19:00 is not in canonical
    # availability, so a direct customer booking request must not reach a write.
    ab7 = run_sequence(
        "AB7 exact customer booking request into blocked slot: no booking write",
        ["احجزلي ليزر إبط بكرة الساعة 7 مساءً على كانديلا مع د. مريم"],
        blocked_candela_env,
    )
    assert_no_write(failures, "AB7", ab7)

    reopened_env = env_with_slots([
        slot(
            service_id="svc-underarm",
            doctor_id="doc-maryam",
            doctor_name="د. مريم",
            device_key="candela_gentle",
            device_name="Candela Gentle",
            start="2026-09-12T19:00:00+03:00",
            end="2026-09-12T19:30:00+03:00",
        )
    ])
    ab8 = run_sequence(
        "AB8 reopened interval: exact customer booking may proceed",
        ["احجزلي ليزر إبط بكرة الساعة 7 مساءً على كانديلا مع د. مريم"],
        reopened_env,
    )
    if "booking" not in writes(ab8[0]):
        failures.append(f"AB8 reopened exact booking did not reach simulated booking write: {writes(ab8[0])}")

    no_availability_env = env_with_slots([])
    ab9 = run_sequence(
        "AB9 all standard availability blocked: no invented slot",
        [
            "إيه المواعيد المتاحة لليزر الإبط بكرة بعد 6؟",
            "مفيش أي حاجة خالص؟ حتى 7 أو 8؟",
        ],
        no_availability_env,
    )
    assert_no_write(failures, "AB9", ab9)

    print("\n" + "#" * 110)
    print("AUTOMATED SAFETY CHECKS")
    if failures:
        for failure in failures:
            print("FAIL:", failure)
        raise SystemExit(1)
    print("PASS: blocked/no-availability scenarios produced no simulated booking/reschedule write")
    print("PASS: reopened exact verified slot reached a simulated booking write")
    print("NOTE: conversational correctness and wording require human review of AGENT_REPLY lines above")


if __name__ == "__main__":
    main()
