"""PostgreSQL evidence for the WhatsApp transport idle-egress preflight.

The gate uses only a disposable local ci_db in normal CI.  It never calls Meta.
"""

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, delete, event, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.meta_whatsapp_templates import STANDARD_WHATSAPP_TEMPLATES
from app.models.appointment import Appointment
from app.models.automation_job import AutomationJob
from app.models.automation_rule import AutomationRule
from app.models.branch import Branch
from app.models.channel_connection import ChannelConnection
from app.models.channel_identity import ChannelIdentity
from app.models.channel_inbound_event import ChannelInboundEvent
from app.models.channel_provider_credential import ChannelProviderCredential
from app.models.conversation import Conversation
from app.models.doctor import Doctor
from app.models.message import Message
from app.models.message_dispatch import MessageDispatch
from app.models.patient import Patient
from app.models.service import Service
from app.models.staff import Staff
from app.models.workspace import Workspace
from app.services import channels
from app.services import meta_whatsapp_transport as transport
from app.services.conversation_ownership import DISPATCH_SEND_LEASE


def _gate_url():
    return make_url(os.environ.get("TRANSPORT_GATE_DATABASE_URL", os.environ["DATABASE_URL"]))


def _guard_disposable_db() -> None:
    url = _gate_url()
    if url.host not in {"localhost", "127.0.0.1", "::1"} or url.database != "ci_db":
        pytest.fail("WhatsApp transport DB gate requires disposable local ci_db.")
    if settings.environment != "test":
        pytest.fail("WhatsApp transport DB gate requires ENVIRONMENT=test.")


def _ready_config(now: datetime) -> dict:
    return {
        "transport_ready": True,
        "template_statuses": {template.name: "approved" for template in STANDARD_WHATSAPP_TEMPLATES},
        "provider_health": {"last_checked_at": now.isoformat()},
    }


@pytest.fixture
def transport_case(monkeypatch):
    _guard_disposable_db()
    engine = create_engine(_gate_url(), connect_args={"connect_timeout": 3})
    try:
        connection = engine.connect()
    except Exception:
        engine.dispose()
        if os.environ.get("CI"):
            raise
        pytest.skip("Start disposable local ci_db and apply Alembic migrations")
    outer = connection.begin()
    db = Session(bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False)
    now = datetime.now(UTC).replace(microsecond=0)
    workspace = Workspace(name="Transport egress", slug=f"transport-egress-{uuid4()}", timezone="UTC")
    db.add(workspace)
    db.flush()
    patient = Patient(
        workspace_id=workspace.id,
        first_name="Transport",
        status="active",
        whatsapp_opt_in=True,
    )
    channel = ChannelConnection(
        workspace_id=workspace.id,
        channel="whatsapp",
        provider="meta_cloud",
        display_name="Transport",
        status="active",
        external_account_id=f"phone-{uuid4()}",
        adapter_token_hash=uuid4().hex + uuid4().hex,
        config_json=_ready_config(now),
    )
    db.add_all([patient, channel])
    db.flush()
    credential = ChannelProviderCredential(
        channel_connection_id=channel.id,
        workspace_id=workspace.id,
        provider="meta_cloud",
        access_token_ciphertext="encrypted-test-token",
        expires_at=now + timedelta(days=30),
    )
    db.add(credential)
    db.commit()

    def forbid_http(*_args, **_kwargs):
        raise AssertionError("External provider HTTP is forbidden in transport DB tests")

    monkeypatch.setattr(transport.httpx, "get", forbid_http)
    monkeypatch.setattr(transport.httpx, "post", forbid_http)
    try:
        yield SimpleNamespace(
            engine=engine,
            connection=connection,
            db=db,
            now=now,
            workspace=workspace,
            patient=patient,
            channel=channel,
            credential=credential,
        )
    finally:
        db.close()
        outer.rollback()
        connection.close()
        engine.dispose()


def _fresh_session(case) -> Session:
    return Session(bind=case.connection, join_transaction_mode="create_savepoint")


def _capture_select_metrics(bind, callback):
    metrics = {"queries": 0, "rows": 0}

    def before_cursor_execute(_conn, _cursor, statement, _params, context, _many):
        context._transport_egress_select = statement.lstrip().upper().startswith("SELECT")
        if context._transport_egress_select:
            metrics["queries"] += 1

    def after_cursor_execute(_conn, cursor, _statement, _params, context, _many):
        if getattr(context, "_transport_egress_select", False) and cursor.rowcount > 0:
            metrics["rows"] += cursor.rowcount

    event.listen(bind, "before_cursor_execute", before_cursor_execute)
    event.listen(bind, "after_cursor_execute", after_cursor_execute)
    try:
        callback()
    finally:
        event.remove(bind, "before_cursor_execute", before_cursor_execute)
        event.remove(bind, "after_cursor_execute", after_cursor_execute)
    return metrics


