from __future__ import annotations

from types import SimpleNamespace

import pytest

import scripts.run_final_combined_agent_e2e_cost_audit as audit
from scripts.run_final_combined_agent_e2e_cost_audit import (
    UsageCall,
    UsageCollector,
    _c9_final_write_failure,
    _c9_validate_correction_record,
    price_usage_call,
)


def _call(**overrides) -> UsageCall:
    values = {
        "scenario_id": "C1",
        "turn_number": 1,
        "customer_intent": "book",
        "call_stage": "interpreter:turn",
        "stage_group": "interpreter",
        "requested_model": "gpt-6-luna",
        "actual_model": "gpt-6-luna",
        "service_tier": "default",
        "input_tokens": 1_000,
        "cached_input_tokens": 200,
        "cache_write_tokens": 100,
        "output_tokens": 50,
        "reasoning_tokens": 20,
        "total_tokens": 1_050,
        "latency_ms": 100,
        "success": True,
        "usage_status": "MEASURED",
    }
    values.update(overrides)
    return UsageCall(**values)


def test_luna_cost_uses_billable_input_categories_without_double_counting_reasoning() -> None:
    call = _call()
    price_usage_call(call)

    expected = ((700 * 0.10) + (200 * 0.01) + (100 * 0.125) + (50 * 0.50)) / 1_000_000
    assert call.cost_usd == pytest.approx(expected)
    assert call.pricing_status == "MEASURED"

    same_billable_tokens = _call(reasoning_tokens=0)
    price_usage_call(same_billable_tokens)
    assert same_billable_tokens.cost_usd == pytest.approx(call.cost_usd)


def test_luna_long_context_uses_long_context_snapshot() -> None:
    call = _call(
        input_tokens=300_000,
        cached_input_tokens=0,
        cache_write_tokens=0,
        output_tokens=1_000,
        total_tokens=301_000,
    )
    price_usage_call(call)
    expected = ((300_000 * 0.20) + (1_000 * 0.75)) / 1_000_000
    assert call.cost_usd == pytest.approx(expected)


def test_unknown_cache_write_price_is_not_silently_estimated() -> None:
    call = _call(
        requested_model="gpt-5-mini",
        actual_model="gpt-5-mini",
        cache_write_tokens=10,
    )
    price_usage_call(call)
    assert call.cost_usd is None
    assert call.pricing_status == "CACHE_WRITE_PRICE_UNAVAILABLE"


def test_attempt_and_fallback_classification_is_per_stage_and_model(monkeypatch) -> None:
    monkeypatch.setattr(
        "scripts.run_final_combined_agent_e2e_cost_audit.settings.openai_fallback_model",
        "gpt-5-mini",
    )
    collector = UsageCollector()
    collector.calls = [
        _call(),
        _call(),
        _call(requested_model="gpt-5-mini", actual_model="gpt-5-mini"),
    ]

    collector.finalize_attempts()

    assert collector.calls[0].attempt_number == 1
    assert collector.calls[0].retry is False
    assert collector.calls[1].attempt_number == 2
    assert collector.calls[1].retry is True
    assert collector.calls[2].attempt_number == 1
    assert collector.calls[2].fallback is True


def test_real_agent_audit_source_uses_real_arabic_not_mojibake() -> None:
    import ast
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "scripts" / "run_final_combined_agent_e2e_cost_audit.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    values = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    expected_prefix = "\u0639\u0627\u064a\u0632\u0629 \u0623\u062d\u062c\u0632"
    assert any(expected_prefix in value for value in values)
    assert not any(any(marker in value for marker in ("\u00c3", "\u00c2", "\u00d8", "\u00d9")) for value in values)

C9_SERVICE_ID = "bikini-service-id"


def _c9_row(start_at: str) -> dict[str, object]:
    return {
        "service_id": C9_SERVICE_ID,
        "status": "pending",
        "start_at": start_at,
        "laser_device_key": "candela_gentle",
        "additional_service_ids": [],
        "additional_device_keys": [],
    }


def _c9_written_record(start_at: str, *, agent: str = "تمام، الحجز الساعة 12 ظهرًا") -> dict[str, object]:
    return {
        "active_task": None,
        "agent": agent,
        "appointments": [_c9_row(start_at)],
    }


