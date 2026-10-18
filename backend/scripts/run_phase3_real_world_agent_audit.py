from __future__ import annotations

import json
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import uuid4

import run_grouped_multiservice_intent_live_review as rag
from langchain_core.messages import AIMessage, HumanMessage
from sqlalchemy import select

from app.models.appointment import Appointment
from app.models.branch import Branch
from app.models.conversation import Conversation
from app.services.agent_v2.live_chat import (
    _availability_reference_context_from_turn,
    _verified_read_context_from_turn,
)
from app.services.agent_v2.orchestrator import orchestrate_v2_turn


@dataclass(frozen=True)
class ScenarioSpec:
    name: str
    messages: tuple[str, ...]
    validator: Callable[[list[dict[str, object]], dict[str, str]], None]
    fixture_variant: str | None = None


def _task(record: dict[str, object]) -> dict[str, object]:
    value = record.get("active_task")
    return value if isinstance(value, dict) else {}


def _constraints(record: dict[str, object]) -> dict[str, object]:
    value = _task(record).get("constraints")
    return value if isinstance(value, dict) else {}


def _proposal(record: dict[str, object]) -> dict[str, object] | None:
    value = record.get("proposal")
    return value if isinstance(value, dict) else None


def _services(record: dict[str, object]) -> set[str]:
    return {str(item) for item in record.get("requested_service_ids") or []}


def _status(record: dict[str, object]) -> str | None:
    value = _task(record).get("status")
    return str(value) if value is not None else None


def _local_start(proposal: dict[str, object]) -> datetime:
    return datetime.fromisoformat(str(proposal["start_at"])).astimezone(rag.CAIRO)


def _proposal_services(proposal: dict[str, object]) -> set[str]:
    return {str(item) for item in proposal.get("service_ids") or []}


def _proposal_signature(proposal: dict[str, object]) -> tuple[object, ...]:
    visits = proposal.get("visits")
    normalized_visits: list[tuple[object, ...]] = []
    if isinstance(visits, list):
        for visit in visits:
            if not isinstance(visit, dict):
                continue
            normalized_visits.append(
                (
                    tuple(sorted(str(item) for item in visit.get("service_ids") or [])),
                    str(visit.get("doctor_id") or ""),
                    str(visit.get("device_key") or ""),
                    str(visit.get("start_at") or ""),
                    str(visit.get("end_at") or ""),
                )
            )
    return (
        tuple(sorted(_proposal_services(proposal))),
        str(proposal.get("doctor_id") or ""),
        str(proposal.get("device_key") or ""),
        str(proposal.get("start_at") or ""),
        tuple(sorted(normalized_visits)),
    )


def _proposal_visits(record: dict[str, object]) -> list[dict[str, object]]:
    proposal = _proposal(record)
    visits = proposal.get("visits") if proposal else None
    return [item for item in visits or [] if isinstance(item, dict)]


def _visit_for(record: dict[str, object], services: set[str]) -> dict[str, object]:
    for visit in _proposal_visits(record):
        if {str(item) for item in visit.get("service_ids") or []} == services:
            return visit
    raise AssertionError(f"missing proposal visit for services={sorted(services)}")


def _assert_zero_write(records: list[dict[str, object]]) -> None:
    for index, record in enumerate(records, start=1):
        assert record.get("pending_write") is None, f"turn {index}: premature pending_write"


def _normalized_customer_reply(record: dict[str, object]) -> str:
    reply = str(record.get("reply") or "")
    return reply.translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")).replace("أ", "ا").replace("إ", "ا")


def _assert_grouped_laser_reply_mentions_both_services(
    record: dict[str, object],
    *,
    scenario: str,
) -> None:
    reply = _normalized_customer_reply(record)
    assert "ابط" in reply and "بكيني" in reply, (
        f"{scenario} customer reply omitted one of the requested grouped laser services"
    )


def _assert_reply_mentions_visit_start_times(
    record: dict[str, object],
    *,
    expected_times: tuple[str, ...],
    scenario: str,
) -> None:
    reply = _normalized_customer_reply(record)
    for value in expected_times:
        hour, minute = value.split(":")
        hour24 = int(hour)
        hour12 = hour24 % 12 or 12
        candidates = [value, f"{hour12}:{minute}"]
        if minute == "00":
            candidates.append(f"{hour12} مساء" if hour24 >= 12 else f"{hour12} صباح")
        assert any(candidate in reply for candidate in candidates), (
            f"{scenario} customer reply omitted visit start time {value}"
        )


