from __future__ import annotations

import inspect
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.appointment import Appointment
from app.models.branch import Branch
from app.models.conversation import Conversation
from app.models.doctor import Doctor
from app.models.handoff_request import HandoffRequest
from app.models.message import Message
from app.models.patient import Patient
from app.models.service import Service
from app.models.staff import Staff
from app.models.workspace import Workspace
from app.services.agent_v2 import live_chat


class _FakeDB:
    def __init__(self) -> None:
        self.added: list[object] = []
        self.commits = 0
        self.rollbacks = 0

    def add(self, value: object) -> None:
        if isinstance(value, Message) and value.id is None:
            value.id = uuid4()
        self.added.append(value)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def _state():
    workspace_id = uuid4()
    patient_id = uuid4()
    conversation = SimpleNamespace(
        id=uuid4(),
        workspace_id=workspace_id,
        patient_id=patient_id,
        channel_connection_id=None,
        owner_type="human",
        status="pending",
        assigned_user_id=None,
        last_message_at=None,
    )
    patient = SimpleNamespace(id=patient_id, workspace_id=workspace_id)
    handoff = SimpleNamespace(
        id=uuid4(),
        workspace_id=workspace_id,
        conversation_id=conversation.id,
        patient_id=patient_id,
        source="ai",
        status="pending",
        assigned_user_id=None,
    )
    return SimpleNamespace(
        workspace=SimpleNamespace(id=workspace_id),
        patient=patient,
        conversation=conversation,
        handoff=handoff,
    )


def _fail(name: str):
    def inner(*args, **kwargs):
        raise AssertionError(f"{name} must not run during pending-handoff continuation")

    return inner


