import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from alembic import command
from app.models.channel_connection import ChannelConnection
from app.models.channel_inbound_event import ChannelInboundEvent
from app.models.conversation import Conversation
from app.models.message import Message
from app.models.patient import Patient
from app.models.workspace import Workspace
from app.schemas.agent import AgentChatResponse
from app.services import agent_chat, channels
from app.services import meta_whatsapp_transport as transport


@pytest.fixture
def p0_case():
    url = make_url(os.environ["DATABASE_URL"])
    if url.host not in {"localhost", "127.0.0.1", "::1", "host.docker.internal", "tia-p0-postgres"} or url.database != "ci_db":
        pytest.fail("P0 single-flight tests require disposable local ci_db.")
    engine = create_engine(url, pool_pre_ping=True)
    with Session(engine) as db:
        workspace = Workspace(name="P0 gate", slug=f"p0-{uuid4()}", timezone="UTC")
        db.add(workspace)
        db.flush()
        patient = Patient(workspace_id=workspace.id, first_name="P0", status="active")
        connection = ChannelConnection(
            workspace_id=workspace.id,
            channel="whatsapp",
            provider="meta_cloud",
            display_name="P0",
            external_account_id=f"phone-{uuid4()}",
            adapter_token_hash=uuid4().hex + uuid4().hex,
            config_json={"transport_ready": True},
        )
        db.add_all([patient, connection])
        db.flush()
        conversation = Conversation(
            workspace_id=workspace.id,
            patient_id=patient.id,
            channel="whatsapp",
            channel_connection_id=connection.id,
            external_conversation_id=f"conv-{uuid4()}",
            status="open",
            owner_type="ai",
            unread_count=0,
            started_at=datetime.now(UTC),
            last_message_at=datetime.now(UTC),
        )
        db.add(conversation)
        db.flush()
        inbound = Message(
            workspace_id=workspace.id,
            conversation_id=conversation.id,
            channel_connection_id=connection.id,
            sender_type="patient",
            direction="inbound",
            message_type="text",
            content="test inbound",
            delivery_status="received",
            metadata_json={},
        )
        db.add(inbound)
        db.flush()
        event = ChannelInboundEvent(
            workspace_id=workspace.id,
            channel_connection_id=connection.id,
            message_id=inbound.id,
            external_event_id=f"event-{uuid4()}",
            status="received",
            attempts=0,
            payload_json={},
        )
        db.add(event)
        db.commit()
        ids = SimpleNamespace(
            workspace=workspace.id,
            patient=patient.id,
            connection=connection.id,
            conversation=conversation.id,
            inbound=inbound.id,
            event=event.id,
        )
    try:
        yield SimpleNamespace(engine=engine, ids=ids)
    finally:
        with engine.begin() as conn:
            params = {"workspace_id": ids.workspace}
            for table in (
                "message_dispatches", "channel_delivery_events", "channel_inbound_events",
                "channel_identities", "messages", "conversations",
                "channel_connections", "patients",
            ):
                conn.execute(text(f"DELETE FROM {table} WHERE workspace_id = :workspace_id"), params)
            conn.execute(text("DELETE FROM workspaces WHERE id = :workspace_id"), params)
        engine.dispose()


def _connection(db: Session, case):
    return db.get(ChannelConnection, case.ids.connection)


def _expire(db: Session, case) -> None:
    expired_at = datetime.now(UTC) - timedelta(seconds=1)
    event = db.get(ChannelInboundEvent, case.ids.event)
    conversation = db.get(Conversation, case.ids.conversation)
    event.processing_lease_expires_at = expired_at
    conversation.agent_processing_lease_expires_at = expired_at
    db.commit()


def _add_later_inbound(case, *, body: str = "later inbound"):
    with Session(case.engine) as db:
        original = db.get(Message, case.ids.inbound)
        created_at = original.created_at + timedelta(seconds=1)
        inbound = Message(
            workspace_id=original.workspace_id,
            conversation_id=original.conversation_id,
            channel_connection_id=original.channel_connection_id,
            sender_type="patient",
            direction="inbound",
            message_type="text",
            content=body,
            delivery_status="received",
            metadata_json={},
            created_at=created_at,
        )
        db.add(inbound)
        db.flush()
        event = ChannelInboundEvent(
            workspace_id=original.workspace_id,
            channel_connection_id=original.channel_connection_id,
            message_id=inbound.id,
            external_event_id=f"event-{uuid4()}",
            status="received",
            attempts=0,
            payload_json={},
            created_at=created_at,
        )
        db.add(event)
        db.commit()
        return SimpleNamespace(inbound=inbound.id, event=event.id)