def _assert_known_services(records: list[dict[str, object]], ids: dict[str, str]) -> None:
    known = {ids["underarm"], ids["bikini"], ids["arms"], ids["dermatology"]}
    for index, record in enumerate(records, start=1):
        assert _services(record) <= known, f"turn {index}: unknown requested service id"


def _assert_resource_safety(records: list[dict[str, object]], ids: dict[str, str]) -> None:
    for turn_index, record in enumerate(records, start=1):
        for visit in _proposal_visits(record):
            services = {str(item) for item in visit.get("service_ids") or []}
            doctor_id = str(visit.get("doctor_id") or "")
            device_key = visit.get("device_key")
            if ids["dermatology"] in services:
                assert services == {ids["dermatology"]}, (
                    f"turn {turn_index}: dermatology contaminated with another category"
                )
                assert doctor_id == ids["dermatology_doctor"], (
                    f"turn {turn_index}: wrong dermatology doctor"
                )
                assert device_key is None, f"turn {turn_index}: device leaked onto dermatology"
            elif services:
                assert doctor_id == ids["doctor"], f"turn {turn_index}: wrong laser doctor"


def _assert_common_safety(records: list[dict[str, object]], ids: dict[str, str]) -> None:
    _assert_known_services(records, ids)
    _assert_resource_safety(records, ids)


def _assert_proposal(
    record: dict[str, object],
    *,
    services: set[str],
    date: str,
    time: str,
    device_key: str | None = "candela_gentle",
    doctor_id: str | None = None,
) -> dict[str, object]:
    proposal = _proposal(record)
    assert proposal is not None, "expected current booking proposal"
    assert _proposal_services(proposal) == services, "proposal service set mismatch"
    start = _local_start(proposal)
    assert start.date().isoformat() == date, f"proposal date mismatch: {start.date().isoformat()}"
    assert start.strftime("%H:%M") == time, f"proposal time mismatch: {start.strftime('%H:%M')}"
    if device_key is not None:
        assert proposal.get("device_key") == device_key, "proposal device mismatch"
    if doctor_id is not None:
        assert proposal.get("doctor_id") == doctor_id, "proposal doctor mismatch"
    assert _status(record) == "awaiting_confirmation", "proposal must await confirmation"
    return proposal


def _has_availability_slots(record: dict[str, object]) -> bool:
    for trace in record.get("traces") or []:
        if isinstance(trace, dict) and trace.get("availability_slots"):
            return True
    return False


def _device_choices(record: dict[str, object]) -> set[str]:
    choices: set[str] = set()
    task = _task(record)
    snapshot = task.get("option_snapshot")
    if isinstance(snapshot, dict) and snapshot.get("purpose") == "device":
        for option in snapshot.get("options") or []:
            if not isinstance(option, dict):
                continue
            payload = option.get("payload")
            if isinstance(payload, dict):
                value = payload.get("device_key") or payload.get("laser_device_key")
                if value:
                    choices.add(str(value))
    plan = record.get("plan")
    if isinstance(plan, dict):
        for step in plan.get("steps") or []:
            if not isinstance(step, dict):
                continue
            facts = step.get("facts")
            if not isinstance(facts, dict):
                continue
            for value in facts.get("compatible_visit_device_keys") or []:
                choices.add(str(value))
    return choices


def _pending_step(record: dict[str, object]) -> dict[str, object]:
    pending = record.get("pending_write")
    assert isinstance(pending, dict), "expected pending write boundary"
    step = pending.get("step")
    if hasattr(step, "model_dump"):
        step = step.model_dump(mode="json")
    assert isinstance(step, dict), "pending write step is not inspectable"
    return step


def _validate_rw1(records: list[dict[str, object]], ids: dict[str, str]) -> None:
    assert len(records) == 1
    record = records[0]
    expected = {ids["underarm"], ids["bikini"]}
    assert _services(record) == expected, "RW-1 lost or added a service"
    assert rag._collapsed_service_set(record) == expected, "RW-1 services not kept as one grouped visit"
    assert _constraints(record).get("device_key") == "candela_gentle", "RW-1 lost explicit Candela choice"
    assert _has_availability_slots(record), "RW-1 did not ground availability in canonical slots"
    _assert_grouped_laser_reply_mentions_both_services(record, scenario="RW-1")
    _assert_zero_write(records)


