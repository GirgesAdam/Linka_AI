from __future__ import annotations

"""Focused package regression suite for Linka's single-service package model.

Business invariant under test:
- Every package belongs to exactly one service_id.
- Every booked appointment consumes/reserves one session from that same service package.
- A package must never be applied to another service.
- A patient may have only one active package for a given service at a time.

The suite creates its own package fixtures and rolls everything back by default.
"""

import argparse
import json
import sys
import traceback
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any
from uuid import UUID

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.agents.clinic_grounding import build_clinic_catalog
from app.core.config import settings
from app.models.appointment import Appointment
from app.models.service import Service
from app.models.workspace import Workspace
from app.services.agent_chat import (
    _customer_package_payload,
    _package_refund_quote_payload,
)
from app.services.patient_packages import (
    consume_package_usage,
    create_patient_package,
    list_patient_packages,
    reserve_package_usage,
)

from run_agent_e2e_matrix import (
    _appointments_for,
    _catalog_row,
    _find_future_slot,
    _fixture_patients,
    _format_local_slot,
    _send,
)


@dataclass
class TurnResult:
    turn: int
    message: str
    reply: str | None
    duration_ms: int
    new_appointment_ids: list[str] = field(default_factory=list)


@dataclass
class CaseResult:
    name: str
    status: str
    duration_ms: int
    turns: list[TurnResult] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


@dataclass
class Report:
    started_at: str
    workspace_id: str
    workspace_slug: str
    rollback: bool
    results: list[CaseResult] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        result = {"PASS": 0, "FAIL": 0}
        for row in self.results:
            result[row.status] = result.get(row.status, 0) + 1
        return result


def _primary_branch_row(workspace: Workspace, catalog: dict[str, Any]) -> dict[str, Any]:
    branches = list(catalog.get("branches") or [])
    if workspace.primary_branch_id is not None:
        for row in branches:
            if str(row.get("id")) == str(workspace.primary_branch_id):
                return row
        raise RuntimeError("Primary branch was not found in the active catalog.")
    if len(branches) == 1:
        return branches[0]
    raise RuntimeError(
        "Package focus test expects the current single-branch product model."
    )


def _doctor_name(row: dict[str, Any]) -> str:
    return str(
        row.get("name")
        or row.get("display_name")
        or row.get("full_name")
        or ""
    ).strip()


def _find_doctor_and_slot(
    db: Session,
    workspace: Workspace,
    catalog: dict[str, Any],
    *,
    branch_id: UUID,
    service_id: UUID,
):
    errors: list[str] = []
    for doctor in catalog.get("doctors") or []:
        doctor_id = doctor.get("id")
        name = _doctor_name(doctor)
        if not doctor_id or not name:
            continue
        try:
            booking_date, timezone_name, slot = _find_future_slot(
                db,
                workspace,
                branch_id=branch_id,
                service_id=service_id,
                doctor_id=UUID(str(doctor_id)),
            )
            return doctor, booking_date, timezone_name, slot
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{name}: {exc}")
    raise RuntimeError(
        f"No available doctor/slot for service {service_id}. Tried: {errors[:6]}"
    )


def _new_rows(
    db: Session,
    workspace: Workspace,
    patient,
    before_ids: set[UUID],
) -> list[Appointment]:
    return [
        row
        for row in _appointments_for(db, workspace, patient)
        if row.id not in before_ids
    ]


def _send_turn(
    *,
    db: Session,
    workspace: Workspace,
    patient,
    message: str,
    conversation_id: UUID | None,
    before_ids: set[UUID],
    turn_number: int,
):
    response, duration_ms = _send(
        db,
        workspace,
        patient,
        message,
        conversation_id,
    )
    created = _new_rows(db, workspace, patient, before_ids)
    return response, TurnResult(
        turn=turn_number,
        message=message,
        reply=response.reply,
        duration_ms=duration_ms,
        new_appointment_ids=[str(row.id) for row in created],
    )


