"""Functional release gate on migrated CI PostgreSQL or explicitly pinned Staging.

Every service commit stays inside an outer transaction which is rolled back.
HTTP is blocked by default; provider tests inject only controlled responses.
This is engine/DB evidence, never live Meta or deployed Staging evidence.
"""

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import create_engine, event, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.meta_whatsapp_config import meta_whatsapp_settings
from app.models.appointment import Appointment
from app.models.automation_job import AutomationJob
from app.models.branch import Branch
from app.models.channel_connection import ChannelConnection
from app.models.channel_delivery_event import ChannelDeliveryEvent
from app.models.channel_identity import ChannelIdentity
from app.models.doctor import Doctor
from app.models.message import Message
from app.models.message_dispatch import MessageDispatch
from app.models.patient import Patient
from app.models.service import Service
from app.models.staff import Staff
from app.models.workspace import Workspace
from app.services import automations, channels
from app.services import meta_whatsapp_transport as transport


@pytest.fixture
def case(monkeypatch):
    url = make_url(os.environ["DATABASE_URL"])
    staging_ref = "ycuxjlkhnubztgqmhtom"
    staging = os.environ.get("AUTOMATION_GATE_STAGING_PROJECT") == staging_ref
    if staging:
        direct = url.host == f"db.{staging_ref}.supabase.co"
        pooled = (url.host or "").endswith(
            ".pooler.supabase.com"
        ) and url.username == f"postgres.{staging_ref}"
        if not (direct or pooled) or url.database != "postgres":
            pytest.fail("Staging gate DB URL must identify the approved Staging project.")
    elif url.host not in {"localhost", "127.0.0.1", "::1"} or url.database != "ci_db":
        pytest.fail("Automation DB gate requires disposable local ci_db.")
    if settings.environment != "test":
        pytest.fail("Automation DB gate requires ENVIRONMENT=test.")

    def block_http(*args, **kwargs):
        raise AssertionError("External HTTP is forbidden in the automation DB gate")

    monkeypatch.setattr(httpx.Client, "send", block_http)
    monkeypatch.setattr(httpx.AsyncClient, "send", block_http)
    monkeypatch.setattr(settings, "demo_mode", False)
    monkeypatch.setattr(settings, "channel_dispatch_max_attempts", 3)
    monkeypatch.setattr(meta_whatsapp_settings, "meta_graph_api_version", "v23.0")
    engine = create_engine(url, connect_args={"connect_timeout": 3})
    try:
        conn = engine.connect()
    except Exception:
        engine.dispose()
        if os.environ.get("CI") or staging:
            raise
        pytest.skip("Start disposable local ci_db and apply Alembic migrations")
    outer = conn.begin()
    db = Session(bind=conn, join_transaction_mode="create_savepoint")
    try:
        now = datetime.now(UTC).replace(microsecond=0)
        workspace = Workspace(name="Automation gate", slug=f"gate-{uuid4()}", timezone="UTC")
        db.add(workspace)
        db.flush()
        workspace_id = workspace.id
        patient = Patient(
            workspace_id=workspace.id,
            first_name="Gate",
            phone="01001112223",
            status="active",
            whatsapp_opt_in=True,
        )
        branch = Branch(
            workspace_id=workspace.id, name="Cairo", code="gate", timezone="Africa/Cairo"
        )
        staff = Staff(workspace_id=workspace.id, first_name="Gate", last_name="Doctor")
        service = Service(
            workspace_id=workspace.id, name="Gate service", slug="gate", duration_minutes=30
        )
        connection = ChannelConnection(
            workspace_id=workspace.id,
            channel="whatsapp",
            provider="meta_cloud",
            display_name="Gate",
            external_account_id=str(uuid4()),
            adapter_token_hash=uuid4().hex + uuid4().hex,
            config_json={"runtime_kind": "real"},
        )
        db.add_all([patient, branch, staff, service, connection])
        db.flush()
        doctor = Doctor(workspace_id=workspace.id, staff_id=staff.id)
        db.add(doctor)
        db.flush()
        start = now + timedelta(hours=6)
        appointment = Appointment(
            workspace_id=workspace.id,
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
        db.add(appointment)
        rules = automations.ensure_default_rules(db, workspace.id)
        rule = next(r for r in rules if r.key == "appointment_reminder_6h")
        yield SimpleNamespace(
            db=db,
            now=now,
            workspace=workspace,
            patient=patient,
            appointment=appointment,
            connection=connection,
            rule=rule,
            rules=rules,
        )
    finally:
        db.close()
        outer.rollback()
        try:
            if "workspace_id" in locals():
                assert conn.scalar(select(Workspace.id).where(Workspace.id == workspace_id)) is None
        finally:
            conn.close()
            engine.dispose()


def plan(case):
    automations.plan_automation_jobs(case.db, workspace_id=case.workspace.id, now=case.now)
    return case.db.scalar(
        select(AutomationJob).where(AutomationJob.workspace_id == case.workspace.id)
    )


def execute(case):
    job = plan(case)
    claimed = automations.claim_due_jobs(
        case.db, workspace_id=case.workspace.id, limit=10, now=case.now
    )
    assert [c.job_id for c in claimed] == [job.id]
    result = automations.execute_job(
        case.db, workspace_id=case.workspace.id, job_id=job.id, now=case.now
    )
    return result.job


def claim(case):
    return channels.claim_dispatches(case.db, connection=case.connection, limit=10)


def record(case, dispatch_id, status="sent", provider_id="wamid.gate", retry=None, metadata=None):
    return channels.record_dispatch_result(
        case.db,
        connection=case.connection,
        dispatch_id=dispatch_id,
        result_status=status,
        provider_message_id=provider_id,
        error="controlled failure" if status == "failed" else None,
        retry_after_seconds=retry,
        metadata=metadata or {},
    )


def webhook(case, status, timestamp=None, provider_id="wamid.gate"):
    return transport.ingest_meta_webhook(
        case.db,
        {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "metadata": {
                                    "phone_number_id": case.connection.external_account_id
                                },
                                "statuses": [
                                    {
                                        "id": provider_id,
                                        "status": status,
                                        "timestamp": str(timestamp or int(case.now.timestamp())),
                                        "recipient_id": "201001112223",
                                    }
                                ],
                            }
                        }
                    ]
                }
            ]
        },
    )


