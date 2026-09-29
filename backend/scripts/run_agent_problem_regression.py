from __future__ import annotations

"""Focused regression suite for Linka booking/inquiry/time semantic problems.

Unlike the broad matrix, this runner contains only previously problematic
surfaces plus new nearby variants. Each case evaluates the structured turn
decision and, by default, sends the same first turn through the real customer
agent so the JSON report includes the actual reply and reply model.

All database writes are wrapped by an outer transaction and rolled back unless
--keep-data is explicitly supplied. The cases are deliberately first-turn,
non-confirmation scenarios and additionally assert that they did not create a
new appointment.
"""

import argparse
import json
import sys
import traceback
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any, Callable
from uuid import UUID
from zoneinfo import ZoneInfo

from langchain_core.messages import HumanMessage
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.agents.clinic_grounding import build_clinic_catalog
from app.agents.turn_interpreter import interpret_customer_turn
from app.core.config import settings
from app.models.workspace import Workspace
from app.services.agent_chat import run_agent_chat
from run_agent_e2e_matrix import (
    _agent_payload,
    _appointments_for,
    _catalog_row,
    _fixture_patients,
)


Check = Callable[[Any, dict[str, Any]], tuple[bool, str]]


@dataclass(frozen=True)
class FocusedCase:
    name: str
    category: str
    message: str
    check: Check


@dataclass
class Result:
    name: str
    category: str
    status: str
    duration_ms: int
    details: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


def _has(*names: str) -> Check:
    def check(decision: Any, _: dict[str, Any]) -> tuple[bool, str]:
        capabilities = {str(item) for item in (decision.capabilities or [])}
        missing = [name for name in names if name not in capabilities]
        return not missing, f"capabilities={sorted(capabilities)} missing={missing}"

    return check


def _lacks(*names: str) -> Check:
    def check(decision: Any, _: dict[str, Any]) -> tuple[bool, str]:
        capabilities = {str(item) for item in (decision.capabilities or [])}
        unexpected = [name for name in names if name in capabilities]
        return not unexpected, f"capabilities={sorted(capabilities)} unexpected={unexpected}"

    return check


def _flow(expected: str) -> Check:
    def check(decision: Any, _: dict[str, Any]) -> tuple[bool, str]:
        actual = str(decision.flow_signal)
        return actual == expected, f"flow_signal={actual} expected={expected}"

    return check


def _package_intent(expected: str) -> Check:
    def check(decision: Any, _: dict[str, Any]) -> tuple[bool, str]:
        actual = str(decision.package_intent)
        return actual == expected, f"package_intent={actual} expected={expected}"

    return check


def _entity(field: str, expected_key: str) -> Check:
    def check(decision: Any, expected: dict[str, Any]) -> tuple[bool, str]:
        actual = getattr(decision.entity_hints, field)
        wanted = expected[expected_key]
        return str(actual or "") == str(wanted), f"{field}={actual} expected={wanted}"

    return check


def _time_window(*, after: str | None = None, before: str | None = None) -> Check:
    def check(decision: Any, _: dict[str, Any]) -> tuple[bool, str]:
        hints = decision.entity_hints
        ok = (
            hints.requested_start_time is None
            and hints.not_before_time == after
            and hints.not_after_time == before
        )
        return ok, (
            f"start={hints.requested_start_time} "
            f"not_before={hints.not_before_time} not_after={hints.not_after_time}"
        )

    return check


def _date_present(decision: Any, _: dict[str, Any]) -> tuple[bool, str]:
    value = decision.entity_hints.requested_date
    return bool(value), f"requested_date={value}"


def _all(*checks: Check) -> Check:
    def combined(decision: Any, expected: dict[str, Any]) -> tuple[bool, str]:
        messages: list[str] = []
        ok_all = True
        for check in checks:
            ok, message = check(decision, expected)
            messages.append(message)
            ok_all = ok_all and ok
        return ok_all, " | ".join(messages)

    return combined