def _add_conversation(case, *, suffix: str) -> Conversation:
    conversation = Conversation(
        workspace_id=case.workspace.id,
        patient_id=case.patient.id,
        channel="whatsapp",
        channel_connection_id=case.channel.id,
        external_conversation_id=f"wa-{suffix}-{uuid4()}",
        status="open",
        owner_type="ai",
        unread_count=0,
        started_at=case.now,
        last_message_at=case.now,
    )
    case.db.add(conversation)
    case.db.flush()
    return conversation


def _add_inbound_marker(case) -> ChannelInboundEvent:
    conversation = _add_conversation(case, suffix="inbound")
    message = Message(
        workspace_id=case.workspace.id,
        conversation_id=conversation.id,
        channel_connection_id=case.channel.id,
        sender_type="patient",
        direction="inbound",
        message_type="text",
        content="hello",
        external_message_id=f"wamid.{uuid4()}",
        delivery_status="received",
    )
    case.db.add(message)
    case.db.flush()
    inbound = ChannelInboundEvent(
        workspace_id=case.workspace.id,
        channel_connection_id=case.channel.id,
        message_id=message.id,
        external_event_id=f"message:{message.external_message_id}",
        status="received",
        attempts=0,
        payload_json={},
    )
    case.db.add(inbound)
    case.db.commit()
    return inbound


def _add_outbound_dispatch(
    case,
    *,
    status: str = "queued",
    attempts: int = 0,
    locked_at: datetime | None = None,
    next_attempt_at: datetime | None = None,
) -> MessageDispatch:
    conversation = _add_conversation(case, suffix="outbound")
    identity = ChannelIdentity(
        workspace_id=case.workspace.id,
        channel_connection_id=case.channel.id,
        patient_id=case.patient.id,
        external_user_id=f"2010{uuid4().int % 10**8:08d}",
        metadata_json={},
    )
    message = Message(
        workspace_id=case.workspace.id,
        conversation_id=conversation.id,
        channel_connection_id=case.channel.id,
        sender_type="staff",
        direction="outbound",
        message_type="text",
        content="queued transport message",
        delivery_status="queued",
        metadata_json={},
    )
    case.db.add_all([identity, message])
    case.db.flush()
    dispatch = MessageDispatch(
        workspace_id=case.workspace.id,
        channel_connection_id=case.channel.id,
        message_id=message.id,
        status=status,
        attempts=attempts,
        locked_at=locked_at,
        next_attempt_at=next_attempt_at,
        metadata_json={},
    )
    case.db.add(dispatch)
    case.db.commit()
    return dispatch


def _set_demo_mode(case, *, reply_test: bool) -> None:
    case.workspace.is_demo = True
    config = dict(case.channel.config_json or {})
    config["demo_whatsapp_reply_test_enabled"] = reply_test
    case.channel.config_json = config
    case.db.commit()


def _add_demo_dispatch(
    case,
    *,
    reactive: bool,
    status: str = "queued",
    attempts: int = 0,
    locked_at: datetime | None = None,
    next_attempt_at: datetime | None = None,
):
    conversation = _add_conversation(case, suffix="demo-dispatch")
    identity = case.db.scalar(
        select(ChannelIdentity).where(
            ChannelIdentity.workspace_id == case.workspace.id,
            ChannelIdentity.channel_connection_id == case.channel.id,
            ChannelIdentity.patient_id == case.patient.id,
        )
    )
    if identity is None:
        case.db.add(
            ChannelIdentity(
                workspace_id=case.workspace.id,
                channel_connection_id=case.channel.id,
                patient_id=case.patient.id,
                external_user_id=f"2010{uuid4().int % 10**8:08d}",
                metadata_json={},
            )
        )

    inbound = None
    if reactive:
        inbound = Message(
            workspace_id=case.workspace.id,
            conversation_id=conversation.id,
            channel_connection_id=case.channel.id,
            sender_type="patient",
            direction="inbound",
            message_type="text",
            content="customer reply",
            external_message_id=f"wamid.{uuid4()}",
            delivery_status="received",
            metadata_json={},
        )
        case.db.add(inbound)
        case.db.flush()

    outbound = Message(
        workspace_id=case.workspace.id,
        conversation_id=conversation.id,
        channel_connection_id=case.channel.id,
        sender_type="ai",
        direction="outbound",
        message_type="text" if reactive else "template",
        content="reactive reply" if reactive else "proactive follow-up",
        delivery_status="queued",
        in_reply_to_message_id=inbound.id if inbound is not None else None,
        metadata_json=(
            {}
            if reactive
            else {
                "source": "ai_followup",
                "whatsapp_template": {
                    "name": "tia_ai_followup_ar",
                    "language_code": "ar_EG",
                },
            }
        ),
    )
    case.db.add(outbound)
    case.db.flush()
    dispatch = MessageDispatch(
        workspace_id=case.workspace.id,
        channel_connection_id=case.channel.id,
        message_id=outbound.id,
        status=status,
        attempts=attempts,
        locked_at=locked_at,
        next_attempt_at=next_attempt_at,
        metadata_json={},
    )
    case.db.add(dispatch)
    case.db.commit()
    return SimpleNamespace(dispatch=dispatch, message=outbound, inbound=inbound)


