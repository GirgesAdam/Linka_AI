from __future__ import annotations

import argparse
import sys
from datetime import time
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

BACKEND_DIR = Path(__file__).resolve().parent
# When copied to backend/scripts/, backend is the parent of scripts.
if BACKEND_DIR.name == "scripts":
    BACKEND_DIR = BACKEND_DIR.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.models.booking_settings import BookingSettings
from app.models.branch import Branch
from app.models.doctor import Doctor
from app.models.doctor_branch import DoctorBranch
from app.models.doctor_service import DoctorService
from app.models.patient import Patient
from app.models.service import Service
from app.models.staff import Staff
from app.models.working_hours import BranchWorkingHour, DoctorWorkingHour
from app.models.workspace import Workspace


class SeedSettings(BaseSettings):
    database_url: str

    model_config = SettingsConfigDict(
        env_file=BACKEND_DIR / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create idempotent demo clinic data for testing the Linka agent."
    )
    parser.add_argument("--workspace-slug", default="tia")
    return parser.parse_args()


def get_or_create_branch(db: Session, workspace: Workspace) -> Branch:
    branch = db.scalar(
        select(Branch).where(
            Branch.workspace_id == workspace.id,
            Branch.code == "demo-main",
        )
    )
    if branch is None:
        branch = Branch(
            workspace_id=workspace.id,
            name="Linka Demo Branch",
            code="demo-main",
            phone="+201000000000",
            address_line1="Demo address — staging only",
            city="Cairo",
            country_code="EG",
            timezone=workspace.timezone,
            is_active=True,
        )
        db.add(branch)
        db.flush()
    return branch


def get_or_create_doctor(db: Session, workspace: Workspace) -> Doctor:
    staff = db.scalar(
        select(Staff).where(
            Staff.workspace_id == workspace.id,
            Staff.email == "demo-doctor@tia.local",
        )
    )
    if staff is None:
        staff = Staff(
            workspace_id=workspace.id,
            first_name="طبيب",
            last_name="تجريبي",
            email="demo-doctor@tia.local",
            job_title="Demo Doctor",
            is_active=True,
        )
        db.add(staff)
        db.flush()

    doctor = db.scalar(
        select(Doctor).where(
            Doctor.workspace_id == workspace.id,
            Doctor.staff_id == staff.id,
        )
    )
    if doctor is None:
        doctor = Doctor(
            workspace_id=workspace.id,
            staff_id=staff.id,
            specialization="Aesthetic Medicine — Demo",
            bio="Demo doctor used only for Linka staging tests.",
            booking_enabled=True,
            is_active=True,
        )
        db.add(doctor)
        db.flush()
    return doctor


def get_or_create_service(db: Session, workspace: Workspace) -> Service:
    service = db.scalar(
        select(Service).where(
            Service.workspace_id == workspace.id,
            Service.slug == "demo-laser-hair-removal",
        )
    )
    if service is None:
        service = Service(
            workspace_id=workspace.id,
            name="ليزر إزالة الشعر — Demo",
            slug="demo-laser-hair-removal",
            category="Laser",
            description="خدمة تجريبية لاختبار Linka على Staging فقط.",
            duration_minutes=60,
            buffer_before_minutes=0,
            buffer_after_minutes=0,
            price_minor=150000,
            currency="EGP",
            requires_medical_review=False,
            is_active=True,
        )
        db.add(service)
        db.flush()
    return service


def ensure_assignments(
    db: Session,
    workspace: Workspace,
    branch: Branch,
    doctor: Doctor,
    service: Service,
) -> None:
    doctor_branch = db.scalar(
        select(DoctorBranch).where(
            DoctorBranch.workspace_id == workspace.id,
            DoctorBranch.doctor_id == doctor.id,
            DoctorBranch.branch_id == branch.id,
        )
    )
    if doctor_branch is None:
        doctor_branch = DoctorBranch(
            workspace_id=workspace.id,
            doctor_id=doctor.id,
            branch_id=branch.id,
            is_primary=True,
            is_active=True,
        )
        db.add(doctor_branch)
        db.flush()
    else:
        doctor_branch.is_active = True

    doctor_service = db.scalar(
        select(DoctorService).where(
            DoctorService.workspace_id == workspace.id,
            DoctorService.doctor_id == doctor.id,
            DoctorService.service_id == service.id,
        )
    )
    if doctor_service is None:
        db.add(
            DoctorService(
                workspace_id=workspace.id,
                doctor_id=doctor.id,
                service_id=service.id,
                is_active=True,
            )
        )
    else:
        doctor_service.is_active = True


