from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

from app.api.routes import inbox as inbox_routes

ROOT = Path(__file__).resolve().parents[2]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_inbox_idle_polling_budget_reduces_backend_requests_over_70_percent() -> None:
    page = read("frontend/src/app/(dashboard)/inbox/page.tsx")
    navigation = read("frontend/src/components/dashboard-navigation.tsx")
    assert 'endpoint={`/api/inbox/live-list?${query.toString()}`}' in page
    assert "intervalMs={10_000}" in page
    assert 'endpoint="/api/inbox/channel-health"' in page
    assert "intervalMs={60_000}" in page
    assert "const inboxPollIntervalMs = 15_000" in navigation

    baseline_route_reads = (60_000 // 3_000) * 2
    after_route_reads = (60_000 // 10_000) + (60_000 // 60_000)
    navigation_badge_reads = 60_000 // 15_000

    assert baseline_route_reads == 40
    assert after_route_reads == 7
    assert 1 - (after_route_reads / baseline_route_reads) >= 0.70

    baseline_with_navigation = baseline_route_reads + navigation_badge_reads
    after_with_navigation = after_route_reads + navigation_badge_reads
    assert baseline_with_navigation == 44
    assert after_with_navigation == 11
    assert 1 - (after_with_navigation / baseline_with_navigation) >= 0.70


def test_live_revision_refresh_pauses_hidden_checks_and_refreshes_only_on_change() -> None:
    source = read("frontend/src/components/live-route-refresh.tsx")

    assert 'document.visibilityState !== "visible"' in source
    assert 'document.visibilityState === "visible"' in source
    assert 'document.addEventListener("visibilitychange", handleVisibilityChange)' in source
    assert "payload.revision === currentRevision" in source
    assert "currentRevision = payload.revision" in source
    assert "router.refresh()" in source
    assert "window.setInterval" in source
    assert "if (!response.ok) return" in source
    assert "normal interval remains the only retry mechanism" in source
    assert "setTimeout" not in source


def test_conversation_idle_uses_compact_revision_not_full_history_polling() -> None:
    page = read("frontend/src/app/(dashboard)/inbox/[conversationId]/page.tsx")
    route = read("backend/app/api/routes/inbox.py")
    start = route.index("def get_inbox_conversation_live_revision(")
    end = route.index('@router.get(\n    "/conversations/{conversation_id}",', start)
    live_block = route[start:end]

    assert 'endpoint={`/api/inbox/conversations/${conversationId}/revision`}' in page
    assert "intervalMs={10_000}" in page
    assert "/live-revision`" in page
    assert "HandoffEvent" not in live_block
    assert ".limit(500)" not in live_block
    assert ".limit(100)" not in live_block
    assert "func.count(Message.id)" in live_block
    assert "func.max(Message.updated_at)" in live_block

    baseline_full_history_loads = 60_000 // 3_000
    after_idle_full_history_loads = 0
    assert baseline_full_history_loads == 20
    assert after_idle_full_history_loads == 0


def test_conversation_live_revision_changes_for_messages_delivery_and_handoffs(monkeypatch) -> None:
    workspace_id = uuid4()
    conversation_id = uuid4()
    patient_id = uuid4()
    user_id = uuid4()
    handoff_id = uuid4()
    now = datetime(2026, 10, 27, 15, 0, tzinfo=UTC)
    conversation = SimpleNamespace(
        id=conversation_id,
        patient_id=patient_id,
        assigned_user_id=user_id,
        updated_at=now,
        status="open",
        owner_type="human",
        unread_count=0,
        last_message_at=now,
        closed_at=None,
        ownership_changed_at=now,
    )
    handoff = SimpleNamespace(
        id=handoff_id,
        updated_at=now,
        status="claimed",
        priority="normal",
        assigned_user_id=user_id,
    )
    access = SimpleNamespace(workspace=SimpleNamespace(id=workspace_id))

    monkeypatch.setattr(inbox_routes, "_get_conversation", MagicMock(return_value=conversation))
    active_handoff = MagicMock(return_value=handoff)
    monkeypatch.setattr(inbox_routes, "get_active_handoff", active_handoff)

    db = MagicMock()
    db.scalar.side_effect = [now, now]
    db.execute.return_value.one.return_value = (2, now)
    first = inbox_routes.get_inbox_conversation_live_revision(
        conversation_id, access=access, db=db
    )

    assert len(first.revision) == 64
    inbox_routes._get_conversation.assert_called_with(
        db,
        workspace_id=workspace_id,
        conversation_id=conversation_id,
    )
    active_handoff.assert_called_with(
        db, workspace_id=workspace_id, conversation_id=conversation_id
    )
    message_stmt = db.execute.call_args.args[0]
    message_sql = str(message_stmt)
    assert "messages.workspace_id" in message_sql
    assert "messages.conversation_id" in message_sql

    db.scalar.side_effect = [now, now]
    db.execute.return_value.one.return_value = (2, now)
    unchanged = inbox_routes.get_inbox_conversation_live_revision(
        conversation_id, access=access, db=db
    )
    assert unchanged.revision == first.revision

    db.scalar.side_effect = [now, now]
    db.execute.return_value.one.return_value = (3, now + timedelta(seconds=1))
    message_added = inbox_routes.get_inbox_conversation_live_revision(
        conversation_id, access=access, db=db
    )
    assert message_added.revision != first.revision

    db.scalar.side_effect = [now, now]
    db.execute.return_value.one.return_value = (3, now + timedelta(seconds=2))
    delivery_changed = inbox_routes.get_inbox_conversation_live_revision(
        conversation_id, access=access, db=db
    )
    assert delivery_changed.revision != message_added.revision

    handoff.updated_at = now + timedelta(seconds=3)
    handoff.priority = "high"
    db.scalar.side_effect = [now, now]
    db.execute.return_value.one.return_value = (3, now + timedelta(seconds=2))
    handoff_changed = inbox_routes.get_inbox_conversation_live_revision(
        conversation_id, access=access, db=db
    )
    assert handoff_changed.revision != delivery_changed.revision


def test_navigation_unread_summary_uses_count_endpoint_not_full_conversation_payload() -> None:
    frontend_route = read("frontend/src/app/api/inbox/summary/route.ts")
    backend_route = read("backend/app/api/routes/inbox.py")
    start = backend_route.index("def get_inbox_summary(")
    end = backend_route.index('@router.get("/conversations"', start)
    summary_block = backend_route[start:end]

    assert 'tiaRequest<InboxSummary>("/inbox/summary")' in frontend_route
    assert "/inbox/conversations?" not in frontend_route
    assert "func.count(Conversation.id)" in summary_block
    assert "Conversation.workspace_id == access.workspace.id" in summary_block
    assert 'Conversation.status == "open"' in summary_block
    assert "Conversation.unread_count > 0" in summary_block

    workspace_id = uuid4()
    db = MagicMock()
    db.scalar.return_value = 12
    summary = inbox_routes.get_inbox_summary(
        access=SimpleNamespace(workspace=SimpleNamespace(id=workspace_id)),
        db=db,
    )
    assert summary.unread_conversations == 12
    assert "conversations.workspace_id" in str(db.scalar.call_args.args[0])


def test_live_list_proxy_preserves_filters_search_and_pagination() -> None:
    route = read("frontend/src/app/api/inbox/live-list/route.ts")
    for key in (
        '"limit"',
        '"offset"',
        '"owner_type"',
        '"status"',
        '"assigned_to_me"',
        '"unread_only"',
        '"q"',
    ):
        assert key in route
    assert "ALLOWED_QUERY_KEYS.has(key)" in route
    assert "/inbox/conversations/live-revision?" in route
    assert "/inbox/conversations?${query.toString()}" not in route

    backend_route = read("backend/app/api/routes/inbox.py")
    start = backend_route.index("def get_inbox_conversations_live_revision(")
    end = backend_route.index('@router.get("/handoffs"', start)
    live_list_block = backend_route[start:end]
    assert "list_inbox_conversations(" in live_list_block
    assert "owner_type=owner_type" in live_list_block
    assert "conversation_status=conversation_status" in live_list_block
    assert "assigned_to_me=assigned_to_me" in live_list_block
    assert "unread_only=unread_only" in live_list_block
    assert "q=q" in live_list_block
    assert "limit=limit" in live_list_block
    assert "offset=offset" in live_list_block