def _validate_rw2(records: list[dict[str, object]], ids: dict[str, str]) -> None:
    assert len(records) == 3
    assert _services(records[0]) == {ids["underarm"], ids["bikini"]}, "RW-2 turn 1 service set mismatch"
    _assert_grouped_laser_reply_mentions_both_services(records[0], scenario="RW-2 turn 1")
    assert _services(records[1]) == {ids["underarm"], ids["bikini"], ids["arms"]}, "RW-2 addition failed"
    assert _services(records[2]) == {ids["underarm"], ids["arms"]}, "RW-2 removal failed"
    assert ids["bikini"] not in _services(records[2]), "RW-2 resurrected removed Bikini"
    _assert_zero_write(records)


def _validate_rw3(records: list[dict[str, object]], ids: dict[str, str]) -> None:
    assert len(records) == 3
    expected = {ids["underarm"], ids["bikini"]}
    first = _assert_proposal(
        records[0], services=expected, date="2026-10-17", time="18:00",
        doctor_id=ids["doctor"],
    )
    assert records[1].get("reply"), "RW-3 side question produced no reply"
    assert "1200" in _normalized_customer_reply(records[1]), (
        "RW-3 side price question omitted the verified grouped total of 1200 EGP"
    )
    assert _services(records[1]) == expected, "RW-3 side question changed services"
    second = _proposal(records[1])
    assert second is not None and _proposal_signature(second) == _proposal_signature(first), (
        "RW-3 side question changed current proposal"
    )
    third = _proposal(records[2])
    assert third is not None and _proposal_signature(third) == _proposal_signature(first), (
        "RW-3 detail resume changed current proposal"
    )
    assert _status(records[2]) == "awaiting_confirmation", "RW-3 lost awaiting_confirmation"
    _assert_zero_write(records)


def _validate_rw4(records: list[dict[str, object]], ids: dict[str, str]) -> None:
    assert len(records) == 2
    expected = {ids["underarm"], ids["bikini"]}
    old = _assert_proposal(records[0], services=expected, date="2026-10-17", time="18:00")
    fresh = _assert_proposal(records[1], services=expected, date="2026-10-17", time="19:00")
    assert str(fresh.get("start_at")) != str(old.get("start_at")), "RW-4 stale 18:00 proposal survived"
    _assert_zero_write(records)


def _validate_rw5(records: list[dict[str, object]], ids: dict[str, str]) -> None:
    assert len(records) == 2
    expected = {ids["underarm"], ids["bikini"]}
    old = _assert_proposal(records[0], services=expected, date="2026-10-17", time="18:00")
    fresh = _assert_proposal(records[1], services=expected, date="2026-10-17", time="19:00")
    assert str(fresh.get("start_at")) != str(old.get("start_at")), "RW-5 stale 18:00 proposal survived"
    assert records[1].get("pending_write") is None, "RW-5 confirm+modify incorrectly authorized a write"
    assert _status(records[1]) == "awaiting_confirmation", "RW-5 must require confirmation again"


def _assert_compound(
    record: dict[str, object],
    ids: dict[str, str],
    *,
    laser_time: str,
    derm_time: str,
) -> None:
    expected = {ids["underarm"], ids["bikini"], ids["dermatology"]}
    proposal = _proposal(record)
    assert proposal is not None, "expected compound proposal"
    assert _proposal_services(proposal) == expected, "compound proposal service set mismatch"
    assert len(_proposal_visits(record)) == 2, "compound proposal must have two visits"
    laser = _visit_for(record, {ids["underarm"], ids["bikini"]})
    derm = _visit_for(record, {ids["dermatology"]})
    assert str(laser.get("doctor_id")) == ids["doctor"], "wrong laser doctor"
    assert laser.get("device_key") == "candela_gentle", "wrong laser device"
    assert str(derm.get("doctor_id")) == ids["dermatology_doctor"], "wrong dermatology doctor"
    assert derm.get("device_key") is None, "dermatology must not have a device"
    laser_start = datetime.fromisoformat(str(laser["start_at"])).astimezone(rag.CAIRO)
    derm_start = datetime.fromisoformat(str(derm["start_at"])).astimezone(rag.CAIRO)
    assert laser_start.strftime("%H:%M") == laser_time, "laser visit start mismatch"
    assert derm_start.strftime("%H:%M") == derm_time, "dermatology sequencing mismatch"
    assert _status(record) == "awaiting_confirmation", "compound proposal must await confirmation"