def _service(db: Session, workspace: Workspace, service_id: UUID) -> Service:
    row = db.scalar(
        select(Service).where(
            Service.workspace_id == workspace.id,
            Service.id == service_id,
        )
    )
    if row is None:
        raise RuntimeError(f"Service not found: {service_id}")
    return row


def _create_package(
    db: Session,
    *,
    workspace: Workspace,
    patient,
    service: Service,
    name: str,
    sessions: int,
    sale_price_minor: int | None = None,
):
    sale_price = (
        int(sale_price_minor)
        if sale_price_minor is not None
        else max(int(service.price_minor), int(service.price_minor) * max(1, sessions - 1))
    )
    return create_patient_package(
        db,
        workspace_id=workspace.id,
        patient_id=patient.id,
        service_id=service.id,
        name=name,
        sessions_purchased=sessions,
        sale_price_minor=sale_price,
        amount_paid_minor=sale_price,
        payment_method="cash",
        created_by_user_id=None,
        purchased_at=datetime.now(UTC),
        external_reference=None,
        external_id=None,
        idempotency_key=None,
    )


def _create_consumed_package_session(
    db: Session,
    *,
    workspace: Workspace,
    patient,
    package,
    service: Service,
    branch_id: UUID,
    doctor_id: UUID,
    slot,
) -> Appointment:
    appointment = Appointment(
        workspace_id=workspace.id,
        patient_id=patient.id,
        branch_id=branch_id,
        doctor_id=doctor_id,
        service_id=service.id,
        lead_id=None,
        created_by_user_id=None,
        status="completed",
        source="ai",
        start_at=slot.start_at,
        end_at=slot.end_at,
        busy_start_at=slot.start_at,
        busy_end_at=slot.end_at,
        duration_minutes=slot.duration_minutes,
        price_minor=slot.price_minor,
        currency=slot.currency,
        customer_note="package-focus-consumed-fixture",
        completed_at=slot.end_at,
    )
    db.add(appointment)
    db.flush()
    reserve_package_usage(
        db,
        appointment=appointment,
        package=package,
        sessions=1,
        actor_user_id=None,
    )
    consume_package_usage(
        db,
        appointment=appointment,
        used_at=slot.end_at,
        actor_user_id=None,
    )
    db.flush()
    return appointment


_ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")


def _normalized_numbers(text: str | None) -> str:
    return (text or "").translate(_ARABIC_DIGITS).replace(",", "")


def _package_remaining(
    db: Session,
    *,
    workspace: Workspace,
    patient,
    service_id: UUID,
) -> int:
    rows = list_patient_packages(
        db,
        workspace_id=workspace.id,
        patient_id=patient.id,
        service_id=service_id,
        usable_only=False,
    )
    if len(rows) != 1:
        raise RuntimeError(f"Expected exactly one package, found {len(rows)}")
    return int(rows[0].sessions_remaining)


