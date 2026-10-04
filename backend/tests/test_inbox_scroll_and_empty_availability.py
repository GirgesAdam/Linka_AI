from pathlib import Path
from types import SimpleNamespace

from app.agents.grounded_response import _deterministic_empty_availability_reply


def test_empty_reschedule_availability_never_claims_the_day_is_available() -> None:
    result = _deterministic_empty_availability_reply(
        semantic_decision=SimpleNamespace(capabilities=["appointment_reschedule"]),
        verified_data={
            "get_reschedule_options": {
                "ok": True,
                "date": "2026-09-09",
                "slots": [],
                "availability_windows": [],
                "matching_slot_count": 0,
                "requested_time_window": {
                    "not_before_time": None,
                    "not_after_time": None,
                },
            }
        },
    )

    assert result is not None
    reply, model = result
    assert "مفيش مواعيد متاحة" in reply
    assert "الأربعاء 9 سبتمبر" in reply
    assert "اختار" not in reply
    assert "قولي الوقت" not in reply
    assert model == "deterministic:verified-empty-get_reschedule_options"


def test_inbox_conversation_is_height_bounded_and_scrolls_inside_messages() -> None:
    backend = Path(__file__).resolve().parent.parent
    repo = backend.parent
    page = (
        repo / "frontend/src/app/(dashboard)/inbox/[conversationId]/page.tsx"
    ).read_text(encoding="utf-8")
    scroll = (
        repo
        / "frontend/src/app/(dashboard)/inbox/[conversationId]/conversation-scroll.tsx"
    ).read_text(encoding="utf-8")

    assert "h-[calc(100dvh-9rem)]" in page
    assert "min-h-0 flex-1 flex-col" in page
    assert "ConversationScroll messageCount={conversation.messages.length}" in page
    assert "shrink-0 border-t" in page

    assert "min-h-0 flex-1" in scroll
    assert "overflow-y-auto" in scroll
    assert "overscroll-contain" in scroll
    assert "container.scrollTop = container.scrollHeight" in scroll
    assert "distanceFromBottom < 96" in scroll