def test_c9_t1_active_task_at_12_without_write_passes() -> None:
    record = {
        "active_task": {"constraints": {"time": {"kind": "exact", "value": "12:00"}}},
        "agent": "تمام",
        "appointments": [],
    }
    already_written, failure = _c9_validate_correction_record(record, expected_service_id=C9_SERVICE_ID)
    assert already_written is False
    assert failure is None


def test_c9_t2_proposal_at_12_cairo_without_write_passes() -> None:
    record = {
        "active_task": {
            "constraints": {"time": {"kind": "exact", "value": "12:00"}},
            "proposal": {"start_at": "2026-10-20T09:00:00+00:00"},
        },
        "agent": "تمام",
        "appointments": [],
    }
    already_written, failure = _c9_validate_correction_record(record, expected_service_id=C9_SERVICE_ID)
    assert already_written is False
    assert failure is None


def test_c9_t3_cleared_task_after_single_pending_12_cairo_write_passes() -> None:
    record = _c9_written_record("2026-10-20T09:00:00+00:00")
    already_written, failure = _c9_validate_correction_record(record, expected_service_id=C9_SERVICE_ID)
    assert already_written is True
    assert failure is None


def test_c9_t4_write_at_old_11_fails() -> None:
    failure = _c9_final_write_failure(
        _c9_written_record("2026-10-20T08:00:00+00:00"),
        expected_service_id=C9_SERVICE_ID,
    )
    assert failure is not None
    assert failure.defect_id == "C9-db-time"


def test_c9_t5_write_at_another_time_fails() -> None:
    failure = _c9_final_write_failure(
        _c9_written_record("2026-10-20T10:00:00+00:00", agent="تمام، الحجز الساعة 1 ظهرًا"),
        expected_service_id=C9_SERVICE_ID,
    )
    assert failure is not None
    assert failure.defect_id == "C9-db-time"


def test_c9_t6_two_appointments_fail() -> None:
    record = _c9_written_record("2026-10-20T09:00:00+00:00")
    record["appointments"] = [
        _c9_row("2026-10-20T09:00:00+00:00"),
        _c9_row("2026-10-20T09:00:00+00:00"),
    ]
    failure = _c9_final_write_failure(record, expected_service_id=C9_SERVICE_ID)
    assert failure is not None
    assert failure.defect_id == "C9-write-count"
    assert failure.safety_counter == "duplicate_appointments"


def test_c9_t7_agent_says_12_but_db_is_not_12_fails() -> None:
    failure = _c9_final_write_failure(
        _c9_written_record("2026-10-20T10:00:00+00:00", agent="تمام، الحجز الساعة 12 ظهرًا"),
        expected_service_id=C9_SERVICE_ID,
    )
    assert failure is not None
    assert failure.defect_id == "C9-db-time"


def test_c9_t8_successful_correction_write_does_not_send_another_confirmation(monkeypatch) -> None:
    turns = [
        {"active_task": {"constraints": {}}, "agent": "تمام", "appointments": []},
        _c9_written_record("2026-10-20T09:00:00+00:00"),
    ]
    customer_messages: list[str] = []

    def fake_chat(_ctx, customer: str):
        customer_messages.append(customer)
        return turns.pop(0)

    def unexpected_completion(_ctx):
        raise AssertionError("audit sent an unnecessary repeat-confirm turn")

    monkeypatch.setattr(audit, "_chat", fake_chat)
    monkeypatch.setattr(audit, "_complete_booking_from_current_state", unexpected_completion)
    monkeypatch.setattr(audit, "_scenario_pass", lambda *_args, **_kwargs: None)

    ctx = SimpleNamespace(
        scenario_id="C9",
        services={"bikini": SimpleNamespace(id=C9_SERVICE_ID)},
    )
    audit.scenario_c9(ctx)

    assert customer_messages == [
        "عايزة أحجز ليزر بكيني على Candela Gentle يوم الثلاثاء 20 أكتوبر 2026 الساعة 11 صباحًا",
        "خليه 12 بدل 11",
    ]
