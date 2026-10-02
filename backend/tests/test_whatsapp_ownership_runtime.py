from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.schemas.crm import MessageRead
from app.services.conversation_ownership import (
    OwnershipTransitionBlockedError,
    ai_dispatch_is_sendable,
    cancel_dispatch_for_ownership,
    ensure_staff_outbox_drained_before_ai,
    quiesce_ai_dispatches_for_human,
)


def _root() -> Path:
    return Path(__file__).resolve().parents[2]


def _message(
    *,
    handoff_ack: bool = False,
    handoff_continuation: bool = False,
    handoff_id: object | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        workspace_id=uuid4(),
        conversation_id=uuid4(),
        sender_type="ai",
        direction="outbound",
        metadata_json={
            "handoff_ack": handoff_ack,
            "handoff_continuation": handoff_continuation,
            "handoff_id": str(handoff_id) if handoff_id is not None else None,
        },
        delivery_status="queued",
    )


def _conversation(*, owner_type: str = "ai", status: str = "open") -> SimpleNamespace:
    return SimpleNamespace(owner_type=owner_type, status=status)


def test_normal_ai_dispatch_requires_live_ai_ownership() -> None:
    message = _message()
    assert ai_dispatch_is_sendable(
        conversation=_conversation(owner_type="ai", status="open"),  # type: ignore[arg-type]
        message=message,  # type: ignore[arg-type]
        active_handoff=None,
    )
    assert not ai_dispatch_is_sendable(
        conversation=_conversation(owner_type="human", status="pending"),  # type: ignore[arg-type]
        message=message,  # type: ignore[arg-type]
        active_handoff=None,
    )


def test_handoff_ack_is_only_sendable_before_staff_claim() -> None:
    message = _message(handoff_ack=True)
    pending = SimpleNamespace(
        source="ai",
        status="pending",
        assigned_user_id=None,
    )
    claimed = SimpleNamespace(
        source="ai",
        status="claimed",
        assigned_user_id=uuid4(),
    )
    human = _conversation(owner_type="human", status="pending")

    assert ai_dispatch_is_sendable(
        conversation=human,  # type: ignore[arg-type]
        message=message,  # type: ignore[arg-type]
        active_handoff=pending,  # type: ignore[arg-type]
    )
    assert not ai_dispatch_is_sendable(
        conversation=human,  # type: ignore[arg-type]
        message=message,  # type: ignore[arg-type]
        active_handoff=claimed,  # type: ignore[arg-type]
    )


def test_handoff_continuation_is_revalidated_at_dispatch_time() -> None:
    handoff_id = uuid4()
    message = _message(handoff_continuation=True, handoff_id=handoff_id)
    pending = SimpleNamespace(
        id=handoff_id,
        source="ai",
        status="pending",
        assigned_user_id=None,
    )
    claimed = SimpleNamespace(
        id=handoff_id,
        source="ai",
        status="claimed",
        assigned_user_id=uuid4(),
    )
    different_pending = SimpleNamespace(
        id=uuid4(),
        source="ai",
        status="pending",
        assigned_user_id=None,
    )
    staff_pending = SimpleNamespace(
        id=handoff_id,
        source="staff",
        status="pending",
        assigned_user_id=None,
    )
    human = _conversation(owner_type="human", status="pending")

    assert ai_dispatch_is_sendable(
        conversation=human,  # type: ignore[arg-type]
        message=message,  # type: ignore[arg-type]
        active_handoff=pending,  # type: ignore[arg-type]
    )
    assert not ai_dispatch_is_sendable(
        conversation=human,  # type: ignore[arg-type]
        message=message,  # type: ignore[arg-type]
        active_handoff=claimed,  # type: ignore[arg-type]
    )
    assert not ai_dispatch_is_sendable(
        conversation=human,  # type: ignore[arg-type]
        message=message,  # type: ignore[arg-type]
        active_handoff=different_pending,  # type: ignore[arg-type]
    )
    assert not ai_dispatch_is_sendable(
        conversation=human,  # type: ignore[arg-type]
        message=message,  # type: ignore[arg-type]
        active_handoff=staff_pending,  # type: ignore[arg-type]
    )


def test_ownership_cancellation_is_visible_on_dispatch_and_message() -> None:
    dispatch = SimpleNamespace(
        status="queued",
        last_error=None,
        next_attempt_at=datetime(2026, 8, 24, 22, 0, tzinfo=UTC),
        locked_at=None,
        metadata_json={},
    )
    message = SimpleNamespace(delivery_status="queued", metadata_json={})

    cancel_dispatch_for_ownership(
        dispatch,  # type: ignore[arg-type]
        message,  # type: ignore[arg-type]
        reason="human takeover",
    )

    assert dispatch.status == "cancelled"
    assert dispatch.next_attempt_at is None
    assert message.delivery_status == "cancelled"
    assert dispatch.metadata_json["cancelled_by_ownership"] is True
    assert message.metadata_json["cancelled_by_ownership"] is True


def test_message_schema_accepts_cancelled_delivery_status() -> None:
    now = datetime(2026, 8, 24, 22, 0, tzinfo=UTC)
    payload = MessageRead(
        id=uuid4(),
        workspace_id=uuid4(),
        conversation_id=uuid4(),
        sender_type="ai",
        direction="outbound",
        message_type="text",
        content="suppressed",
        external_message_id=None,
        delivery_status="cancelled",
        sent_by_user_id=None,
        metadata_json={},
        created_at=now,
        updated_at=now,
    )
    assert payload.delivery_status == "cancelled"


