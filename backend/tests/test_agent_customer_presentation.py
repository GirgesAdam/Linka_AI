from pathlib import Path

from app.agents.availability_presentation import format_availability_windows_reply
from app.agents.doctor_names import format_doctor_name
from app.agents.semantic_actions import format_reschedule_success


def test_availability_uses_neutral_doctor_wording() -> None:
    reply = format_availability_windows_reply(
        {
            "ok": True,
            "date": "2026-09-05",
            "availability_windows": [
                {
                    "doctor_name": "مريم حسن",
                    "start_local": "2026-09-05T15:00:00+03:00",
                    "end_local": "2026-09-05T18:00:00+03:00",
                }
            ],
        },
        booking_authorized=False,
    )

    assert reply is not None
    assert "المتاح مع د. مريم حسن من 3 م لـ6 م." in reply
    assert "د. د. مريم" not in reply
    assert "مريم حسن متاح" not in reply


def test_doctor_name_normalizer_never_duplicates_known_prefixes() -> None:
    assert format_doctor_name("مريم", arabic=True) == "د. مريم"
    assert format_doctor_name("د. مريم", arabic=True) == "د. مريم"
    assert format_doctor_name("دكتور مريم", arabic=True) == "دكتور مريم"
    assert format_doctor_name("دكتورة مريم", arabic=True) == "دكتورة مريم"
    assert format_doctor_name("Dr. Maryam", arabic=True) == "Dr. Maryam"


def test_legacy_unavailable_renderer_contains_no_system_language() -> None:
    reply = format_availability_windows_reply(
        {
            "ok": True,
            "date": "2026-09-05",
            "requested_time_unavailable": True,
            "requested_start_time": "19:00",
            "availability_windows": [],
            "slots": [],
        },
        booking_authorized=False,
    )

    assert reply is not None
    for phrase in ("نطاق البحث", "النطاق اللي اتفحص", "البيانات", "تم التحقق", "fallback", "validation"):
        assert phrase not in reply


def test_reschedule_confirmation_uses_verified_details() -> None:
    reply = format_reschedule_success(
        {
            "doctor": "نور علي",
            "start_local": "2026-09-05T17:00:00+03:00",
        }
    )

    assert reply == "تمام، الموعد اتغيّر ليوم 05/09/2026 الساعة 5 م مع نور علي."


def test_structured_reschedule_uses_verified_detail_formatter() -> None:
    backend = Path(__file__).resolve().parent.parent
    source = (backend / "app/services/agent_chat.py").read_text(encoding="utf-8")

    assert "format_reschedule_success(appointment)" in source
