from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from threading import Barrier
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import OperationalError

import app.api.routes.inbox as inbox_routes
from app.database.session import SessionLocal, engine
from app.models.channel_connection import ChannelConnection
from app.models.conversation import Conversation
from app.models.handoff_event import HandoffEvent
from app.models.handoff_request import HandoffRequest
from app.models.message import Message
from app.models.message_dispatch import MessageDispatch
from app.models.patient import Patient
from app.models.user import User
from app.models.workspace import Workspace
from app.models.workspace_member import WorkspaceMember
from app.schemas.inbox import StaffReplyRequest
from app.services.channels import queue_message_dispatch
from app.services.conversation_ownership import (
    agent_can_reply,
    lock_conversation_ownership,
    record_customer_inbound,
)
from app.services.handoffs import (
    HandoffStateError,
    add_staff_reply,
    assign_handoff,
    claim_handoff,
    create_handoff,
    get_active_handoff,
    resolve_handoff,
)


@dataclass(frozen=True)
class InboxPgFixture:
    workspace_id: UUID
    patient_id: UUID
    connection_id: UUID
    conversation_id: UUID
    handoff_id: UUID
    admin_id: UUID
    receptionist_a_id: UUID
    receptionist_b_id: UUID


def _require_postgres() -> None:
    if engine.dialect.name != "postgresql":
        pytest.fail("Inbox concurrency tests require PostgreSQL.")
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except OperationalError:
        if os.getenv("CI"):
            raise
        pytest.skip("PostgreSQL test service is not available locally.")


@pytest.fixture
def inbox_pg() -> InboxPgFixture:
    _require_postgres()
    suffix = uuid4().hex
    now = datetime.now(UTC)

    with SessionLocal() as db:
        workspace = Workspace(name=f"Inbox Reliability {suffix[:8]}", slug=f"inbox-reliability-{suffix}")
        admin = User(email=f"admin-{suffix}@example.test", full_name="Inbox Admin")
        receptionist_a = User(email=f"staff-a-{suffix}@example.test", full_name="Receptionist A")
        receptionist_b = User(email=f"staff-b-{suffix}@example.test", full_name="Receptionist B")
        db.add_all([workspace, admin, receptionist_a, receptionist_b])
        db.flush()

        db.add_all(
            [
                WorkspaceMember(workspace_id=workspace.id, user_id=admin.id, role="admin"),
                WorkspaceMember(workspace_id=workspace.id, user_id=receptionist_a.id, role="member"),
                WorkspaceMember(workspace_id=workspace.id, user_id=receptionist_b.id, role="member"),
            ]
        )
        patient = Patient(
            workspace_id=workspace.id,
            first_name="Inbox",
            last_name="Patient",
            phone=f"+2010{suffix[:8]}",
            phone_normalized=f"2010{suffix[:8]}",
            source="other",
            status="active",
        )
        connection = ChannelConnection(
            workspace_id=workspace.id,
            channel="web",
            provider="test",
            display_name="Inbox reliability test",
            status="active",
            adapter_token_hash=sha256(suffix.encode()).hexdigest(),
            created_by_user_id=admin.id,
            config_json={},
        )
        db.add_all([patient, connection])
        db.flush()

        conversation = Conversation(
            workspace_id=workspace.id,
            patient_id=patient.id,
            channel="web",
            channel_connection_id=connection.id,
            status="pending",
            owner_type="human",
            unread_count=0,
            ownership_changed_at=now,
            started_at=now,
            last_message_at=now,
        )
        db.add(conversation)
        db.flush()
        handoff = HandoffRequest(
            workspace_id=workspace.id,
            conversation_id=conversation.id,
            patient_id=patient.id,
            status="pending",
            category="customer_request",
            priority="normal",
            source="staff",
            reason="PostgreSQL reliability test handoff.",
            context_json={},
            created_by_user_id=admin.id,
        )
        db.add(handoff)
        db.commit()

        fixture = InboxPgFixture(
            workspace_id=workspace.id,
            patient_id=patient.id,
            connection_id=connection.id,
            conversation_id=conversation.id,
            handoff_id=handoff.id,
            admin_id=admin.id,
            receptionist_a_id=receptionist_a.id,
            receptionist_b_id=receptionist_b.id,
        )

    try:
        yield fixture
    finally:
        with SessionLocal() as db:
            db.execute(
                delete(MessageDispatch).where(
                    MessageDispatch.workspace_id == fixture.workspace_id
                )
            )
            db.execute(delete(Message).where(Message.workspace_id == fixture.workspace_id))
            db.execute(delete(Workspace).where(Workspace.id == fixture.workspace_id))
            db.commit()
            db.execute(
                delete(User).where(
                    User.id.in_(
                        (fixture.admin_id, fixture.receptionist_a_id, fixture.receptionist_b_id)
                    )
                )
            )
            db.commit()


