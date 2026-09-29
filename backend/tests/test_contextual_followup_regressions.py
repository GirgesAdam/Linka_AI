from datetime import datetime
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from app.agents.semantic_actions import select_slot_from_structured_selection
from app.agents.turn_interpreter import (
    _interpreter_system_prompt,
    _latest_customer_turn,
    _option_summary,
    _recent_conversation_excerpt,
    interpret_customer_turn,
)
from app.core.config import settings

SERVICE_A = "22222222-2222-4222-8222-222222222222"
SERVICE_B = "33333333-3333-4333-8333-333333333333"
DOCTOR_A = "11111111-1111-4111-8111-111111111111"
DOCTOR_B = "44444444-4444-4444-8444-444444444444"
APPOINTMENT_ID = "55555555-5555-4555-8555-555555555555"
DATE = "2026-09-12"
TIME_PHRASE = "من ٣"


def _catalog() -> dict[str, object]:
    return {
        "services": [
            {
                "id": SERVICE_A,
                "name": "ليزر إزالة الشعر - إبط",
                "doctor_ids": [DOCTOR_A, DOCTOR_B],
            },
            {
                "id": SERVICE_B,
                "name": "HydraFacial",
                "doctor_ids": [DOCTOR_B],
            },
        ],
        "branches": [],
        "doctors": [
            {
                "id": DOCTOR_A,
                "name": "د. سارة نبيل",
                "service_ids": [SERVICE_A],
            },
            {
                "id": DOCTOR_B,
                "name": "د. منى عادل",
                "service_ids": [SERVICE_A, SERVICE_B],
            },
        ],
        "appointments": [
            {
                "id": APPOINTMENT_ID,
                "appointment_id": APPOINTMENT_ID,
                "service_id": SERVICE_A,
                "service_name": "ليزر إزالة الشعر - إبط",
                "doctor_id": DOCTOR_A,
                "doctor_name": "د. سارة نبيل",
                "status": "confirmed",
                "start_local": "2026-09-11T16:00:00+03:00",
                "end_local": "2026-09-11T16:30:00+03:00",
            }
        ],
    }


def _doctor_choice_flow() -> SimpleNamespace:
    return SimpleNamespace(
        is_active=True,
        flow_type="booking",
        status="awaiting_option_selection",
        capabilities=["availability_discovery", "appointment_creation"],
        entity_state={"service_id": SERVICE_A, "requested_date": DATE},
        missing_information=["doctor"],
        option_snapshot={
            "doctors": [
                {"doctor_id": DOCTOR_A, "doctor_name": "د. سارة نبيل"},
                {"doctor_id": DOCTOR_B, "doctor_name": "د. منى عادل"},
            ]
        },
    )


def _service_choice_flow() -> SimpleNamespace:
    return SimpleNamespace(
        is_active=True,
        flow_type="booking",
        status="awaiting_option_selection",
        capabilities=["availability_discovery", "appointment_creation"],
        entity_state={"requested_date": DATE},
        missing_information=["service"],
        option_snapshot={
            "services": [
                {"service_id": SERVICE_A, "service_name": "ليزر إزالة الشعر - إبط"},
                {"service_id": SERVICE_B, "service_name": "HydraFacial"},
            ]
        },
    )


def _doctor_reference_after_side_read_history() -> list:
    return [
        AIMessage(
            content=(
                "لقيت دكتورين مناسبين: 1) د. سارة نبيل 2) د. منى عادل. "
                "اختار الدكتور اللي يناسبك."
            )
        ),
        HumanMessage(content="والجلسة بكام؟"),
        AIMessage(content="سعر الجلسة 550 جنيه."),
        HumanMessage(content="خلاص التانية"),
    ]


def _service_reference_after_side_read_history() -> list:
    return [
        AIMessage(
            content=(
                "الخدمات المطابقة: 1) ليزر إزالة الشعر - إبط 2) HydraFacial. "
                "اختار الخدمة اللي تقصدها."
            )
        ),
        HumanMessage(content="والتانية بكام؟"),
        AIMessage(content="HydraFacial سعرها 1200 جنيه."),
        HumanMessage(content="تمام عايز دي"),
    ]