def test_outbox_claim_uses_conversation_then_dispatch_lock_and_ownership_guard() -> None:
    source = (_root() / "backend/app/services/channels.py").read_text(encoding="utf-8")
    start = source.index("def claim_dispatches(")
    end = source.index("def _reconcile_pending_delivery_events(", start)
    block = source[start:end]

    conversation_lock = block.index("select(Conversation)")
    dispatch_lock = block.index("select(MessageDispatch)", conversation_lock)
    assert conversation_lock < dispatch_lock
    assert ".with_for_update(skip_locked=True)" in block[conversation_lock:dispatch_lock]
    assert ".with_for_update(skip_locked=True)" in block[dispatch_lock:]
    assert "ai_dispatch_is_sendable(" in block
    assert "cancel_dispatch_for_ownership(" in block
    assert "other_processing" in block
    assert "allow_demo_reply_dispatch" in block
    assert "Message.in_reply_to_message_id.is_not(None)" in block


def test_staff_handoff_paths_quiesce_ai_and_safe_resume_waits_for_staff_outbox() -> None:
    source = (_root() / "backend/app/services/handoffs.py").read_text(encoding="utf-8")

    claim_start = source.index("def claim_handoff(")
    assign_start = source.index("def assign_handoff(")
    reply_start = source.index("def add_staff_reply(")
    resolve_start = source.index("def resolve_handoff(")

    claim = source[claim_start:assign_start]
    assign = source[assign_start:source.index("def ensure_active_workspace_user(", assign_start)]
    reply = source[reply_start:resolve_start]
    resolve = source[resolve_start:]

    assert "_quiesce_ai_before_staff(" in claim
    assert "_quiesce_ai_before_staff(" in assign
    assert "_quiesce_ai_before_staff(" in reply
    assert "ensure_staff_outbox_drained_before_ai(" in resolve


def test_agent_marks_handoff_ack_explicitly_for_outbox_policy() -> None:
    source = (_root() / "backend/app/services/agent_chat.py").read_text(encoding="utf-8")
    assert '"handoff_ack": handoff_ack_allowed' in source


def test_cancelled_message_migration_extends_only_delivery_status_constraint() -> None:
    migration = (_root() / "backend/alembic/versions/0021_msg_cancelled.py").read_text(
        encoding="utf-8"
    )
    assert 'down_revision: str | Sequence[str] | None = "0020_conv_ownership"' in migration
    assert '"message_delivery_status_valid"' in migration
    assert "'cancelled'" in migration


class _OwnershipDB:
    def __init__(self, rows=None, scalar_value=None) -> None:
        self.rows = list(rows or [])
        self.scalar_value = scalar_value

    def execute(self, statement):
        return list(self.rows)

    def scalar(self, statement):
        return self.scalar_value


def test_quiesce_cancels_queued_ai_before_staff_ownership() -> None:
    conversation = SimpleNamespace(id=uuid4(), workspace_id=uuid4())
    dispatch = SimpleNamespace(
        status="queued",
        locked_at=None,
        last_error=None,
        next_attempt_at=None,
        metadata_json={},
    )
    message = SimpleNamespace(delivery_status="queued", metadata_json={})
    db = _OwnershipDB(rows=[(dispatch, message)])

    cancelled = quiesce_ai_dispatches_for_human(
        db,  # type: ignore[arg-type]
        conversation=conversation,  # type: ignore[arg-type]
        now=datetime(2026, 8, 24, 22, 30, tzinfo=UTC),
    )

    assert cancelled == 1
    assert dispatch.status == "cancelled"
    assert message.delivery_status == "cancelled"


def test_quiesce_blocks_staff_while_ai_send_lease_is_fresh() -> None:
    now = datetime(2026, 8, 24, 22, 30, tzinfo=UTC)
    conversation = SimpleNamespace(id=uuid4(), workspace_id=uuid4())
    dispatch = SimpleNamespace(
        status="processing",
        locked_at=now,
        last_error=None,
        next_attempt_at=None,
        metadata_json={},
    )
    message = SimpleNamespace(delivery_status="queued", metadata_json={})
    db = _OwnershipDB(rows=[(dispatch, message)])

    with pytest.raises(OwnershipTransitionBlockedError, match="already being delivered"):
        quiesce_ai_dispatches_for_human(
            db,  # type: ignore[arg-type]
            conversation=conversation,  # type: ignore[arg-type]
            now=now,
        )

    assert dispatch.status == "processing"
    assert message.delivery_status == "queued"


def test_ai_handoff_creation_may_leave_existing_send_lease_but_staff_cannot() -> None:
    now = datetime(2026, 8, 24, 22, 30, tzinfo=UTC)
    conversation = SimpleNamespace(id=uuid4(), workspace_id=uuid4())
    dispatch = SimpleNamespace(
        status="processing",
        locked_at=now,
        last_error=None,
        next_attempt_at=None,
        metadata_json={},
    )
    message = SimpleNamespace(delivery_status="queued", metadata_json={})
    db = _OwnershipDB(rows=[(dispatch, message)])

    cancelled = quiesce_ai_dispatches_for_human(
        db,  # type: ignore[arg-type]
        conversation=conversation,  # type: ignore[arg-type]
        now=now,
        allow_inflight=True,
    )

    assert cancelled == 0
    assert dispatch.status == "processing"


def test_ai_resume_waits_for_pending_staff_dispatch() -> None:
    conversation = SimpleNamespace(id=uuid4(), workspace_id=uuid4())
    with pytest.raises(OwnershipTransitionBlockedError, match="staff reply"):
        ensure_staff_outbox_drained_before_ai(
            _OwnershipDB(scalar_value=uuid4()),  # type: ignore[arg-type]
            conversation=conversation,  # type: ignore[arg-type]
        )

    ensure_staff_outbox_drained_before_ai(
        _OwnershipDB(scalar_value=None),  # type: ignore[arg-type]
        conversation=conversation,  # type: ignore[arg-type]
    )