def _validate_rw6(records: list[dict[str, object]], ids: dict[str, str]) -> None:
    assert len(records) == 3
    _assert_compound(records[1], ids, laser_time="18:00", derm_time="18:30")
    _assert_reply_mentions_visit_start_times(
        records[1], expected_times=("18:00", "18:30"), scenario="RW-6"
    )
    before = _proposal(records[1])
    after = _proposal(records[2])
    assert before is not None and after is not None
    assert _proposal_signature(after) == _proposal_signature(before), "RW-6 doctor side question changed proposal"
    assert records[2].get("reply"), "RW-6 doctor side question produced no reply"
    _assert_zero_write(records)


def _validate_rw7(records: list[dict[str, object]], ids: dict[str, str]) -> None:
    assert len(records) == 3
    _assert_compound(records[1], ids, laser_time="18:00", derm_time="18:30")
    _assert_reply_mentions_visit_start_times(
        records[1], expected_times=("18:00", "18:30"), scenario="RW-7 turn 2"
    )
    _assert_compound(records[2], ids, laser_time="19:00", derm_time="19:30")
    _assert_reply_mentions_visit_start_times(
        records[2], expected_times=("19:00", "19:30"), scenario="RW-7 turn 3"
    )
    time_constraint = _constraints(records[2]).get("time")
    if isinstance(time_constraint, dict) and time_constraint.get("mode") == "exact":
        assert time_constraint.get("start_time") == "19:00", (
            "RW-7 operational 19:30 leaked into customer semantic constraint"
        )
    _assert_zero_write(records)


def _validate_rw8(records: list[dict[str, object]], ids: dict[str, str]) -> None:
    assert len(records) == 2
    expected = {ids["underarm"], ids["bikini"]}
    assert _services(records[0]) == expected, "RW-8 turn 1 service set mismatch"
    _assert_grouped_laser_reply_mentions_both_services(records[0], scenario="RW-8 turn 1")
    first = _proposal(records[0])
    if first is not None:
        first_start = _local_start(first)
        assert not (
            first_start.date().isoformat() == "2026-10-17" and first_start.strftime("%H:%M") == "18:00"
        ), "RW-8 invented proposal at canonically unavailable 18:00 slot"
    second = _assert_proposal(
        records[1], services=expected, date="2026-10-24", time="18:00",
        doctor_id=ids["doctor"],
    )
    assert _local_start(second).date().isoformat() != "2026-10-18", "RW-8 interpreted next Saturday as Sunday"
    _assert_zero_write(records)


def _validate_rw9(records: list[dict[str, object]], ids: dict[str, str]) -> None:
    assert len(records) == 1
    expected = {ids["underarm"], ids["bikini"]}
    _assert_proposal(
        records[0], services=expected, date="2026-10-17", time="15:00",
        doctor_id=ids["doctor"],
    )
    _assert_zero_write(records)


def _validate_rw10(records: list[dict[str, object]], ids: dict[str, str]) -> None:
    assert len(records) == 2
    expected = {ids["underarm"], ids["bikini"]}
    first = records[0]
    assert _services(first) == expected, "RW-10 turn 1 service set mismatch"
    assert _constraints(first).get("device_key") in (None, ""), "RW-10 arbitrarily selected a device"
    first_proposal = _proposal(first)
    assert first_proposal is None or first_proposal.get("device_key") in (None, ""), (
        "RW-10 created proposal with an arbitrary device"
    )
    choices = _device_choices(first)
    assert {"candela_gentle", "prime_lase"} <= choices, "RW-10 did not expose both valid device choices"
    _assert_proposal(
        records[1], services=expected, date="2026-10-17", time="18:00",
        device_key="candela_gentle", doctor_id=ids["doctor"],
    )
    assert _services(records[1]) == expected, "RW-10 lost services after device selection"
    _assert_zero_write(records)