def test_reminder_plan_claim_execute_payload_and_dedupe(case):
    job = execute(case)
    assert job.scheduled_for == case.now
    assert plan(case).id == job.id
    assert (
        automations.claim_due_jobs(case.db, workspace_id=case.workspace.id, limit=10, now=case.now)
        == []
    )
    repeated = automations.execute_job(
        case.db, workspace_id=case.workspace.id, job_id=job.id, now=case.now
    )
    assert repeated.job.dispatch_id == job.dispatch_id
    assert (
        case.db.scalar(
            select(func.count())
            .select_from(MessageDispatch)
            .where(MessageDispatch.workspace_id == case.workspace.id)
        )
        == 1
    )
    (item,) = claim(case)
    assert item.external_user_id == "201001112223"
    assert (
        case.db.scalar(
            select(ChannelIdentity).where(ChannelIdentity.workspace_id == case.workspace.id)
        ).patient_id
        == case.patient.id
    )
    display = item.metadata["appointment"]
    assert display["timezone"] == "Africa/Cairo"  # branch overrides workspace UTC
    from zoneinfo import ZoneInfo

    expected_time = case.appointment.start_at.astimezone(ZoneInfo("Africa/Cairo")).strftime("%H:%M")
    body = transport.build_meta_message_payload(item)
    assert body["to"] == "201001112223"
    assert body["template"]["name"] == "tia_appointment_confirmation_01"
    components = body["template"]["components"]
    params = components[0]["parameters"]
    expected_date = case.appointment.start_at.astimezone(ZoneInfo("Africa/Cairo")).strftime("%d/%m/%Y")
    assert [p["text"] for p in params] == ["Gate", "Gate service", expected_date, expected_time]
    buttons = [component for component in components if component["type"] == "button"]
    assert [button["parameters"][0]["payload"] for button in buttons] == [
        f"tia.booking.confirm:{case.appointment.id}",
        f"tia.booking.reschedule:{case.appointment.id}",
    ]
    assert claim(case) == []


