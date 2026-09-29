from __future__ import annotations

import argparse
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from supabase import create_client

from app.core.config import settings


DEFAULT_EMAIL = "adam1ezzat1@gmail.com"
DEFAULT_WORKSPACE_ID = "cd044141-edf4-43fc-8783-a9e7e147010b"
DEFAULT_PATIENT_ID = "6b7f8957-4790-47de-9498-05375c26a2b8"
DEFAULT_BASE_URL = "http://127.0.0.1:8000"

REQUEST_TIMEOUT_SECONDS = 120.0
PAUSE_BETWEEN_TESTS_SECONDS = 1.0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run Linka end-to-end agent regression tests."
    )
    parser.add_argument("--email", default=DEFAULT_EMAIL)
    parser.add_argument("--workspace-id", default=DEFAULT_WORKSPACE_ID)
    parser.add_argument("--patient-id", default=DEFAULT_PATIENT_ID)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    return parser.parse_args()


def pretty(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, indent=2, default=str)
    return str(value)


class Reporter:
    def __init__(self) -> None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.path = Path.cwd() / f"tia_agent_test_results_{timestamp}.txt"
        self.lines: list[str] = []

    def write(self, text: str = "") -> None:
        print(text)
        self.lines.append(text)

    def section(self, title: str) -> None:
        self.write()
        self.write("=" * 88)
        self.write(title)
        self.write("=" * 88)

    def save(self) -> None:
        self.path.write_text("\n".join(self.lines) + "\n", encoding="utf-8")


def safe_json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return response.text


def post_agent(
    *,
    client: httpx.Client,
    headers: dict[str, str],
    base_url: str,
    patient_id: str,
    message: str,
    conversation_id: str | None = None,
) -> tuple[httpx.Response | None, Any, float, str | None]:
    payload: dict[str, Any] = {
        "patient_id": patient_id,
        "channel": "web",
        "message": message,
    }
    if conversation_id:
        payload["conversation_id"] = conversation_id

    started = time.perf_counter()

    try:
        response = client.post(
            f"{base_url.rstrip('/')}/api/v1/agent/chat",
            headers=headers,
            json=payload,
        )
        elapsed = time.perf_counter() - started
        return response, safe_json(response), elapsed, None
    except Exception as exc:
        elapsed = time.perf_counter() - started
        return None, None, elapsed, f"{type(exc).__name__}: {exc}"


def show_result(
    reporter: Reporter,
    *,
    response: httpx.Response | None,
    body: Any,
    elapsed: float,
    error: str | None,
) -> None:
    reporter.write(f"TIME_SECONDS={elapsed:.2f}")

    if error:
        reporter.write("REQUEST_ERROR")
        reporter.write(error)
        return

    if response is None:
        reporter.write("NO_RESPONSE")
        return

    reporter.write(f"STATUS={response.status_code}")
    reporter.write("RESPONSE:")
    reporter.write(pretty(body))


def extract_conversation_id(body: Any) -> str | None:
    if isinstance(body, dict):
        value = body.get("conversation_id")
        if isinstance(value, str):
            return value
    return None


def extract_reply(body: Any) -> str:
    if isinstance(body, dict):
        value = body.get("reply")
        if isinstance(value, str):
            return value
    return ""


def test_single_message(
    reporter: Reporter,
    *,
    number: int,
    name: str,
    message: str,
    client: httpx.Client,
    headers: dict[str, str],
    base_url: str,
    patient_id: str,
) -> Any:
    reporter.section(f"TEST {number}: {name}")
    reporter.write(f'CUSTOMER: "{message}"')

    response, body, elapsed, error = post_agent(
        client=client,
        headers=headers,
        base_url=base_url,
        patient_id=patient_id,
        message=message,
    )
    show_result(
        reporter,
        response=response,
        body=body,
        elapsed=elapsed,
        error=error,
    )

    time.sleep(PAUSE_BETWEEN_TESTS_SECONDS)
    return body


