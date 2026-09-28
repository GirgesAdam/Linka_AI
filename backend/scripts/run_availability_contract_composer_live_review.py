"""Six-call live review for the Phase 3A availability contract composer."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage

from app.agents.v2 import availability_composer
from app.agents.v2.availability_composer import (
    AvailabilityComposerDraft,
    AvailabilityComposerValidationError,
    compose_availability_contract_reply,
    deterministic_availability_fallback,
    resolve_availability_composer_draft,
)
from app.services.agent_v2.outcome import TurnOutcome
from app.services.agent_v2.response_contract import build_customer_response_contract


def _window(
    date: str,
    start: str,
    end: str | None = None,
    *,
    doctor: str = "د. مريم",
    device: str | None = None,
) -> dict[str, object]:
    end_value = end or start
    row: dict[str, object] = {
        "doctor_name": doctor,
        "start_local": f"{date}T{start}:00+03:00",
        "end_local": f"{date}T{end_value}:00+03:00",
        "start_time_24h": start,
        "end_time_24h": end_value,
    }
    if device:
        row["laser_device_name"] = device
    return row


def _present(windows: list[dict[str, object]], dates: list[str]) -> TurnOutcome:
    return TurnOutcome(
        status="answered",
        response_goal="present_availability",
        facts={
            "availability": {
                "service_name": "Hydrafacial",
                "checked_dates": dates,
                "availability_windows": windows,
                "available_option_count": len(windows),
                "search_truncated": False,
            }
        },
    )


def _miss(time: str = "19:00") -> TurnOutcome:
    return TurnOutcome(
        status="blocked",
        response_goal="requested_time_unavailable",
        facts={
            "time": {"mode": "exact", "start_time": time},
            "availability": {
                "service_name": "Hydrafacial",
                "checked_dates": ["2026-10-04"],
                "availability_windows": [],
                "available_option_count": 0,
                "search_truncated": False,
            },
        },
    )


def _zero() -> TurnOutcome:
    return TurnOutcome(
        status="blocked",
        response_goal="no_availability",
        facts={
            "availability": {
                "service_name": "Hydrafacial",
                "checked_dates": [
                    f"2026-10-{day:02d}"
                    for day in range(4, 18)
                ],
                "availability_windows": [],
                "available_option_count": 0,
                "search_truncated": True,
            }
        },
    )


CASES: list[tuple[str, str, list[TurnOutcome]]] = [
    (
        "one_available_slot",
        "إيه المتاح يوم 1 أكتوبر؟",
        [_present([_window("2026-10-01", "17:00")], ["2026-10-01"])],
    ),
    (
        "several_discrete_slots",
        "إيه المواعيد المتاحة؟",
        [
            _present(
                [
                    _window("2026-10-02", "17:00"),
                    _window("2026-10-02", "19:00"),
                ],
                ["2026-10-02"],
            )
        ],
    ),
    (
        "doctor_device_grouped_availability",
        "وريني المتاح على الأجهزة المختلفة",
        [
            _present(
                [
                    _window(
                        "2026-10-03",
                        "17:00",
                        "18:00",
                        doctor="د. مريم",
                        device="Prime Lase",
                    ),
                    _window(
                        "2026-10-03",
                        "19:00",
                        doctor="د. سارة",
                        device="Candela Gentle",
                    ),
                ],
                ["2026-10-03"],
            )
        ],
    ),
    (
        "requested_time_unavailable",
        "الساعة 7 متاحة يوم 4 أكتوبر؟",
        [_miss("19:00")],
    ),
    (
        "requested_time_miss_plus_separate_alternatives",
        "لو 7 مش متاحة وريني بدائل",
        [
            _miss("19:00"),
            _present(
                [
                    _window("2026-10-04", "18:00"),
                    _window("2026-10-04", "20:00"),
                ],
                ["2026-10-04"],
            ),
        ],
    ),
    (
        "bounded_no_availability",
        "شوفلي أول ميعاد متاح",
        [_zero()],
    ),
]


def _run(name: str, message: str, outcomes: list[TurnOutcome]) -> dict[str, Any]:
    contract = build_customer_response_contract(outcomes)
    captured: dict[str, object] = {}
    original = availability_composer.invoke_with_model_chain

    def capture(**kwargs):
        result = original(**kwargs)
        captured["draft"] = result.value.model_dump(mode="json")
        captured["model_name"] = result.model_name
        captured["used_fallback_model"] = result.used_fallback
        return result

    availability_composer.invoke_with_model_chain = capture
    try:
        final_text, source = compose_availability_contract_reply(
            history=[HumanMessage(content=message)],
            contract=contract,
        )
    finally:
        availability_composer.invoke_with_model_chain = original

    raw_draft = captured.get("draft")
    if not isinstance(raw_draft, dict):
        raise AssertionError(f"{name}: missing structured composer draft")
    draft = AvailabilityComposerDraft.model_validate(raw_draft)
    validation_error: str | None = None
    try:
        resolved = resolve_availability_composer_draft(
            contract,
            draft,
            arabic=True,
        )
    except AvailabilityComposerValidationError as exc:
        validation_error = str(exc)
        resolved = deterministic_availability_fallback(
            contract,
            arabic=True,
        )

    if final_text != resolved:
        raise AssertionError(f"{name}: final text differs from backend resolution")
    if validation_error is None:
        if not source.startswith("availability-contract:"):
            raise AssertionError(f"{name}: unexpected source {source}")
    elif source != "deterministic:availability-contract-fallback":
        raise AssertionError(
            f"{name}: invalid draft did not use deterministic contract fallback"
        )

    draft_payload = json.dumps(raw_draft, ensure_ascii=False)
    forbidden_values = [
        "17:00",
        "18:00",
        "19:00",
        "20:00",
        "2026-10-01",
        "2026-10-02",
        "2026-10-03",
        "2026-10-04",
        "د. مريم",
        "د. سارة",
        "Prime Lase",
        "Candela Gentle",
        "Hydrafacial",
    ]
    leaked = [value for value in forbidden_values if value in draft_payload]
    if leaked:
        raise AssertionError(f"{name}: exact values leaked into draft: {leaked}")

    expected_window_count = sum(
        len(
            next(
                (
                    fact.value
                    for fact in unit.facts
                    if fact.key == "availability_windows"
                    and isinstance(fact.value, list)
                ),
                [],
            )
        )
        for unit in contract.units
    )
    draft_window_count = sum(len(unit.window_refs) for unit in draft.units)
    if validation_error is None and draft_window_count != expected_window_count:
        raise AssertionError(
            f"{name}: omitted availability refs "
            f"{draft_window_count}!={expected_window_count}"
        )

    return {
        "name": name,
        "turn_outcome": [outcome.model_dump(mode="json") for outcome in outcomes],
        "customer_response_contract": contract.model_dump(mode="json"),
        "structured_availability_draft": raw_draft,
        "final_resolved_text": final_text,
        "response_source": source,
        "structured_validation_error": validation_error,
        "fallback_model_used": captured.get("used_fallback_model"),
        "db_effect": "none (composition-only synthetic validation)",
        "input_tokens": "not exposed by invoke_with_model_chain",
        "output_tokens": "not exposed by invoke_with_model_chain",
        "naturalness": "manual_review_pending",
    }


def main() -> int:
    rows = []
    for index, (name, message, outcomes) in enumerate(CASES, start=1):
        print(f"[{index:02d}/{len(CASES):02d}] {name}", flush=True)
        row = _run(name, message, outcomes)
        rows.append(row)
        print(
            json.dumps(
                {
                    "name": name,
                    "reply": row["final_resolved_text"],
                    "source": row["response_source"],
                },
                ensure_ascii=False,
            ),
            flush=True,
        )

    payload = {
        "scenario_count": len(rows),
        "llm_calls": len(rows),
        "additional_verifier_calls": 0,
        "database_writes_persisted": False,
        "results": rows,
    }
    report = Path("artifacts/availability-contract-composer-live-review.json")
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"Report: {report}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
