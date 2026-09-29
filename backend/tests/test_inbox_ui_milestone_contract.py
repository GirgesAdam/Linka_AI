from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_inbox_queue_preserves_operational_filters_and_attention_axes() -> None:
    page = read("frontend/src/app/(dashboard)/inbox/page.tsx")
    for token in (
        'query.set("owner_type", filters.owner)',
        'query.set("status", filters.status)',
        'query.set("assigned_to_me", "true")',
        'query.set("unread_only", "true")',
        'query.set("q", filters.q)',
        "attentionLabel(conversation)",
        '<StatusBadge domain="conversation"',
        '<StatusBadge domain="priority"',
        "LiveRouteRefresh",
    ):
        assert token in page
    assert "teal-" not in page


def test_conversation_workspace_preserves_ownership_reply_and_followup_paths() -> None:
    page = read("frontend/src/app/(dashboard)/inbox/[conversationId]/page.tsx")
    for token in (
        "ConversationReadMarker",
        "InboxMessageBody",
        "InboxReplyForm",
        "takeOverConversation",
        "claimHandoff",
        "assignHandoff",
        "resolveHandoff",
        "sendWhatsappFollowup",
        "WHATSAPP_FREEFORM_WINDOW_MS",
        '<StatusBadge domain="message"',
        '<StatusBadge domain="handoff"',
    ):
        assert token in page
    assert "teal-" not in page


def test_inbox_server_actions_keep_business_contracts() -> None:
    actions = read("frontend/src/app/(dashboard)/inbox/actions.ts")
    assert '/claim`' in actions
    assert '/assign`' in actions
    assert '/takeover`' in actions
    assert '/messages`' in actions
    assert '/whatsapp-followup`' in actions
    assert '/resolve`' in actions
    assert 'conversation_status_after: closeConversation ? "closed" : "open"' in actions
    assert '/read`' in actions
