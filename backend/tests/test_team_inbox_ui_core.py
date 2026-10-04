from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from app.schemas.inbox import (
    HandoffRead,
    InboxAssigneeRead,
    InboxConversationListItem,
    InboxMessageRead,
    InboxPatientRead,
)


def _root() -> Path:
    return Path(__file__).resolve().parents[2]


def test_conversation_list_schema_contains_inbox_metadata_without_llm_fields() -> None:
    conversation_id = uuid4()
    workspace_id = uuid4()
    patient_id = uuid4()
    user_id = uuid4()
    now = datetime(2026, 8, 24, 20, 0, tzinfo=UTC)

    item = InboxConversationListItem(
        id=conversation_id,
        workspace_id=workspace_id,
        patient_id=patient_id,
        channel="whatsapp",
        status="pending",
        owner_type="human",
        unread_count=3,
        assigned_user_id=user_id,
        assigned_user=InboxAssigneeRead(id=user_id, full_name="Mona", email="mona@example.com"),
        subject=None,
        started_at=now,
        last_message_at=now,
        patient=InboxPatientRead(
            id=patient_id,
            first_name="Nour",
            last_name=None,
            phone="+201000000000",
        ),
        active_handoff=HandoffRead(
            id=uuid4(),
            workspace_id=workspace_id,
            conversation_id=conversation_id,
            patient_id=patient_id,
            status="claimed",
            category="customer_request",
            priority="normal",
            source="staff",
            reason="Manual takeover",
            assigned_user_id=user_id,
            created_by_user_id=user_id,
            claimed_at=now,
            resolved_at=None,
            resolved_by_user_id=None,
            resolution_note=None,
            created_at=now,
            updated_at=now,
        ),
        last_message=InboxMessageRead(
            id=uuid4(),
            conversation_id=conversation_id,
            channel_connection_id=None,
            sender_type="patient",
            direction="inbound",
            message_type="text",
            content="محتاج اكلم حد",
            delivery_status="received",
            sent_by_user_id=None,
            metadata_json={},
            created_at=now,
        ),
    )

    assert item.owner_type == "human"
    assert item.unread_count == 3
    assert item.assigned_user and item.assigned_user.id == user_id
    assert item.last_message and item.last_message.sender_type == "patient"


def test_conversation_list_endpoint_is_database_driven_and_batched() -> None:
    source = (_root() / "backend/app/api/routes/inbox.py").read_text(encoding="utf-8")
    start = source.index("def list_inbox_conversations(")
    end = source.index('@router.get("/handoffs"', start)
    block = source[start:end]

    assert 'Conversation.workspace_id == access.workspace.id' in block
    assert 'Conversation.owner_type == owner_type' in block
    assert 'Conversation.status == conversation_status' in block
    assert 'Conversation.assigned_user_id == access.user.id' in block
    assert 'Conversation.unread_count > 0' in block
    assert 'func.row_number()' in block
    assert 'Message.conversation_id.in_(conversation_ids)' in block
    assert 'HandoffRequest.conversation_id.in_(conversation_ids)' in block
    assert "llm" not in block.lower()
    assert "keyword" not in block.lower()


def test_team_inbox_list_uses_conversation_endpoint_and_ownership_filters() -> None:
    source = (_root() / "frontend/src/app/(dashboard)/inbox/page.tsx").read_text(encoding="utf-8")

    assert "/inbox/conversations?" in source
    assert 'query.set("owner_type", filters.owner)' in source
    assert 'query.set("assigned_to_me", "true")' in source
    assert 'query.set("unread_only", "true")' in source
    assert "conversation.unread_count" in source
    assert "conversation.assigned_user" in source
    assert "conversation.last_message" in source
    assert "LiveRouteRefresh" in source


def test_conversation_detail_exposes_takeover_claim_assign_reply_and_read_paths() -> None:
    detail = (_root() / "frontend/src/app/(dashboard)/inbox/[conversationId]/page.tsx").read_text(
        encoding="utf-8"
    )
    reply_form = (
        _root() / "frontend/src/app/(dashboard)/inbox/[conversationId]/reply-form.tsx"
    ).read_text(encoding="utf-8")
    actions = (_root() / "frontend/src/app/(dashboard)/inbox/actions.ts").read_text(encoding="utf-8")
    marker = (_root() / "frontend/src/components/conversation-read-marker.tsx").read_text(encoding="utf-8")
    live_refresh = (_root() / "frontend/src/components/live-route-refresh.tsx").read_text(
        encoding="utf-8"
    )

    assert "takeOverConversation" in detail
    assert "claimHandoff" in detail
    assert "assignHandoff" in detail
    assert "InboxReplyForm" in detail
    assert "ConversationReadMarker" in detail
    assert "LiveRouteRefresh" in detail
    assert 'ctx.workspace.role === "admin"' in detail
    assert "سلّم المحادثة لـ Linka" in detail

    assert "replyToConversation" in reply_form
    assert 'event.key !== "Enter"' in reply_form
    assert "event.shiftKey" in reply_form
    assert "requestSubmit()" in reply_form

    assert "/takeover" in actions
    assert "/assign" in actions
    assert "/messages" in actions
    assert "/read" in actions
    assert "markConversationRead" in marker
    assert "router.refresh()" in marker
    assert "setInterval" in live_refresh
    assert "router.refresh()" in live_refresh


def test_inbox_idle_refresh_uses_lightweight_revision_and_count_paths() -> None:
    root = _root()
    list_page = (root / "frontend/src/app/(dashboard)/inbox/page.tsx").read_text(encoding="utf-8")
    detail_page = (root / "frontend/src/app/(dashboard)/inbox/[conversationId]/page.tsx").read_text(encoding="utf-8")
    live_refresh = (root / "frontend/src/components/live-route-refresh.tsx").read_text(encoding="utf-8")
    summary_route = (root / "frontend/src/app/api/inbox/summary/route.ts").read_text(encoding="utf-8")
    navigation = (root / "frontend/src/components/dashboard-navigation.tsx").read_text(encoding="utf-8")

    assert 'tiaRequest<{ revision: string }>("/inbox/revision")' in list_page
    assert 'conversation_id=' in detail_page and '/inbox/revision?' in detail_page
    assert 'fetch(watchUrl' in live_refresh
    assert 'payload.revision !== currentRevision' in live_refresh
    assert 'router.refresh()' in live_refresh
    assert 'tiaRequest<InboxSummary>("/inbox/summary")' in summary_route
    assert 'unread_only=true' not in summary_route
    assert 'pathname.startsWith("/inbox")' in navigation


def test_inbox_revision_covers_visible_list_delivery_patient_and_assignee_state() -> None:
    root = _root()
    backend = (root / "backend/app/api/routes/inbox.py").read_text(encoding="utf-8")
    list_page = (root / "frontend/src/app/(dashboard)/inbox/page.tsx").read_text(encoding="utf-8")

    start = backend.index("def inbox_revision(")
    end = backend.index('@router.get("/conversations"', start)
    revision_block = backend[start:end]

    assert 'conversation.last_message.delivery_status === "failed"' in list_page
    assert "conversation.patient.first_name" in list_page
    assert "conversation.assigned_user" in list_page
    assert "func.max(Message.updated_at)" in revision_block
    assert "func.max(Patient.updated_at)" in revision_block
    assert "func.max(User.updated_at)" in revision_block
    assert "func.max(ChannelConnection.updated_at)" in revision_block