def _cases() -> list[FocusedCase]:
    # Only the remaining problem surface plus new nearby variants.
    # For read-only availability checks, appointment_creation is the meaningful
    # authorization distinction. The runtime may keep a lightweight flow only to
    # preserve presented options for a possible follow-up booking.
    return [
        FocusedCase(
            "nearest_availability_question",
            "remaining_problem",
            "ممكن أعرف أقرب ميعاد متاح لليزر ابط؟",
            _all(
                _has("availability_discovery"),
                _lacks("appointment_creation"),
                _entity("service_id", "underarm_service_id"),
            ),
        ),
        FocusedCase(
            "price_and_availability_question",
            "remaining_problem",
            "جلسة ليزر ابط بكام وهل في مواعيد فاضية بكرة؟",
            _all(
                _has("pricing", "availability_discovery"),
                _lacks("appointment_creation"),
                _entity("service_id", "underarm_service_id"),
                _date_present,
            ),
        ),
        FocusedCase(
            "package_comparison_reply_quality",
            "remaining_problem",
            "أنا أول مرة وعايز ليزر ابط، أحجز جلسة واحدة ولا أبدأ كورس جلسات كامل؟",
            _all(
                _package_intent("inquire"),
                _has("package_information", "pricing"),
                _lacks("appointment_creation"),
            ),
        ),
        FocusedCase(
            "price_and_nearest_availability",
            "compound_inquiry",
            "جلسة ليزر ابط بكام وأقرب ميعاد متاح إمتى؟",
            _all(
                _has("pricing", "availability_discovery"),
                _lacks("appointment_creation"),
                _entity("service_id", "underarm_service_id"),
            ),
        ),
        FocusedCase(
            "availability_then_decide",
            "booking_vs_inquiry",
            "ممكن أشوف مواعيد ليزر ابط الأول وبعدها أقرر أحجز ولا لأ؟",
            _all(
                _has("availability_discovery"),
                _lacks("appointment_creation"),
                _entity("service_id", "underarm_service_id"),
            ),
        ),
        FocusedCase(
            "doctor_availability_then_decide",
            "booking_vs_inquiry",
            "شوفلي مواعيد د احمد محمود الأسبوع الجاي وأنا أقرر",
            _all(
                _has("availability_discovery"),
                _lacks("appointment_creation"),
                _entity("doctor_id", "ahmed_doctor_id"),
            ),
        ),
        FocusedCase(
            "explicit_conditional_booking",
            "booking_vs_inquiry",
            "لو فيه ميعاد ليزر ابط بكرة بعد الساعة ٧ احجزلي",
            _all(
                _has("appointment_creation", "availability_discovery"),
                _flow("start_booking"),
                _entity("service_id", "underarm_service_id"),
                _time_window(after="19:00"),
                _date_present,
            ),
        ),
        FocusedCase(
            "price_plus_explicit_booking",
            "booking_vs_inquiry",
            "جلسة ليزر ابط بكام؟ واحجزلي بكرة لو فيه ميعاد",
            _all(
                _has("pricing", "appointment_creation", "availability_discovery"),
                _flow("start_booking"),
                _entity("service_id", "underarm_service_id"),
                _date_present,
            ),
        ),
        FocusedCase(
            "time_window_inquiry_with_service",
            "time_semantics",
            "إيه المواعيد المتاحة لليزر ابط بين ٦ و٨ بالليل؟",
            _all(
                _has("availability_discovery"),
                _lacks("appointment_creation"),
                _entity("service_id", "underarm_service_id"),
                _time_window(after="18:00", before="20:00"),
            ),
        ),
        FocusedCase(
            "time_window_booking_with_service",
            "time_semantics",
            "احجزلي ليزر ابط في أي ميعاد بين ٦ و٨ بالليل",
            _all(
                _has("appointment_creation", "availability_discovery"),
                _flow("start_booking"),
                _entity("service_id", "underarm_service_id"),
                _time_window(after="18:00", before="20:00"),
            ),
        ),
        FocusedCase(
            "availability_without_service",
            "booking_vs_inquiry",
            "ممكن أعرف عندكم مواعيد فاضية بكرة؟",
            _all(
                _has("availability_discovery"),
                _lacks("appointment_creation"),
                _date_present,
            ),
        ),
    ]


def _semantic_expected(catalog: dict[str, Any]) -> dict[str, Any]:
    underarm = _catalog_row(catalog, "services", "ليزر إزالة الشعر - إبط")
    ahmed = _catalog_row(catalog, "doctors", "أحمد محمود")
    return {
        "underarm_service_id": str(underarm["id"]),
        "ahmed_doctor_id": str(ahmed["id"]),
    }