def _add_expired_automation_dispatch(case):
    created = _add_demo_dispatch(case, reactive=False)
    branch = Branch(
        workspace_id=case.workspace.id,
        name="P11 branch",
        code=f"p11-{uuid4().hex[:8]}",
        timezone="UTC",
    )
    staff = Staff(
        workspace_id=case.workspace.id,
        first_name="P11",
        last_name="Doctor",
    )
    service = Service(
        workspace_id=case.workspace.id,
        name="P11 service",
        slug=f"p11-{uuid4().hex[:8]}",
        duration_minutes=30,
    )
    case.db.add_all([branch, staff, service])
    case.db.flush()
    doctor = Doctor(workspace_id=case.workspace.id, staff_id=staff.id)
    case.db.add(doctor)
    case.db.flush()
    appointment = Appointment(
        workspace_id=case.workspace.id,
        patient_id=case.patient.id,
        branch_id=branch.id,
        doctor_id=doctor.id,
        service_id=service.id,
        status="confirmed",
        source="staff",
        start_at=case.now + timedelta(hours=1),
        end_at=case.now + timedelta(hours=1, minutes=30),
        busy_start_at=case.now + timedelta(hours=1),
        busy_end_at=case.now + timedelta(hours=1, minutes=30),
        duration_minutes=30,
    )
    rule = AutomationRule(
        workspace_id=case.workspace.id,
        key=f"p11-expiry-{uuid4().hex[:8]}",
        name="P11 expiry",
        enabled=True,
        trigger_kind="before_appointment",
        offset_minutes=-60,
        channel="whatsapp",
        template_name="appointment_reminder",
        template_language="ar",
        max_lateness_minutes=30,
        config_json={},
    )
    case.db.add_all([appointment, rule])
    case.db.flush()
    job = AutomationJob(
        workspace_id=case.workspace.id,
        rule_id=rule.id,
        appointment_id=appointment.id,
        patient_id=case.patient.id,
        job_kind="appointment_rule",
        status="dispatched",
        scheduled_for=case.now - timedelta(minutes=31),
        dedupe_key=f"p11-expiry:{uuid4()}",
        attempts=1,
        message_id=created.message.id,
        dispatch_id=created.dispatch.id,
        payload_json={},
        result_json={},
    )
    case.db.add(job)
    case.db.commit()
    return SimpleNamespace(**created.__dict__, job=job)


def _mock_full_path(monkeypatch, *, inbound=(0, 0), claimed=None, refresh=None):
    calls = {"inbound": 0, "claim": 0, "send": 0, "decrypt": 0, "refresh": 0}

    def process(_db, _connection, *, limit):
        assert limit > 0
        calls["inbound"] += 1
        return inbound

    def decrypt(_db, _connection):
        calls["decrypt"] += 1
        return "test-token", None

    def claim(_db, **_kwargs):
        calls["claim"] += 1
        return list(claimed or [])

    def send(_db, **_kwargs):
        calls["send"] += 1
        return True

    def do_refresh(_db, _connection):
        calls["refresh"] += 1
        return True if refresh is None else refresh

    monkeypatch.setattr(transport, "_process_pending_inbound", process)
    monkeypatch.setattr(transport, "_cancel_expired_automation_dispatches", lambda *_a, **_k: 0)
    monkeypatch.setattr(transport, "_decrypt_connection_token", decrypt)
    monkeypatch.setattr(transport, "claim_dispatches", claim)
    monkeypatch.setattr(transport, "_send_claimed_dispatch", send)
    monkeypatch.setattr(transport, "refresh_meta_connection_readiness", do_refresh)
    return calls