def _fake_agent(counter: dict[str, int]):
    def run(*, db, workspace, patient, conversation, inbound, source="channel_adapter"):
        del workspace, source
        counter["calls"] = counter.get("calls", 0) + 1
        patient.first_name = f"P0-{counter['calls']}"
        run_id = uuid4()
        outbound = Message(
            workspace_id=inbound.workspace_id,
            conversation_id=inbound.conversation_id,
            channel_connection_id=inbound.channel_connection_id,
            sender_type="ai",
            direction="outbound",
            in_reply_to_message_id=inbound.id,
            message_type="text",
            content="durable reply",
            delivery_status="queued",
            metadata_json={"agent_run_id": str(run_id), "model": "test"},
        )
        db.add(outbound)
        db.commit()
        return AgentChatResponse(
            run_id=run_id,
            conversation_id=conversation.id,
            inbound_message_id=inbound.id,
            outbound_message_id=outbound.id,
            reply=outbound.content,
            handoff_required=False,
            agent_paused=False,
            model="test",
        )
    return run


def test_later_turn_cannot_claim_before_older_turn(p0_case):
    later = _add_later_inbound(p0_case)
    with Session(p0_case.engine) as db:
        with pytest.raises(channels.ChannelConflictError, match="older inbound turn"):
            channels._claim_inbound_event(
                db, connection=_connection(db, p0_case), event_id=later.event
            )
    with Session(p0_case.engine) as db:
        later_event = db.get(ChannelInboundEvent, later.event)
        conversation = db.get(Conversation, p0_case.ids.conversation)
        assert later_event.attempts == 0
        assert later_event.processing_token is None
        assert conversation.agent_processing_token is None


def test_active_conversation_lease_blocks_later_turn_without_attempt(p0_case):
    later = _add_later_inbound(p0_case)
    with Session(p0_case.engine) as first:
        _, first_token, _ = channels._claim_inbound_event(
            first, connection=_connection(first, p0_case), event_id=p0_case.ids.event
        )
    with Session(p0_case.engine) as second:
        with pytest.raises(channels.ChannelConflictError, match="Conversation already"):
            channels._claim_inbound_event(
                second, connection=_connection(second, p0_case), event_id=later.event
            )
    with Session(p0_case.engine) as db:
        later_event = db.get(ChannelInboundEvent, later.event)
        conversation = db.get(Conversation, p0_case.ids.conversation)
        assert later_event.attempts == 0
        assert later_event.processing_token is None
        assert conversation.agent_processing_token == first_token


def test_active_lease_blocks_second_worker_without_false_completion(p0_case, monkeypatch):
    counter = {}
    monkeypatch.setattr(channels, "run_agent_for_existing_inbound", _fake_agent(counter))
    with Session(p0_case.engine) as a:
        event, token, processed = channels._claim_inbound_event(
            a, connection=_connection(a, p0_case), event_id=p0_case.ids.event
        )
        assert token and processed is None and event.status == "received"
    with Session(p0_case.engine) as b:
        with pytest.raises(channels.ChannelConflictError):
            channels.process_inbound_event(
                b, connection=_connection(b, p0_case), event_id=p0_case.ids.event
            )
    with Session(p0_case.engine) as db:
        event = db.get(ChannelInboundEvent, p0_case.ids.event)
        assert counter.get("calls", 0) == 0
        assert event.status != "processed"
        assert event.processing_token == token


def test_expired_lease_is_reclaimable(p0_case):
    with Session(p0_case.engine) as a:
        _, token_a, _ = channels._claim_inbound_event(
            a, connection=_connection(a, p0_case), event_id=p0_case.ids.event
        )
    with Session(p0_case.engine) as db:
        _expire(db, p0_case)
    with Session(p0_case.engine) as b:
        event, token_b, _ = channels._claim_inbound_event(
            b, connection=_connection(b, p0_case), event_id=p0_case.ids.event
        )
        assert token_b != token_a
        assert event.attempts == 2