def _run_case(
    *,
    db: Session,
    workspace: Workspace,
    patient: Any,
    catalog: dict[str, Any],
    expected: dict[str, Any],
    case: FocusedCase,
    with_replies: bool,
) -> Result:
    started = perf_counter()
    timezone_name = (workspace.timezone or "Africa/Cairo").strip()
    local_now = datetime.now(ZoneInfo(timezone_name))

    decision = interpret_customer_turn(
        flow=None,
        history=[HumanMessage(content=case.message)],
        timezone_name=timezone_name,
        local_now=local_now,
        clinic_catalog=catalog,
    )
    semantic_ok, semantic_check = case.check(decision, expected)

    reply_text: str | None = None
    reply_model: str | None = None
    reply_ok = True
    no_unexpected_write = True
    reply_error: str | None = None

    if with_replies:
        before = {row.id for row in _appointments_for(db, workspace, patient)}
        try:
            payload = _agent_payload(
                patient_id=patient.id,
                message=case.message,
                conversation_id=None,
            )
            response = run_agent_chat(db=db, workspace=workspace, payload=payload)
            reply_text = response.reply
            reply_model = response.model
            reply_ok = bool((reply_text or "").strip())
        except Exception as exc:  # noqa: BLE001 - report and continue
            reply_ok = False
            reply_error = f"{type(exc).__name__}: {exc}"
        after = {row.id for row in _appointments_for(db, workspace, patient)}
        no_unexpected_write = before == after

    ok = semantic_ok and reply_ok and no_unexpected_write
    details = {
        "message": case.message,
        "semantic_ok": semantic_ok,
        "semantic_check": semantic_check,
        "capabilities": list(decision.capabilities or []),
        "flow_signal": str(decision.flow_signal),
        "package_intent": str(decision.package_intent),
        "entity_hints": decision.entity_hints.model_dump(mode="json"),
        "confidence": decision.confidence,
        "reason": decision.reason,
        "reply_checked": with_replies,
        "reply_ok": reply_ok,
        "reply": reply_text,
        "reply_model": reply_model,
        "reply_error": reply_error,
        "no_unexpected_write": no_unexpected_write,
    }
    failures: list[str] = []
    if not semantic_ok:
        failures.append(semantic_check)
    if not reply_ok:
        failures.append(reply_error or "Agent reply was empty.")
    if not no_unexpected_write:
        failures.append("First-turn case unexpectedly created an appointment.")

    return Result(
        name=case.name,
        category=case.category,
        status="PASS" if ok else "FAIL",
        duration_ms=int((perf_counter() - started) * 1000),
        details=details,
        error=" | ".join(failures) if failures else None,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run only Linka's currently problematic booking/inquiry/time regressions."
    )
    parser.add_argument("--workspace-slug", default="tia")
    parser.add_argument("--workspace-id", type=UUID, default=None)
    parser.add_argument(
        "--semantic-only",
        action="store_true",
        help="Skip run_agent_chat reply generation and evaluate only the structured interpreter.",
    )
    parser.add_argument(
        "--keep-data",
        action="store_true",
        help="Commit test conversations instead of rolling them back. Not recommended.",
    )
    parser.add_argument(
        "--report",
        default="artifacts/agent-problem-regression-report.json",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if str(settings.environment or "").strip().lower() == "production":
        print("Refusing to run the focused agent regression suite in production.", file=sys.stderr)
        return 2

    engine = create_engine(settings.database_url, pool_pre_ping=True)
    connection = engine.connect()
    outer = connection.begin()
    db = Session(
        bind=connection,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )

    results: list[Result] = []
    report_meta: dict[str, Any] = {}
    exit_code = 1

    try:
        if args.workspace_id is not None:
            workspace = db.scalar(select(Workspace).where(Workspace.id == args.workspace_id))
        else:
            workspace = db.scalar(select(Workspace).where(Workspace.slug == args.workspace_slug))
        if workspace is None:
            raise RuntimeError("Workspace not found.")

        catalog = build_clinic_catalog(db, workspace)
        if not catalog.get("services") or not catalog.get("doctors") or not catalog.get("branches"):
            raise RuntimeError("Active clinic catalog is incomplete.")

        patients = _fixture_patients(db, workspace)
        patient = patients.get("busy-evening")
        if patient is None:
            raise RuntimeError(
                "Focused regression patient is missing. Run the realistic fixture seed first."
            )

        expected = _semantic_expected(catalog)
        report_meta = {
            "started_at": datetime.now(UTC).isoformat(),
            "workspace_id": str(workspace.id),
            "workspace_slug": workspace.slug,
            "suite": "focused-problem-regression",
            "with_replies": not args.semantic_only,
            "rollback": not args.keep_data,
        }

        for case in _cases():
            try:
                result = _run_case(
                    db=db,
                    workspace=workspace,
                    patient=patient,
                    catalog=catalog,
                    expected=expected,
                    case=case,
                    with_replies=not args.semantic_only,
                )
            except Exception as exc:  # noqa: BLE001
                result = Result(
                    name=case.name,
                    category=case.category,
                    status="FAIL",
                    duration_ms=0,
                    details={"message": case.message},
                    error=f"{type(exc).__name__}: {exc}",
                )
            results.append(result)
            print(
                f"[{result.status}] {result.category}/{result.name} "
                f"({result.duration_ms} ms)"
            )
            if result.error:
                print(f"       {result.error}")

        exit_code = 1 if any(row.status == "FAIL" for row in results) else 0

    except Exception as exc:  # noqa: BLE001
        print(traceback.format_exc(), file=sys.stderr)
        results.append(
            Result(
                name="suite_exception",
                category="setup",
                status="FAIL",
                duration_ms=0,
                error=f"{type(exc).__name__}: {exc}",
            )
        )
        exit_code = 1
    finally:
        try:
            db.close()
        finally:
            if args.keep_data:
                outer.commit()
            else:
                outer.rollback()
            connection.close()
            engine.dispose()

    counts = {
        "PASS": sum(1 for row in results if row.status == "PASS"),
        "FAIL": sum(1 for row in results if row.status == "FAIL"),
    }
    path = Path(args.report)
    if not path.is_absolute():
        path = Path.cwd() / path
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        **report_meta,
        "counts": counts,
        "results": [asdict(row) for row in results],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\nSummary:", json.dumps(counts, ensure_ascii=False))
    print(f"Report: {path}")
    if not args.keep_data:
        print("Database writes rolled back: yes")
    print("Customer-agent replies included:", "no" if args.semantic_only else "yes")
    print("WhatsApp/n8n used: no")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