def test_post_visit_follows_completion_anchor(case):
    case.rule.enabled = False
    followup = next(r for r in case.rules if r.key == "post_visit_followup")
    followup.enabled = True
    case.appointment.status = "completed"
    case.appointment.completed_at = case.now - timedelta(days=1)
    case.db.commit()
    job = execute(case)
    assert job.scheduled_for == case.now
    (item,) = claim(case)
    assert item.metadata["whatsapp_template"]["name"] == "tia_post_visit_01"


@pytest.mark.parametrize(
    "status,opt_in,reason",
    [
        ("blocked", True, "patient_not_active"),
        ("inactive", True, "patient_not_active"),
        ("active", False, "whatsapp_opt_in_required"),
    ],
)
def test_ineligible_patient_does_not_create_dispatch(case, status, opt_in, reason):
    case.patient.status = status
    case.patient.whatsapp_opt_in = opt_in
    case.db.commit()
    job = execute(case)
    assert job.status == "skipped"
    assert job.result_json["reason"] == reason
    assert (
        case.db.scalar(
            select(func.count())
            .select_from(MessageDispatch)
            .where(MessageDispatch.workspace_id == case.workspace.id)
        )
        == 0
    )


@pytest.mark.parametrize("change", ["reschedule", "cancel", "disable"])
def test_lifecycle_invalidates_queued_delivery(case, change):
    job = execute(case)
    old_dispatch = case.db.get(MessageDispatch, job.dispatch_id)
    old_message = case.db.get(Message, job.message_id)
    if change == "reschedule":
        for field in ("start_at", "end_at", "busy_start_at", "busy_end_at"):
            setattr(case.appointment, field, getattr(case.appointment, field) + timedelta(days=1))
    elif change == "cancel":
        case.appointment.status = "cancelled"
        case.appointment.cancelled_at = case.now
    else:
        case.rule.enabled = False
    case.db.commit()
    plan(case)
    assert old_dispatch.status == "cancelled"
    assert old_message.delivery_status == "cancelled"
    assert claim(case) == []
    assert job.status == ("queued" if change == "reschedule" else "cancelled")
    if change == "reschedule":
        assert job.scheduled_for == case.now + timedelta(days=1)
    if change == "disable":
        case.rule.enabled = True
        case.db.commit()
        replanned = execute(case)
        assert replanned.id == job.id
        assert replanned.dispatch_id != old_dispatch.id
        assert len(claim(case)) == 1


def test_overdue_dispatch_expires_before_provider_claim(case):
    job = execute(case)
    assert (
        transport._cancel_expired_automation_dispatches(
            case.db, connection=case.connection, now=case.now + timedelta(minutes=31)
        )
        == 1
    )
    assert job.status == "cancelled"
    assert claim(case) == []


def test_engine_reclaims_expired_lease_once(case):
    job = plan(case)
    assert (
        len(
            automations.claim_due_jobs(
                case.db, workspace_id=case.workspace.id, limit=10, now=case.now
            )
        )
        == 1
    )
    assert (
        automations.claim_due_jobs(case.db, workspace_id=case.workspace.id, limit=10, now=case.now)
        == []
    )
    reclaimed = automations.claim_due_jobs(
        case.db, workspace_id=case.workspace.id, limit=10, now=case.now + timedelta(minutes=11)
    )
    assert [(j.job_id, j.attempt) for j in reclaimed] == [(job.id, 2)]


def test_dispatch_lease_recovery_and_max_attempts(case):
    job = execute(case)
    (item,) = claim(case)
    dispatch = case.db.get(MessageDispatch, item.dispatch_id)
    dispatch.locked_at = case.now - timedelta(hours=1)
    case.db.commit()
    (recovered,) = claim(case)
    assert recovered.dispatch_id == item.dispatch_id
    assert recovered.attempt == 2
    dispatch.attempts = settings.channel_dispatch_max_attempts
    dispatch.locked_at = case.now - timedelta(hours=1)
    case.db.commit()
    assert claim(case) == []
    assert dispatch.status == "failed"
    assert case.db.get(Message, job.message_id).delivery_status == "failed"


def test_demo_workspace_does_not_claim_or_mutate_queue(case):
    case.workspace.is_demo = True
    case.db.commit()
    job = execute(case)
    assert claim(case) == []
    dispatch = case.db.get(MessageDispatch, job.dispatch_id)
    assert (dispatch.status, dispatch.attempts, dispatch.locked_at) == ("queued", 0, None)