def _validate_rw11(records: list[dict[str, object]], ids: dict[str, str]) -> None:
    assert len(records) == 2
    expected = {ids["underarm"], ids["bikini"]}
    proposal = _assert_proposal(
        records[0], services=expected, date="2026-10-17", time="18:00",
        doctor_id=ids["doctor"],
    )
    assert records[0].get("pending_write") is None, "RW-11 wrote before explicit confirmation"
    assert _services(records[1]) == expected, "RW-11 confirmation changed service set"
    current = _proposal(records[1])
    assert current is not None and _proposal_signature(current) == _proposal_signature(proposal), (
        "RW-11 confirmation did not reference the same current proposal"
    )
    step = _pending_step(records[1])
    intent = step.get("write_intent")
    assert isinstance(intent, dict), "RW-11 pending boundary lacks write intent"
    assert intent.get("kind") == "booking_proposal", "RW-11 pending write is not proposal-backed booking"
    assert intent.get("authorized") is True, "RW-11 booking proposal write was not authorized"
    parameters = intent.get("parameters")
    assert isinstance(parameters, dict), "RW-11 pending write lacks proposal parameters"
    assert parameters.get("proposal_id") == proposal.get("proposal_id"), (
        "RW-11 pending write did not bind the current server proposal"
    )
    snapshot = parameters.get("proposal")
    assert isinstance(snapshot, dict), "RW-11 pending write lacks proposal snapshot"
    assert {str(item) for item in snapshot.get("service_ids") or []} == expected, (
        "RW-11 pending proposal snapshot changed services"
    )
    assert str(snapshot.get("doctor_id")) == ids["doctor"], "RW-11 pending proposal changed doctor"
    assert snapshot.get("device_key") == "candela_gentle", "RW-11 pending proposal changed device"
    snapshot_start = datetime.fromisoformat(str(snapshot["start_at"])).astimezone(rag.CAIRO)
    assert snapshot_start.date().isoformat() == "2026-10-17", "RW-11 pending proposal changed date"
    assert snapshot_start.strftime("%H:%M") == "18:00", "RW-11 pending proposal changed time"


RW1 = (
    "أنا عايزة أعمل ليزر الإبط والبكيني مع بعض السبت، خليهم في نفس الزيارة وعلى كانديلا لو ينفع، "
    "وشوفيلي ميعاد بعد الساعة 5 بس متحجزيش دلوقتي."
)
RW2 = (
    "عايزة الإبط والبكيني مع بعض السبت، وريني المواعيد بس.",
    "طب ضيفي Full Arms معاهم كمان.",
    "لا معلش، شيل البكيني وخلي الإبط وFull Arms بس.",
)
RW3 = (
    "عايزة الإبط والبكيني السبت الساعة 6، وريني كل التفاصيل قبل ما تأكدي أي حاجة.",
    "الدكتورة مين والسعر كام؟",
    "تمام، وريني تفاصيل الميعاد تاني بس لسه متحجزيش.",
)
RW4 = (
    "عايزة الإبط والبكيني السبت الساعة 6، وريني التفاصيل قبل التأكيد.",
    "خليها الساعة 7 بدل 6.",
)
RW5 = (
    "عايزة الإبط والبكيني السبت الساعة 6، وريني التفاصيل قبل التأكيد.",
    "تمام أكدي الحجز، بس خليها الساعة 7 بدل 6.",
)
RW6 = (
    "أنا عايزة أعمل الإبط والبكيني، وبعدهم كشف جلدية في نفس المشوار يوم السبت، "
    "خليهم ورا بعض ووريني التفاصيل قبل ما تحجزي.",
    "الساعة 6 مناسبة.",
    "دكتور الكشف مين؟",
)
RW7 = (
    RW6[0],
    "الساعة 6 مناسبة.",
    "خلي بداية الزيارة الساعة 7 بدل 6.",
)
RW8 = (
    "عايزة الإبط والبكيني السبت الساعة 6.",
    "طب السبت اللي بعده الساعة 6؟",
)
RW9 = ("السبت الساعة 3 ينفع للإبط والبكيني؟ وريني التفاصيل قبل الحجز.",)
RW10 = (
    "عايزة الإبط والبكيني السبت الساعة 6، أي جهاز مناسب عندكم تمام.",
    "خليها Candela.",
)
RW11 = (
    "عايزة الإبط والبكيني السبت الساعة 6، وريني التفاصيل الأول.",
    "أيوه تمام، أكدي نفس الحجز زي ما هو.",
)