def test_expired_crash_lease_is_reclaimable_at_retry_budget(p0_case):
    with Session(p0_case.engine) as first:
        _, token_a, _ = channels._claim_inbound_event(
            first, connection=_connection(first, p0_case), event_id=p0_case.ids.event
        )
    with Session(p0_case.engine) as admin:
        event = admin.get(ChannelInboundEvent, p0_case.ids.event)
        event.attempts = channels.MAX_INBOUND_PROCESS_ATTEMPTS
        _expire(admin, p0_case)
    with Session(p0_case.engine) as recovered:
        event, token_b, processed = channels._claim_inbound_event(
            recovered,
            connection=_connection(recovered, p0_case),
            event_id=p0_case.ids.event,
        )
        assert processed is None
        assert token_b != token_a
        assert event.attempts == channels.MAX_INBOUND_PROCESS_ATTEMPTS + 1


def test_stale_owner_cannot_commit_after_reclaim(p0_case):
    with Session(p0_case.engine) as a:
        _, token_a, _ = channels._claim_inbound_event(
            a, connection=_connection(a, p0_case), event_id=p0_case.ids.event
        )
        with Session(p0_case.engine) as admin:
            _expire(admin, p0_case)
        with Session(p0_case.engine) as b:
            _, token_b, _ = channels._claim_inbound_event(
                b, connection=_connection(b, p0_case), event_id=p0_case.ids.event
            )
        inbound = a.get(Message, p0_case.ids.inbound)
        stale = Message(
            workspace_id=inbound.workspace_id,
            conversation_id=inbound.conversation_id,
            channel_connection_id=inbound.channel_connection_id,
            sender_type="ai",
            direction="outbound",
            in_reply_to_message_id=inbound.id,
            message_type="text",
            content="stale owner reply",
            delivery_status="queued",
            metadata_json={},
        )
        a.add(stale)
        with pytest.raises(channels.InboundProcessingLeaseLost):
            with channels._fence_inbound_commits(a, event_id=p0_case.ids.event, token=token_a):
                a.commit()
        a.rollback()
    with Session(p0_case.engine) as db:
        assert db.get(ChannelInboundEvent, p0_case.ids.event).processing_token == token_b
        assert db.scalar(select(func.count()).select_from(Message).where(Message.content == "stale owner reply")) == 0


def test_stale_failure_handler_cannot_overwrite_new_owner(p0_case):
    with Session(p0_case.engine) as a:
        _, token_a, _ = channels._claim_inbound_event(
            a, connection=_connection(a, p0_case), event_id=p0_case.ids.event
        )
    with Session(p0_case.engine) as admin:
        _expire(admin, p0_case)
    with Session(p0_case.engine) as b:
        _, token_b, _ = channels._claim_inbound_event(
            b, connection=_connection(b, p0_case), event_id=p0_case.ids.event
        )
    with Session(p0_case.engine) as stale:
        assert channels._mark_inbound_failed_if_owned(
            stale, event_id=p0_case.ids.event, token=token_a, error=RuntimeError("stale")
        ) is False
    with Session(p0_case.engine) as db:
        event = db.get(ChannelInboundEvent, p0_case.ids.event)
        assert event.processing_token == token_b
        assert event.last_error is None


def test_crash_after_claim_recovers_after_expiry(p0_case, monkeypatch):
    counter = {}
    monkeypatch.setattr(channels, "run_agent_for_existing_inbound", _fake_agent(counter))
    crashed = Session(p0_case.engine)
    channels._claim_inbound_event(
        crashed, connection=_connection(crashed, p0_case), event_id=p0_case.ids.event
    )
    crashed.close()
    with Session(p0_case.engine) as admin:
        _expire(admin, p0_case)
    with Session(p0_case.engine) as recovered:
        result = channels.process_inbound_event(
            recovered, connection=_connection(recovered, p0_case), event_id=p0_case.ids.event
        )
        assert result.event.status == "processed"
        assert counter["calls"] == 1