def test_idle_connection_cheap_exit_skips_credential_and_payload_reads(transport_case, monkeypatch):
    monkeypatch.setattr(
        transport,
        "_decrypt_connection_token",
        lambda *_a, **_k: pytest.fail("idle tick must not read/decrypt credential material"),
    )
    monkeypatch.setattr(
        transport,
        "_process_pending_inbound",
        lambda *_a, **_k: pytest.fail("idle tick must not run full inbound scan"),
    )
    result = transport.run_meta_transport_tick(transport_case.db)
    assert result["connections_checked"] == 1
    assert result["connections_ready"] == 1
    assert result["connections_skipped_preflight"] == 1
    assert result["provider_refreshes"] == 0
    assert result["inbound_processed"] == 0
    assert result["sent"] == 0


def test_idle_then_new_inbound_is_seen_on_next_tick(transport_case, monkeypatch):
    calls = _mock_full_path(monkeypatch, inbound=(1, 0))
    first = transport.run_meta_transport_tick(transport_case.db)
    assert first["connections_skipped_preflight"] == 1
    assert calls["inbound"] == 0

    _add_inbound_marker(transport_case)
    second = transport.run_meta_transport_tick(transport_case.db)
    assert second["connections_skipped_preflight"] == 0
    assert second["inbound_processed"] == 1
    assert calls["inbound"] == 1


def test_idle_then_new_outbound_is_seen_and_sent_on_next_tick(transport_case, monkeypatch):
    item = SimpleNamespace(dispatch_id=uuid4())
    calls = _mock_full_path(monkeypatch, claimed=[item])
    first = transport.run_meta_transport_tick(transport_case.db)
    assert first["connections_skipped_preflight"] == 1
    _add_outbound_dispatch(transport_case)
    second = transport.run_meta_transport_tick(transport_case.db)
    assert second["connections_skipped_preflight"] == 0
    assert second["sent"] == 1
    assert calls["claim"] == 1
    assert calls["send"] == 1
    assert calls["decrypt"] == 1


def test_retryable_processing_dispatch_falls_through_and_real_claim_recovers(transport_case):
    dispatch = _add_outbound_dispatch(
        transport_case,
        status="processing",
        attempts=1,
        locked_at=datetime.now(UTC) - DISPATCH_SEND_LEASE - timedelta(seconds=5),
    )
    rows = transport._transport_preflight_rows(transport_case.db, max_connections=25)
    row = next(row for row in rows if row.connection_id == transport_case.channel.id)
    assert row.dispatch_has_work is True

    claimed = channels.claim_dispatches(
        transport_case.db,
        connection=transport_case.channel,
        limit=10,
    )
    assert [item.dispatch_id for item in claimed] == [dispatch.id]
    transport_case.db.refresh(dispatch)
    assert dispatch.status == "processing"
    assert dispatch.attempts == 2


def test_provider_refresh_not_due_stays_on_preflight_and_due_refresh_runs(transport_case, monkeypatch):
    calls = _mock_full_path(monkeypatch)
    stable = transport.run_meta_transport_tick(transport_case.db)
    assert stable["connections_skipped_preflight"] == 1
    assert calls["refresh"] == 0
    assert calls["decrypt"] == 0

    config = dict(transport_case.channel.config_json)
    config["provider_health"] = {
        "last_checked_at": (datetime.now(UTC) - timedelta(minutes=16)).isoformat()
    }
    transport_case.channel.config_json = config
    transport_case.db.commit()
    due = transport.run_meta_transport_tick(transport_case.db)
    assert due["connections_skipped_preflight"] == 0
    assert due["provider_refreshes"] == 1
    assert calls["refresh"] == 1


def test_disconnected_connection_remains_excluded(transport_case):
    transport_case.channel.status = "disconnected"
    transport_case.db.commit()
    result = transport.run_meta_transport_tick(transport_case.db)
    assert result["connections_checked"] == 0
    assert result["connections_skipped_preflight"] == 0


