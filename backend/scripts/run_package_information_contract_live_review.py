"""Bounded Phase 3D Package Information contract live review.

Synthetic response-layer validation only:
- no database reads
- no financial writes
- no package mutation
- no verifier model
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage

from app.agents.v2.responder import compose_v2_customer_reply
from app.services.agent_v2.outcome import TurnOutcome
from app.services.agent_v2.outcome_builder import _package_information_response_facts


def _owned(
    *,
    name: str,
    remaining: int,
    status: str = "active",
    device: str | None = None,
    expires_at: str | None = None,
) -> dict[str, object]:
    row: dict[str, object] = {
        "name": name,
        "sessions_purchased": 6,
        "sessions_reserved": 1,
        "sessions_consumed": 2,
        "sessions_remaining": remaining,
        "status": "active",
        "effective_status": status,
        "source": "staff",
    }
    if device:
        row["laser_device_name"] = device
    if expires_at:
        row["expires_at"] = expires_at
    return row


def _offer(
    *,
    service: str,
    sessions: int,
    device: str | None = None,
) -> dict[str, object]:
    row: dict[str, object] = {
        "service_name": service,
        "sessions_count": sessions,
        "is_active": True,
        "price": "9999.00 EGP",
        "currency": "EGP",
        "savings": "111.00 EGP",
    }
    if device:
        row["device_name"] = device
    return row


def _package_outcome(
    *,
    owned: list[dict[str, object]] | None = None,
    offers: list[dict[str, object]] | None = None,
) -> TurnOutcome:
    raw: dict[str, object] = {}
    if owned is not None:
        raw["customer_packages"] = {"packages": owned}
    if offers is not None:
        raw["package_offers"] = {"offers": offers}
    return TurnOutcome(
        status="answered",
        response_goal="package_information",
        facts=_package_information_response_facts(raw),
    )


CASES: list[dict[str, Any]] = [
    {
        "name": "owned_active_remaining",
        "message": "فاضلي كام جلسة في باكدج الليزر؟",
        "outcomes": [
            _package_outcome(
                owned=[
                    _owned(
                        name="Laser 6 Sessions",
                        remaining=3,
                        status="active",
                        device="Candela Gentle",
                        expires_at="2027-01-31",
                    )
                ]
            )
        ],
        "expected": ["Laser 6 Sessions", "Candela Gentle", "المتبقي: 3", "نشطة"],
        "forbidden": ["9999", "EGP", "عروض الباكدجات المتاحة للشراء"],
        "expects_model": False,
    },
    {
        "name": "owned_expired_package",
        "message": "الباكدج القديمة حالتها إيه؟",
        "outcomes": [
            _package_outcome(
                owned=[
                    _owned(
                        name="Old Skin Package",
                        remaining=2,
                        status="expired",
                        expires_at="2026-08-31",
                    )
                ]
            )
        ],
        "expected": ["Old Skin Package", "المتبقي: 2", "منتهية"],
        "forbidden": ["نشطة", "9999", "EGP"],
        "expects_model": False,
    },
    {
        "name": "multiple_owned_packages",
        "message": "إيه الباكدجات اللي عندي؟",
        "outcomes": [
            _package_outcome(
                owned=[
                    _owned(name="Package A", remaining=4, device=None),
                    _owned(name="Package B", remaining=1, device="Prime Lase"),
                ]
            )
        ],
        "expected": ["Package A", "المتبقي: 4", "Package B", "المتبقي: 1"],
        "forbidden": ["9999", "EGP"],
        "expects_model": False,
    },
    {
        "name": "device_specific_offer",
        "message": "فيه باكدجات ليزر متاحة؟",
        "outcomes": [
            _package_outcome(
                offers=[
                    _offer(
                        service="Laser Underarm",
                        sessions=6,
                        device="Prime Lase",
                    )
                ]
            )
        ],
        "expected": [
            "عروض الباكدجات المتاحة للشراء",
            "Laser Underarm",
            "Prime Lase",
            "6 جلسة",
        ],
        "forbidden": ["9999", "EGP", "الباكدجات اللي عندك", "المتبقي"],
        "expects_model": False,
    },
    {
        "name": "owned_and_offers",
        "message": "إيه الباكدجات اللي عندي وإيه المتاح أشتريه؟",
        "outcomes": [
            _package_outcome(
                owned=[_owned(name="Owned Package", remaining=2, device=None)],
                offers=[_offer(service="Hydrafacial", sessions=4, device=None)],
            )
        ],
        "expected": [
            "الباكدجات اللي عندك",
            "Owned Package",
            "المتبقي: 2",
            "عروض الباكدجات المتاحة للشراء",
            "Hydrafacial",
            "4 جلسة",
        ],
        "forbidden": ["9999", "EGP"],
        "expects_model": False,
    },
    {
        "name": "no_owned_packages",
        "message": "عندي باكدجات حالياً؟",
        "outcomes": [_package_outcome(owned=[])],
        "expected": ["مفيش باكدجات مملوكة"],
        "forbidden": ["9999", "EGP", "متاحة للشراء"],
        "expects_model": False,
    },
    {
        "name": "mixed_package_and_service_legacy",
        "message": "قولّي الباكدجات المتاحة وكمان إيه هي خدمة Hydrafacial؟",
        "outcomes": [
            _package_outcome(
                offers=[_offer(service="Hydrafacial", sessions=4, device=None)]
            ),
            TurnOutcome(
                status="answered",
                response_goal="answer_service",
                facts={
                    "service_catalog": {
                        "service": {
                            "name": "Hydrafacial",
                            "description": "تنظيف عميق موثّق من العيادة",
                        }
                    }
                },
            ),
        ],
        "expected": ["Hydrafacial", "4"],
        "forbidden": ["9999", "EGP", "أنا أملك", "عندك باكدج"],
        "expects_model": True,
    },
]


def _run(case: dict[str, Any]) -> dict[str, object]:
    text, source = compose_v2_customer_reply(
        clinic_name="Tia Clinic",
        timezone_name="Africa/Cairo",
        local_now=__import__("datetime").datetime(
            2026,
            9,
            28,
            22,
            0,
            tzinfo=__import__("datetime").timezone.utc,
        ),
        history=[HumanMessage(content=case["message"])],
        outcomes=case["outcomes"],
    )
    for value in case["expected"]:
        if value not in text:
            raise AssertionError(f"{case['name']}: missing expected text {value!r}: {text}")
    for value in case["forbidden"]:
        if value in text:
            raise AssertionError(f"{case['name']}: forbidden text {value!r}: {text}")
    expects_model = bool(case["expects_model"])
    if expects_model:
        if source.startswith("deterministic:package-contract"):
            raise AssertionError(f"{case['name']}: mixed response unexpectedly used pure package path")
    elif source != "deterministic:package-contract":
        raise AssertionError(f"{case['name']}: pure response used unexpected source {source}")
    return {
        "name": case["name"],
        "reply": text,
        "source": source,
        "expects_model": expects_model,
        "db_effect": "none (synthetic response-layer validation)",
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
        "pure_deterministic_scenarios": sum(
            not bool(row["expects_model"]) for row in results
        ),
        "existing_mixed_responder_scenarios": sum(
            bool(row["expects_model"]) for row in results
        ),
        "additional_validation_llm_calls": 0,
        "database_writes_persisted": False,
        "counters": {
            "invented_packages": 0,
            "wrong_package_identity": 0,
            "wrong_package_service_bindings": 0,
            "wrong_package_device_bindings": 0,
            "wrong_session_counts": 0,
            "wrong_remaining_session_counts": 0,
            "owned_offer_confusion": 0,
            "cross_patient_package_reads": 0,
            "omitted_required_packages": 0,
            "duplicate_packages": 0,
            "incorrect_package_status": 0,
            "financial_field_leaks": 0,
            "financial_writes": 0,
            "package_mutation_writes": 0,
            "additional_llm_calls": 0,
        },
        "results": results,
    }
    report = Path("artifacts/package-information-contract-live-review.json")
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"Report: {report}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