SCENARIOS = (
    ScenarioSpec("RW-1", (RW1,), _validate_rw1),
    ScenarioSpec("RW-2", RW2, _validate_rw2),
    ScenarioSpec("RW-3", RW3, _validate_rw3),
    ScenarioSpec("RW-4", RW4, _validate_rw4),
    ScenarioSpec("RW-5", RW5, _validate_rw5),
    ScenarioSpec("RW-6", RW6, _validate_rw6),
    ScenarioSpec("RW-7", RW7, _validate_rw7),
    ScenarioSpec("RW-8", RW8, _validate_rw8, fixture_variant="busy_2026_10_17_1800"),
    ScenarioSpec("RW-9", RW9, _validate_rw9),
    ScenarioSpec("RW-10", RW10, _validate_rw10, fixture_variant="second_laser_device"),
    ScenarioSpec("RW-11", RW11, _validate_rw11),
)


def _seed_busy_exact_slot(db, *, workspace, patient, doctor, underarm) -> None:
    branch = db.scalar(select(Branch).where(Branch.id == workspace.primary_branch_id))
    assert branch is not None
    start = datetime(2026, 10, 17, 18, 0, tzinfo=rag.CAIRO)
    end = start + timedelta(minutes=30)
    db.add(
        Appointment(
            workspace_id=workspace.id,
            patient_id=patient.id,
            branch_id=branch.id,
            doctor_id=doctor.id,
            doctor_assignment_known=True,
            is_quick_booking=False,
            service_id=underarm.id,
            status="confirmed",
            source="staff",
            start_at=start,
            end_at=end,
            busy_start_at=start,
            busy_end_at=end,
            duration_minutes=30,
            price_minor=50_000,
            discount_minor=0,
            currency="EGP",
            laser_device_key="candela_gentle",
            laser_device_name="Candela Gentle",
            payment_status="unknown",
            payment_method="unknown",
            billing_context="standard",
        )
    )
    db.flush()


def _add_second_laser_device(db, *, workspace, underarm, bikini) -> None:
    for service, price in ((underarm, 45_000), (bikini, 65_000)):
        rag._device_price(
            db,
            workspace=workspace,
            service=service,
            device_key="prime_lase",
            device_name="Prime Lase",
            price_minor=price,
            duration_minutes=30,
        )
    db.flush()


def _failure_evidence(
    *,
    name: str,
    messages: tuple[str, ...],
    records: list[dict[str, object]],
    before: int | None,
    after: int | None,
    error: str,
) -> dict[str, object]:
    turns = []
    for record in records:
        task = _task(record)
        proposal = _proposal(record)
        turns.append(
            {
                "customer": record.get("customer"),
                "raw_model_structured": record.get("raw_model_structured"),
                "requested_service_ids": record.get("requested_service_ids"),
                "active_task_before": record.get("active_task_before"),
                "active_task_after": record.get("active_task"),
                "status": task.get("status"),
                "proposal": proposal,
                "proposal_visits": proposal.get("visits") if proposal else None,
                "doctor_id": record.get("doctor_id"),
                "device_key": record.get("device_key"),
                "canonical_reads": record.get("outcomes"),
                "pending_write": record.get("pending_write"),
                "reply": record.get("reply"),
            }
        )
    return {
        "scenario": name,
        "customer_turns": list(messages),
        "turns": turns,
        "appointment_count_before": before,
        "appointment_count_after": after,
        "appointment_delta": (after - before) if before is not None and after is not None else None,
        "exact_failed_invariant": error,
    }