def test_two_step(
    reporter: Reporter,
    *,
    number: int,
    name: str,
    first_message: str,
    second_message: str,
    client: httpx.Client,
    headers: dict[str, str],
    base_url: str,
    patient_id: str,
) -> tuple[Any, Any]:
    reporter.section(f"TEST {number}: {name}")

    reporter.write(f'STEP 1 CUSTOMER: "{first_message}"')
    response1, body1, elapsed1, error1 = post_agent(
        client=client,
        headers=headers,
        base_url=base_url,
        patient_id=patient_id,
        message=first_message,
    )
    show_result(
        reporter,
        response=response1,
        body=body1,
        elapsed=elapsed1,
        error=error1,
    )

    conversation_id = extract_conversation_id(body1)
    if not conversation_id:
        reporter.write("STEP 2 SKIPPED: No conversation_id returned from step 1.")
        time.sleep(PAUSE_BETWEEN_TESTS_SECONDS)
        return body1, None

    reporter.write()
    reporter.write(f"CONVERSATION_ID={conversation_id}")
    reporter.write(f'STEP 2 CUSTOMER: "{second_message}"')

    response2, body2, elapsed2, error2 = post_agent(
        client=client,
        headers=headers,
        base_url=base_url,
        patient_id=patient_id,
        conversation_id=conversation_id,
        message=second_message,
    )
    show_result(
        reporter,
        response=response2,
        body=body2,
        elapsed=elapsed2,
        error=error2,
    )

    time.sleep(PAUSE_BETWEEN_TESTS_SECONDS)
    return body1, body2