def test_production_workspace_dispatch_ignores_legacy_global_demo_switch(case, monkeypatch):
    case.workspace.is_demo = False
    case.db.commit()
    job = execute(case)
    monkeypatch.setattr(settings, "demo_mode", True)
    monkeypatch.setattr(settings, "demo_allow_external_dispatch", False)
    (item,) = claim(case)
    assert item.dispatch_id == job.dispatch_id


def test_new_workspace_defaults_to_production_behavior(case):
    workspace = Workspace(name="New clinic", slug=f"new-clinic-{uuid4()}", timezone="UTC")
    case.db.add(workspace)
    case.db.flush()
    case.db.refresh(workspace)
    assert workspace.is_demo is False


def test_renaming_demo_workspace_does_not_enable_dispatch(case):
    case.workspace.is_demo = True
    case.workspace.slug = f"renamed-demo-{uuid4()}"
    case.db.commit()
    job = execute(case)
    assert claim(case) == []
    dispatch = case.db.get(MessageDispatch, job.dispatch_id)
    assert dispatch.status == "queued"


def test_transient_failure_waits_then_retries_same_dispatch(case):
    job = execute(case)
    claim(case)
    before = datetime.now(UTC)
    dispatch = record(case, job.dispatch_id, status="failed", provider_id=None, retry=60)
    assert dispatch.status == "queued"
    assert (
        before + timedelta(seconds=60)
        <= dispatch.next_attempt_at
        <= datetime.now(UTC) + timedelta(seconds=60)
    )
    assert dispatch.locked_at is None
    assert case.db.get(Message, job.message_id).delivery_status == "queued"
    assert claim(case) == []
    dispatch.next_attempt_at = case.now - timedelta(seconds=1)
    case.db.commit()
    (item,) = claim(case)
    assert (item.dispatch_id, item.attempt) == (job.dispatch_id, 2)
    assert record(case, job.dispatch_id).status == "sent"
    assert claim(case) == []


@pytest.mark.parametrize("failure", ["permanent", "cap"])
def test_permanent_failure_or_exhaustion_is_terminal(case, failure):
    job = execute(case)
    claim(case)
    dispatch = case.db.get(MessageDispatch, job.dispatch_id)
    metadata = (
        {"errors": [{"code": 131031, "title": "Business Account locked"}]}
        if failure == "permanent"
        else {}
    )
    if failure == "cap":
        dispatch.attempts = settings.channel_dispatch_max_attempts
        case.db.commit()
    record(case, job.dispatch_id, status="failed", provider_id=None, retry=60, metadata=metadata)
    assert dispatch.status == "failed"
    assert dispatch.next_attempt_at is None
    assert dispatch.locked_at is None
    assert case.db.get(Message, job.message_id).delivery_status == "failed"
    assert claim(case) == []
    if failure == "permanent":
        assert case.connection.status == "paused"
    else:
        assert dispatch.metadata_json["retry_exhausted"] is True


def test_provider_id_persists_and_cannot_be_remapped(case):
    job = execute(case)
    claim(case)
    dispatch = record(case, job.dispatch_id)
    assert dispatch.provider_message_id == "wamid.gate"
    assert case.db.get(Message, job.message_id).external_message_id == "wamid.gate"
    assert record(case, job.dispatch_id).id == dispatch.id
    with pytest.raises(channels.ChannelConflictError):
        record(case, job.dispatch_id, provider_id="wamid.other")
    assert dispatch.provider_message_id == "wamid.gate"


def test_webhooks_update_db_and_audit_without_duplicates_or_downgrades(case):
    job = execute(case)
    claim(case)
    dispatch = record(case, job.dispatch_id)
    webhook(case, "delivered")
    webhook(case, "delivered")
    assert (
        case.db.scalar(
            select(func.count())
            .select_from(ChannelDeliveryEvent)
            .where(ChannelDeliveryEvent.workspace_id == case.workspace.id)
        )
        == 1
    )
    assert dispatch.status == "delivered"
    webhook(case, "read")
    webhook(case, "sent")
    webhook(case, "failed")
    assert dispatch.status == "read"
    assert case.db.get(Message, job.message_id).delivery_status == "read"
    assert dispatch.read_at == case.now
    events = list(
        case.db.scalars(
            select(ChannelDeliveryEvent).where(
                ChannelDeliveryEvent.workspace_id == case.workspace.id
            )
        )
    )
    assert len(events) == 4
    assert all(e.processed_at and e.provider_message_id == "wamid.gate" for e in events)
    assert all(e.payload_json["metadata"]["recipient_id"] == "201001112223" for e in events)


