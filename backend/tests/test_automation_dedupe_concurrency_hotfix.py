import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.appointment import Appointment
from app.models.automation_job import AutomationJob
from app.models.branch import Branch
from app.models.doctor import Doctor
from app.models.message import Message
from app.models.message_dispatch import MessageDispatch
from app.models.patient import Patient
from app.models.service import Service
from app.models.staff import Staff
from app.models.workspace import Workspace
from app.services import automations


def _ci_engine():
    url = make_url(os.environ["DATABASE_URL"])
    if url.host not in {"localhost", "127.0.0.1", "::1"} or url.database != "ci_db":
        pytest.fail("Automation dedupe concurrency gate requires disposable local ci_db.")
    if settings.environment != "test":
        pytest.fail("Automation dedupe concurrency gate requires ENVIRONMENT=test.")
    return create_engine(url, connect_args={"connect_timeout": 3})


def _seed_candidate(engine, *, now: datetime):
    with Session(engine) as seed:
        workspace = Workspace(
            name="Concurrent scheduler hotfix gate",
            slug=f"concurrent-hotfix-{uuid4()}",
            timezone="UTC",
        )
        seed.add(workspace)
        seed.flush()
        workspace_id = workspace.id
        patient = Patient(
            workspace_id=workspace_id,
            first_name="Concurrent",
            phone="01005556667",
            status="active",
            whatsapp_opt_in=True,
        )
        branch = Branch(
            workspace_id=workspace_id,
            name="Main",
            code=f"concurrent-{uuid4().hex[:8]}",
            timezone="Africa/Cairo",
        )
        staff = Staff(
            workspace_id=workspace_id,
            first_name="Concurrent",
            last_name="Doctor",
        )
        service = Service(
            workspace_id=workspace_id,
            name="Concurrent service",
            slug=f"concurrent-service-{uuid4().hex[:8]}",
            duration_minutes=30,
        )
        seed.add_all([patient, branch, staff, service])
        seed.flush()
        doctor = Doctor(workspace_id=workspace_id, staff_id=staff.id)
        seed.add(doctor)
        seed.flush()
        start = now + timedelta(hours=6)
        appointment = Appointment(
            workspace_id=workspace_id,
            patient_id=patient.id,
            branch_id=branch.id,
            doctor_id=doctor.id,
            service_id=service.id,
            status="pending",
            start_at=start,
            end_at=start + timedelta(minutes=30),
            busy_start_at=start,
            busy_end_at=start + timedelta(minutes=30),
            duration_minutes=30,
            created_at=now - timedelta(days=2),
        )
        seed.add(appointment)
        automations.ensure_default_rules(seed, workspace_id)
        seed.commit()
        return workspace_id


def _cleanup_workspace(engine, workspace_id):
    with Session(engine) as cleanup:
        workspace = cleanup.get(Workspace, workspace_id)
        if workspace is not None:
            cleanup.delete(workspace)
            cleanup.commit()


def test_concurrent_planning_dedupe_is_atomic_and_transaction_safe_x20():
    engine = _ci_engine()
    now = datetime.now(UTC).replace(microsecond=0)
    try:
        for _iteration in range(20):
            workspace_id = _seed_candidate(engine, now=now)
            try:
                barrier = Barrier(2)

                def plan_in_session(
                    *, barrier: Barrier = barrier, workspace_id=workspace_id
                ):
                    with Session(engine) as db:
                        barrier.wait(timeout=5)
                        result = automations.plan_automation_jobs(
                            db,
                            workspace_id=workspace_id,
                            now=now,
                        )
                        assert db.scalar(select(1)) == 1
                        return result

                with ThreadPoolExecutor(max_workers=2) as pool:
                    results = list(pool.map(lambda _index: plan_in_session(), range(2)))

                assert sorted(result.planned for result in results) == [0, 1]

                with Session(engine) as verify:
                    reminder_filter = (
                        AutomationJob.workspace_id == workspace_id,
                        AutomationJob.dedupe_key.like(
                            "appointment:%:rule:appointment_reminder_6h"
                        ),
                    )
                    jobs = list(
                        verify.scalars(select(AutomationJob).where(*reminder_filter))
                    )
                    assert len(jobs) == 1
                    first_job_id = jobs[0].id
                    assert (
                        verify.scalar(
                            select(func.count())
                            .select_from(Message)
                            .where(Message.workspace_id == workspace_id)
                        )
                        == 0
                    )
                    assert (
                        verify.scalar(
                            select(func.count())
                            .select_from(MessageDispatch)
                            .where(MessageDispatch.workspace_id == workspace_id)
                        )
                        == 0
                    )

                    replanned = automations.plan_automation_jobs(
                        verify,
                        workspace_id=workspace_id,
                        now=now,
                    )
                    assert replanned.planned == 0
                    jobs_after_replan = list(
                        verify.scalars(select(AutomationJob).where(*reminder_filter))
                    )
                    assert [job.id for job in jobs_after_replan] == [first_job_id]
                    assert verify.scalar(select(1)) == 1
            finally:
                _cleanup_workspace(engine, workspace_id)
    finally:
        engine.dispose()