def _set_lock_timeout(db) -> None:
    db.execute(text("SET LOCAL lock_timeout = '4s'"))


def _claim_fixture_handoff(fixture: InboxPgFixture, user_id: UUID) -> None:
    with SessionLocal() as db:
        _set_lock_timeout(db)
        handoff, conversation = inbox_routes._lock_handoff_and_conversation(
            db,
            workspace_id=fixture.workspace_id,
            handoff_id=fixture.handoff_id,
        )
        user = db.get(User, user_id)
        assert user is not None
        claim_handoff(db, handoff=handoff, conversation=conversation, user=user)


def test_conversation_detail_returns_latest_500_messages_in_chronological_order(
    inbox_pg: InboxPgFixture,
) -> None:
    base = datetime(2026, 1, 1, tzinfo=UTC)
    ordinary: list[Message] = []
    with SessionLocal() as db:
        for index in range(601):
            ordinary.append(
                Message(
                    workspace_id=inbox_pg.workspace_id,
                    conversation_id=inbox_pg.conversation_id,
                    channel_connection_id=inbox_pg.connection_id,
                    sender_type="patient",
                    direction="inbound",
                    message_type="text",
                    content=f"message-{index}",
                    delivery_status="received",
                    created_at=base + timedelta(seconds=index),
                    metadata_json={},
                )
            )
        tie_ids = sorted((uuid4(), uuid4()))
        tie_time = base + timedelta(seconds=601)
        ties = [
            Message(
                id=message_id,
                workspace_id=inbox_pg.workspace_id,
                conversation_id=inbox_pg.conversation_id,
                channel_connection_id=inbox_pg.connection_id,
                sender_type="patient",
                direction="inbound",
                message_type="text",
                content=f"tie-{index}",
                delivery_status="received",
                created_at=tie_time,
                metadata_json={},
            )
            for index, message_id in enumerate(tie_ids)
        ]
        db.add_all([*ordinary, *ties])
        db.commit()

        access = SimpleNamespace(workspace=SimpleNamespace(id=inbox_pg.workspace_id))
        response = inbox_routes.get_inbox_conversation(
            inbox_pg.conversation_id,
            access=access,
            db=db,
        )

    assert len(response.messages) == 500
    returned_ids = [message.id for message in response.messages]
    assert ordinary[-1].id in returned_ids
    assert set(message.id for message in ordinary[:103]).isdisjoint(returned_ids)
    assert returned_ids[0] == ordinary[103].id
    assert returned_ids[-2:] == tie_ids
    ordering = [(message.created_at, message.id) for message in response.messages]
    assert ordering == sorted(ordering)


def test_two_receptionists_claim_same_handoff_first_claim_wins_without_deadlock(
    inbox_pg: InboxPgFixture,
) -> None:
    barrier = Barrier(2)

    def worker(user_id: UUID) -> str:
        with SessionLocal() as db:
            _set_lock_timeout(db)
            user = db.get(User, user_id)
            assert user is not None
            barrier.wait(timeout=5)
            try:
                handoff, conversation = inbox_routes._lock_handoff_and_conversation(
                    db,
                    workspace_id=inbox_pg.workspace_id,
                    handoff_id=inbox_pg.handoff_id,
                )
                claim_handoff(db, handoff=handoff, conversation=conversation, user=user)
                return "claimed"
            except HandoffStateError:
                db.rollback()
                return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(worker, (inbox_pg.receptionist_a_id, inbox_pg.receptionist_b_id))
        )

    assert sorted(results) == ["claimed", "conflict"]
    with SessionLocal() as db:
        handoff = db.get(HandoffRequest, inbox_pg.handoff_id)
        conversation = db.get(Conversation, inbox_pg.conversation_id)
        assert handoff is not None and conversation is not None
        assert handoff.assigned_user_id in {
            inbox_pg.receptionist_a_id,
            inbox_pg.receptionist_b_id,
        }
        assert conversation.assigned_user_id == handoff.assigned_user_id
        assert conversation.owner_type == "human"
        claimed_events = db.scalar(
            select(func.count(HandoffEvent.id)).where(
                HandoffEvent.handoff_request_id == inbox_pg.handoff_id,
                HandoffEvent.event_type == "claimed",
            )
        )
        assert claimed_events == 1