def test_webhook_before_send_result_is_reconciled(case):
    job = execute(case)
    claim(case)
    webhook(case, "read")
    event = case.db.scalar(
        select(ChannelDeliveryEvent).where(ChannelDeliveryEvent.workspace_id == case.workspace.id)
    )
    assert event.processed_at is None
    dispatch = record(case, job.dispatch_id)
    assert dispatch.status == "read"
    case.db.refresh(event)
    assert event.processed_at is not None
    webhook(case, "read")
    assert (
        case.db.scalar(
            select(func.count())
            .select_from(ChannelDeliveryEvent)
            .where(ChannelDeliveryEvent.workspace_id == case.workspace.id)
        )
        == 1
    )


@pytest.mark.parametrize("outcome", ["success", "transient", "permanent", "timeout"])
def test_native_transport_records_controlled_provider_response(case, monkeypatch, outcome):
    job = execute(case)
    (item,) = claim(case)
    calls = []

    def fake_post(url, **kwargs):
        calls.append(kwargs["json"])
        if outcome == "timeout":
            raise httpx.ConnectTimeout("controlled")
        if outcome == "success":
            return httpx.Response(200, json={"messages": [{"id": "wamid.gate"}]})
        code = 131031 if outcome == "permanent" else 130429
        return httpx.Response(
            400 if outcome == "permanent" else 429,
            json={"error": {"code": code, "message": "controlled"}},
        )

    monkeypatch.setattr(transport.httpx, "post", fake_post)
    accepted = transport._send_claimed_dispatch(
        case.db, connection=case.connection, token="test-only", item=item
    )
    assert accepted is (outcome == "success")
    assert len(calls) == 1
    assert calls[0]["to"] == "201001112223"
    dispatch = case.db.get(MessageDispatch, job.dispatch_id)
    expected = {
        "success": "sent",
        "transient": "queued",
        "permanent": "failed",
        "timeout": "queued",
    }[outcome]
    assert dispatch.status == expected
    assert case.db.get(Message, job.message_id).delivery_status == expected
    if outcome == "success":
        assert dispatch.provider_message_id == "wamid.gate"
        webhook(case, "delivered")
        assert dispatch.status == "delivered"


def test_scheduler_preflight_skips_when_no_planning_inputs(case):
    for rule in case.rules:
        rule.enabled = False
    case.db.delete(case.appointment)
    case.db.commit()

    assert automations.automation_planning_may_have_work(
        case.db,
        workspace_id=case.workspace.id,
        rules=case.rules,
        planning_horizon_days=14,
        now=case.now,
    ) is False


def test_scheduler_preflight_skips_active_rule_without_candidate(case):
    case.appointment.start_at = case.now + timedelta(days=30)
    case.appointment.end_at = case.appointment.start_at + timedelta(minutes=30)
    case.appointment.busy_start_at = case.appointment.start_at
    case.appointment.busy_end_at = case.appointment.end_at
    case.db.commit()

    assert automations.automation_planning_may_have_work(
        case.db,
        workspace_id=case.workspace.id,
        rules=case.rules,
        planning_horizon_days=14,
        now=case.now,
    ) is False


def test_scheduler_preflight_keeps_due_automation_visible(case):
    assert automations.automation_planning_may_have_work(
        case.db,
        workspace_id=case.workspace.id,
        rules=case.rules,
        planning_horizon_days=14,
        now=case.now,
    ) is True


