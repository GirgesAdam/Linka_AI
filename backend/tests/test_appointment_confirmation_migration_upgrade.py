from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, select
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from alembic import command
from app.core.config import settings
from app.models.appointment import Appointment
from app.models.branch import Branch
from app.models.doctor import Doctor
from app.models.patient import Patient
from app.models.service import Service
from app.models.staff import Staff
from app.models.workspace import Workspace

BACKEND = Path(__file__).resolve().parent.parent


def _upgrade(url: str, revision: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIGRATION_DATABASE_URL", url)
    config = Config(str(BACKEND / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND / "alembic"))
    command.upgrade(config, revision)


@pytest.fixture
def migration_database(monkeypatch: pytest.MonkeyPatch):
    base = make_url(settings.database_url)
    database_name = f"tia_confirmation_upgrade_{uuid4().hex[:12]}"
    admin_url = base.set(database="postgres")
    admin_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        with admin_engine.connect() as connection:
            connection.exec_driver_sql(f'CREATE DATABASE "{database_name}"')
    except OperationalError:
        admin_engine.dispose()
        pytest.skip("PostgreSQL is not available for populated migration integration test.")

    target_url = base.set(database=database_name)
    try:
        yield target_url.render_as_string(hide_password=False)
    finally:
        admin_engine.dispose()
        cleanup_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
        try:
            with cleanup_engine.connect() as connection:
                connection.exec_driver_sql(
                    f'DROP DATABASE IF EXISTS "{database_name}" WITH (FORCE)'
                )
        finally:
            cleanup_engine.dispose()


def _seed_confirmation_case(
    db: Session,
    *,
    suffix: str,
    workspace_timezone: str,
    branch_timezone: str | None,
    start_at: datetime,
    created_at: datetime,
) -> UUID:
    workspace = Workspace(
        name=f"Confirmation migration {suffix}",
        slug=f"confirmation-migration-{suffix}-{uuid4().hex[:8]}",
        timezone=workspace_timezone,
    )
    db.add(workspace)
    db.flush()

    branch = Branch(
        workspace_id=workspace.id,
        name="Main",
        code=f"MAIN-{suffix}",
        timezone=branch_timezone,
    )
    patient = Patient(
        workspace_id=workspace.id,
        first_name="Migration",
        phone=f"0100{uuid4().int % 100000000:08d}",
        status="active",
        whatsapp_opt_in=True,
    )
    staff = Staff(
        workspace_id=workspace.id,
        first_name="Migration",
        last_name="Doctor",
    )
    service = Service(
        workspace_id=workspace.id,
        name="Migration service",
        slug=f"migration-service-{suffix}-{uuid4().hex[:8]}",
        duration_minutes=30,
    )
    db.add_all([branch, patient, staff, service])
    db.flush()
    doctor = Doctor(workspace_id=workspace.id, staff_id=staff.id)
    db.add(doctor)
    db.flush()

    appointment = Appointment(
        workspace_id=workspace.id,
        patient_id=patient.id,
        branch_id=branch.id,
        doctor_id=doctor.id,
        service_id=service.id,
        status="confirmed",
        start_at=start_at,
        end_at=start_at + timedelta(minutes=30),
        busy_start_at=start_at,
        busy_end_at=start_at + timedelta(minutes=30),
        duration_minutes=30,
        created_at=created_at,
        confirmed_at=created_at,
    )
    db.add(appointment)
    db.flush()
    appointment_id = appointment.id
    db.commit()
    return appointment_id


def test_0092_invalid_timezone_strings_use_safe_workspace_then_utc_fallback(
    migration_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _upgrade(migration_database, "0091_availability_block_scope", monkeypatch)
    engine = create_engine(migration_database, pool_pre_ping=True)
    try:
        with Session(engine) as db:
            # 00:30 Cairo on Oct 11. Created Oct 9 local, which is too early
            # for Cairo (window opens Oct 10) but valid under UTC (window opens Oct 9).
            start_at = datetime(2026, 10, 10, 22, 30, tzinfo=UTC)
            created_at = datetime(2026, 10, 9, 20, 0, tzinfo=UTC)
            workspace_fallback_id = _seed_confirmation_case(
                db,
                suffix="workspace-fallback",
                workspace_timezone="Africa/Cairo",
                branch_timezone="Invalid/Branch-Timezone",
                start_at=start_at,
                created_at=created_at,
            )
            utc_fallback_id = _seed_confirmation_case(
                db,
                suffix="utc-fallback",
                workspace_timezone="Invalid/Workspace-Timezone",
                branch_timezone="Invalid/Branch-Timezone",
                start_at=start_at,
                created_at=created_at,
            )

        _upgrade(migration_database, "0092_appt_confirmation", monkeypatch)

        with Session(engine) as db:
            workspace_fallback = db.scalar(
                select(Appointment).where(Appointment.id == workspace_fallback_id)
            )
            utc_fallback = db.scalar(
                select(Appointment).where(Appointment.id == utc_fallback_id)
            )
            assert workspace_fallback is not None
            assert utc_fallback is not None
            assert workspace_fallback.status == "pending"
            assert workspace_fallback.confirmed_at is None
            assert utc_fallback.status == "confirmed"
            assert utc_fallback.confirmed_at == datetime(2026, 10, 9, 20, 0, tzinfo=UTC)
    finally:
        engine.dispose()