def test_committed_reply_is_recovered_after_finalization_failure(p0_case, monkeypatch):
    counter = {}
    monkeypatch.setattr(channels, "run_agent_for_existing_inbound", _fake_agent(counter))
    real_queue = channels.queue_message_dispatch
    def fail_queue(*args, **kwargs):
        raise RuntimeError("controlled finalization failure")
    monkeypatch.setattr(channels, "queue_message_dispatch", fail_queue)
    with Session(p0_case.engine) as first:
        with pytest.raises(RuntimeError, match="controlled finalization failure"):
            channels.process_inbound_event(
                first, connection=_connection(first, p0_case), event_id=p0_case.ids.event
            )
    monkeypatch.setattr(channels, "queue_message_dispatch", real_queue)
    with Session(p0_case.engine) as retry:
        result = channels.process_inbound_event(
            retry, connection=_connection(retry, p0_case), event_id=p0_case.ids.event
        )
        assert result.event.status == "processed"
    with Session(p0_case.engine) as db:
        patient = db.get(Patient, p0_case.ids.patient)
        assert counter["calls"] == 1
        assert patient.first_name == "P0-1"
        assert db.scalar(select(func.count()).select_from(Message).where(Message.in_reply_to_message_id == p0_case.ids.inbound, Message.sender_type == "ai")) == 1


def test_old_reply_is_directly_recoverable_after_over_100_messages(p0_case):
    run_id = uuid4()
    with Session(p0_case.engine) as db:
        inbound = db.get(Message, p0_case.ids.inbound)
        conversation = db.get(Conversation, p0_case.ids.conversation)
        reply = Message(
            workspace_id=inbound.workspace_id,
            conversation_id=inbound.conversation_id,
            sender_type="ai", direction="outbound", in_reply_to_message_id=inbound.id,
            message_type="text", content="old direct reply", delivery_status="queued",
            metadata_json={"agent_run_id": str(run_id)},
        )
        db.add(reply)
        db.flush()
        for i in range(110):
            db.add(Message(
                workspace_id=inbound.workspace_id,
                conversation_id=inbound.conversation_id,
                sender_type="system", direction="internal", message_type="text",
                content=f"later-{i}", delivery_status="received", metadata_json={},
            ))
        db.commit()
        recovered = agent_chat._existing_agent_response_for_inbound(
            db, conversation=conversation, inbound=inbound, run_id=run_id
        )
        assert recovered is not None and recovered.outbound_message_id == reply.id


def test_unique_execution_reply_index_allows_exactly_one_winner(p0_case):
    def insert_reply(label):
        try:
            with Session(p0_case.engine) as db:
                inbound = db.get(Message, p0_case.ids.inbound)
                db.add(Message(
                    workspace_id=inbound.workspace_id,
                    conversation_id=inbound.conversation_id,
                    sender_type="ai", direction="outbound", in_reply_to_message_id=inbound.id,
                    message_type="text", content=label, delivery_status="queued", metadata_json={},
                ))
                db.commit()
                return "ok"
        except IntegrityError:
            return "integrity"
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(insert_reply, ["race-a", "race-b"]))
    assert sorted(outcomes) == ["integrity", "ok"]


def test_poller_selects_only_oldest_turn_per_conversation(p0_case, monkeypatch):
    later = _add_later_inbound(p0_case)
    calls = []
    monkeypatch.setattr(
        transport,
        "process_inbound_event",
        lambda db, connection, event_id: calls.append(event_id),
    )
    with Session(p0_case.engine) as db:
        assert transport._process_pending_inbound(
            db, _connection(db, p0_case), limit=10
        ) == (1, 0)
    assert calls == [p0_case.ids.event]
    assert later.event not in calls


def test_next_turn_can_claim_after_older_turn_finishes(p0_case, monkeypatch):
    later = _add_later_inbound(p0_case)
    counter = {}
    monkeypatch.setattr(channels, "run_agent_for_existing_inbound", _fake_agent(counter))
    with Session(p0_case.engine) as first:
        result = channels.process_inbound_event(
            first, connection=_connection(first, p0_case), event_id=p0_case.ids.event
        )
        assert result.event.status == "processed"
    with Session(p0_case.engine) as second:
        event, token, processed = channels._claim_inbound_event(
            second, connection=_connection(second, p0_case), event_id=later.event
        )
        assert processed is None
        assert event.attempts == 1
        assert event.processing_token == token
        conversation = second.get(Conversation, p0_case.ids.conversation)
        assert conversation.agent_processing_token == token