def test_planner_batches_mixed_existing_and_new_candidates_idempotently(case):
    first = plan(case)
    second_start = case.appointment.start_at + timedelta(hours=1)
    second = Appointment(
        workspace_id=case.workspace.id,
        patient_id=case.patient.id,
        branch_id=case.appointment.branch_id,
        doctor_id=case.appointment.doctor_id,
        service_id=case.appointment.service_id,
        status="pending",
        start_at=second_start,
        end_at=second_start + timedelta(minutes=30),
        busy_start_at=second_start,
        busy_end_at=second_start + timedelta(minutes=30),
        duration_minutes=30,
        created_at=case.now - timedelta(days=2),
    )
    case.db.add(second)
    case.db.commit()

    automations.plan_automation_jobs(
        case.db,
        workspace_id=case.workspace.id,
        now=case.now,
        rules=case.rules,
    )
    jobs = list(
        case.db.scalars(
            select(AutomationJob)
            .where(AutomationJob.workspace_id == case.workspace.id)
            .order_by(AutomationJob.scheduled_for)
        )
    )
    assert len(jobs) == 2
    assert first.id in {job.id for job in jobs}

    automations.plan_automation_jobs(
        case.db,
        workspace_id=case.workspace.id,
        now=case.now,
        rules=case.rules,
    )
    assert (
        case.db.scalar(
            select(func.count())
            .select_from(AutomationJob)
            .where(AutomationJob.workspace_id == case.workspace.id)
        )
        == 2
    )


def test_disabled_rule_is_not_a_preflight_candidate(case):
    case.rule.enabled = False
    case.db.commit()
    assert automations.automation_planning_may_have_work(
        case.db,
        workspace_id=case.workspace.id,
        rules=case.rules,
        planning_horizon_days=14,
        now=case.now,
    ) is False


def _add_scheduler_benchmark_workspace(case, index: int, *, appointment_count: int = 10):
    workspace = Workspace(
        name=f"Scheduler benchmark {index}",
        slug=f"scheduler-benchmark-{index}-{uuid4()}",
        timezone="Africa/Cairo",
    )
    case.db.add(workspace)
    case.db.flush()
    patient = Patient(
        workspace_id=workspace.id,
        first_name=f"Benchmark {index}",
        phone=f"0109{index:07d}",
        status="active",
        whatsapp_opt_in=True,
    )
    branch = Branch(
        workspace_id=workspace.id,
        name="Main",
        code=f"bench-{index}",
        timezone="Africa/Cairo",
    )
    staff = Staff(workspace_id=workspace.id, first_name="Bench", last_name=str(index))
    service = Service(
        workspace_id=workspace.id,
        name="Benchmark service",
        slug=f"benchmark-service-{index}",
        duration_minutes=30,
    )
    case.db.add_all([patient, branch, staff, service])
    case.db.flush()
    doctor = Doctor(workspace_id=workspace.id, staff_id=staff.id)
    case.db.add(doctor)
    case.db.flush()
    for appointment_index in range(appointment_count):
        start = case.now + timedelta(hours=8, minutes=appointment_index * 30)
        case.db.add(
            Appointment(
                workspace_id=workspace.id,
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
                created_at=case.now - timedelta(days=2),
            )
        )
    case.db.commit()
    rules = automations.ensure_default_rules(case.db, workspace.id)
    automations.plan_automation_jobs(
        case.db,
        workspace_id=workspace.id,
        now=case.now,
        rules=rules,
    )
    return workspace, rules


def _capture_select_metrics(connection, callback):
    metrics = {"queries": 0, "rows": 0}

    def before_cursor_execute(_conn, _cursor, statement, _params, context, _many):
        context._scheduler_benchmark_select = statement.lstrip().upper().startswith("SELECT")
        if context._scheduler_benchmark_select:
            metrics["queries"] += 1

    def after_cursor_execute(_conn, cursor, _statement, _params, context, _many):
        if getattr(context, "_scheduler_benchmark_select", False) and cursor.rowcount > 0:
            metrics["rows"] += cursor.rowcount

    event.listen(connection, "before_cursor_execute", before_cursor_execute)
    event.listen(connection, "after_cursor_execute", after_cursor_execute)
    try:
        callback()
    finally:
        event.remove(connection, "before_cursor_execute", before_cursor_execute)
        event.remove(connection, "after_cursor_execute", after_cursor_execute)
    return metrics


