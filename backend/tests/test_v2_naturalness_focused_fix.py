from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from langchain_core.messages import HumanMessage

from app.agents.availability_presentation import availability_windows_from_slots
from app.agents.v2 import availability_composer
from app.agents.v2.availability_composer import (
    compose_availability_contract_reply,
    presented_availability_window_keys,
)
from app.agents.v2.responder import compose_v2_customer_reply
from app.services.agent_v2.orchestrator import _availability_presentation_context
from app.services.agent_v2.outcome import OutcomeChoice, TurnOutcome
from app.services.agent_v2.response_contract import build_customer_response_contract

NOW = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)


def _compose(outcomes: list[TurnOutcome], message: str) -> tuple[str, str]:
    return compose_v2_customer_reply(
        clinic_name="Linka Clinic",
        timezone_name="Africa/Cairo",
        local_now=NOW,
        history=[HumanMessage(content=message)],
        outcomes=outcomes,
    )


def _choice_outcome(
    goal: str,
    *,
    refs_and_labels: list[tuple[str, str]],
) -> TurnOutcome:
    needed = goal.removeprefix("ask_").removesuffix("_choice")
    return TurnOutcome(
        status="needs_input",
        response_goal=goal,
        facts={"needed": needed},
        choices=[
            OutcomeChoice(ref=ref, label=label, facts={})
            for ref, label in refs_and_labels
        ],
    )


def _appointment_choice(
    ref: str,
    appointment_id: str,
    service: str,
    doctor: str,
    start_local: str,
) -> OutcomeChoice:
    return OutcomeChoice(
        ref=ref,
        label=f"{service} · {doctor} · {start_local}",
        facts={
            "appointment_id": appointment_id,
            "service_name": service,
            "doctor_name": doctor,
            "start_local": start_local,
        },
    )


def _slot(
    *,
    service_id: str = "s1",
    service_name: str = "Hydrafacial",
    doctor_id: str = "d1",
    doctor_name: str = "د. مريم",
    device_key: str | None = None,
    device_name: str | None = None,
    date: str = "2026-10-01",
    start: str,
    end: str,
) -> dict[str, object]:
    row: dict[str, object] = {
        "service_id": service_id,
        "service_name": service_name,
        "doctor_id": doctor_id,
        "doctor_name": doctor_name,
        "start_local": f"{date}T{start}:00+03:00",
        "end_local": f"{date}T{end}:00+03:00",
        "start_time_24h": start,
        "end_time_24h": end,
    }
    if device_key:
        row["laser_device_key"] = device_key
    if device_name:
        row["laser_device_name"] = device_name
    return row


def _window(
    *,
    date: str,
    start: str,
    end: str | None = None,
    doctor: str = "د. مريم",
    device: str | None = None,
) -> dict[str, object]:
    finish = end or start
    row: dict[str, object] = {
        "doctor_name": doctor,
        "start_local": f"{date}T{start}:00+03:00",
        "end_local": f"{date}T{finish}:00+03:00",
        "start_time_24h": start,
        "end_time_24h": finish,
    }
    if device:
        row["laser_device_name"] = device
    return row


def _present(windows: list[dict[str, object]]) -> TurnOutcome:
    return TurnOutcome(
        status="answered",
        response_goal="present_availability",
        facts={
            "availability": {
                "service_name": "Hydrafacial",
                "checked_dates": ["2026-10-01", "2026-10-02", "2026-10-03"],
                "availability_windows": windows,
                "available_option_count": len(windows),
            }
        },
    )


def _force_availability_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        availability_composer,
        "build_realtime_composer_model",
        lambda: (_ for _ in ()).throw(RuntimeError("deterministic test")),
    )


def test_same_semantic_ambiguity_from_two_units_renders_once() -> None:
    options = [("S_PRP_SKIN", "PRP للبشرة"), ("S_PRP_HAIR", "PRP للشعر")]
    outcomes = [
        _choice_outcome("ask_service_choice", refs_and_labels=options),
        _choice_outcome("ask_service_choice", refs_and_labels=options),
    ]

    text, source = _compose(outcomes, "سعر PRP والباقات بتاعته؟")

    assert source == "deterministic:verified-choice-contract"
    assert text.count("تقصد أنهي خدمة من دول؟") == 1
    assert text.count("PRP للبشرة") == 1
    assert text.count("PRP للشعر") == 1


