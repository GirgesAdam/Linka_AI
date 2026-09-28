"""Bounded Phase 3E Patient/CRM response-contract live review.

Uses the real V2 interpreter/planner/outcome/responder with deterministic fixtures.
No database access, patient mutation, appointment mutation, or financial write occurs.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage

from app.services.agent_v2.test_harness import (
    V2FixtureEnvironment,
    run_v2_fixture_turn,
)

NOW = datetime.fromisoformat("2026-09-28T23:00:00+03:00")


def _profile_env(**updates: object) -> V2FixtureEnvironment:
    base = V2FixtureEnvironment()
    profile = dict(base.profile)
    profile.update(updates)
    return replace(base, profile=profile)


def _history_env() -> V2FixtureEnvironment:
    base = V2FixtureEnvironment()
    history = dict(base.history)
    history["profile"] = {
        "first_name": "PRIVATE_NAME",
        "phone": "01099999999",
        "gender": "female",
        "birth_date": "1990-01-01",
    }
    history["money"] = [{"currency": "EGP", "net_paid": "500.00 EGP"}]
    return replace(base, history=history)


def _operation(result: object, index: int = 0):
    return result.understanding.operations[index]


CASES: list[dict[str, Any]] = [
    {
        "name": "broad_profile",
        "history": [HumanMessage(content="إيه البيانات المسجلة عندكم ليا؟")],
        "env": _profile_env(),
        "expected_details": set(),
        "expected_reply": ["سارة أحمد", "01012345678", "العربية"],
        "forbidden_reply": ["active", "blocked"],
        "pure": True,
    },
    {
        "name": "name_only_stale_chat",
        "history": [
            HumanMessage(content="اسمي إيه؟"),
            AIMessage(content="اسمك هدى."),
            HumanMessage(content="طيب اسمي المسجل عندكم إيه بالظبط؟"),
        ],
        "env": _profile_env(),
        "expected_details": {"name"},
        "expected_reply": ["سارة أحمد"],
        "forbidden_reply": ["هدى", "01012345678", "العربية"],
        "pure": True,
    },

    {
        "name": "phone_only",
        "history": [HumanMessage(content="رقم الموبايل المسجل عندكم ليا إيه؟")],
        "env": _profile_env(phone="+20 101 234 5678"),
        "expected_details": {"phone"},
        "expected_reply": ["+20 101 234 5678"],
        "forbidden_reply": ["سارة", "العربية", "201012345678"],
        "pure": True,
    },
    {
        "name": "preferred_language_only",
        "history": [HumanMessage(content="What language do you have saved as my preference?")],
        "env": _profile_env(preferred_language="en"),
        "expected_details": {"preferred_language"},
        "expected_reply": ["English"],
        "forbidden_reply": ["سارة", "01012345678", "active"],
        "pure": True,
    },
    {
        "name": "missing_phone",
        "history": [HumanMessage(content="رقمي المسجل عندكم إيه؟")],
        "env": _profile_env(phone=None),
        "expected_details": {"phone"},
        "expected_reply": ["مفيش رقم موبايل مسجل عندنا"],
        "forbidden_reply": ["01012345678", "سارة", "العربية"],
        "pure": True,
    },

    {
        "name": "history_profile_pii_filtered",
        "history": [HumanMessage(content="آخر زيارة عملتها كانت إيه؟")],
        "env": _history_env(),
        "expected_details": None,
        "expected_reply": ["ليزر إبط"],
        "forbidden_reply": [
            "PRIVATE_NAME",
            "01099999999",
            "1990-01-01",
            "500",
            "جنيه",
        ],
        "pure": False,
        "history_privacy": True,
    },
    {
        "name": "mixed_phone_and_service",
        "history": [
            HumanMessage(
                content="رقم الموبايل المسجل عندكم إيه وكمان إيه الخدمات اللي عندكم؟"
            )
        ],
        "env": _profile_env(),
        "expected_details": {"phone"},
        "expected_reply": ["01012345678"],
        "forbidden_reply": ["active", "blocked", "phone_normalized"],
        "pure": False,
        "mixed": True,
    },
]


def _assert_profile_scope(result: object, expected: set[str]) -> None:
    operations = [
        operation
        for operation in result.understanding.operations
        if operation.type == "customer_profile"
    ]
    if len(operations) != 1:
        raise AssertionError(f"Expected one customer_profile operation, got {operations!r}")
    actual = set(operations[0].requested_patient_details)
    if actual != expected:
        raise AssertionError(
            f"Patient detail scope mismatch: expected {expected}, got {actual}"
        )

    traces = [
        trace for trace in result.traces if trace.operation_type == "customer_profile"
    ]
    if len(traces) != 1:
        raise AssertionError("Expected one customer_profile trace.")
    facts = traces[0].outcome.facts.get("customer_profile")
    if not isinstance(facts, dict):
        raise AssertionError("Customer profile facts missing.")

    requested = set(facts.get("requested_details") or [])
    expected_effective = expected or {"name", "phone", "preferred_language"}
    if requested != expected_effective:
        raise AssertionError(
            f"Outcome scope mismatch: expected {expected_effective}, got {requested}"
        )
    patient = facts.get("patient")
    if not isinstance(patient, dict):
        raise AssertionError("Customer profile patient facts missing.")
    allowed_keys: set[str] = set()
    if "name" in expected_effective:
        allowed_keys.update({"first_name", "last_name"})
    if "phone" in expected_effective:
        allowed_keys.add("phone")
    if "preferred_language" in expected_effective:
        allowed_keys.add("preferred_language")
    if not set(patient).issubset(allowed_keys):
        raise AssertionError(f"Unexpected profile fields leaked: {set(patient) - allowed_keys}")


def _assert_history_privacy(result: object) -> None:
    traces = [trace for trace in result.traces if trace.operation_type == "customer_history"]
    if len(traces) != 1:
        raise AssertionError("Expected one customer_history trace.")

    serialized = json.dumps(
        traces[0].outcome.model_dump(mode="json"),
        ensure_ascii=False,
    )
    for secret in (
        "PRIVATE_NAME",
        "01099999999",
        "female",
        "1990-01-01",
        '"money"',
        "net_paid",
        "payment_status",
        "payment_method",
        "price_minor",
        "net_paid_minor",
    ):
        if secret in serialized:
            raise AssertionError(
                f"History sensitive/financial field leaked into outcome: {secret}"
            )


def _run(case: dict[str, Any]) -> dict[str, object]:
    result = run_v2_fixture_turn(
        history=case["history"],
        local_now=NOW,
        env=case["env"],
        simulate_writes=False,
    )
    expected_details = case.get("expected_details")
    if isinstance(expected_details, set):
        _assert_profile_scope(result, expected_details)
    if case.get("history_privacy"):
        _assert_history_privacy(result)

    for value in case["expected_reply"]:
        if value not in result.reply:
            raise AssertionError(
                f"{case['name']}: missing expected reply text {value!r}: {result.reply}"
            )

    for value in case["forbidden_reply"]:
        if value in result.reply:
            raise AssertionError(
                f"{case['name']}: forbidden reply text {value!r}: {result.reply}"
            )

    if case["pure"]:
        if result.responder_model != "deterministic:patient-contract":
            raise AssertionError(
                f"{case['name']}: pure profile used {result.responder_model}"
            )
    elif case.get("mixed"):
        if result.responder_model == "deterministic:patient-contract":
            raise AssertionError("Mixed response unexpectedly used pure patient path.")

    return {
        "name": case["name"],
        "reply": result.reply,
        "responder_model": result.responder_model,
        "operations": [
            {
                "type": operation.type,
                "requested_patient_details": operation.requested_patient_details,
            }
            for operation in result.understanding.operations
        ],
        "db_effect": "none (deterministic fixture runtime)",
    }


def main() -> int:
    results: list[dict[str, object]] = []
    for index, case in enumerate(CASES, start=1):
        print(f"[{index:02d}/{len(CASES):02d}] {case['name']}", flush=True)
        row = _run(case)
        results.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)

    payload = {
        "scenario_count": len(results),
        "pure_deterministic_profile_scenarios": sum(
            bool(case["pure"]) for case in CASES
        ),
        "additional_llm_calls": 0,
        "database_reads": 0,
        "database_writes": 0,
        "patient_mutation_writes": 0,
        "financial_writes": 0,
        "results": results,
    }
    report = Path("artifacts/patient-crm-contract-live-review.json")

    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"Report: {report}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