def ensure_working_hours(
    db: Session,
    workspace: Workspace,
    branch: Branch,
    doctor: Doctor,
) -> None:
    start = time(10, 0)
    end = time(22, 0)

    for weekday in range(7):
        branch_hours = db.scalar(
            select(BranchWorkingHour).where(
                BranchWorkingHour.workspace_id == workspace.id,
                BranchWorkingHour.branch_id == branch.id,
                BranchWorkingHour.weekday == weekday,
                BranchWorkingHour.start_time == start,
                BranchWorkingHour.end_time == end,
            )
        )
        if branch_hours is None:
            db.add(
                BranchWorkingHour(
                    workspace_id=workspace.id,
                    branch_id=branch.id,
                    weekday=weekday,
                    start_time=start,
                    end_time=end,
                )
            )

        doctor_hours = db.scalar(
            select(DoctorWorkingHour).where(
                DoctorWorkingHour.workspace_id == workspace.id,
                DoctorWorkingHour.doctor_id == doctor.id,
                DoctorWorkingHour.branch_id == branch.id,
                DoctorWorkingHour.weekday == weekday,
                DoctorWorkingHour.start_time == start,
                DoctorWorkingHour.end_time == end,
            )
        )
        if doctor_hours is None:
            db.add(
                DoctorWorkingHour(
                    workspace_id=workspace.id,
                    doctor_id=doctor.id,
                    branch_id=branch.id,
                    weekday=weekday,
                    start_time=start,
                    end_time=end,
                )
            )


def ensure_booking_settings(db: Session, workspace: Workspace) -> BookingSettings:
    settings = db.scalar(
        select(BookingSettings).where(BookingSettings.workspace_id == workspace.id)
    )
    if settings is None:
        settings = BookingSettings(
            workspace_id=workspace.id,
            slot_interval_minutes=30,
            minimum_notice_minutes=30,
            booking_horizon_days=90,
            cancellation_notice_minutes=120,
            allow_same_day_booking=True,
            require_confirmation=True,
            default_currency="EGP",
        )
        db.add(settings)
        db.flush()
    return settings


def get_or_create_patient(db: Session, workspace: Workspace, branch: Branch) -> Patient:
    phone = "+201000000001"
    patient = db.scalar(
        select(Patient).where(
            Patient.workspace_id == workspace.id,
            Patient.phone_normalized == phone,
        )
    )
    if patient is None:
        patient = Patient(
            workspace_id=workspace.id,
            first_name="عميل",
            last_name="تجريبي",
            phone=phone,
            phone_normalized=phone,
            preferred_language="ar",
            preferred_branch_id=branch.id,
            source="website",
            source_detail="Linka agent demo seed",
            status="active",
            marketing_consent=False,
        )
        db.add(patient)
        db.flush()
    return patient


def main() -> int:
    args = parse_args()

    try:
        settings = SeedSettings()
    except Exception as exc:
        print(f"Could not load DATABASE_URL from backend/.env: {exc}", file=sys.stderr)
        return 1

    if not settings.database_url.startswith("postgresql+psycopg://"):
        print("DATABASE_URL must start with postgresql+psycopg://", file=sys.stderr)
        return 1

    engine = create_engine(settings.database_url, pool_pre_ping=True)
    SessionLocal = sessionmaker(
        bind=engine,
        class_=Session,
        autoflush=False,
        expire_on_commit=False,
    )

    with SessionLocal() as db:
        workspace = db.scalar(
            select(Workspace).where(Workspace.slug == args.workspace_slug.strip().lower())
        )
        if workspace is None:
            print(f"Workspace not found: {args.workspace_slug}", file=sys.stderr)
            return 1

        try:
            branch = get_or_create_branch(db, workspace)
            doctor = get_or_create_doctor(db, workspace)
            service = get_or_create_service(db, workspace)
            ensure_assignments(db, workspace, branch, doctor, service)
            ensure_working_hours(db, workspace, branch, doctor)
            ensure_booking_settings(db, workspace)
            patient = get_or_create_patient(db, workspace, branch)
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            print(f"Demo seed failed because of a database constraint: {exc}", file=sys.stderr)
            return 1

        print("Linka agent demo data is ready")
        print(f"workspace_id={workspace.id}")
        print(f"patient_id={patient.id}")
        print(f"branch_id={branch.id}")
        print(f"doctor_id={doctor.id}")
        print(f"service_id={service.id}")
        print("demo_hours=10:00-22:00 every day")
        print("demo_only=true")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