def test_scheduler_idle_planning_benchmark_reduces_queries_and_row_width(case):
    case.db.expire_on_commit = False
    benchmark = [_add_scheduler_benchmark_workspace(case, index) for index in range(4)]
    horizon = case.now + timedelta(days=14)

    def legacy_idle_probe():
        for workspace, _rules in benchmark:
            workspace_id = workspace.id
            case.db.execute(
                text("SELECT * FROM automation_rules WHERE workspace_id = :workspace_id"),
                {"workspace_id": workspace_id},
            ).all()
            case.db.execute(
                text(
                    "SELECT * FROM crm_tasks WHERE workspace_id = :workspace_id "
                    "AND source = 'system' AND execution_mode = 'ai' "
                    "AND dedupe_key LIKE 'automation:lead-not-booked:%' "
                    "AND status IN ('pending', 'in_progress')"
                ),
                {"workspace_id": workspace_id},
            ).all()
            candidates = case.db.execute(
                text(
                    "SELECT * FROM appointments WHERE workspace_id = :workspace_id "
                    "AND status IN ('pending', 'confirmed') "
                    "AND start_at > :now AND start_at <= :appointment_horizon"
                ),
                {
                    "workspace_id": workspace_id,
                    "now": case.now,
                    "appointment_horizon": horizon + timedelta(hours=6),
                },
            ).mappings().all()
            for appointment in candidates:
                case.db.execute(
                    text(
                        "SELECT * FROM automation_jobs WHERE workspace_id = :workspace_id "
                        "AND dedupe_key = :dedupe_key"
                    ),
                    {
                        "workspace_id": workspace_id,
                        "dedupe_key": (
                            f"appointment:{appointment['id']}:rule:appointment_reminder_6h"
                        ),
                    },
                ).all()
            case.db.execute(
                text(
                    "SELECT j.*, a.* FROM automation_jobs j JOIN appointments a "
                    "ON a.workspace_id = j.workspace_id AND a.id = j.appointment_id "
                    "WHERE j.workspace_id = :workspace_id "
                    "AND j.status IN ('queued', 'failed', 'dispatched')"
                ),
                {"workspace_id": workspace_id},
            ).all()

    before = _capture_select_metrics(case.db.bind, legacy_idle_probe)

    def optimized_idle_cycle():
        for workspace, rules in benchmark:
            assert automations.automation_planning_may_have_work(
                case.db,
                workspace_id=workspace.id,
                rules=rules,
                planning_horizon_days=14,
                now=case.now,
            ) is False

    after = _capture_select_metrics(case.db.bind, optimized_idle_cycle)

    before_bytes = 0.0
    after_bytes = 0.0
    for workspace, _rules in benchmark:
        sizes = case.db.execute(
            text(
                "SELECT "
                "COALESCE((SELECT avg(pg_column_size(r)) FROM automation_rules r "
                "WHERE r.workspace_id = :workspace_id), 0) AS rule_bytes, "
                "COALESCE((SELECT avg(pg_column_size(a)) FROM appointments a "
                "WHERE a.workspace_id = :workspace_id), 0) AS appointment_bytes, "
                "COALESCE((SELECT avg(pg_column_size(j)) FROM automation_jobs j "
                "WHERE j.workspace_id = :workspace_id), 0) AS job_bytes, "
                "COALESCE((SELECT avg(pg_column_size(row(j.dedupe_key, j.status, "
                "j.scheduled_for, j.next_attempt_at, j.locked_at, j.completed_at, "
                "j.last_error, j.result))) FROM automation_jobs j "
                "WHERE j.workspace_id = :workspace_id), 0) AS compact_dedupe_bytes, "
                "COALESCE((SELECT avg(pg_column_size(row(j.rule_id, j.status, "
                "a.status, a.completed_at, a.no_show_at, a.cancelled_at))) "
                "FROM automation_jobs j JOIN appointments a "
                "ON a.workspace_id = j.workspace_id AND a.id = j.appointment_id "
                "WHERE j.workspace_id = :workspace_id), 0) AS compact_eligibility_bytes, "
                "COALESCE((SELECT avg(pg_column_size(row(a.id, a.created_at, a.start_at, "
                "a.completed_at, a.no_show_at, a.cancelled_at))) FROM appointments a "
                "WHERE a.workspace_id = :workspace_id), 0) AS compact_candidate_bytes"
            ),
            {"workspace_id": workspace.id},
        ).one()
        rule_bytes = float(sizes.rule_bytes)
        appointment_bytes = float(sizes.appointment_bytes)
        job_bytes = float(sizes.job_bytes)
        compact_dedupe_bytes = float(sizes.compact_dedupe_bytes)
        compact_eligibility_bytes = float(sizes.compact_eligibility_bytes)
        compact_candidate_bytes = float(sizes.compact_candidate_bytes)
        before_bytes += (
            4 * rule_bytes
            + 10 * appointment_bytes
            + 10 * job_bytes
            + 10 * (job_bytes + appointment_bytes)
        )
        after_bytes += (
            10 * compact_eligibility_bytes
            + 10 * compact_candidate_bytes
            + 10 * compact_dedupe_bytes
        )

    print(
        "scheduler_idle_egress_benchmark "
        f"before_queries={before['queries']} before_rows={before['rows']} "
        f"after_queries={after['queries']} after_rows={after['rows']} "
        f"before_estimated_row_bytes={round(before_bytes)} "
        f"after_estimated_row_bytes={round(after_bytes)}"
    )
    assert before == {"queries": 56, "rows": 136}
    assert after == {"queries": 16, "rows": 120}
    assert after_bytes <= before_bytes * 0.6