def _case_remaining_sessions(
    *,
    db: Session,
    workspace: Workspace,
    patient,
    service: Service,
) -> CaseResult:
    started = perf_counter()
    turns: list[TurnResult] = []
    try:
        package = _create_package(
            db,
            workspace=workspace,
            patient=patient,
            service=service,
            name="Underarm 6 Sessions",
            sessions=6,
        )
        expected = _package_remaining(
            db,
            workspace=workspace,
            patient=patient,
            service_id=service.id,
        )
        before_ids = {row.id for row in _appointments_for(db, workspace, patient)}
        response, turn = _send_turn(
            db=db,
            workspace=workspace,
            patient=patient,
            message="فاضلي كام جلسة في باكدج ليزر الإبط؟",
            conversation_id=None,
            before_ids=before_ids,
            turn_number=1,
        )
        turns.append(turn)

        internal = _customer_package_payload(
            db=db,
            workspace_id=workspace.id,
            patient_id=patient.id,
            service_id=str(service.id),
        )
        numbers = _normalized_numbers(response.reply)
        ok = (
            internal.get("ok") is True
            and len(internal.get("packages") or []) == 1
            and int((internal["packages"][0]).get("sessions_remaining") or -1) == expected
            and str(expected) in numbers
            and len(_new_rows(db, workspace, patient, before_ids)) == 0
        )
        return CaseResult(
            name="package_remaining_sessions",
            status="PASS" if ok else "FAIL",
            duration_ms=int((perf_counter() - started) * 1000),
            turns=turns,
            details={
                "package_id": str(package.id),
                "expected_remaining": expected,
                "internal_payload": internal,
            },
            error=None if ok else (
                "Expected the agent to report the deterministic remaining-session count "
                "for the customer's same-service package without creating an appointment."
            ),
        )
    except Exception as exc:  # noqa: BLE001
        return CaseResult(
            name="package_remaining_sessions",
            status="FAIL",
            duration_ms=int((perf_counter() - started) * 1000),
            turns=turns,
            error=f"{type(exc).__name__}: {exc}",
        )


def _case_refund_quote(
    *,
    db: Session,
    workspace: Workspace,
    patient,
    service: Service,
    branch_id: UUID,
    doctor,
    slot,
) -> CaseResult:
    started = perf_counter()
    turns: list[TurnResult] = []
    try:
        # Five sessions sold for the price of four. One consumed session loses
        # the package discount and is charged at the standalone price.
        sale_price = int(service.price_minor) * 4
        package = _create_package(
            db,
            workspace=workspace,
            patient=patient,
            service=service,
            name="Underarm 5 Sessions",
            sessions=5,
            sale_price_minor=sale_price,
        )
        _create_consumed_package_session(
            db,
            workspace=workspace,
            patient=patient,
            package=package,
            service=service,
            branch_id=branch_id,
            doctor_id=UUID(str(doctor["id"])),
            slot=slot,
        )

        quote = _package_refund_quote_payload(
            db=db,
            workspace_id=workspace.id,
            patient_id=patient.id,
            service_id=str(service.id),
        )
        expected_refund_minor = sale_price - int(service.price_minor)

        before_ids = {row.id for row in _appointments_for(db, workspace, patient)}
        response, turn = _send_turn(
            db=db,
            workspace=workspace,
            patient=patient,
            message="لو لغيت باكدج ليزر الإبط دلوقتي هيرجعلي كام؟",
            conversation_id=None,
            before_ids=before_ids,
            turn_number=1,
        )
        turns.append(turn)

        actual_quote = quote.get("quote") if isinstance(quote, dict) else None
        refund_egp = expected_refund_minor // 100
        numbers = _normalized_numbers(response.reply)
        ok = (
            isinstance(actual_quote, dict)
            and int(actual_quote.get("consumed_sessions") or -1) == 1
            and int(actual_quote.get("refundable_minor") or -1) == expected_refund_minor
            and str(refund_egp) in numbers
            and len(_new_rows(db, workspace, patient, before_ids)) == 0
        )
        return CaseResult(
            name="package_refund_quote_after_one_consumed",
            status="PASS" if ok else "FAIL",
            duration_ms=int((perf_counter() - started) * 1000),
            turns=turns,
            details={
                "package_id": str(package.id),
                "standalone_price_minor": int(service.price_minor),
                "sale_price_minor": sale_price,
                "expected_refund_minor": expected_refund_minor,
                "quote": quote,
            },
            error=None if ok else (
                "Expected refund = collected package value - one consumed session "
                "repriced at the standalone session price."
            ),
        )
    except Exception as exc:  # noqa: BLE001
        return CaseResult(
            name="package_refund_quote_after_one_consumed",
            status="FAIL",
            duration_ms=int((perf_counter() - started) * 1000),
            turns=turns,
            error=f"{type(exc).__name__}: {exc}",
        )


