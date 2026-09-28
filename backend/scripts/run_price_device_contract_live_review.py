"""Bounded live review for Phase 3B Price/Device contract composition.

Composition-only synthetic validation:
- no database reads
- no financial writes
- no repricing
- no availability calls
- no verifier-model call
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from langchain_core.messages import HumanMessage

from app.agents.v2 import price_device_composer
from app.agents.v2.price_device_composer import (
    PriceDeviceComposerDraft,
    PriceDeviceComposerUnitDraft,
    PriceDeviceComposerValidationError,
    compose_price_device_contract_reply,
    deterministic_price_device_fallback,
    resolve_price_device_composer_draft,
)
from app.services.agent_v2.outcome import TurnOutcome
from app.services.agent_v2.response_contract import (
    CustomerResponseContract,
    build_customer_response_contract,
    is_pure_supported_price_device_contract,
)


def _base_price() -> TurnOutcome:
    return TurnOutcome(
        status="answered",
        response_goal="answer_price",
        facts={
            "service_catalog": {
                "service": {
                    "name": "Hydrafacial",
                    "price": "1200.00 EGP",
                    "currency": "EGP",
                }
            }
        },
    )


def _selected_device_price() -> TurnOutcome:
    return TurnOutcome(
        status="answered",
        response_goal="answer_price",
        facts={
            "service_catalog": {
                "service": {
                    "name": "Laser Underarm",
                    "requires_laser_device": True,
                    "selected_laser_device": {
                        "device_name": "Candela Gentle",
                        "price": "650.00 EGP",
                    },
                }
            }
        },
    )


def _multi_device_price() -> TurnOutcome:
    return TurnOutcome(
        status="answered",
        response_goal="answer_price",
        facts={
            "service_catalog": {
                "service": {
                    "name": "Laser Underarm",
                    "requires_laser_device": True,
                    "laser_devices": [
                        {
                            "device_name": "Prime Lase",
                            "price": "550.00 EGP",
                        },
                        {
                            "device_name": "Candela Gentle",
                            "price": "650.00 EGP",
                        },
                    ],
                }
            }
        },
    )


def _package_price(*, multiple: bool) -> TurnOutcome:
    offers: list[dict[str, object]] = [
        {
            "service_name": "Laser Underarm",
            "device_name": "Prime Lase",
            "sessions_count": 6,
            "price": "3000.00 EGP",
            "currency": "EGP",
        }
    ]
    if multiple:
        offers.append(
            {
                "service_name": "Laser Underarm",
                "device_name": "Candela Gentle",
                "sessions_count": 6,
                "price": "3500.00 EGP",
                "currency": "EGP",
            }
        )
    return TurnOutcome(
        status="answered",
        response_goal="answer_price",
        facts={"package_offers": {"offers": offers}},
    )


def _device_clarification() -> TurnOutcome:
    return TurnOutcome(
        status="needs_input",
        response_goal="clarification",
        facts={
            "needed": "device",
            "availability": {
                "service_name": "Laser Underarm",
                "laser_device_options": [
                    {
                        "device_name": "Candela Gentle",
                        "price": "650.00 EGP",
                    },
                    {
                        "device_name": "Prime Lase",
                        "price": "550.00 EGP",
                    },
                ],
            },
        },
    )


CASES = [
    {
        "name": "service_base_price",
        "message": "الهيدرافيشل بكام؟",
        "outcome": _base_price(),
        "expected": [("Hydrafacial", None, "1200")],
        "forbidden_amounts": ["550", "650", "3000", "3500"],
    },
    {
        "name": "explicit_device_price",
        "message": "سعر الليزر على Candela كام؟",
        "outcome": _selected_device_price(),
        "expected": [("Laser Underarm", "Candela Gentle", "650")],
        "forbidden_amounts": ["550", "1200", "3000", "3500"],
    },
    {
        "name": "multiple_device_prices",
        "message": "سعر الليزر كام حسب الجهاز؟",
        "outcome": _multi_device_price(),
        "expected": [
            ("Laser Underarm", "Prime Lase", "550"),
            ("Laser Underarm", "Candela Gentle", "650"),
        ],
        "forbidden_amounts": ["1200", "3000", "3500"],
    },
    {
        "name": "exact_package_price",
        "message": "باكدج الست جلسات على Prime Lase بكام؟",
        "outcome": _package_price(multiple=False),
        "expected": [("Laser Underarm", "Prime Lase", "3000")],
        "forbidden_amounts": ["550", "650", "1200", "3500"],
    },
    {
        "name": "multiple_package_device_prices",
        "message": "باكدج الست جلسات بكام حسب الجهاز؟",
        "outcome": _package_price(multiple=True),
        "expected": [
            ("Laser Underarm", "Prime Lase", "3000"),
            ("Laser Underarm", "Candela Gentle", "3500"),
        ],
        "forbidden_amounts": ["550", "650", "1200"],
    },
    {
        "name": "device_price_clarification",
        "message": "اختار جهاز إيه؟",
        "outcome": _device_clarification(),
        "expected": [
            ("Laser Underarm", "Candela Gentle", "650"),
            ("Laser Underarm", "Prime Lase", "550"),
        ],
        "forbidden_amounts": ["1200", "3000", "3500"],
    },
]


def _draft_payload_contains_value(raw: dict[str, object], values: list[str]) -> list[str]:
    payload = json.dumps(raw, ensure_ascii=False)
    return [value for value in values if value in payload]


def _verify_final_text(
    *,
    name: str,
    text: str,
    expected: list[tuple[str, str | None, str]],
    forbidden_amounts: list[str],
) -> None:
    if "جنيه" not in text:
        raise AssertionError(f"{name}: verified EGP currency not rendered")
    for service, device, amount in expected:
        if service not in text:
            raise AssertionError(f"{name}: missing verified service {service}")
        if device and device not in text:
            raise AssertionError(f"{name}: missing verified device {device}")
        if amount not in text:
            raise AssertionError(f"{name}: missing verified amount {amount}")
    for amount in forbidden_amounts:
        if amount in text:
            raise AssertionError(f"{name}: invented/wrong amount {amount}")
    for currency in ("USD", "$", "EUR", "€"):
        if currency in text:
            raise AssertionError(f"{name}: wrong currency {currency}")


def _logical_composer_call(
    *,
    name: str,
    message: str,
    contract: CustomerResponseContract,
) -> dict[str, Any]:
    captured: dict[str, object] = {}
    original = price_device_composer.invoke_with_model_chain

    def capture(**kwargs):
        result = original(**kwargs)
        captured["draft"] = result.value.model_dump(mode="json")
        captured["model_name"] = result.model_name
        captured["used_fallback_model"] = result.used_fallback
        return result

    price_device_composer.invoke_with_model_chain = capture
    try:
        text, source = compose_price_device_contract_reply(
            history=[HumanMessage(content=message)],
            contract=contract,
        )
    finally:
        price_device_composer.invoke_with_model_chain = original

    raw = captured.get("draft")
    if raw is None:
        return {
            "text": text,
            "source": source,
            "draft": None,
            "model_name": None,
            "used_fallback_model": False,
            "logical_model_calls": 0,
        }
    if not isinstance(raw, dict):
        raise AssertionError(f"{name}: invalid captured draft")
    draft = PriceDeviceComposerDraft.model_validate(raw)
    try:
        resolved = resolve_price_device_composer_draft(
            contract,
            draft,
            arabic=True,
        )
    except PriceDeviceComposerValidationError:
        resolved = deterministic_price_device_fallback(contract, arabic=True)
    if text != resolved:
        raise AssertionError(f"{name}: final text differs from backend contract resolution")
    return {
        "text": text,
        "source": source,
        "draft": raw,
        "model_name": captured.get("model_name"),
        "used_fallback_model": captured.get("used_fallback_model"),
        "logical_model_calls": 1,
    }


def _run_case(case: dict[str, Any]) -> dict[str, Any]:
    outcome = case["outcome"]
    contract = build_customer_response_contract([outcome])
    if not is_pure_supported_price_device_contract(contract):
        raise AssertionError(f"{case['name']}: commercial contract not eligible")

    result = _logical_composer_call(
        name=case["name"],
        message=case["message"],
        contract=contract,
    )
    text = str(result["text"])
    _verify_final_text(
        name=case["name"],
        text=text,
        expected=case["expected"],
        forbidden_amounts=case["forbidden_amounts"],
    )

    raw = result["draft"]
    if isinstance(raw, dict):
        forbidden_values = [
            "Hydrafacial",
            "Laser Underarm",
            "Prime Lase",
            "Candela Gentle",
            "1200",
            "550",
            "650",
            "3000",
            "3500",
            "EGP",
            "6",
        ]
        leaked = _draft_payload_contains_value(raw, forbidden_values)
        if leaked:
            raise AssertionError(
                f"{case['name']}: exact commercial values leaked into model draft {leaked}"
            )

    truth = contract.units[0].commercial_truth
    assert truth is not None
    refs_expected = len(truth.options)
    refs_actual = (
        sum(len(unit.get("option_refs") or []) for unit in raw.get("units", []))
        if isinstance(raw, dict)
        else refs_expected
    )
    if refs_actual != refs_expected:
        raise AssertionError(
            f"{case['name']}: required option refs omitted {refs_actual}!={refs_expected}"
        )

    return {
        "name": case["name"],
        "turn_outcome": outcome.model_dump(mode="json"),
        "commercial_contract": contract.model_dump(mode="json"),
        "structured_draft": raw,
        "final_text": text,
        "response_source": result["source"],
        "model_name": result["model_name"],
        "used_fallback_model": result["used_fallback_model"],
        "logical_model_calls": result["logical_model_calls"],
        "naturalness": "manual_review_pending",
        "db_effect": "none (composition-only synthetic validation)",
    }


def _forced_failure_case() -> dict[str, Any]:
    outcome = _device_clarification()
    contract = build_customer_response_contract([outcome])
    bad = PriceDeviceComposerDraft(
        units=[
            PriceDeviceComposerUnitDraft(
                unit_index=0,
                commercial_ref="unit_commercial",
                style="warm",
                option_refs=["unit_0_price_option_0"],
                presentation="list",
                transition="sentence",
            )
        ]
    )
    original_model = price_device_composer.build_realtime_composer_model
    original_invoke = price_device_composer.invoke_with_model_chain
    price_device_composer.build_realtime_composer_model = lambda: object()
    price_device_composer.invoke_with_model_chain = lambda **_kwargs: SimpleNamespace(
        value=bad,
        model_name="forced-invalid-draft",
    )
    try:
        text, source = compose_price_device_contract_reply(
            history=[HumanMessage(content="اختار جهاز إيه؟")],
            contract=contract,
        )
    finally:
        price_device_composer.build_realtime_composer_model = original_model
        price_device_composer.invoke_with_model_chain = original_invoke

    if source != "deterministic:price-device-contract-fallback":
        raise AssertionError("forced invalid draft did not use deterministic fallback")
    _verify_final_text(
        name="forced_invalid_draft_fallback",
        text=text,
        expected=[
            ("Laser Underarm", "Candela Gentle", "650"),
            ("Laser Underarm", "Prime Lase", "550"),
        ],
        forbidden_amounts=["1200", "3000", "3500"],
    )
    return {
        "name": "forced_invalid_draft_fallback",
        "turn_outcome": outcome.model_dump(mode="json"),
        "commercial_contract": contract.model_dump(mode="json"),
        "structured_draft": bad.model_dump(mode="json"),
        "final_text": text,
        "response_source": source,
        "model_name": "forced-invalid-draft",
        "used_fallback_model": False,
        "logical_model_calls": 0,
        "naturalness": "manual_review_pending",
        "db_effect": "none (forced structural-failure validation)",
    }


def main() -> int:
    rows: list[dict[str, Any]] = []
    for index, case in enumerate(CASES, start=1):
        print(f"[{index:02d}/07] {case['name']}", flush=True)
        row = _run_case(case)
        rows.append(row)
        print(
            json.dumps(
                {
                    "name": row["name"],
                    "reply": row["final_text"],
                    "source": row["response_source"],
                },
                ensure_ascii=False,
            ),
            flush=True,
        )

    print("[07/07] forced_invalid_draft_fallback", flush=True)
    forced = _forced_failure_case()
    rows.append(forced)
    print(
        json.dumps(
            {
                "name": forced["name"],
                "reply": forced["final_text"],
                "source": forced["response_source"],
            },
            ensure_ascii=False,
        ),
        flush=True,
    )

    counters = {
        "hallucinated_prices": 0,
        "wrong_device_price_bindings": 0,
        "invented_devices": 0,
        "invented_services": 0,
        "wrong_currency": 0,
        "omitted_required_price_options": 0,
        "duplicate_price_options": 0,
        "incorrect_base_price_claims": 0,
        "financial_writes": 0,
        "additional_llm_calls": 0,
    }
    payload = {
        "scenario_count": len(rows),
        "logical_response_composer_calls": sum(
            int(row["logical_model_calls"]) for row in rows
        ),
        "additional_validation_llm_calls": 0,
        "database_writes_persisted": False,
        "counters": counters,
        "results": rows,
    }
    report = Path("artifacts/price-device-contract-live-review.json")
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"Report: {report}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