def test_active_lease_is_not_reselected_by_poller(p0_case, monkeypatch):
    with Session(p0_case.engine) as db:
        channels._claim_inbound_event(
            db, connection=_connection(db, p0_case), event_id=p0_case.ids.event
        )
    calls = []
    monkeypatch.setattr(transport, "process_inbound_event", lambda *a, **k: calls.append(k))
    with Session(p0_case.engine) as db:
        assert transport._process_pending_inbound(db, _connection(db, p0_case), limit=10) == (0, 0)
    assert calls == []


def test_provider_ingest_survives_worker_crash_and_becomes_retryable(p0_case, monkeypatch):
    payload = {
        "entry": [{"changes": [{"value": {
            "metadata": {"phone_number_id": None},
            "contacts": [{"wa_id": "201234567890", "profile": {"name": "Crash"}}],
            "messages": [{"id": f"wamid-{uuid4()}", "from": "201234567890", "type": "text", "text": {"body": "hello"}}],
        }}]}]
    }
    with Session(p0_case.engine) as db:
        connection = _connection(db, p0_case)
        payload["entry"][0]["changes"][0]["value"]["metadata"]["phone_number_id"] = connection.external_account_id
        assert transport.ingest_meta_webhook(
            db,
            connection=connection,
            payload=payload,
        )["accepted_inbound"] == 1
        event = db.scalar(select(ChannelInboundEvent).where(ChannelInboundEvent.workspace_id == p0_case.ids.workspace, ChannelInboundEvent.id != p0_case.ids.event).order_by(ChannelInboundEvent.created_at.desc()))
        event_id = event.id
        channels._claim_inbound_event(db, connection=connection, event_id=event_id)
    with Session(p0_case.engine) as admin:
        original = admin.get(ChannelInboundEvent, p0_case.ids.event)
        original.status = "processed"
        event = admin.get(ChannelInboundEvent, event_id)
        inbound = admin.get(Message, event.message_id)
        conversation = admin.get(Conversation, inbound.conversation_id)
        expired_at = datetime.now(UTC) - timedelta(seconds=1)
        event.processing_lease_expires_at = expired_at
        conversation.agent_processing_lease_expires_at = expired_at
        admin.commit()
    calls = []
    monkeypatch.setattr(transport, "process_inbound_event", lambda db, connection, event_id: calls.append(event_id))
    with Session(p0_case.engine) as db:
        processed, failed = transport._process_pending_inbound(db, _connection(db, p0_case), limit=10)
        assert (processed, failed) == (1, 0)
    assert calls == [event_id]


def test_same_provider_timestamp_preserves_webhook_message_order(p0_case, monkeypatch):
    first_body = "same-ts-first"
    second_body = "same-ts-second"
    sender = "201234567891"
    provider_timestamp = str(int(datetime.now(UTC).timestamp()))
    with Session(p0_case.engine) as db:
        original = db.get(ChannelInboundEvent, p0_case.ids.event)
        original.status = "processed"
        connection = _connection(db, p0_case)
        phone_number_id = connection.external_account_id
        db.commit()

    payload = {
        "entry": [{"changes": [{"value": {
            "metadata": {"phone_number_id": phone_number_id},
            "contacts": [{"wa_id": sender, "profile": {"name": "Ordered"}}],
            "messages": [
                {"id": f"wamid-{uuid4()}", "from": sender, "timestamp": provider_timestamp,
                 "type": "text", "text": {"body": first_body}},
                {"id": f"wamid-{uuid4()}", "from": sender, "timestamp": provider_timestamp,
                 "type": "text", "text": {"body": second_body}},
            ],
        }}]}]
    }
    with Session(p0_case.engine) as db:
        connection = _connection(db, p0_case)
        assert transport.ingest_meta_webhook(
            db,
            connection=connection,
            payload=payload,
        )["accepted_inbound"] == 2

    with Session(p0_case.engine) as db:
        rows = list(
            db.execute(
                select(Message, ChannelInboundEvent)
                .join(ChannelInboundEvent, ChannelInboundEvent.message_id == Message.id)
                .where(Message.content.in_((first_body, second_body)))
                .order_by(Message.created_at, Message.id)
            )
        )
        assert [message.content for message, _ in rows] == [first_body, second_body]
        assert rows[0][0].created_at < rows[1][0].created_at
        first_event_id = rows[0][1].id
        second_event_id = rows[1][1].id

    calls = []
    monkeypatch.setattr(
        transport,
        "process_inbound_event",
        lambda db, connection, event_id: calls.append(event_id),
    )
    with Session(p0_case.engine) as db:
        assert transport._process_pending_inbound(
            db, _connection(db, p0_case), limit=10
        ) == (1, 0)
    assert calls == [first_event_id]
    assert second_event_id not in calls