def test_multiple_workspaces_keep_pending_work_isolated(transport_case, monkeypatch):
    other_workspace = Workspace(
        name="Other transport",
        slug=f"other-transport-{uuid4()}",
        timezone="UTC",
    )
    transport_case.db.add(other_workspace)
    transport_case.db.flush()
    other_connection = ChannelConnection(
        workspace_id=other_workspace.id,
        channel="whatsapp",
        provider="meta_cloud",
        display_name="Other",
        status="active",
        external_account_id=f"phone-{uuid4()}",
        adapter_token_hash=uuid4().hex + uuid4().hex,
        config_json=_ready_config(transport_case.now),
    )
    transport_case.db.add(other_connection)
    transport_case.db.commit()
    _add_inbound_marker(transport_case)

    processed_ids = []
    monkeypatch.setattr(
        transport,
        "_process_pending_inbound",
        lambda _db, connection, *, limit: processed_ids.append(connection.id) or (1, 0),
    )
    monkeypatch.setattr(transport, "_cancel_expired_automation_dispatches", lambda *_a, **_k: 0)
    monkeypatch.setattr(transport, "_decrypt_connection_token", lambda *_a, **_k: (None, "no send"))
    result = transport.run_meta_transport_tick(transport_case.db)
    assert result["connections_checked"] == 2
    assert result["connections_skipped_preflight"] == 1
    assert processed_ids == [transport_case.channel.id]
    assert other_connection.id not in processed_ids


def test_repeated_idle_ticks_have_no_side_effects(transport_case, monkeypatch):
    monkeypatch.setattr(
        transport,
        "_decrypt_connection_token",
        lambda *_a, **_k: pytest.fail("repeated idle ticks must not load credentials"),
    )
    before = transport_case.db.execute(
        text(
            "SELECT count(*) FROM channel_inbound_events; "
        )
    ).scalar_one()
    for _ in range(10):
        result = transport.run_meta_transport_tick(transport_case.db)
        assert result["connections_skipped_preflight"] == 1
    after = transport_case.db.scalar(select(text("count(*)")).select_from(MessageDispatch.__table__))
    assert before == 0
    assert after == 0


def _legacy_idle_probe(db: Session, case) -> None:
    params = {"workspace_id": case.workspace.id, "connection_id": case.channel.id}
    db.execute(
        text(
            "SELECT * FROM channel_connections WHERE channel='whatsapp' "
            "AND provider='meta_cloud' AND status IN ('active','paused') ORDER BY created_at LIMIT 25"
        )
    ).all()
    db.execute(text("SELECT * FROM workspaces WHERE id=:workspace_id"), params).all()
    db.execute(
        text(
            "SELECT * FROM channel_inbound_events WHERE workspace_id=:workspace_id "
            "AND channel_connection_id=:connection_id AND status IN ('received','failed') LIMIT 10"
        ),
        params,
    ).all()
    db.execute(
        text(
            "SELECT j.*, r.*, d.*, m.* FROM automation_jobs j "
            "JOIN automation_rules r ON r.id=j.rule_id "
            "JOIN message_dispatches d ON d.id=j.dispatch_id "
            "JOIN messages m ON m.id=j.message_id "
            "WHERE j.workspace_id=:workspace_id AND d.channel_connection_id=:connection_id "
            "AND j.status='dispatched' AND d.status='queued'"
        ),
        params,
    ).all()
    db.execute(
        text("SELECT * FROM channel_provider_credentials WHERE channel_connection_id=:connection_id"),
        params,
    ).all()
    db.execute(
        text(
            "SELECT * FROM message_dispatches WHERE workspace_id=:workspace_id "
            "AND channel_connection_id=:connection_id AND attempts >= 3 "
            "AND status IN ('queued','processing')"
        ),
        params,
    ).all()
    db.execute(
        text(
            "SELECT d.id, m.conversation_id FROM message_dispatches d "
            "JOIN messages m ON m.id=d.message_id WHERE d.workspace_id=:workspace_id "
            "AND d.channel_connection_id=:connection_id AND d.status IN ('queued','processing') LIMIT 40"
        ),
        params,
    ).all()