def test_scheduler_preflight_is_workspace_isolated(case):
    other_workspace, _other_rules = _add_scheduler_benchmark_workspace(
        case, 77, appointment_count=1
    )
    other_job_count = case.db.scalar(
        select(func.count())
        .select_from(AutomationJob)
        .where(AutomationJob.workspace_id == other_workspace.id)
    )
    assert other_job_count == 1

    case.rule.enabled = False
    case.db.delete(case.appointment)
    case.db.commit()

    assert automations.automation_planning_may_have_work(
        case.db,
        workspace_id=case.workspace.id,
        rules=case.rules,
        planning_horizon_days=14,
        now=case.now,
    ) is False


def test_concurrent_planning_keeps_single_dedupe_job(case):
    engine = case.db.bind.engine
    workspace_id = None
    try:
        with Session(engine) as seed:
            workspace = Workspace(
                name="Concurrent scheduler gate",
                slug=f"concurrent-scheduler-{uuid4()}",
                timezone="UTC",
            )
            seed.add(workspace)
            seed.flush()
            workspace_id = workspace.id
            patient = Patient(
                workspace_id=workspace.id,
                first_name="Concurrent",
                phone="01005556667",
                status="active",
                whatsapp_opt_in=True,
            )
            branch = Branch(
                workspace_id=workspace.id,
                name="Main",
                code=f"concurrent-{uuid4().hex[:8]}",
                timezone="Africa/Cairo",
            )
            staff = Staff(
                workspace_id=workspace.id,
                first_name="Concurrent",
                last_name="Doctor",
            )
            service = Service(
                workspace_id=workspace.id,
                name="Concurrent service",
                slug=f"concurrent-service-{uuid4().hex[:8]}",
                duration_minutes=30,
            )
            seed.add_all([patient, branch, staff, service])
            seed.flush()
            doctor = Doctor(workspace_id=workspace.id, staff_id=staff.id)
            seed.add(doctor)
            seed.flush()
            start = case.now + timedelta(hours=6)
            seed.add(
                Appointment(
                    workspace_id=workspace.id,
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
                    created_at=case.now - timedelta(days=2),
                )
            )
            automations.ensure_default_rules(seed, workspace.id)
            seed.commit()

        barrier = Barrier(2)

        def plan_in_session():
            with Session(engine) as db:
                barrier.wait(timeout=5)
                return automations.plan_automation_jobs(
                    db,
                    workspace_id=workspace_id,
                    now=case.now,
                )

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _index: plan_in_session(), range(2)))
        assert len(results) == 2

        with Session(engine) as verify:
            jobs = list(
                verify.scalars(
                    select(AutomationJob).where(
                        AutomationJob.workspace_id == workspace_id,
                        AutomationJob.dedupe_key.like("appointment:%:rule:appointment_reminder_6h"),
                    )
                )
            )
            assert len(jobs) == 1
    finally:
        if workspace_id is not None:
            with Session(engine) as cleanup:
                workspace = cleanup.get(Workspace, workspace_id)
                if workspace is not None:
                    cleanup.delete(workspace)
                    cleanup.commit()