@pytest.mark.parametrize(
    "customer_text",
    ["Ø­ØªÙ‰ Ù„Ùˆ Ø£Ù†Ø§ Ù…ÙˆØ§ÙÙ‚ØŸ", "Ø·Ø¨ Ù…ÙÙŠØ´ Ø·Ø±ÙŠÙ‚Ø©ØŸ", "ØªÙ…Ø§Ù…"],
)
def test_pending_ai_handoff_followups_get_deterministic_nonempty_reply_without_agent_pipeline(
    monkeypatch: pytest.MonkeyPatch,
    customer_text: str,
) -> None:
    state = _state()
    db = _FakeDB()
    inbound = SimpleNamespace(
        id=uuid4(),
        content=customer_text,
        created_at=datetime.now(UTC),
    )
    original_handoff_id = state.handoff.id
    original_owner = state.conversation.owner_type
    appointment = SimpleNamespace(status="confirmed")

    monkeypatch.setattr(
        live_chat,
        "get_active_handoff",
        lambda *args, **kwargs: state.handoff,
    )
    monkeypatch.setattr(
        live_chat,
        "lock_conversation_ownership",
        lambda *args, **kwargs: state.conversation,
    )
    monkeypatch.setattr(
        live_chat,
        "_previous_handoff_ack_reply",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(live_chat, "agent_can_reply", _fail("agent_can_reply"))
    monkeypatch.setattr(live_chat, "orchestrate_v2_turn", _fail("orchestrator"))
    monkeypatch.setattr(live_chat, "get_clinic_adapter", _fail("clinic adapter"))
    monkeypatch.setattr(live_chat, "create_handoff", _fail("create_handoff"))
    monkeypatch.setattr(live_chat, "execute_write_ready_step", _fail("write executor"))

    result = live_chat._run_v2_after_inbound(
        db=db,  # type: ignore[arg-type]
        workspace=state.workspace,  # type: ignore[arg-type]
        patient=state.patient,  # type: ignore[arg-type]
        conversation=state.conversation,  # type: ignore[arg-type]
        inbound=inbound,  # type: ignore[arg-type]
        run_id=uuid4(),
        outbound_delivery_status="queued",
        source="test",
    )

    assert result.reply is not None and result.reply.strip()
    assert result.handoff_required is True
    assert result.agent_paused is True
    assert result.model == "deterministic:handoff-continuation"
    assert state.handoff.id == original_handoff_id
    assert state.handoff.status == "pending"
    assert state.handoff.assigned_user_id is None
    assert state.conversation.owner_type == original_owner == "human"
    assert appointment.status == "confirmed"
    assert db.commits == 1
    outbound = next(value for value in db.added if isinstance(value, Message))
    assert outbound.metadata_json["handoff_continuation"] is True
    assert outbound.metadata_json["handoff_id"] == str(original_handoff_id)
    assert outbound.metadata_json["handoff_ack_reused"] is False
    assert outbound.metadata_json.get("handoff_ack") is not True


def test_pending_handoff_continuation_reuses_grounded_ack_for_same_handoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state()
    db = _FakeDB()
    inbound = SimpleNamespace(
        id=uuid4(),
        content="Ø­ØªÙ‰ Ù„Ùˆ Ø£Ù†Ø§ Ù…ÙˆØ§ÙÙ‚ØŸ",
        created_at=datetime.now(UTC),
    )
    grounded = "Ø­ÙˆÙ‘Ù„Øª Ø§Ù„Ù…Ø­Ø§Ø¯Ø«Ø© Ù„ÙØ±ÙŠÙ‚ Ø§Ù„Ø¹ÙŠØ§Ø¯Ø© Ø¹Ø´Ø§Ù† ÙŠØ³Ø§Ø¹Ø¯Ùƒ."

    monkeypatch.setattr(live_chat, "get_active_handoff", lambda *a, **k: state.handoff)
    monkeypatch.setattr(
        live_chat,
        "lock_conversation_ownership",
        lambda *a, **k: state.conversation,
    )
    monkeypatch.setattr(
        live_chat,
        "_previous_handoff_ack_reply",
        lambda *a, **k: grounded,
    )
    monkeypatch.setattr(live_chat, "agent_can_reply", _fail("agent_can_reply"))
    monkeypatch.setattr(live_chat, "orchestrate_v2_turn", _fail("orchestrator"))
    monkeypatch.setattr(live_chat, "create_handoff", _fail("create_handoff"))

    result = live_chat._run_v2_after_inbound(
        db=db,  # type: ignore[arg-type]
        workspace=state.workspace,  # type: ignore[arg-type]
        patient=state.patient,  # type: ignore[arg-type]
        conversation=state.conversation,  # type: ignore[arg-type]
        inbound=inbound,  # type: ignore[arg-type]
        run_id=uuid4(),
        outbound_delivery_status="queued",
        source="test",
    )

    assert result.reply == grounded
    outbound = next(value for value in db.added if isinstance(value, Message))
    assert outbound.metadata_json["handoff_ack_reused"] is True


@pytest.mark.parametrize(
    "handoff",
    [
        None,
        SimpleNamespace(source="ai", status="claimed", assigned_user_id=uuid4()),
        SimpleNamespace(source="ai", status="pending", assigned_user_id=uuid4()),
        SimpleNamespace(source="staff", status="pending", assigned_user_id=None),
    ],
)
def test_invalid_handoff_state_gets_no_continuation_authority(handoff: object) -> None:
    conversation = SimpleNamespace(owner_type="human", status="pending")
    assert not live_chat._v2_handoff_continuation_allowed(
        conversation,  # type: ignore[arg-type]
        handoff,
    )


def test_initial_handoff_ack_metadata_is_bound_to_handoff_id() -> None:
    source = inspect.getsource(live_chat._run_v2_after_inbound)
    assert '"handoff_ack": handoff_ack_allowed' in source
    assert '"handoff_id": (' in source


@contextmanager
def _db_session():
    engine = create_engine(settings.database_url, pool_pre_ping=True)
    connection = engine.connect()
    outer = connection.begin()
    db = Session(
        bind=connection,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    try:
        yield db
    finally:
        db.close()
        if outer.is_active:
            outer.rollback()
        connection.close()
        engine.dispose()


def test_pending_handoff_realistic_db_regression_preserves_appointment_and_handoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _db_session() as db:
        suffix = uuid4().hex[:10]
        now = datetime.now(UTC)
        workspace = Workspace(
            name=f"F7 {suffix}",
            slug=f"f7-{suffix}",
            timezone="Africa/Cairo",
            is_active=True,
            is_demo=True,
        )
        db.add(workspace)
        db.flush()

        branch = Branch(
            workspace_id=workspace.id,
            name="Main",
            code=f"F7-{suffix}",
            city="Cairo",
            country_code="EG",
            timezone="Africa/Cairo",
            is_active=True,
        )
        staff = Staff(
            workspace_id=workspace.id,
            first_name="Test",
            last_name="Doctor",
            is_active=True,
        )
        service = Service(
            workspace_id=workspace.id,
            name="Hydrafacial",
            slug=f"hydrafacial-{suffix}",
            operational_category="dermatology",
            duration_minutes=30,
            price_minor=100_000,
            currency="EGP",
            is_active=True,
        )
        patient = Patient(
            workspace_id=workspace.id,
            first_name="F7",
            preferred_language="ar",
            source="other",
            status="active",
        )
        db.add_all([branch, staff, service, patient])
        db.flush()
        workspace.primary_branch_id = branch.id

        doctor = Doctor(
            workspace_id=workspace.id,
            staff_id=staff.id,
            doctor_type="regular",
            booking_enabled=True,
            is_active=True,
        )
        db.add(doctor)
        db.flush()

        start = now + timedelta(hours=2)
        appointment = Appointment(
            workspace_id=workspace.id,
            patient_id=patient.id,
            branch_id=branch.id,
            doctor_id=doctor.id,
            doctor_assignment_known=True,
            service_id=service.id,
            status="confirmed",
            source="ai",
            start_at=start,
            end_at=start + timedelta(minutes=30),
            busy_start_at=start,
            busy_end_at=start + timedelta(minutes=30),
            duration_minutes=30,
            price_minor=100_000,
            discount_minor=0,
            currency="EGP",
            payment_status="unpaid",
            payment_method="unknown",
            billing_context="standard",
        )
        conversation = Conversation(
            workspace_id=workspace.id,
            patient_id=patient.id,
            channel="web",
            status="pending",
            owner_type="human",
            unread_count=1,
            started_at=now,
            last_message_at=now,
            ownership_changed_at=now,
        )
        db.add_all([appointment, conversation])
        db.flush()

        handoff = HandoffRequest(
            workspace_id=workspace.id,
            conversation_id=conversation.id,
            patient_id=patient.id,
            status="pending",
            category="customer_request",
            priority="normal",
            source="ai",
            reason="staff_review_required",
        )
        db.add(handoff)
        db.flush()

        first_inbound = Message(
            workspace_id=workspace.id,
            conversation_id=conversation.id,
            sender_type="patient",
            direction="inbound",
            created_at=now,
            message_type="text",
            content="Ø¹Ø§ÙŠØ² Ø£Ù„ØºÙŠ Ø§Ù„Ù…ÙˆØ¹Ø¯",
            delivery_status="received",
            metadata_json={},
        )
        db.add(first_inbound)
        db.flush()
        first_ack = Message(
            workspace_id=workspace.id,
            conversation_id=conversation.id,
            sender_type="ai",
            direction="outbound",
            in_reply_to_message_id=first_inbound.id,
            created_at=now + timedelta(seconds=1),
            message_type="text",
            content="Ø­ÙˆÙ‘Ù„Øª Ø§Ù„Ù…Ø­Ø§Ø¯Ø«Ø© Ù„ÙØ±ÙŠÙ‚ Ø§Ù„Ø¹ÙŠØ§Ø¯Ø© Ø¹Ø´Ø§Ù† ÙŠØ³Ø§Ø¹Ø¯Ùƒ.",
            delivery_status="sent",
            metadata_json={
                "runtime": "v2",
                "handoff_ack": True,
                "handoff_id": str(handoff.id),
            },
        )
        db.add(first_ack)
        db.flush()
        followup = Message(
            workspace_id=workspace.id,
            conversation_id=conversation.id,
            sender_type="patient",
            direction="inbound",
            created_at=now + timedelta(seconds=2),
            message_type="text",
            content="Ø­ØªÙ‰ Ù„Ùˆ Ø£Ù†Ø§ Ù…ÙˆØ§ÙÙ‚ØŸ",
            delivery_status="received",
            metadata_json={},
        )
        db.add(followup)
        db.commit()

        appointment_status_before = appointment.status
        handoff_id_before = handoff.id
        handoff_count_before = db.scalar(
            select(func.count(HandoffRequest.id)).where(
                HandoffRequest.workspace_id == workspace.id,
                HandoffRequest.conversation_id == conversation.id,
            )
        )

        monkeypatch.setattr(live_chat, "orchestrate_v2_turn", _fail("orchestrator/LLM"))
        monkeypatch.setattr(live_chat, "get_clinic_adapter", _fail("clinic adapter"))
        monkeypatch.setattr(live_chat, "create_handoff", _fail("create_handoff"))
        monkeypatch.setattr(live_chat, "execute_write_ready_step", _fail("write executor"))
        monkeypatch.setattr(live_chat, "agent_can_reply", _fail("agent_can_reply"))

        result = live_chat._run_v2_after_inbound(
            db=db,
            workspace=workspace,
            patient=patient,
            conversation=conversation,
            inbound=followup,
            run_id=uuid4(),
            outbound_delivery_status="queued",
            source="test-db",
        )

        db.expire_all()
        appointment_after = db.get(Appointment, appointment.id)
        handoff_after = db.get(HandoffRequest, handoff.id)
        conversation_after = db.get(Conversation, conversation.id)
        handoff_count_after = db.scalar(
            select(func.count(HandoffRequest.id)).where(
                HandoffRequest.workspace_id == workspace.id,
                HandoffRequest.conversation_id == conversation.id,
            )
        )
        outbound = db.scalar(
            select(Message).where(
                Message.in_reply_to_message_id == followup.id,
                Message.sender_type == "ai",
            )
        )

        assert result.reply == first_ack.content
        assert result.reply is not None and result.reply.strip()
        assert result.agent_paused is True
        assert result.handoff_required is True
        assert appointment_after is not None
        assert appointment_after.status == appointment_status_before == "confirmed"
        assert handoff_after is not None
        assert handoff_after.id == handoff_id_before
        assert handoff_after.status == "pending"
        assert handoff_after.source == "ai"
        assert handoff_after.assigned_user_id is None
        assert handoff_count_before == handoff_count_after == 1
        assert conversation_after is not None
        assert conversation_after.owner_type == "human"
        assert conversation_after.status == "pending"
        assert outbound is not None
        assert outbound.metadata_json["handoff_continuation"] is True
        assert outbound.metadata_json["handoff_id"] == str(handoff_id_before)
        assert outbound.metadata_json["handoff_ack_reused"] is True
