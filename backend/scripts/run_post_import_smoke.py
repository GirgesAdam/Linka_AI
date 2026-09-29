from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any
from uuid import UUID

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


@dataclass
class Check:
    key: str
    status: str
    message: str
    details: dict[str, Any]


def _check(key: str, ok: bool, message: str, **details: Any) -> Check:
    return Check(key=key, status="pass" if ok else "fail", message=message, details=details)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only smoke test for canonical runtime data after Linka historical import."
    )
    parser.add_argument("--workspace-id")
    parser.add_argument("--workspace-slug")
    parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args()

    from sqlalchemy import and_, func, select

    from app.database.session import SessionLocal
    from app.models.appointment import Appointment
    from app.models.branch import Branch
    from app.models.doctor import Doctor
    from app.models.historical_import import ClinicHistoricalImportBatch, ClinicHistoricalImportLink
    from app.models.patient import Patient
    from app.models.patient_package import PatientPackage
    from app.models.payment_transaction import PaymentTransaction
    from app.models.service import Service
    from app.models.staff import Staff
    from app.models.workspace import Workspace
    from app.services.analytics import analytics_overview

    with SessionLocal() as db:
        workspace = None
        if args.workspace_id:
            workspace = db.get(Workspace, UUID(args.workspace_id))
        elif args.workspace_slug:
            workspace = db.scalar(select(Workspace).where(Workspace.slug == args.workspace_slug))
        else:
            workspace = db.scalar(
                select(Workspace)
                .join(
                    ClinicHistoricalImportBatch,
                    ClinicHistoricalImportBatch.workspace_id == Workspace.id,
                )
                .where(ClinicHistoricalImportBatch.status == "imported")
                .order_by(ClinicHistoricalImportBatch.completed_at.desc().nullslast())
                .limit(1)
            )

        if workspace is None:
            payload = {"passed": False, "error": "workspace_not_found"}
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 2

        wid = workspace.id
        checks: list[Check] = []

        imported_batches = int(
            db.scalar(
                select(func.count())
                .select_from(ClinicHistoricalImportBatch)
                .where(
                    ClinicHistoricalImportBatch.workspace_id == wid,
                    ClinicHistoricalImportBatch.status == "imported",
                )
            )
            or 0
        )
        failed_batches = int(
            db.scalar(
                select(func.count())
                .select_from(ClinicHistoricalImportBatch)
                .where(
                    ClinicHistoricalImportBatch.workspace_id == wid,
                    ClinicHistoricalImportBatch.status == "failed",
                )
            )
            or 0
        )
        checks.append(
            _check(
                "history.imported_batches",
                imported_batches > 0,
                f"Imported historical batches: {imported_batches}.",
                imported_batches=imported_batches,
                failed_batches=failed_batches,
            )
        )

        model_by_type = {
            "patient": Patient,
            "appointment": Appointment,
            "payment": PaymentTransaction,
            "package": PatientPackage,
        }
        link_counts: dict[str, int] = {}
        missing_links: dict[str, int] = {}
        for entity_type, model in model_by_type.items():
            link_count = int(
                db.scalar(
                    select(func.count())
                    .select_from(ClinicHistoricalImportLink)
                    .where(
                        ClinicHistoricalImportLink.workspace_id == wid,
                        ClinicHistoricalImportLink.entity_type == entity_type,
                    )
                )
                or 0
            )
            canonical_count = int(
                db.scalar(
                    select(func.count())
                    .select_from(ClinicHistoricalImportLink)
                    .join(
                        model,
                        and_(
                            model.workspace_id == ClinicHistoricalImportLink.workspace_id,
                            model.id == ClinicHistoricalImportLink.canonical_id,
                        ),
                    )
                    .where(
                        ClinicHistoricalImportLink.workspace_id == wid,
                        ClinicHistoricalImportLink.entity_type == entity_type,
                    )
                )
                or 0
            )
            link_counts[entity_type] = link_count
            missing_links[entity_type] = max(link_count - canonical_count, 0)
        checks.append(
            _check(
                "history.provenance_links",
                all(value == 0 for value in missing_links.values()),
                "Every historical-import provenance link resolves to a canonical row."
                if all(value == 0 for value in missing_links.values())
                else "Some historical-import links point to missing canonical rows.",
                links=link_counts,
                missing=missing_links,
            )
        )

        active_patients = int(
            db.scalar(
                select(func.count()).select_from(Patient).where(
                    Patient.workspace_id == wid, Patient.status == "active"
                )
            )
            or 0
        )
        checks.append(
            _check(
                "runtime.patients",
                active_patients > 0 and link_counts["patient"] > 0,
                f"Canonical active patients: {active_patients}.",
                active_patients=active_patients,
                imported_patient_links=link_counts["patient"],
            )
        )

        appointment_count = int(
            db.scalar(select(func.count()).select_from(Appointment).where(Appointment.workspace_id == wid)) or 0
        )
        joined_appointments = int(
            db.scalar(
                select(func.count())
                .select_from(Appointment)
                .join(Patient, and_(Patient.workspace_id == Appointment.workspace_id, Patient.id == Appointment.patient_id))
                .join(Service, and_(Service.workspace_id == Appointment.workspace_id, Service.id == Appointment.service_id))
                .join(Branch, and_(Branch.workspace_id == Appointment.workspace_id, Branch.id == Appointment.branch_id))
                .join(Doctor, and_(Doctor.workspace_id == Appointment.workspace_id, Doctor.id == Appointment.doctor_id))
                .join(Staff, and_(Staff.workspace_id == Doctor.workspace_id, Staff.id == Doctor.staff_id))
                .where(Appointment.workspace_id == wid)
            )
            or 0
        )
        checks.append(
            _check(
                "runtime.appointment_joins",
                appointment_count > 0 and appointment_count == joined_appointments,
                "All appointments resolve patient/service/branch/doctor/staff joins."
                if appointment_count == joined_appointments
                else "Some appointments are invisible to dashboard-style joins.",
                appointments=appointment_count,
                fully_joined=joined_appointments,
                missing_join_rows=max(appointment_count - joined_appointments, 0),
            )
        )

        payments = int(
            db.scalar(select(func.count()).select_from(PaymentTransaction).where(PaymentTransaction.workspace_id == wid)) or 0
        )
        refunds = int(
            db.scalar(
                select(func.count()).select_from(PaymentTransaction).where(
                    PaymentTransaction.workspace_id == wid,
                    PaymentTransaction.transaction_type == "refund",
                )
            )
            or 0
        )
        invalid_payment_refs = int(
            db.scalar(
                select(func.count()).select_from(PaymentTransaction).where(
                    PaymentTransaction.workspace_id == wid,
                    PaymentTransaction.transaction_type == "payment",
                    PaymentTransaction.reference_transaction_id.is_not(None),
                )
            )
            or 0
        )
        checks.append(
            _check(
                "runtime.payments",
                payments > 0 and invalid_payment_refs == 0,
                f"Canonical payments/refunds: {payments} total, {refunds} refunds.",
                transactions=payments,
                refunds=refunds,
                invalid_payment_reference_rows=invalid_payment_refs,
            )
        )

        packages = int(
            db.scalar(select(func.count()).select_from(PatientPackage).where(PatientPackage.workspace_id == wid)) or 0
        )
        invalid_packages = int(
            db.scalar(
                select(func.count()).select_from(PatientPackage).where(
                    PatientPackage.workspace_id == wid,
                    PatientPackage.opening_sessions_remaining.is_not(None),
                    PatientPackage.opening_sessions_remaining > PatientPackage.sessions_purchased,
                )
            )
            or 0
        )
        checks.append(
            _check(
                "runtime.packages",
                packages > 0 and invalid_packages == 0,
                f"Canonical patient packages: {packages}.",
                packages=packages,
                invalid_opening_balances=invalid_packages,
            )
        )

        analytics_error = None
        analytics_payload: dict[str, Any] = {}
        try:
            overview = analytics_overview(
                db,
                workspace_id=wid,
                timezone_name=workspace.timezone,
                days=90,
            )
            analytics_payload = {
                "total_appointments_90d": overview.total_appointments,
                "completed_appointments_90d": overview.completed_appointments,
                "new_patients_90d": overview.new_patients,
                "money_rows": len(overview.money),
                "top_services": len(overview.top_services),
            }
        except Exception as exc:  # smoke gate must report, not hide, runtime query failures
            analytics_error = f"{type(exc).__name__}: {exc}"
        checks.append(
            _check(
                "runtime.analytics_overview",
                analytics_error is None,
                "Analytics 90-day overview executed successfully."
                if analytics_error is None
                else "Analytics overview raised an exception.",
                error=analytics_error,
                **analytics_payload,
            )
        )

    passed = all(item.status == "pass" for item in checks)
    payload = {
        "passed": passed,
        "workspace": {"id": str(workspace.id), "slug": workspace.slug, "name": workspace.name},
        "checks": [asdict(item) for item in checks],
    }

    if args.json_output:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        print(f"=== Linka POST-IMPORT SMOKE — {workspace.name} ({workspace.slug}) ===")
        for item in checks:
            print(f"[{item.status.upper()}] {item.key} — {item.message}")
            if item.details:
                print("       " + json.dumps(item.details, ensure_ascii=False, default=str))
        print()
        print("RESULT=" + ("PASS" if passed else "FAIL"))

    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