def _reschedule_flow(*, presented: bool) -> SimpleNamespace:
    status = "collecting_requirements"
    option_snapshot: dict[str, object] = {}
    if presented:
        status = "awaiting_option_selection"
        option_snapshot = {
            "date": DATE,
            "availability_windows": [
                {
                    "doctor_id": DOCTOR_A,
                    "doctor_name": "د. سارة نبيل",
                    "start_time_24h": "15:00",
                    "end_time_24h": "17:00",
                }
            ],
            "slots": [
                {
                    "doctor_id": DOCTOR_A,
                    "doctor_name": "د. سارة نبيل",
                    "start_time_24h": "15:00",
                    "end_time_24h": "15:30",
                },
                {
                    "doctor_id": DOCTOR_A,
                    "doctor_name": "د. سارة نبيل",
                    "start_time_24h": "15:30",
                    "end_time_24h": "16:00",
                },
            ],
        }
    return SimpleNamespace(
        is_active=True,
        flow_type="appointment_reschedule",
        status=status,
        capabilities=["availability_discovery", "appointment_reschedule"],
        entity_state={
            "appointment_id": APPOINTMENT_ID,
            "service_id": SERVICE_A,
            "doctor_id": DOCTOR_A,
            "requested_date": DATE,
        },
        missing_information=[],
        option_snapshot=option_snapshot,
    )


def _reschedule_selection_history() -> list:
    return [
        AIMessage(
            content=(
                "المواعيد البديلة المتاحة مع د. سارة من 3 م لـ5 م. "
                "اختار الوقت اللي تحب أنقل الحجز عليه."
            )
        ),
        HumanMessage(content=TIME_PHRASE),
    ]


def _reschedule_search_history() -> list:
    return [
        AIMessage(content="تمام، تحب أدورلك على مواعيد بديلة تبدأ من الساعة كام؟"),
        HumanMessage(content=TIME_PHRASE),
    ]


def _ambiguous_time_flow() -> SimpleNamespace:
    return SimpleNamespace(
        is_active=True,
        flow_type="booking",
        status="awaiting_option_selection",
        capabilities=["availability_discovery", "appointment_creation"],
        entity_state={"service_id": SERVICE_A, "requested_date": DATE},
        missing_information=["doctor"],
        option_snapshot={
            "date": DATE,
            "availability_windows": [
                {
                    "doctor_id": DOCTOR_A,
                    "doctor_name": "د. سارة نبيل",
                    "start_time_24h": "16:00",
                    "end_time_24h": "17:00",
                },
                {
                    "doctor_id": DOCTOR_B,
                    "doctor_name": "د. منى عادل",
                    "start_time_24h": "16:00",
                    "end_time_24h": "17:00",
                },
            ],
            "slots": [
                {
                    "doctor_id": DOCTOR_A,
                    "doctor_name": "د. سارة نبيل",
                    "start_time_24h": "16:00",
                    "end_time_24h": "16:30",
                },
                {
                    "doctor_id": DOCTOR_B,
                    "doctor_name": "د. منى عادل",
                    "start_time_24h": "16:00",
                    "end_time_24h": "16:30",
                },
            ],
        },
    )


def test_verified_doctor_options_survive_one_harmless_side_read() -> None:
    history = _doctor_reference_after_side_read_history()
    context = _recent_conversation_excerpt(history)
    options = _option_summary(_doctor_choice_flow())

    assert _latest_customer_turn(history) == "خلاص التانية"
    assert "1) د. سارة نبيل 2) د. منى عادل" in context
    assert "سعر الجلسة 550 جنيه" in context
    assert options["doctors"][1]["id"] == DOCTOR_B


def test_verified_service_options_survive_one_harmless_side_read() -> None:
    history = _service_reference_after_side_read_history()
    context = _recent_conversation_excerpt(history)
    options = _option_summary(_service_choice_flow())

    assert _latest_customer_turn(history) == "تمام عايز دي"
    assert "1) ليزر إزالة الشعر - إبط 2) HydraFacial" in context
    assert "HydraFacial سعرها 1200 جنيه" in context
    assert options["services"][1]["id"] == SERVICE_B


def test_reschedule_same_phrase_receives_distinct_selection_and_search_context() -> None:
    selection_history = _reschedule_selection_history()
    search_history = _reschedule_search_history()

    assert _latest_customer_turn(selection_history) == TIME_PHRASE
    assert _latest_customer_turn(search_history) == TIME_PHRASE
    assert "أنقل الحجز عليه" in _recent_conversation_excerpt(selection_history)
    assert "تبدأ من الساعة كام" in _recent_conversation_excerpt(search_history)
    assert _option_summary(_reschedule_flow(presented=True))["availability_windows"]
    assert _option_summary(_reschedule_flow(presented=False)) == {}