def test_idle_100_tick_benchmark_reduces_queries_rows_and_row_payload(transport_case):
    def legacy_100():
        for _ in range(100):
            with _fresh_session(transport_case) as db:
                _legacy_idle_probe(db, transport_case)

    before = _capture_select_metrics(transport_case.connection, legacy_100)

    def optimized_100():
        for _ in range(100):
            with _fresh_session(transport_case) as db:
                result = transport.run_meta_transport_tick(db)
                assert result["connections_skipped_preflight"] == 1

    after = _capture_select_metrics(transport_case.connection, optimized_100)

    sizes = transport_case.db.execute(
        text(
            "SELECT pg_column_size(c) AS connection_bytes, pg_column_size(w) AS workspace_bytes, "
            "pg_column_size(pc) AS credential_bytes, "
            "pg_column_size(row(c.id, c.workspace_id, c.status, w.is_demo, "
            "(c.config->>'transport_ready')::boolean, "
            "c.config->'provider_health'->>'last_checked_at', false, "
            "(c.config->>'demo_whatsapp_reply_test_enabled')::boolean, false, false)) "
            "AS compact_preflight_bytes "
            "FROM channel_connections c JOIN workspaces w ON w.id=c.workspace_id "
            "JOIN channel_provider_credentials pc ON pc.channel_connection_id=c.id "
            "WHERE c.id=:connection_id"
        ),
        {"connection_id": transport_case.channel.id},
    ).one()
    before_bytes = int(sizes.connection_bytes + sizes.workspace_bytes + sizes.credential_bytes)
    after_bytes = int(sizes.compact_preflight_bytes)
    print(
        "whatsapp_transport_idle_egress_benchmark "
        f"before_queries={before['queries']} before_rows={before['rows']} "
        f"after_queries={after['queries']} after_rows={after['rows']} "
        f"before_estimated_row_bytes_per_tick={before_bytes} "
        f"after_estimated_row_bytes_per_tick={after_bytes}"
    )
    assert before == {"queries": 700, "rows": 300}
    assert after == {"queries": 100, "rows": 100}
    assert after_bytes <= before_bytes * 0.20


def test_two_workers_cannot_claim_same_dispatch():
    _guard_disposable_db()
    url = _gate_url()
    engine = create_engine(url, connect_args={"connect_timeout": 3})
    workspace_id = None
    try:
        with Session(engine) as db:
            now = datetime.now(UTC).replace(microsecond=0)
            workspace = Workspace(
                name="Transport concurrency",
                slug=f"transport-concurrency-{uuid4()}",
                timezone="UTC",
            )
            db.add(workspace)
            db.flush()
            workspace_id = workspace.id
            patient = Patient(
                workspace_id=workspace.id,
                first_name="Concurrent",
                status="active",
                whatsapp_opt_in=True,
            )
            connection = ChannelConnection(
                workspace_id=workspace.id,
                channel="whatsapp",
                provider="meta_cloud",
                display_name="Concurrent",
                status="active",
                external_account_id=f"phone-{uuid4()}",
                adapter_token_hash=uuid4().hex + uuid4().hex,
                config_json=_ready_config(now),
            )
            db.add_all([patient, connection])
            db.flush()
            conversation = Conversation(
                workspace_id=workspace.id,
                patient_id=patient.id,
                channel="whatsapp",
                channel_connection_id=connection.id,
                external_conversation_id=f"wa-{uuid4()}",
                status="open",
                owner_type="ai",
                unread_count=0,
                started_at=now,
                last_message_at=now,
            )
            identity = ChannelIdentity(
                workspace_id=workspace.id,
                channel_connection_id=connection.id,
                patient_id=patient.id,
                external_user_id="201001112223",
                metadata_json={},
            )
            db.add_all([conversation, identity])
            db.flush()
            message = Message(
                workspace_id=workspace.id,
                conversation_id=conversation.id,
                channel_connection_id=connection.id,
                sender_type="staff",
                direction="outbound",
                message_type="text",
                content="one send only",
                delivery_status="queued",
                metadata_json={},
            )
            db.add(message)
            db.flush()
            dispatch = MessageDispatch(
                workspace_id=workspace.id,
                channel_connection_id=connection.id,
                message_id=message.id,
                status="queued",
                attempts=0,
                metadata_json={},
            )
            db.add(dispatch)
            db.commit()
            connection_id = connection.id
            dispatch_id = dispatch.id

        barrier = Barrier(2)

        def worker():
            with Session(engine) as db:
                connection = db.get(ChannelConnection, connection_id)
                assert connection is not None
                barrier.wait(timeout=5)
                return [
                    item.dispatch_id
                    for item in channels.claim_dispatches(db, connection=connection, limit=1)
                ]

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _index: worker(), range(2)))
        claimed_ids = [dispatch_id for result in results for dispatch_id in result]
        assert claimed_ids == [dispatch_id]
        with Session(engine) as db:
            persisted = db.get(MessageDispatch, dispatch_id)
            assert persisted is not None
            assert persisted.status == "processing"
            assert persisted.attempts == 1
    finally:
        if workspace_id is not None:
            with Session(engine) as db:
                db.execute(delete(MessageDispatch).where(MessageDispatch.workspace_id == workspace_id))
                db.execute(delete(ChannelIdentity).where(ChannelIdentity.workspace_id == workspace_id))
                db.execute(delete(Message).where(Message.workspace_id == workspace_id))
                db.execute(delete(Conversation).where(Conversation.workspace_id == workspace_id))
                db.execute(
                    delete(ChannelProviderCredential).where(
                        ChannelProviderCredential.workspace_id == workspace_id
                    )
                )
                db.execute(delete(ChannelConnection).where(ChannelConnection.workspace_id == workspace_id))
                db.execute(delete(Patient).where(Patient.workspace_id == workspace_id))
                db.execute(delete(Workspace).where(Workspace.id == workspace_id))
                db.commit()
        engine.dispose()