def test_staff_takeover_invalidates_an_ai_turn_that_started_before_takeover(
    inbox_pg: InboxPgFixture,
) -> None:
    with SessionLocal() as db:
        handoff = db.get(HandoffRequest, inbox_pg.handoff_id)
        assert handoff is not None
        db.delete(handoff)
        conversation = db.get(Conversation, inbox_pg.conversation_id)
        assert conversation is not None
        conversation.owner_type = "ai"
        conversation.status = "open"
        conversation.assigned_user_id = None
        conversation.ownership_changed_at = datetime.now(UTC) - timedelta(seconds=1)
        db.commit()

    with SessionLocal() as ai_db:
        stale_conversation = ai_db.get(Conversation, inbox_pg.conversation_id)
        assert stale_conversation is not None
        assert agent_can_reply(stale_conversation) is True

        with SessionLocal() as staff_db:
            _set_lock_timeout(staff_db)
            conversation = staff_db.get(Conversation, inbox_pg.conversation_id)
            patient = staff_db.get(Patient, inbox_pg.patient_id)
            user = staff_db.get(User, inbox_pg.receptionist_a_id)
            assert conversation is not None and patient is not None and user is not None
            handoff = create_handoff(
                staff_db,
                workspace_id=inbox_pg.workspace_id,
                conversation=conversation,
                patient=patient,
                reason="Receptionist takeover while AI is working.",
                category="customer_request",
                priority="normal",
                source="staff",
                created_by_user_id=user.id,
                commit=False,
            )
            claim_handoff(
                staff_db,
                handoff=handoff,
                conversation=conversation,
                user=user,
            )

        refreshed = lock_conversation_ownership(
            ai_db,
            workspace_id=inbox_pg.workspace_id,
            conversation_id=inbox_pg.conversation_id,
        )
        assert refreshed is stale_conversation
        assert refreshed.owner_type == "human"
        assert agent_can_reply(refreshed) is False
        ai_db.rollback()

    with SessionLocal() as db:
        ai_outbound = db.scalar(
            select(func.count(Message.id)).where(
                Message.workspace_id == inbox_pg.workspace_id,
                Message.conversation_id == inbox_pg.conversation_id,
                Message.sender_type == "ai",
                Message.direction == "outbound",
            )
        )
        assert ai_outbound == 0


def test_customer_inbound_during_handoff_keeps_human_ownership_and_increments_unread(
    inbox_pg: InboxPgFixture,
) -> None:
    _claim_fixture_handoff(inbox_pg, inbox_pg.receptionist_a_id)
    inbound_id = uuid4()

    with SessionLocal() as db:
        _set_lock_timeout(db)
        conversation = inbox_routes._get_conversation(
            db,
            workspace_id=inbox_pg.workspace_id,
            conversation_id=inbox_pg.conversation_id,
            for_update=True,
        )
        inbound_at = datetime.now(UTC)
        db.add(
            Message(
                id=inbound_id,
                workspace_id=inbox_pg.workspace_id,
                conversation_id=inbox_pg.conversation_id,
                channel_connection_id=inbox_pg.connection_id,
                sender_type="patient",
                direction="inbound",
                message_type="text",
                content="new customer message during human handoff",
                delivery_status="received",
                created_at=inbound_at,
                metadata_json={},
            )
        )
        record_customer_inbound(conversation, now=inbound_at)
        db.commit()

    with SessionLocal() as db:
        conversation = db.get(Conversation, inbox_pg.conversation_id)
        handoff = get_active_handoff(
            db,
            workspace_id=inbox_pg.workspace_id,
            conversation_id=inbox_pg.conversation_id,
        )
        assert conversation is not None and handoff is not None
        assert conversation.owner_type == "human"
        assert conversation.assigned_user_id == inbox_pg.receptionist_a_id
        assert conversation.unread_count == 1
        assert handoff.status == "claimed"
        assert handoff.assigned_user_id == inbox_pg.receptionist_a_id
        assert agent_can_reply(conversation) is False
        assert db.get(Message, inbound_id) is not None