def test_independent_service_and_doctor_ambiguities_remain_separate() -> None:
    outcomes = [
        _choice_outcome(
            "ask_service_choice",
            refs_and_labels=[("S1", "PRP للبشرة"), ("S2", "PRP للشعر")],
        ),
        _choice_outcome(
            "ask_doctor_choice",
            refs_and_labels=[("D1", "د. مريم"), ("D2", "د. سارة")],
        ),
    ]

    text, _source = _compose(outcomes, "عايز PRP مع دكتورة")

    assert text.count("تقصد أنهي خدمة من دول؟") == 1
    assert text.count("تقصد مين فيهم") == 1
    assert "د. مريم" in text
    assert "د. سارة" in text


def test_appointment_choices_use_human_local_datetime_not_iso() -> None:
    outcome = TurnOutcome(
        status="needs_input",
        response_goal="ask_appointment_choice",
        facts={"needed": "appointment"},
        choices=[
            _appointment_choice(
                "appointment-choice-1",
                "a1",
                "PRP للبشرة",
                "د. مريم",
                "2026-10-05T10:00:00+03:00",
            ),
            _appointment_choice(
                "appointment-choice-2",
                "a2",
                "PRP للبشرة",
                "د. مريم",
                "2026-10-05T15:30:00+03:00",
            ),
        ],
    )

    text, _source = _compose(outcome and [outcome], "تقصد أنهي معاد؟")

    assert "5 أكتوبر 2026 الساعة 10 صباحًا" in text
    assert "5 أكتوبر 2026 الساعة 3:30 مساءً" in text
    assert "T10:00:00" not in text
    assert "+03:00" not in text


def test_continuous_and_gapped_slots_keep_existing_range_semantics() -> None:
    slots = [
        _slot(start="15:00", end="15:30"),
        _slot(start="15:30", end="16:00"),
        _slot(start="16:00", end="16:30"),
        _slot(start="19:00", end="19:30"),
        _slot(start="19:30", end="20:00"),
        _slot(start="20:00", end="20:30"),
    ]

    windows = availability_windows_from_slots(slots)

    assert [
        (window["start_time_24h"], window["end_time_24h"])
        for window in windows
    ] == [("15:00", "16:00"), ("19:00", "20:00")]


def test_ranges_never_merge_across_doctor_device_or_service_binding() -> None:
    slots = [
        _slot(start="14:00", end="14:30", doctor_id="d1", doctor_name="د. مريم"),
        _slot(start="14:30", end="15:00", doctor_id="d1", doctor_name="د. مريم"),
        _slot(start="14:00", end="14:30", doctor_id="d2", doctor_name="د. سارة"),
        _slot(
            start="14:00",
            end="14:30",
            doctor_id="d1",
            doctor_name="د. مريم",
            device_key="candela",
            device_name="Candela",
        ),
        _slot(
            start="14:00",
            end="14:30",
            service_id="s2",
            service_name="PRP",
            doctor_id="d1",
            doctor_name="د. مريم",
        ),
    ]

    windows = availability_windows_from_slots(slots)

    assert len(windows) == 4
    identities = {
        (
            window.get("service_name"),
            window.get("doctor_name"),
            window.get("laser_device_name"),
        )
        for window in windows
    }
    assert ("Hydrafacial", "د. مريم", None) in identities
    assert ("Hydrafacial", "د. سارة", None) in identities
    assert ("Hydrafacial", "د. مريم", "Candela") in identities
    assert ("PRP", "د. مريم", None) in identities


def test_availability_windows_are_nearest_first_across_bindings() -> None:
    slots = [
        _slot(
            date="2026-10-03",
            start="14:00",
            end="14:30",
            doctor_id="d1",
            doctor_name="أ دكتور",
        ),
        _slot(
            date="2026-10-01",
            start="17:00",
            end="17:30",
            doctor_id="d2",
            doctor_name="ي دكتور",
        ),
    ]

    windows = availability_windows_from_slots(slots)

    assert str(windows[0]["start_local"]).startswith("2026-10-01")
    assert str(windows[1]["start_local"]).startswith("2026-10-03")


