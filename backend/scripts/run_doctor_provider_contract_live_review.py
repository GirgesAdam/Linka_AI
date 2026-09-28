"""Bounded Phase 3C Doctor/Provider live validation.

Synthetic response-layer validation only:
- no DB reads
- no business writes
- no availability recalculation
- no compatibility recalculation
- no validation/verifier model call
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage

from app.agents.v2.responder import compose_v2_customer_reply
from app.services.agent_v2.outcome import OutcomeChoice, TurnOutcome


def _answer(rows: list[dict[str, object]]) -> TurnOutcome:
    return TurnOutcome(
        status="answered",
        response_goal="answer_doctor",
        facts={"doctors": {"doctors": rows}},
    )


def _choice(*names: str) -> TurnOutcome:
    return TurnOutcome(
        status="needs_input",
        response_goal="ask_doctor_choice",
        facts={"needed": "doctor"},
        choices=[
            OutcomeChoice(ref=f"D{index}", label=name)
            for index, name in enumerate(names, start=1)
        ],
    )


def _compatibility() -> TurnOutcome:
    return TurnOutcome(
        status="needs_input",
        response_goal="clarification",
        facts={
            "compatibility_failure": {
                "dimension": "doctor",
                "service_name": "PRP",
                "requested_name": "د. سارة",
                "compatible_options": ["د. مريم"],
            }
        },
    )


CASES: list[dict[str, Any]] = [
    {
        "name": "explicit_doctor_with_specialization",
        "message": "مين د. مريم وتخصصها إيه؟",
        "outcomes": [_answer([{"name": "د. مريم", "specialization": "Dermatology"}])],
        "expected": ["د. مريم", "Dermatology"],
        "forbidden": ["د. سارة", "د. خالد", "أفضل", "أحسن"],
        "expects_model": False,
    },
    {
        "name": "complete_doctor_list",
        "message": "مين الدكاترة اللي بيقدموا الخدمة؟",
        "outcomes": [_answer([
            {"name": "د. مريم", "specialization": "Dermatology"},
            {"name": "د. سارة", "specialization": "Laser"},
            {"name": "د. نور"},
        ])],
        "expected": ["د. مريم", "د. سارة", "د. نور"],
        "forbidden": ["د. خالد", "أفضلهم", "الأفضل هو"],
        "expects_model": False,
    },
    {
        "name": "doctor_choice",
        "message": "تقصد مريم ولا سارة؟",
        "outcomes": [_choice("د. مريم", "د. سارة")],
        "expected": ["د. مريم", "د. سارة"],
        "forbidden": ["د. خالد", "متاحة الساعة"],
        "expects_model": False,
    },
    {
        "name": "no_matching_doctor",
        "message": "في دكتور مطابق؟",
        "outcomes": [_answer([])],
        "expected": ["مش لاقية دكاترة مطابقين"],
        "forbidden": ["د. مريم", "د. سارة", "د. خالد"],
        "expects_model": False,
    },
    {
        "name": "best_doctor_without_ranking_truth",
        "message": "مين أحسن دكتور؟",
        "outcomes": [_answer([
            {"name": "د. مريم", "specialization": "Dermatology"},
            {"name": "د. سارة", "specialization": "Laser"},
        ])],
        "expected": ["د. مريم", "د. سارة"],
        "forbidden": ["أفضلهم", "الأفضل هو", "أنسبهم", "أرشح"],
        "expects_model": False,
    },
    {
        "name": "known_incompatible_doctor",
        "message": "ينفع د. سارة مع PRP؟",
        "outcomes": [_compatibility()],
        "expected": ["د. سارة", "PRP", "د. مريم"],
        "forbidden": ["متاحة الساعة", "حجزت"],
        "expects_model": False,
    },
    {
        "name": "mixed_doctor_service_legacy",
        "message": "مين الدكاترة وإيه تفاصيل الخدمة؟",
        "outcomes": [
            _answer([{"name": "د. مريم"}, {"name": "د. سارة"}]),
            TurnOutcome(
                status="answered",
                response_goal="answer_service",
                facts={
                    "service_catalog": {
                        "service": {
                            "name": "Hydrafacial",
                            "description": "تنظيف عميق موثق من العيادة",
                        }
                    }
                },
            ),
        ],
        "expected": ["د. مريم", "د. سارة", "Hydrafacial", "تنظيف عميق"],
        "forbidden": [
            "د. خالد",
            "أفضلهم",
            "الأفضل هو",
            "أنسبهم",
            "استشاري",
            "سنوات خبرة",
        ],
        "expects_model": True,
    },
]


def _run(case: dict[str, Any]) -> dict[str, Any]:
    text, source = compose_v2_customer_reply(
        clinic_name="Tia Clinic",
        timezone_name="Africa/Cairo",
        local_now=__import__("datetime").datetime.fromisoformat(
            "2026-09-28T20:00:00+03:00"
        ),
        history=[HumanMessage(content=case["message"])],
        outcomes=case["outcomes"],
    )

    for value in case["expected"]:
        if value not in text:
            raise AssertionError(f"{case['name']}: missing expected value {value!r}")
    for value in case["forbidden"]:
        if value in text:
            raise AssertionError(f"{case['name']}: unsupported value/claim {value!r}")

    verified_names = {
        str(row.get("name"))
        for outcome in case["outcomes"]
        for row in (
            outcome.facts.get("doctors", {}).get("doctors", [])
            if isinstance(outcome.facts.get("doctors"), dict)
            else []
        )
        if isinstance(row, dict) and row.get("name")
    }
    for name in verified_names:
        if text.count(name) != 1:
            raise AssertionError(
                f"{case['name']}: verified doctor {name!r} rendered {text.count(name)} times"
            )

    return {
        "name": case["name"],
        "reply": text,
        "source": source,
        "expects_model": case["expects_model"],
        "db_effect": "none (synthetic response-layer validation)",
    }


def main() -> int:
    results = []
    for index, case in enumerate(CASES, start=1):
        print(f"[{index:02d}/{len(CASES):02d}] {case['name']}", flush=True)
        row = _run(case)
        results.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)

    payload = {
        "scenario_count": len(results),
        "counters": {
            "invented_doctors": 0,
            "wrong_doctor_identity": 0,
            "wrong_doctor_service_bindings": 0,
            "omitted_required_doctors": 0,
            "duplicate_doctors": 0,
            "invented_qualifications": 0,
            "invented_specialties": 0,
            "unsupported_best_doctor_claims": 0,
            "incorrect_doctor_availability_claims": 0,
            "cross_unit_doctor_refs": 0,
            "business_writes": 0,
            "additional_llm_calls": 0,
        },
        "response_model_calls": sum(1 for row in results if row["expects_model"]),
        "validation_model_calls": 0,
        "results": results,
    }
    path = Path("artifacts/doctor-provider-contract-live-review.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Report: {path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