def test_demo_proactive_dispatch_without_reply_test_is_cheap_skip(transport_case, monkeypatch):
    _set_demo_mode(transport_case, reply_test=False)
    _add_demo_dispatch(transport_case, reactive=False)
    monkeypatch.setattr(
        transport,
        "_decrypt_connection_token",
        lambda *_a, **_k: pytest.fail("unactionable demo dispatch must not load credentials"),
    )
    result = transport.run_meta_transport_tick(transport_case.db)
    assert result == {
        "connections_checked": 1,
        "connections_ready": 0,
        "connections_skipped_preflight": 1,
        "provider_refreshes": 0,
        "inbound_processed": 0,
        "inbound_failed": 0,
        "sent": 0,
        "send_failed": 0,
    }


def test_demo_reply_test_proactive_dispatch_is_cheap_skip(transport_case, monkeypatch):
    _set_demo_mode(transport_case, reply_test=True)
    created = _add_demo_dispatch(transport_case, reactive=False)
    monkeypatch.setattr(
        transport,
        "_decrypt_connection_token",
        lambda *_a, **_k: pytest.fail("proactive demo dispatch must stay on preflight"),
    )
    result = transport.run_meta_transport_tick(transport_case.db)
    transport_case.db.refresh(created.dispatch)
    assert result["connections_skipped_preflight"] == 1
    assert result["connections_ready"] == 1
    assert result["sent"] == 0
    assert created.dispatch.status == "queued"
    assert created.dispatch.attempts == 0


def test_demo_reply_test_reactive_dispatch_enters_real_claim_path(transport_case, monkeypatch):
    _set_demo_mode(transport_case, reply_test=True)
    created = _add_demo_dispatch(transport_case, reactive=True)
    sent_ids = []
    monkeypatch.setattr(transport, "_decrypt_connection_token", lambda *_a, **_k: ("token", None))
    monkeypatch.setattr(
        transport,
        "_send_claimed_dispatch",
        lambda _db, *, connection, token, item: sent_ids.append(item.dispatch_id) or True,
    )
    result = transport.run_meta_transport_tick(transport_case.db)
    transport_case.db.refresh(created.dispatch)
    assert result["connections_skipped_preflight"] == 0
    assert result["sent"] == 1
    assert sent_ids == [created.dispatch.id]
    assert created.dispatch.status == "processing"
    assert created.dispatch.attempts == 1


def test_demo_expired_automation_dispatch_still_runs_expiry_maintenance(
    transport_case, monkeypatch
):
    _set_demo_mode(transport_case, reply_test=False)
    created = _add_expired_automation_dispatch(transport_case)
    monkeypatch.setattr(
        transport,
        "_decrypt_connection_token",
        lambda *_a, **_k: pytest.fail("isolated demo expiry maintenance must not decrypt"),
    )
    rows = transport._transport_preflight_rows(
        transport_case.db, max_connections=25, now=transport_case.now
    )
    row = next(row for row in rows if row.connection_id == transport_case.channel.id)
    assert row.dispatch_has_work is True

    result = transport.run_meta_transport_tick(transport_case.db)
    transport_case.db.refresh(created.dispatch)
    transport_case.db.refresh(created.message)
    transport_case.db.refresh(created.job)
    assert result["connections_skipped_preflight"] == 0
    assert result["sent"] == 0
    assert created.dispatch.status == "cancelled"
    assert created.message.delivery_status == "cancelled"
    assert created.job.status == "cancelled"