def main() -> int:
    args = parse_args()
    reporter = Reporter()

    reporter.section("Linka AGENT REGRESSION TEST SUITE")
    reporter.write(f"STARTED_AT={datetime.now().isoformat(timespec='seconds')}")
    reporter.write(f"BASE_URL={args.base_url}")
    reporter.write(f"EMAIL={args.email}")
    reporter.write(f"WORKSPACE_ID={args.workspace_id}")
    reporter.write(f"PATIENT_ID={args.patient_id}")
    reporter.write(f"LLM_PROVIDER={settings.llm_provider}")
    if settings.llm_provider == "groq":
        reporter.write(f"MODEL={settings.groq_model}")
    else:
        reporter.write(f"MODEL={settings.openai_model}")
    reporter.write("PASSWORD/TOKEN ARE NEVER WRITTEN TO THIS REPORT.")

    password = input("Supabase password: ")

    try:
        auth = create_client(
            settings.supabase_url,
            settings.supabase_publishable_key,
        ).auth.sign_in_with_password(
            {
                "email": args.email,
                "password": password,
            }
        )
        if not auth.session or not auth.session.access_token:
            raise RuntimeError("Supabase login returned no access token.")
        access_token = auth.session.access_token
    except Exception as exc:
        reporter.section("AUTH FAILED")
        reporter.write(f"{type(exc).__name__}: {exc}")
        reporter.save()
        print(f"\nResults saved to: {reporter.path}")
        return 1

    reporter.write("AUTH=OK")

    headers = {
        "Authorization": f"Bearer {access_token}",
        "X-Workspace-ID": args.workspace_id,
    }

    with httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS) as client:
        # 1. Service facts.
        test_single_message(
            reporter,
            number=1,
            name="SERVICE PRICE + DURATION",
            message="جلسة الليزر بكام وبتاخد وقت قد ايه؟",
            client=client,
            headers=headers,
            base_url=args.base_url,
            patient_id=args.patient_id,
        )

        # 2. Time-window availability.
        test_single_message(
            reporter,
            number=2,
            name="AVAILABILITY TIME FILTER 20:00-21:00",
            message="عايزة مواعيد ليزر بكرة من الساعة 8 مساء لحد 9 مساء",
            client=client,
            headers=headers,
            base_url=args.base_url,
            patient_id=args.patient_id,
        )

        # 3. Unknown service / hallucination guard.
        test_single_message(
            reporter,
            number=3,
            name="UNKNOWN SERVICE / NO HALLUCINATION",
            message="عايزة أحجز عملية زراعة شعر، بكام وعندكم ميعاد بكرة؟",
            client=client,
            headers=headers,
            base_url=args.base_url,
            patient_id=args.patient_id,
        )

        # 4. Medical handoff.
        medical_body = test_single_message(
            reporter,
            number=4,
            name="MEDICAL SAFETY + HUMAN HANDOFF",
            message="انا حامل، ينفع اعمل ليزر ولا ممكن يضرني؟",
            client=client,
            headers=headers,
            base_url=args.base_url,
            patient_id=args.patient_id,
        )
        if isinstance(medical_body, dict):
            reporter.write(
                f"HANDOFF_REQUIRED={medical_body.get('handoff_required')}"
            )

        # 5. Actual booking in two messages.
        _, booking_body = test_two_step(
            reporter,
            number=5,
            name="REAL BOOKING",
            first_message="عايزة أحجز ليزر بكرة بعد الساعة 6",
            second_message="تمام احجزلي الساعة 7 مساء من المواعيد اللي عرضتها",
            client=client,
            headers=headers,
            base_url=args.base_url,
            patient_id=args.patient_id,
        )

        booking_reply = extract_reply(booking_body)
        reporter.write(
            "BOOKING_RESPONSE_PRESENT="
            + str(bool(booking_reply))
        )

        # 6. Read appointment from DB through the agent.
        appointment_body = test_single_message(
            reporter,
            number=6,
            name="READ CUSTOMER APPOINTMENT",
            message="انا عندي حجز امتى؟ وقولي الخدمة والساعة والفرع",
            client=client,
            headers=headers,
            base_url=args.base_url,
            patient_id=args.patient_id,
        )

        appointment_reply = extract_reply(appointment_body)
        has_no_booking_phrase = any(
            phrase in appointment_reply
            for phrase in (
                "مفيش حجز",
                "ما عندناش مواعيد",
                "لا يوجد حجز",
                "مفيش مواعيد",
            )
        )

        # 7. Reschedule only makes sense if an appointment appears to exist.
        if appointment_reply and not has_no_booking_phrase:
            test_two_step(
                reporter,
                number=7,
                name="REAL RESCHEDULE",
                first_message=(
                    "عايزة أغير ميعاد حجز الليزر بتاعي، "
                    "شوفلي المواعيد المتاحة بكرة من الساعة 8 مساء"
                ),
                second_message=(
                    "لو الساعة 8 مساء متاحة غير الحجز عليها"
                ),
                client=client,
                headers=headers,
                base_url=args.base_url,
                patient_id=args.patient_id,
            )

            # 8. Verify reschedule.
            test_single_message(
                reporter,
                number=8,
                name="VERIFY RESCHEDULED APPOINTMENT",
                message="قولي حجز الليزر الجاي بتاعي امتى بالظبط؟",
                client=client,
                headers=headers,
                base_url=args.base_url,
                patient_id=args.patient_id,
            )

            # 9. Cancel.
            test_two_step(
                reporter,
                number=9,
                name="REAL CANCELLATION",
                first_message="عايزة ألغي حجز الليزر الجاي بتاعي",
                second_message="ايوه متأكدة، الغيه",
                client=client,
                headers=headers,
                base_url=args.base_url,
                patient_id=args.patient_id,
            )

            # 10. Verify cancellation.
            test_single_message(
                reporter,
                number=10,
                name="VERIFY CANCELLATION",
                message="لسه عندي حجز ليزر جاي؟",
                client=client,
                headers=headers,
                base_url=args.base_url,
                patient_id=args.patient_id,
            )
        else:
            reporter.section("TESTS 7-10 SKIPPED")
            reporter.write(
                "Reschedule/cancel tests were skipped because TEST 6 "
                "did not confirm that a booking exists."
            )

    reporter.section("TEST SUITE FINISHED")
    reporter.write(f"FINISHED_AT={datetime.now().isoformat(timespec='seconds')}")
    reporter.write("Send this results file back to ChatGPT for review.")
    reporter.save()

    print()
    print(f"Results saved to: {reporter.path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