def _case_single_matching_package_booking(
    *,
    db: Session,
    workspace: Workspace,
    catalog: dict[str, Any],
    patient,
    service: Service,
    primary_branch: dict[str, Any],
) -> CaseResult:
    started = perf_counter()
    turns: list[TurnResult] = []
    try:
        package = _create_package(
            db,
            workspace=workspace,
            patient=patient,
            service=service,
            name="Underarm 6 Sessions",
            sessions=6,
        )
        doctor, _, timezone_name, slot = _find_doctor_and_slot(
            db,
            workspace,
            catalog,
            branch_id=UUID(str(primary_branch["id"])),
            service_id=service.id,
        )
        date_text, time_text = _format_local_slot(slot, timezone_name)
        doctor_name = _doctor_name(doctor)

        before_ids = {row.id for row in _appointments_for(db, workspace, patient)}
        _, turn = _send_turn(
            db=db,
            workspace=workspace,
            patient=patient,
            message=(
                f"عايز احجز ليزر ابط مع {doctor_name} "
                f"يوم {date_text} الساعة {time_text}"
            ),
            conversation_id=None,
            before_ids=before_ids,
            turn_number=1,
        )
        turns.append(turn)

        created = _new_rows(db, workspace, patient, before_ids)
        appointment = created[-1] if created else None
        packages = list_patient_packages(
            db,
            workspace_id=workspace.id,
            patient_id=patient.id,
            service_id=service.id,
            usable_only=False,
        )
        remaining = int(packages[0].sessions_remaining) if len(packages) == 1 else None
        ok = (
            len(created) == 1
            and appointment is not None
            and appointment.patient_package_id == package.id
            and appointment.billing_context == "package_prepaid"
            and appointment.payment_status == "paid"
            and remaining == 5
        )
        return CaseResult(
            name="single_matching_package_booking",
            status="PASS" if ok else "FAIL",
            duration_ms=int((perf_counter() - started) * 1000),
            turns=turns,
            details={
                "package_id": str(package.id),
                "created_appointment_id": str(appointment.id) if appointment else None,
                "appointment_patient_package_id": (
                    str(appointment.patient_package_id)
                    if appointment and appointment.patient_package_id
                    else None
                ),
                "billing_context": appointment.billing_context if appointment else None,
                "payment_status": appointment.payment_status if appointment else None,
                "sessions_remaining_after_booking": remaining,
            },
            error=None if ok else (
                "Expected one matching same-service package to be used automatically: "
                "one session reserved and appointment marked package_prepaid."
            ),
        )
    except Exception as exc:  # noqa: BLE001
        return CaseResult(
            name="single_matching_package_booking",
            status="FAIL",
            duration_ms=int((perf_counter() - started) * 1000),
            turns=turns,
            error=f"{type(exc).__name__}: {exc}",
        )