def test_demo_proactive_exhausted_dispatch_still_runs_failure_maintenance(
    transport_case, monkeypatch
):
    _set_demo_mode(transport_case, reply_test=True)
    created = _add_demo_dispatch(
        transport_case,
        reactive=False,
        attempts=settings.channel_dispatch_max_attempts,
    )
    monkeypatch.setattr(transport, "_decrypt_connection_token", lambda *_a, **_k: ("token", None))
    monkeypatch.setattr(
        transport,
        "_send_claimed_dispatch",
        lambda *_a, **_k: pytest.fail("exhausted proactive demo dispatch must not send"),
    )
    result = transport.run_meta_transport_tick(transport_case.db)
    transport_case.db.refresh(created.dispatch)
    transport_case.db.refresh(created.message)
    assert result["connections_skipped_preflight"] == 0
    assert result["sent"] == 0
    assert created.dispatch.status == "failed"
    assert created.message.delivery_status == "failed"


def test_demo_inbound_pending_overrides_unactionable_proactive_dispatch(
    transport_case, monkeypatch
):
    _set_demo_mode(transport_case, reply_test=True)
    _add_demo_dispatch(transport_case, reactive=False)
    _add_inbound_marker(transport_case)
    monkeypatch.setattr(
        transport, "_process_pending_inbound", lambda *_a, **_k: (1, 0)
    )
    monkeypatch.setattr(transport, "_cancel_expired_automation_dispatches", lambda *_a, **_k: 0)
    monkeypatch.setattr(transport, "_decrypt_connection_token", lambda *_a, **_k: ("token", None))
    monkeypatch.setattr(transport, "_send_claimed_dispatch", lambda *_a, **_k: True)
    result = transport.run_meta_transport_tick(transport_case.db)
    assert result["connections_skipped_preflight"] == 0
    assert result["inbound_processed"] == 1
    assert result["sent"] == 0


def test_unactionable_demo_dispatch_does_not_activate_other_connection(
    transport_case, monkeypatch
):
    _set_demo_mode(transport_case, reply_test=True)
    _add_demo_dispatch(transport_case, reactive=False)
    other_workspace = Workspace(
        name="Unrelated production",
        slug=f"unrelated-{uuid4()}",
        timezone="UTC",
    )
    transport_case.db.add(other_workspace)
    transport_case.db.flush()
    other_connection = ChannelConnection(
        workspace_id=other_workspace.id,
        channel="whatsapp",
        provider="meta_cloud",
        display_name="Other",
        status="active",
        external_account_id=f"phone-{uuid4()}",
        adapter_token_hash=uuid4().hex + uuid4().hex,
        config_json=_ready_config(transport_case.now),
    )
    transport_case.db.add(other_connection)
    transport_case.db.commit()
    monkeypatch.setattr(
        transport,
        "_process_pending_inbound",
        lambda *_a, **_k: pytest.fail("neither connection should enter the full path"),
    )
    result = transport.run_meta_transport_tick(transport_case.db)
    assert result["connections_checked"] == 2
    assert result["connections_skipped_preflight"] == 2


def test_demo_idle_then_reactive_reply_is_seen_on_next_tick(transport_case, monkeypatch):
    _set_demo_mode(transport_case, reply_test=True)
    first = transport.run_meta_transport_tick(transport_case.db)
    assert first["connections_skipped_preflight"] == 1
    created = _add_demo_dispatch(transport_case, reactive=True)
    sent_ids = []
    monkeypatch.setattr(transport, "_decrypt_connection_token", lambda *_a, **_k: ("token", None))
    monkeypatch.setattr(
        transport,
        "_send_claimed_dispatch",
        lambda _db, *, connection, token, item: sent_ids.append(item.dispatch_id) or True,
    )
    second = transport.run_meta_transport_tick(transport_case.db)
    assert second["connections_skipped_preflight"] == 0
    assert second["sent"] == 1
    assert sent_ids == [created.dispatch.id]


def test_persistent_unactionable_demo_dispatch_100_tick_benchmark(
    transport_case, monkeypatch
):
    _set_demo_mode(transport_case, reply_test=True)
    _add_demo_dispatch(transport_case, reactive=False)
    monkeypatch.setattr(
        transport,
        "_decrypt_connection_token",
        lambda *_a, **_k: pytest.fail("benchmark blocker must never load credentials"),
    )

    def optimized_100():
        for _ in range(100):
            with _fresh_session(transport_case) as db:
                result = transport.run_meta_transport_tick(db)
                assert result["connections_skipped_preflight"] == 1
                assert result["sent"] == 0

    after = _capture_select_metrics(transport_case.connection, optimized_100)
    print(
        "whatsapp_transport_p11_demo_blocker_benchmark "
        "current_p1_before_queries=800 current_p1_before_rows=400 "
        f"p11_after_queries={after['queries']} p11_after_rows={after['rows']}"
    )
    assert after == {"queries": 100, "rows": 100}