def test_historical_duplicate_backfill_keeps_history_and_selects_earliest(tmp_path):
    base_url = make_url(os.environ["DATABASE_URL"])
    db_name = f"p0_migration_{uuid4().hex[:12]}"
    admin_url = base_url.set(database="postgres")
    admin_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    test_url = base_url.set(database=db_name)
    try:
        with admin_engine.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{db_name}"'))
        previous = os.environ["MIGRATION_DATABASE_URL"]
        os.environ["MIGRATION_DATABASE_URL"] = test_url.render_as_string(hide_password=False)
        backend = Path(__file__).resolve().parent.parent
        cfg = Config(str(backend / "alembic.ini"))
        cfg.set_main_option("script_location", str(backend / "alembic"))
        command.upgrade(cfg, "0069_service_package_offers_rls")
        engine = create_engine(test_url)
        ids = [uuid4() for _ in range(7)]
        workspace, patient, connection, conversation, inbound, first, second = ids
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO workspaces (id,name,slug) VALUES (:id,'m','m-' || :slug)"), {"id": workspace, "slug": uuid4().hex})
            conn.execute(text("INSERT INTO patients (id,workspace_id,first_name,status) VALUES (:id,:w,'p','active')"), {"id": patient, "w": workspace})
            conn.execute(text("INSERT INTO channel_connections (id,workspace_id,channel,provider,display_name,adapter_token_hash) VALUES (:id,:w,'whatsapp','meta_cloud','m',:h)"), {"id": connection, "w": workspace, "h": uuid4().hex + uuid4().hex})
            conn.execute(text("INSERT INTO conversations (id,workspace_id,patient_id,channel,status,owner_type,unread_count,started_at) VALUES (:id,:w,:p,'whatsapp','open','ai',0,now())"), {"id": conversation, "w": workspace, "p": patient})
            conn.execute(text("INSERT INTO messages (id,workspace_id,conversation_id,sender_type,direction,message_type,content,delivery_status,metadata,created_at) VALUES (:id,:w,:c,'patient','inbound','text','in','received','{}',now())"), {"id": inbound, "w": workspace, "c": conversation})
            for offset, outbound in enumerate((first, second)):
                conn.execute(text("INSERT INTO messages (id,workspace_id,conversation_id,sender_type,direction,message_type,content,delivery_status,metadata,created_at) VALUES (:id,:w,:c,'ai','outbound','text',:body,'queued',CAST(:meta AS jsonb),now() + (:offset || ' seconds')::interval)"), {"id": outbound, "w": workspace, "c": conversation, "body": f"reply-{offset}", "meta": '{"in_reply_to_message_id":"' + str(inbound) + '"}', "offset": offset})
        engine.dispose()
        command.upgrade(cfg, "head")
        engine = create_engine(test_url)
        with engine.connect() as conn:
            rows = conn.execute(text("SELECT id, in_reply_to_message_id FROM messages WHERE id IN (:a,:b) ORDER BY created_at"), {"a": first, "b": second}).all()
            assert len(rows) == 2
            assert rows[0].in_reply_to_message_id == inbound
            assert rows[1].in_reply_to_message_id is None
        engine.dispose()
        os.environ["MIGRATION_DATABASE_URL"] = previous
    finally:
        if "previous" in locals():
            os.environ["MIGRATION_DATABASE_URL"] = previous
        with admin_engine.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)'))
        admin_engine.dispose()