def test_progressive_disclosure_returns_next_ranges_without_repeat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _force_availability_fallback(monkeypatch)
    windows = [
        _window(date="2026-10-01", start="14:00"),
        _window(date="2026-10-01", start="16:00"),
        _window(date="2026-10-02", start="15:00"),
        _window(date="2026-10-02", start="19:00"),
        _window(date="2026-10-03", start="13:00"),
    ]
    outcome = _present(windows)
    contract = build_customer_response_contract([outcome])

    first_text, _source = compose_availability_contract_reply(
        history=[HumanMessage(content="فيه مواعيد هيدرافيشل؟")],
        contract=contract,
    )
    shown_first = frozenset(presented_availability_window_keys(contract))
    second_text, _source = compose_availability_contract_reply(
        history=[HumanMessage(content="في مواعيد تانية؟")],
        contract=contract,
        excluded_window_keys=shown_first,
        continuation=True,
    )

    assert "أقرب المواعيد المتاحة" in first_text
    assert "كمان عندنا" in second_text
    assert "2 مساءً" in first_text
    assert "4 مساءً" in first_text
    assert "3 مساءً" in first_text
    assert "7 مساءً" not in first_text
    assert "1 مساءً" not in first_text
    assert "7 مساءً" in second_text
    assert "1 مساءً" in second_text
    assert "2 مساءً" not in second_text
    assert "4 مساءً" not in second_text


def test_progressive_state_resets_when_verified_scope_changes() -> None:
    windows = [
        _window(date="2026-10-01", start="14:00"),
        _window(date="2026-10-01", start="16:00"),
        _window(date="2026-10-02", start="15:00"),
        _window(date="2026-10-02", start="19:00"),
    ]
    outcome = _present(windows)
    contract = build_customer_response_contract([outcome])
    first_page = list(presented_availability_window_keys(contract))
    understanding = SimpleNamespace(
        operations=[
            SimpleNamespace(type="availability", continues_previous=True)
        ]
    )
    changed_plan = SimpleNamespace(
        steps=[
            SimpleNamespace(
                operation_type="availability",
                disposition="read",
                facts={
                    "service_id": "new-service",
                    "date": {"mode": "next_available"},
                    "time": {"mode": "after", "start_time": "12:00"},
                },
            )
        ]
    )
    recent_context = {
        "operation_type": "availability",
        "service_id": "old-service",
        "date": {"mode": "next_available"},
        "time": {"mode": "after", "start_time": "12:00"},
        "availability_presented_window_keys": first_page,
    }

    excluded, continuation, cumulative = _availability_presentation_context(
        understanding=understanding,
        plan=changed_plan,
        outcomes=[outcome],
        recent_read_context=recent_context,
    )

    assert excluded == frozenset()
    assert continuation is False
    assert cumulative == tuple(first_page)


def test_progressive_state_continues_only_for_same_verified_scope() -> None:
    windows = [
        _window(date="2026-10-01", start="14:00"),
        _window(date="2026-10-01", start="16:00"),
        _window(date="2026-10-02", start="15:00"),
        _window(date="2026-10-02", start="19:00"),
    ]
    outcome = _present(windows)
    contract = build_customer_response_contract([outcome])
    first_page = list(presented_availability_window_keys(contract))
    understanding = SimpleNamespace(
        operations=[
            SimpleNamespace(type="availability", continues_previous=True)
        ]
    )
    scope = {
        "service_id": "same-service",
        "date": {"mode": "next_available"},
        "time": {"mode": "after", "start_time": "12:00"},
    }
    plan = SimpleNamespace(
        steps=[
            SimpleNamespace(
                operation_type="availability",
                disposition="read",
                facts=scope,
            )
        ]
    )
    recent_context = {
        "operation_type": "availability",
        **scope,
        "availability_presented_window_keys": first_page,
    }

    excluded, continuation, cumulative = _availability_presentation_context(
        understanding=understanding,
        plan=plan,
        outcomes=[outcome],
        recent_read_context=recent_context,
    )

    assert excluded == frozenset(first_page)
    assert continuation is True
    assert cumulative[: len(first_page)] == tuple(first_page)
    assert len(cumulative) == 4
