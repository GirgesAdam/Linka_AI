from __future__ import annotations

import argparse
import json
from copy import deepcopy
from datetime import datetime
from zoneinfo import ZoneInfo

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from app.services.agent_v2.test_harness import (
    DEFAULT_CATALOG,
    V2FixtureEnvironment,
    run_v2_fixture_turn,
)


MATRIX_CASES = (
    "ليزر الإبط بكام؟",
    "سعر ليزر الإبط وإيه المتاح بكرة بعد 6؟",
    "إيه المتاح بكرة لليزر إبط مع د مريم؟",
    "احجزلي ليزر إبط بكرة الساعة 7 مع د مريم",
    "احجزلي ليزر إبط بكرة الساعة 7",
    "ألغي ميعاد ليزر الإبط بكرة",
    "غيرلي ميعاد ليزر الإبط بكرة للساعة 8 مع د مريم",
    "فاضلي كام جلسة في باكيدج الإبط؟",
    "عايزة باكيدج 6 جلسات إبط كانديلا",
    "لو لغيت باكيدج الإبط هاخد كام؟",
    "أنا دفعت كام آخر مرة؟",
    "أنا دفعت 1000 وأنتم مسجلين 500",
    "ينفع أعمل ليزر وأنا حامل؟",
    "عملت بوتوكس ووشي ورم ومش قادرة أتنفس",
    "متبعتوليش عروض تاني",
    "عايزة أكلم حد",
    "سعر الإبط وإيه المتاح بكرة واحجزلي لو فيه 8 مع د مريم",
    "شكرا",
)


def _test_environment() -> V2FixtureEnvironment:
    """Mirror authoritative branch hours used by the production clinic catalog."""
    catalog = deepcopy(DEFAULT_CATALOG)
    catalog["branches"] = [
        {
            "id": "single-location",
            "name": "Linka Test Clinic",
            "working_hours": [
                {"weekday": weekday, "start": "10:00", "end": "22:00"}
                for weekday in range(7)
            ],
        }
    ]
    return V2FixtureEnvironment(catalog=catalog)


def _json(value: object) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def _print_result(customer_text: str, result) -> None:
    print("=" * 100)
    print(f"CUSTOMER: {customer_text}")
    print("\nUNDERSTANDING")
    print(_json(result.understanding))
    print("\nPLAN")
    print(_json(result.plan))
    print("\nTRACE")
    for trace in result.traces:
        print(
            _json(
                {
                    "operation_index": trace.operation_index,
                    "operation_type": trace.operation_type,
                    "disposition_before": trace.disposition_before,
                    "disposition_after": trace.disposition_after,
                    "read_kinds": list(trace.read_kinds),
                    "simulated_write": trace.simulated_write,
                    "outcome": trace.outcome.model_dump(mode="json"),
                }
            )
        )
    if any(trace.simulated_write for trace in result.traces):
        print("\n[TEST NOTE] WRITE WAS SIMULATED. No database mutation occurred.")
    print(f"\nTIA V2 ({result.responder_model}): {result.reply}")


def run_matrix(now: datetime) -> None:
    env = _test_environment()
    for customer_text in MATRIX_CASES:
        result = run_v2_fixture_turn(
            history=[HumanMessage(content=customer_text)],
            local_now=now,
            env=env,
        )
        _print_result(customer_text, result)


def run_interactive(now: datetime) -> None:
    print("Linka V2-only test chat")
    print("Fixtures only. V1 is not called. Writes are simulated and never persisted.")
    print("Type /reset to clear dialogue history, /quit to exit.\n")
    history: list[BaseMessage] = []
    env = _test_environment()
    while True:
        customer_text = input("You: ").strip()
        if not customer_text:
            continue
        if customer_text == "/quit":
            return
        if customer_text == "/reset":
            history.clear()
            print("Conversation history cleared.\n")
            continue

        turn_history = [*history, HumanMessage(content=customer_text)]
        result = run_v2_fixture_turn(history=turn_history, local_now=now, env=env)
        print(f"Linka V2: {result.reply}")
        if any(trace.simulated_write for trace in result.traces):
            print("[SIMULATED WRITE — no database mutation]")
        print()
        history.extend(
            [
                HumanMessage(content=customer_text),
                AIMessage(content=result.reply),
            ]
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Agent Core V2 only against test fixtures.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--matrix", action="store_true", help="Run the built-in clinic scenario matrix.")
    mode.add_argument("--interactive", action="store_true", help="Chat manually with V2 using test fixtures.")
    parser.add_argument(
        "--live-clock",
        action="store_true",
        help="Use the current Africa/Cairo clock instead of the deterministic test clock.",
    )
    args = parser.parse_args()

    timezone = ZoneInfo("Africa/Cairo")
    now = (
        datetime.now(timezone)
        if args.live_clock
        else datetime(2026, 9, 11, 16, 0, tzinfo=timezone)
    )
    if args.interactive:
        run_interactive(now)
    else:
        run_matrix(now)


if __name__ == "__main__":
    main()