def _case_different_service_package_not_used(
    *,
    db: Session,
    workspace: Workspace,
    catalog: dict[str, Any],
    patient,
    booking_service: Service,
    other_service: Service,
    primary_branch: dict[str, Any],
) -> CaseResult:
    started = perf_counter()
    turns: list[TurnResult] = []
    try:
        package = _create_package(
            db,
            workspace=workspace,
            patient=patient,
            service=other_service,
            name="Full Body 5 Sessions",
            sessions=5,
        )
        doctor, _, timezone_name, slot = _find_doctor_and_slot(
            db,
            workspace,
            catalog,
            branch_id=UUID(str(primary_branch["id"])),
            service_id=booking_service.id,
        )
        date_text, time_text = _format_local_slot(slot, timezone_name)
        doctor_name = _doctor_name(doctor)
        before_ids = {row.id for row in _appointments_for(db, workspace, patient)}

        _, turn = _send_turn(
            db=db,
            workspace=workspace,
            patient=patient,
            message=(
                f"عايز احجز ليزر ابط مع {doctor_name} "
                f"يوم {date_text} الساعة {time_text}"
            ),
            conversation_id=None,
            before_ids=before_ids,
            turn_number=1,
        )
        turns.append(turn)

        created = _new_rows(db, workspace, patient, before_ids)
        appointment = created[-1] if created else None
        other_remaining = _package_remaining(
            db,
            workspace=workspace,
            patient=patient,
            service_id=other_service.id,
        )
        ok = (
            len(created) == 1
            and appointment is not None
            and appointment.service_id == booking_service.id
            and appointment.patient_package_id is None
            and appointment.billing_context == "standard"
            and other_remaining == 5
        )
        return CaseResult(
            name="different_service_package_not_used",
            status="PASS" if ok else "FAIL",
            duration_ms=int((perf_counter() - started) * 1000),
            turns=turns,
            details={
                "other_package_id": str(package.id),
                "created_appointment_id": str(appointment.id) if appointment else None,
                "billing_context": appointment.billing_context if appointment else None,
                "other_package_sessions_remaining": other_remaining,
            },
            error=None if ok else (
                "A package may only cover appointments for its own service_id."
            ),
        )
    except Exception as exc:  # noqa: BLE001
        return CaseResult(
            name="different_service_package_not_used",
            status="FAIL",
            duration_ms=int((perf_counter() - started) * 1000),
            turns=turns,
            error=f"{type(exc).__name__}: {exc}",
        )



def _case_second_active_package_rejected(
    *,
    db: Session,
    workspace: Workspace,
    patient,
    service: Service,
) -> CaseResult:
    started = perf_counter()
    turns: list[TurnResult] = []
    try:
        first = _create_package(
            db,
            workspace=workspace,
            patient=patient,
            service=service,
            name="Underarm Package A",
            sessions=3,
        )

        rejected = False
        rejection_type = None
        rejection_message = None
        second_id = None

        try:
            second = _create_package(
                db,
                workspace=workspace,
                patient=patient,
                service=service,
                name="Underarm Package B",
                sessions=4,
            )
            second_id = str(second.id)
        except Exception as exc:  # noqa: BLE001
            rejected = True
            rejection_type = type(exc).__name__
            rejection_message = str(exc)

        rows = list_patient_packages(
            db,
            workspace_id=workspace.id,
            patient_id=patient.id,
            service_id=service.id,
            usable_only=True,
        )

        ok = (
            rejected
            and len(rows) == 1
            and str(rows[0].id) == str(first.id)
        )

        return CaseResult(
            name="second_active_same_service_package_rejected",
            status="PASS" if ok else "FAIL",
            duration_ms=int((perf_counter() - started) * 1000),
            turns=turns,
            details={
                "first_package_id": str(first.id),
                "second_package_id": second_id,
                "rejected": rejected,
                "rejection_type": rejection_type,
                "rejection_message": rejection_message,
                "usable_same_service_packages": [
                    {
                        "id": str(row.id),
                        "effective_status": row.effective_status,
                        "sessions_remaining": int(row.sessions_remaining),
                    }
                    for row in rows
                ],
            },
            error=None if ok else (
                "Business rule violation: a patient must not have two active packages "
                "for the same service at the same time. The current package must be "
                "finished/exhausted/cancelled before a new same-service package is created."
            ),
        )
    except Exception as exc:  # noqa: BLE001
        return CaseResult(
            name="second_active_same_service_package_rejected",
            status="FAIL",
            duration_ms=int((perf_counter() - started) * 1000),
            turns=turns,
            error=f"{type(exc).__name__}: {exc}",
        )