def test_duplicate_clock_time_is_not_a_deterministic_slot_choice_without_doctor() -> None:
    booking_output = {
        "ok": True,
        "slots": [
            {"start_time_24h": "16:00", "doctor_id": DOCTOR_A},
            {"start_time_24h": "16:00", "doctor_id": DOCTOR_B},
        ],
    }

    selected = select_slot_from_structured_selection(
        booking_output,
        selection_index=None,
        selection_time="16:00",
        doctor_id=None,
    )

    assert selected is None


def test_semantic_contract_covers_reference_persistence_reschedule_context_and_ambiguity() -> None:
    prompt = _interpreter_system_prompt(
        timezone_name="Africa/Cairo",
        local_now=datetime(2026, 9, 9, 14, 0),
        active_flow=True,
    )

    assert "verified service/doctor option set" in prompt
    assert "harmless side read" in prompt
    assert "intervening factual answer is not a new option list" in prompt
    assert "presented replacement availability window" in prompt
    assert "replacement search should begin/end" in prompt
    assert "same exact time belongs to multiple presented doctors" in prompt


@pytest.mark.skipif(
    not settings.openai_api_key,
    reason="Live contextual regression uses the configured Linka OpenAI model.",
)
def test_live_reference_resolution_survives_side_reads() -> None:
    local_now = datetime(2026, 9, 9, 14, 0)
    catalog = _catalog()

    doctor = interpret_customer_turn(
        flow=_doctor_choice_flow(),
        history=_doctor_reference_after_side_read_history(),
        timezone_name="Africa/Cairo",
        local_now=local_now,
        clinic_catalog=catalog,
    )
    assert doctor.entity_hints.doctor_id != DOCTOR_A
    assert doctor.entity_hints.doctor_id == DOCTOR_B or doctor.selection_index == 2

    service = interpret_customer_turn(
        flow=_service_choice_flow(),
        history=_service_reference_after_side_read_history(),
        timezone_name="Africa/Cairo",
        local_now=local_now,
        clinic_catalog=catalog,
    )
    assert service.entity_hints.service_id != SERVICE_A
    assert service.entity_hints.service_id == SERVICE_B or service.selection_index == 2


@pytest.mark.skipif(
    not settings.openai_api_key,
    reason="Live contextual regression uses the configured Linka OpenAI model.",
)
def test_live_reschedule_same_phrase_changes_meaning_with_context() -> None:
    local_now = datetime(2026, 9, 9, 14, 0)
    catalog = _catalog()

    selection = interpret_customer_turn(
        flow=_reschedule_flow(presented=True),
        history=_reschedule_selection_history(),
        timezone_name="Africa/Cairo",
        local_now=local_now,
        clinic_catalog=catalog,
    )
    assert selection.action == "select_option"
    assert selection.selection_time == "15:00"
    assert selection.entity_hints.not_before_time is None
    assert "appointment_reschedule" in selection.capabilities

    search = interpret_customer_turn(
        flow=_reschedule_flow(presented=False),
        history=_reschedule_search_history(),
        timezone_name="Africa/Cairo",
        local_now=local_now,
        clinic_catalog=catalog,
    )
    assert search.action == "modify"
    assert search.selection_time is None
    assert search.entity_hints.not_before_time == "15:00"


@pytest.mark.skipif(
    not settings.openai_api_key,
    reason="Live contextual regression uses the configured Linka OpenAI model.",
)
def test_live_ambiguous_same_time_does_not_guess_doctor() -> None:
    decision = interpret_customer_turn(
        flow=_ambiguous_time_flow(),
        history=[
            AIMessage(
                content=(
                    "الساعة 4 م متاحة مع د. سارة نبيل ومع د. منى عادل. "
                    "اختار الدكتور اللي يناسبك."
                )
            ),
            HumanMessage(content="الساعة 4"),
        ],
        timezone_name="Africa/Cairo",
        local_now=datetime(2026, 9, 9, 14, 0),
        clinic_catalog=_catalog(),
    )

    assert decision.entity_hints.doctor_id is None
    assert not (decision.action == "select_option" and decision.selection_time == "16:00")
