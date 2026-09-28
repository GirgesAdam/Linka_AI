"""Eight-call live review for the terminal response contract composer.

This is intentionally composition-only: no database reads or writes, no interpreter
call, and no third/verifier model call. It captures the structured draft produced by
the same composer invocation used to render the final customer text.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage

from app.agents.v2 import terminal_composer
from app.agents.v2.terminal_composer import (
    compose_terminal_contract_reply,
    resolve_terminal_composer_draft,
)
from app.services.agent_v2.outcome import TurnOutcome
from app.services.agent_v2.response_contract import (
    build_customer_response_contract,
    is_pure_supported_terminal_contract,
)


@dataclass(frozen=True)
class Case:
    name: str
    message: str
    outcome: TurnOutcome
    forbidden: tuple[str, ...] = ()


def _terminal(
    goal: str,
    *,
    facts: dict[str, object] | None = None,
    action_result: dict[str, object] | None = None,
) -> TurnOutcome:
    return TurnOutcome(
        status="completed",
        response_goal=goal,
        facts=facts or {},
        action_result={"ok": True, **(action_result or {})},
    )


CASES = [
    Case(
        "normal_booking_completion",
        "تمام كده الحجز اتعمل؟",
        _terminal(
            "booking_completed",
            facts={
                "service_name": "Hydrafacial",
                "start_local": "2026-10-04T19:00:00+03:00",
                "doctor_name": "د. مريم",
            },
        ),
    ),
    Case(
        "package_backed_booking_completion",
        "تمام، اتحجزت من الباكدج؟",
        _terminal(
            "booking_completed",
            facts={
                "service_name": "PRP للبشرة",
                "start_local": "2026-10-05T17:30:00+03:00",
                "package_used": True,
                "package_name": "PRP 6 Sessions",
                "availability": {
                    "availability_windows": [
                        {
                            "start_local": "2026-10-05T17:30:00+03:00",
                            "doctor_name": "د. مها",
                        }
                    ],
                    "available_option_count": 1,
                },
            },
        ),
        forbidden=("متاح", "اختار"),
    ),
    Case(
        "laser_device_booking_completion",
        "خلاص ثبت الحجز على الجهاز؟",
        _terminal(
            "booking_completed",
            facts={
                "service_name": "Full Legs Laser",
                "start_local": "2026-10-06T19:00:00+03:00",
                "device_name": "Prime Lase",
                "doctor_name": "د. سارة",
            },
        ),
    ),
    Case(
        "reschedule_completion",
        "يعني الميعاد الجديد اتثبت؟",
        _terminal(
            "reschedule_completed",
            facts={
                "service_name": "Hydrafacial",
                "availability": {
                    "availability_windows": [
                        {
                            "start_local": "2026-10-07T18:30:00+03:00",
                            "doctor_name": "د. مريم",
                        }
                    ],
                    "available_option_count": 1,
                },
            },
        ),
        forbidden=("متاح", "اختار"),
    ),
    Case(
        "cancellation_completion",
        "اتلغى الموعد خلاص؟",
        _terminal(
            "cancellation_completed",
            action_result={"status": "cancelled"},
        ),
        forbidden=("اتأكد", "نحجز"),
    ),
    Case(
        "appointment_confirmation",
        "أكدت الموعد؟",
        _terminal(
            "appointment_confirmed",
            facts={
                "service_name": "PRP للبشرة",
                "start_local": "2026-10-08T16:00:00+03:00",
            },
            action_result={"status": "confirmed"},
        ),
        forbidden=("حجزك اتأكد", "اتلغى"),
    ),
    Case(
        "package_purchase",
        "الباكدج اتضافت؟",
        _terminal(
            "package_purchased",
            facts={"package_name": "Gold 6 Sessions"},
            action_result={
                "status": "active",
                "amount_paid_minor": 0,
            },
        ),
        forbidden=("تم الدفع", "مدفوع", "تسوية", "خصم"),
    ),
    Case(
        "pulse_pack_purchase",
        "باقة البالسز اتضافت؟",
        _terminal(
            "pulse_pack_purchased",
            facts={
                "pulse_count": 2000,
                "device_name": "Candela Gentle",
            },
            action_result={
                "status": "active",
                "amount_paid_minor": 0,
                "sale_price_minor": 250000,
                "currency": "EGP",
            },
        ),
        forbidden=("تم الدفع", "مدفوع", "تسوية", "خصم", "جلسة"),
    ),
]


def _run_case(case: Case) -> dict[str, Any]:
    contract = build_customer_response_contract([case.outcome])
    if not is_pure_supported_terminal_contract(contract):
        raise AssertionError(f"{case.name}: contract not eligible for terminal composer")

    captured: dict[str, object] = {}
    original_invoke = terminal_composer.invoke_with_model_chain

    def capture_invoke(**kwargs):
        result = original_invoke(**kwargs)
        captured["draft"] = result.value.model_dump(mode="json")
        captured["model_name"] = result.model_name
        return result

    terminal_composer.invoke_with_model_chain = capture_invoke
    try:
        final_text, source = compose_terminal_contract_reply(
            history=[HumanMessage(content=case.message)],
            contract=contract,
        )
    finally:
        terminal_composer.invoke_with_model_chain = original_invoke

    draft_raw = captured.get("draft")
    if not isinstance(draft_raw, dict):
        raise AssertionError(f"{case.name}: composer did not produce a captured structured draft")
    draft = terminal_composer.TerminalComposerDraft.model_validate(draft_raw)
    resolved_again = resolve_terminal_composer_draft(
        contract,
        draft,
        arabic=True,
    )
    if final_text != resolved_again:
        raise AssertionError(f"{case.name}: final text is not exact backend resolution of the draft")
    if not source.startswith("contract-composer:"):
        raise AssertionError(f"{case.name}: unexpected response source {source}")

    contract_json = contract.model_dump_json()
    draft_json = json.dumps(draft_raw, ensure_ascii=False, sort_keys=True)
    for secret in (
        "appointment-secret",
        "service-secret",
        "doctor-secret",
        "patient-secret",
        "workspace-secret",
        "transaction-secret",
    ):
        if secret in contract_json or secret in draft_json or secret in final_text:
            raise AssertionError(f"{case.name}: internal identifier leaked")

    for token in case.forbidden:
        if token in final_text:
            raise AssertionError(f"{case.name}: forbidden semantic text present: {token}")

    if "availability_windows" in {fact.key for fact in contract.units[0].facts}:
        raise AssertionError(f"{case.name}: availability presentation leaked into terminal contract")

    return {
        "name": case.name,
        "turn_outcome": case.outcome.model_dump(mode="json"),
        "customer_response_contract": contract.model_dump(mode="json"),
        "composer_structured_output": draft_raw,
        "resolved_final_text": final_text,
        "response_source": source,
        "db_effect": "none (composition-only live validation)",
        "naturalness": "manual_review_pending",
    }


def main() -> int:
    results = []
    for index, case in enumerate(CASES, start=1):
        print(f"[{index:02d}/{len(CASES):02d}] {case.name}", flush=True)
        row = _run_case(case)
        results.append(row)
        print(
            json.dumps(
                {
                    "name": row["name"],
                    "reply": row["resolved_final_text"],
                    "source": row["response_source"],
                },
                ensure_ascii=False,
            ),
            flush=True,
        )

    payload = {
        "scenario_count": len(results),
        "llm_calls": len(results),
        "additional_verifier_calls": 0,
        "database_writes_persisted": False,
        "results": results,
    }
    report = Path("artifacts/terminal-contract-composer-live-review.json")
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Report: {report}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