def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run five focused package checks for Linka single-service, single-active-package rules."
    )
    parser.add_argument("--workspace-slug", default="tia-demo")
    parser.add_argument("--workspace-id", type=UUID, default=None)
    parser.add_argument("--keep-data", action="store_true")
    parser.add_argument(
        "--report",
        default="artifacts/agent-package-focus.json",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if str(settings.environment or "").strip().lower() == "production":
        print("Refusing to run package focus suite in production.", file=sys.stderr)
        return 2

    engine = create_engine(settings.database_url, pool_pre_ping=True)
    connection = engine.connect()
    outer = connection.begin()
    db = Session(
        bind=connection,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    report: Report | None = None
    exit_code = 1

    try:
        workspace = (
            db.scalar(select(Workspace).where(Workspace.id == args.workspace_id))
            if args.workspace_id
            else db.scalar(select(Workspace).where(Workspace.slug == args.workspace_slug))
        )
        if workspace is None:
            raise RuntimeError("Workspace not found.")

        report = Report(
            started_at=datetime.now(UTC).isoformat(),
            workspace_id=str(workspace.id),
            workspace_slug=workspace.slug,
            rollback=not args.keep_data,
        )

        catalog = build_clinic_catalog(db, workspace)
        primary_branch = _primary_branch_row(workspace, catalog)
        underarm_row = _catalog_row(catalog, "services", "ليزر إزالة الشعر - إبط")
        full_body_row = _catalog_row(
            catalog,
            "services",
            "ليزر إزالة الشعر - جسم كامل سيدات",
        )
        underarm = _service(db, workspace, UUID(str(underarm_row["id"])))
        full_body = _service(db, workspace, UUID(str(full_body_row["id"])))

        patients = _fixture_patients(db, workspace)
        required_keys = [
            "busy-evening",
            "pending-new-cairo",
            "injectables",
            "cancelled-slot",
            "history",
        ]
        missing = [key for key in required_keys if key not in patients]
        if missing:
            raise RuntimeError(
                "Missing realistic fixture patients: "
                + ", ".join(missing)
                + ". Run seed_realistic_aesthetic_clinic.py first."
            )

        # Refund fixture needs one real doctor/slot for the consumed usage row.
        refund_doctor, _, _, refund_slot = _find_doctor_and_slot(
            db,
            workspace,
            catalog,
            branch_id=UUID(str(primary_branch["id"])),
            service_id=underarm.id,
        )

        cases = [
            _case_remaining_sessions(
                db=db,
                workspace=workspace,
                patient=patients["busy-evening"],
                service=underarm,
            ),
            _case_refund_quote(
                db=db,
                workspace=workspace,
                patient=patients["pending-new-cairo"],
                service=underarm,
                branch_id=UUID(str(primary_branch["id"])),
                doctor=refund_doctor,
                slot=refund_slot,
            ),
            _case_single_matching_package_booking(
                db=db,
                workspace=workspace,
                catalog=catalog,
                patient=patients["injectables"],
                service=underarm,
                primary_branch=primary_branch,
            ),
            _case_different_service_package_not_used(
                db=db,
                workspace=workspace,
                catalog=catalog,
                patient=patients["cancelled-slot"],
                booking_service=underarm,
                other_service=full_body,
                primary_branch=primary_branch,
            ),
            _case_second_active_package_rejected(
                db=db,
                workspace=workspace,
                patient=patients["history"],
                service=underarm,
            ),
        ]

        for case in cases:
            report.results.append(case)
            print(f"[{case.status}] {case.name} ({case.duration_ms} ms)")
            if case.error:
                print(f"       {case.error}")

        exit_code = 1 if report.counts().get("FAIL", 0) else 0

    except Exception as exc:  # noqa: BLE001
        print(traceback.format_exc(), file=sys.stderr)
        if report is not None:
            report.results.append(
                CaseResult(
                    name="suite_exception",
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

    if report is not None:
        path = Path(args.report)
        if not path.is_absolute():
            path = Path.cwd() / path
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            **{k: v for k, v in asdict(report).items() if k != "results"},
            "counts": report.counts(),
            "results": [asdict(row) for row in report.results],
        }
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print("\nSummary:", json.dumps(report.counts(), ensure_ascii=False))
        print(f"Report: {path}")
        print(f"Database writes rolled back: {'no' if args.keep_data else 'yes'}")

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
