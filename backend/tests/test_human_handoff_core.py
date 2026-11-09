from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

import app.services.handoffs as handoff_service
from app.models.handoff_event import HandoffEvent
from app.services.handoffs import HandoffStateError, add_staff_reply, create_handoff


class _FakeDB:
    def __init__(self) -> None:
        self.added: list[object] = []
        self.commits = 0
        self.refreshes: list[object] = []

    def add(self, value: object) -> None:
        self.added.append(value)

    def flush(self, *args) -> None:
        for value in self.added:
            if hasattr(value, "id") and getattr(value, "id", None) is None:
                value.id = uuid4()

    def execute(self, *args, **kwargs):
        return []

    def commit(self) -> None:
        self.commits += 1

    def refresh(self, value: object) -> None:
        self.refreshes.append(value)


def _conversation(**overrides: object) -> SimpleNamespace:
    workspace_id = overrides.pop("workspace_id", uuid4())
    patient_id = overrides.pop("patient_id", uuid4())
    values: dict[str, object] = {
        "id": uuid4(),
        "workspace_id": workspace_id,
        "patient_id": patient_id,
        "channel": "web",
        "owner_type": "ai",
        "status": "open",
        "assigned_user_id": None,
        "unread_count": 0,
        "ownership_changed_at": datetime(2026, 8, 24, tzinfo=UTC),
        "closed_at": None,
        "last_message_at": None,
        "channel_connection_id": uuid4(),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _patient(conversation: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(
        id=conversation.patient_id,
        workspace_id=conversation.workspace_id,
    )


def _backend() -> Path:
    return Path(__file__).resolve().parent.parent


def _function_block(source: str, name: str, next_name: str) -> str:
    start = source.index(f"def {name}(")
    end = source.index(f"def {next_name}(", start)
    return source[start:end]


def test_handoff_creation_locks_conversation_before_active_handoff_lookup() -> None:
    source = (_backend() / "app/services/handoffs.py").read_text(encoding="utf-8")
    block = _function_block(source, "create_handoff", "_ensure_handoff_context")

    conversation_lock = block.index("lock_conversation_ownership(")
    handoff_lock = block.index("get_active_handoff(")
    assert conversation_lock < handoff_lock
    assert "for_update=True" in block[handoff_lock:]


def test_inbox_handoff_mutations_use_consistent_lock_order() -> None:
    source = (_backend() / "app/api/routes/inbox.py").read_text(encoding="utf-8")
    block = _function_block(source, "_lock_handoff_and_conversation", "_queue_item")

    conversation_lock = block.index("conversation = _get_conversation(")
    handoff_lock = block.index("handoff = _get_handoff(", conversation_lock)
    assert conversation_lock < handoff_lock
    assert "for_update=True" in block[conversation_lock:handoff_lock]
    assert "for_update=True" in block[handoff_lock:]
    assert "populate_existing=True" in source


def test_create_handoff_reuses_active_row_and_keeps_human_ownership(monkeypatch: pytest.MonkeyPatch) -> None:
    conversation = _conversation()
    patient = _patient(conversation)
    existing = SimpleNamespace(
        id=uuid4(),
        workspace_id=conversation.workspace_id,
        conversation_id=conversation.id,
        patient_id=patient.id,
        category="other",
        priority="normal",
        reason="initial reason",
        assigned_user_id=None,
    )
    db = _FakeDB()

    monkeypatch.setattr(
        handoff_service,
        "lock_conversation_ownership",
        lambda *args, **kwargs: conversation,
    )
    monkeypatch.setattr(
        handoff_service,
        "get_active_handoff",
        lambda *args, **kwargs: existing,
    )

    result = create_handoff(
        db,  # type: ignore[arg-type]
        workspace_id=conversation.workspace_id,
        conversation=conversation,  # type: ignore[arg-type]
        patient=patient,  # type: ignore[arg-type]
        reason="customer asked for a person",
        category="customer_request",
        priority="high",
        source="ai",
        commit=False,
    )

    assert result is existing
    assert existing.category == "customer_request"
    assert existing.priority == "high"
    assert "customer asked for a person" in existing.reason
    assert conversation.owner_type == "human"
    assert conversation.status == "pending"
    assert db.commits == 0


def test_create_handoff_rejects_cross_workspace_or_invalid_source() -> None:
    conversation = _conversation()
    patient = _patient(conversation)
    db = _FakeDB()

    other_workspace = uuid4()
    with pytest.raises(HandoffStateError, match="workspace"):
        create_handoff(
            db,  # type: ignore[arg-type]
            workspace_id=other_workspace,
            conversation=conversation,  # type: ignore[arg-type]
            patient=patient,  # type: ignore[arg-type]
            reason="test",
            commit=False,
        )

    with pytest.raises(HandoffStateError, match="source"):
        create_handoff(
            db,  # type: ignore[arg-type]
            workspace_id=conversation.workspace_id,
            conversation=conversation,  # type: ignore[arg-type]
            patient=patient,  # type: ignore[arg-type]
            reason="test",
            source="untrusted",
            commit=False,
        )


def test_staff_reply_can_participate_in_outer_atomic_transaction() -> None:
    user_id = uuid4()
    conversation = _conversation(
        owner_type="human",
        status="pending",
        assigned_user_id=user_id,
        unread_count=3,
    )
    handoff = SimpleNamespace(
        id=uuid4(),
        workspace_id=conversation.workspace_id,
        conversation_id=conversation.id,
        status="claimed",
        assigned_user_id=user_id,
    )
    user = SimpleNamespace(id=user_id)
    db = _FakeDB()

    message = add_staff_reply(
        db,  # type: ignore[arg-type]
        handoff=handoff,  # type: ignore[arg-type]
        conversation=conversation,  # type: ignore[arg-type]
        user=user,  # type: ignore[arg-type]
        content="  حاضر، هراجع مع حضرتك التفاصيل.  ",
        commit=False,
    )

    assert db.commits == 0
    assert message.content == "حاضر، هراجع مع حضرتك التفاصيل."
    assert message.sender_type == "staff"
    assert message.direction == "outbound"
    assert conversation.unread_count == 0
    events = [value for value in db.added if isinstance(value, HandoffEvent)]
    assert len(events) == 1
    assert events[0].event_type == "staff_replied"


def test_staff_reply_rejects_wrong_assignment_context_and_empty_content() -> None:
    user_id = uuid4()
    conversation = _conversation(
        owner_type="human",
        status="pending",
        assigned_user_id=user_id,
    )
    db = _FakeDB()

    wrong_handoff = SimpleNamespace(
        id=uuid4(),
        workspace_id=conversation.workspace_id,
        conversation_id=uuid4(),
        status="claimed",
        assigned_user_id=user_id,
    )
    with pytest.raises(HandoffStateError, match="does not belong"):
        add_staff_reply(
            db,  # type: ignore[arg-type]
            handoff=wrong_handoff,  # type: ignore[arg-type]
            conversation=conversation,  # type: ignore[arg-type]
            user=SimpleNamespace(id=user_id),  # type: ignore[arg-type]
            content="hello",
            commit=False,
        )

    handoff = SimpleNamespace(
        id=uuid4(),
        workspace_id=conversation.workspace_id,
        conversation_id=conversation.id,
        status="claimed",
        assigned_user_id=user_id,
    )
    with pytest.raises(HandoffStateError, match="empty"):
        add_staff_reply(
            db,  # type: ignore[arg-type]
            handoff=handoff,  # type: ignore[arg-type]
            conversation=conversation,  # type: ignore[arg-type]
            user=SimpleNamespace(id=user_id),  # type: ignore[arg-type]
            content="   ",
            commit=False,
        )


def test_staff_reply_and_outbox_are_committed_atomically_by_route() -> None:
    source = (_backend() / "app/api/routes/inbox.py").read_text(encoding="utf-8")
    block = _function_block(source, "send_staff_reply", "resolve_inbox_handoff")

    add_pos = block.index("message = add_staff_reply(")
    dispatch_pos = block.index("dispatch = queue_message_dispatch(")
    commit_pos = block.index("db.commit()")
    assert add_pos < dispatch_pos < commit_pos
    assert "commit=False" in block[add_pos:dispatch_pos]
    assert "commit=False" in block[dispatch_pos:commit_pos]
    assert block.count("db.commit()") == 1


def test_manual_takeover_is_staff_sourced_and_claims_the_handoff() -> None:
    source = (_backend() / "app/api/routes/inbox.py").read_text(encoding="utf-8")
    block = _function_block(source, "take_over_conversation", "send_staff_reply")

    assert 'source="staff"' in block
    assert "created_by_user_id=access.user.id" in block
    assert "commit=False" in block
    assert block.index("create_handoff(") < block.index("claim_handoff(")
    assert 'require_page_permission("inbox.detail", legacy_access="reader")' in block