def test_concurrent_admin_assign_and_receptionist_claim_has_deterministic_admin_assignment(
    inbox_pg: InboxPgFixture,
) -> None:
    barrier = Barrier(2)

    def claim_worker() -> str:
        with SessionLocal() as db:
            _set_lock_timeout(db)
            user = db.get(User, inbox_pg.receptionist_a_id)
            assert user is not None
            barrier.wait(timeout=5)
            try:
                handoff, conversation = inbox_routes._lock_handoff_and_conversation(
                    db,
                    workspace_id=inbox_pg.workspace_id,
                    handoff_id=inbox_pg.handoff_id,
                )
                claim_handoff(db, handoff=handoff, conversation=conversation, user=user)
                return "claimed"
            except HandoffStateError:
                db.rollback()
                return "conflict"

    def assign_worker() -> str:
        with SessionLocal() as db:
            _set_lock_timeout(db)
            actor = db.get(User, inbox_pg.admin_id)
            target = db.get(User, inbox_pg.receptionist_b_id)
            assert actor is not None and target is not None
            barrier.wait(timeout=5)
            handoff, conversation = inbox_routes._lock_handoff_and_conversation(
                db,
                workspace_id=inbox_pg.workspace_id,
                handoff_id=inbox_pg.handoff_id,
            )
            assign_handoff(
                db,
                handoff=handoff,
                conversation=conversation,
                target_user=target,
                actor_user=actor,
            )
            return "assigned"

    with ThreadPoolExecutor(max_workers=2) as pool:
        claim_future = pool.submit(claim_worker)
        assign_future = pool.submit(assign_worker)
        results = {claim_future.result(timeout=10), assign_future.result(timeout=10)}

    assert "assigned" in results
    with SessionLocal() as db:
        handoff = db.get(HandoffRequest, inbox_pg.handoff_id)
        conversation = db.get(Conversation, inbox_pg.conversation_id)
        assert handoff is not None and conversation is not None
        assert handoff.assigned_user_id == inbox_pg.receptionist_b_id
        assert conversation.assigned_user_id == inbox_pg.receptionist_b_id
        assert conversation.owner_type == "human"


def test_return_to_tia_racing_staff_outbound_never_creates_mixed_ownership(
    inbox_pg: InboxPgFixture,
) -> None:
    _claim_fixture_handoff(inbox_pg, inbox_pg.receptionist_a_id)
    barrier = Barrier(2)

    def staff_reply_worker() -> str:
        with SessionLocal() as db:
            _set_lock_timeout(db)
            user = db.get(User, inbox_pg.receptionist_a_id)
            assert user is not None
            barrier.wait(timeout=5)
            conversation = inbox_routes._get_conversation(
                db,
                workspace_id=inbox_pg.workspace_id,
                conversation_id=inbox_pg.conversation_id,
                for_update=True,
            )
            handoff = get_active_handoff(
                db,
                workspace_id=inbox_pg.workspace_id,
                conversation_id=inbox_pg.conversation_id,
                for_update=True,
            )
            if handoff is None:
                db.rollback()
                return "handoff_already_resolved"
            message = add_staff_reply(
                db,
                handoff=handoff,
                conversation=conversation,
                user=user,
                content="staff reply racing handback",
                commit=False,
            )
            dispatch = queue_message_dispatch(
                db,
                message=message,
                conversation=conversation,
                commit=False,
            )
            assert dispatch is not None
            db.commit()
            return "staff_queued"

    def resolve_worker() -> str:
        with SessionLocal() as db:
            _set_lock_timeout(db)
            user = db.get(User, inbox_pg.receptionist_a_id)
            assert user is not None
            barrier.wait(timeout=5)
            handoff, conversation = inbox_routes._lock_handoff_and_conversation(
                db,
                workspace_id=inbox_pg.workspace_id,
                handoff_id=inbox_pg.handoff_id,
            )
            try:
                resolve_handoff(
                    db,
                    handoff=handoff,
                    conversation=conversation,
                    actor_user=user,
                    resolution_note="return to Linka",
                    conversation_status_after="open",
                )
                return "returned_to_ai"
            except HandoffStateError:
                db.rollback()
                return "blocked_by_staff_outbox"

    with ThreadPoolExecutor(max_workers=2) as pool:
        staff_future = pool.submit(staff_reply_worker)
        resolve_future = pool.submit(resolve_worker)
        results = {staff_future.result(timeout=10), resolve_future.result(timeout=10)}

    with SessionLocal() as db:
        conversation = db.get(Conversation, inbox_pg.conversation_id)
        handoff = db.get(HandoffRequest, inbox_pg.handoff_id)
        staff_messages = list(
            db.scalars(
                select(Message).where(
                    Message.workspace_id == inbox_pg.workspace_id,
                    Message.conversation_id == inbox_pg.conversation_id,
                    Message.sender_type == "staff",
                    Message.direction == "outbound",
                )
            )
        )
        queued_dispatches = list(
            db.scalars(
                select(MessageDispatch)
                .join(Message, Message.id == MessageDispatch.message_id)
                .where(
                    Message.workspace_id == inbox_pg.workspace_id,
                    Message.conversation_id == inbox_pg.conversation_id,
                    Message.sender_type == "staff",
                    MessageDispatch.status.in_(("queued", "processing")),
                )
            )
        )
        assert conversation is not None and handoff is not None
        assert len(staff_messages) <= 1
        if "staff_queued" in results:
            assert "blocked_by_staff_outbox" in results
            assert conversation.owner_type == "human"
            assert handoff.status == "claimed"
            assert len(queued_dispatches) == 1
        else:
            assert results == {"handoff_already_resolved", "returned_to_ai"}
            assert conversation.owner_type == "ai"
            assert handoff.status == "resolved"
            assert staff_messages == []
            assert queued_dispatches == []