def _run_scenario(spec: ScenarioSpec) -> dict[str, object]:
    records: list[dict[str, object]] = []
    before: int | None = None
    after: int | None = None
    ids: dict[str, str] = {}
    try:
        with rag.rollback_session() as db:
            (
                workspace,
                patient,
                doctor,
                dermatology_doctor,
                underarm,
                bikini,
                arms,
                dermatology,
            ) = rag.build_fixture(db)
            ids = {
                "underarm": str(underarm.id),
                "bikini": str(bikini.id),
                "arms": str(arms.id),
                "dermatology": str(dermatology.id),
                "doctor": str(doctor.id),
                "dermatology_doctor": str(dermatology_doctor.id),
            }
            if spec.fixture_variant == "busy_2026_10_17_1800":
                _seed_busy_exact_slot(
                    db,
                    workspace=workspace,
                    patient=patient,
                    doctor=doctor,
                    underarm=underarm,
                )
            elif spec.fixture_variant == "second_laser_device":
                _add_second_laser_device(
                    db,
                    workspace=workspace,
                    underarm=underarm,
                    bikini=bikini,
                )

            conversation_id = uuid4()
            db.add(
                Conversation(
                    id=conversation_id,
                    workspace_id=workspace.id,
                    patient_id=patient.id,
                    channel="web",
                    status="open",
                    owner_type="ai",
                    unread_count=0,
                    started_at=rag.NOW,
                    last_message_at=rag.NOW,
                )
            )
            db.flush()

            history = []
            previous_task = None
            recent_read_context: dict[str, object] | None = None
            availability_reference_context: dict[str, object] | None = None
            before = rag.appointment_count(db, workspace.id)

            for index, customer in enumerate(spec.messages, start=1):
                history.append(HumanMessage(content=customer))
                rag.RAW.clear()
                rag.STAGES.clear()
                result = orchestrate_v2_turn(
                    db=db,
                    workspace=workspace,
                    patient=patient,
                    conversation_id=conversation_id,
                    run_id=uuid4(),
                    history=history,
                    local_now=rag.NOW,
                    timezone_name=workspace.timezone,
                    clinic_name=workspace.name,
                    turn_id=f"phase3-real-world-{spec.name.lower()}-{index}",
                    write_executor=None,
                    recent_read_context=recent_read_context,
                    availability_reference_context=availability_reference_context,
                )
                recent_read_context = _verified_read_context_from_turn(
                    db,
                    workspace=workspace,
                    turn=result,
                    previous_read_context=recent_read_context,
                )
                availability_reference_context = _availability_reference_context_from_turn(
                    result,
                    previous_context=availability_reference_context,
                    verified_read_context=recent_read_context,
                )
                record = rag.turn_record(
                    result,
                    customer=customer,
                    raw=list(rag.RAW),
                    stages=list(rag.STAGES),
                    active_task_before=previous_task,
                    availability_reference_context=availability_reference_context,
                )
                record["appointment_count_after_turn"] = rag.appointment_count(db, workspace.id)
                records.append(record)
                previous_task = result.active_task
                if result.reply:
                    history.append(AIMessage(content=result.reply))

            after = rag.appointment_count(db, workspace.id)
            assert after == before, f"audit changed appointment count: before={before} after={after}"
            _assert_common_safety(records, ids)
            spec.validator(records, ids)

            payload = {
                "scenario": spec.name,
                "status": "PASS",
                "ids": ids,
                "appointment_count_before": before,
                "appointment_count_after": after,
                "appointment_delta": after - before,
                "turns": records,
            }
            print(
                "PHASE3_RW_RESULT="
                + json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str),
                flush=True,
            )
            return {"scenario": spec.name, "status": "PASS", "error": None}
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        evidence = _failure_evidence(
            name=spec.name,
            messages=spec.messages,
            records=records,
            before=before,
            after=after,
            error=error,
        )
        print(
            "PHASE3_RW_RESULT="
            + json.dumps(
                {
                    "scenario": spec.name,
                    "status": "FAIL",
                    "ids": ids,
                    "appointment_count_before": before,
                    "appointment_count_after": after,
                    "appointment_delta": (after - before) if before is not None and after is not None else None,
                    "turns": records,
                    "error": error,
                },
                ensure_ascii=False,
                separators=(",", ":"),
                default=str,
            ),
            flush=True,
        )
        print(
            "PHASE3_RW_FAILURE_EVIDENCE="
            + json.dumps(evidence, ensure_ascii=False, separators=(",", ":"), default=str),
            flush=True,
        )
        print(traceback.format_exc(), flush=True)
        return {"scenario": spec.name, "status": "FAIL", "error": error}


def main() -> int:
    results = [_run_scenario(spec) for spec in SCENARIOS]
    failed = [item for item in results if item["status"] == "FAIL"]
    summary = {
        "total": len(results),
        "passed": len(results) - len(failed),
        "failed": len(failed),
        "scenarios": results,
    }
    print(
        "PHASE3_REAL_WORLD_AUDIT_SUMMARY="
        + json.dumps(summary, ensure_ascii=False, separators=(",", ":"), default=str),
        flush=True,
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