def test_staff_reply_idempotency_covers_double_submit_and_post_commit_retry(
    inbox_pg: InboxPgFixture,
) -> None:
    _claim_fixture_handoff(inbox_pg, inbox_pg.receptionist_a_id)
    request_key = str(uuid4())
    barrier = Barrier(2)

    def submit_same_request() -> tuple[UUID, UUID | None]:
        with SessionLocal() as db:
            _set_lock_timeout(db)
            user = db.get(User, inbox_pg.receptionist_a_id)
            assert user is not None
            access = SimpleNamespace(
                workspace=SimpleNamespace(id=inbox_pg.workspace_id),
                user=user,
            )
            barrier.wait(timeout=5)
            response = inbox_routes.send_staff_reply(
                inbox_pg.conversation_id,
                StaffReplyRequest(content="same receptionist reply"),
                access=access,
                db=db,
                idempotency_key=request_key,
            )
            return response.message.id, response.dispatch_id

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(submit_same_request)
        second = pool.submit(submit_same_request)
        concurrent_results = [first.result(timeout=10), second.result(timeout=10)]

    assert concurrent_results[0] == concurrent_results[1]

    # Simulate: transaction committed, response was lost, then the browser retries
    # the exact same logical request with the same idempotency key.
    with SessionLocal() as db:
        user = db.get(User, inbox_pg.receptionist_a_id)
        assert user is not None
        access = SimpleNamespace(workspace=SimpleNamespace(id=inbox_pg.workspace_id), user=user)
        retry = inbox_routes.send_staff_reply(
            inbox_pg.conversation_id,
            StaffReplyRequest(content="same receptionist reply"),
            access=access,
            db=db,
            idempotency_key=request_key,
        )
        assert retry.message.id == concurrent_results[0][0]
        assert retry.dispatch_id == concurrent_results[0][1]

    with SessionLocal() as db:
        staff_count = db.scalar(
            select(func.count(Message.id)).where(
                Message.workspace_id == inbox_pg.workspace_id,
                Message.conversation_id == inbox_pg.conversation_id,
                Message.sender_type == "staff",
                Message.direction == "outbound",
            )
        )
        dispatch_count = db.scalar(
            select(func.count(MessageDispatch.id))
            .join(Message, Message.id == MessageDispatch.message_id)
            .where(
                Message.workspace_id == inbox_pg.workspace_id,
                Message.conversation_id == inbox_pg.conversation_id,
                Message.sender_type == "staff",
            )
        )
        assert staff_count == 1
        assert dispatch_count == 1

        # The protection is request-identity based, not a content/time heuristic:
        # the receptionist may intentionally send the same text again as a new request.
        user = db.get(User, inbox_pg.receptionist_a_id)
        assert user is not None
        access = SimpleNamespace(workspace=SimpleNamespace(id=inbox_pg.workspace_id), user=user)
        distinct = inbox_routes.send_staff_reply(
            inbox_pg.conversation_id,
            StaffReplyRequest(content="same receptionist reply"),
            access=access,
            db=db,
            idempotency_key=str(uuid4()),
        )
        assert distinct.message.id != retry.message.id

    with SessionLocal() as db:
        staff_count = db.scalar(
            select(func.count(Message.id)).where(
                Message.workspace_id == inbox_pg.workspace_id,
                Message.conversation_id == inbox_pg.conversation_id,
                Message.sender_type == "staff",
                Message.direction == "outbound",
            )
        )
        assert staff_count == 2
