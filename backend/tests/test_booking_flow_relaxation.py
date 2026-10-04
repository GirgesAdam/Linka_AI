from types import SimpleNamespace

from app.agents.semantic_actions import format_verified_tool_fallback
from app.agents.turn_models import FlowTurnDecision, SemanticEntityHints
from app.services.agent_chat import _merge_flow_entity_state


def _hints(**values):
    return SemanticEntityHints(
        service_query=values.get("service_query"),
        branch_query=values.get("branch_query"),
        doctor_query=values.get("doctor_query"),
        requested_date=values.get("requested_date"),
        requested_start_time=values.get("requested_start_time"),
        not_before_time=values.get("not_before_time"),
        not_after_time=values.get("not_after_time"),
        appointment_reference=values.get("appointment_reference"),
    )


def _turn(*, hints, clear_entity_fields):
    return FlowTurnDecision(
        action="modify",
        capabilities=["availability_discovery"],
        risk_flags=[],
        entity_hints=hints,
        clear_entity_fields=clear_entity_fields,
        selection_index=None,
        selection_time=None,
        missing_information=[],
        recommended_handoff_category="other",
        recommended_handoff_priority="normal",
        confidence=0.99,
        reason="customer broadened availability",
    )


def test_semantic_relaxation_clears_stale_exact_time_but_keeps_booking_identity() -> None:
    existing = {
        "service_query": "ليزر إزالة الشعر",
        "branch_query": "مدينة نصر",
        "doctor_query": "احمد محمود",
        "requested_date": "2026-08-25",
        "requested_start_time": "18:00",
        "requested_time_window": {
            "not_before_time": None,
            "not_after_time": None,
        },
    }
    turn = _turn(
        hints=_hints(
            service_query="ليزر إزالة الشعر",
            branch_query="مدينة نصر",
            doctor_query="احمد محمود",
            requested_date="2026-08-25",
        ),
        clear_entity_fields=["requested_start_time"],
    )

    merged = _merge_flow_entity_state(existing, turn)

    assert merged["service_query"] == "ليزر إزالة الشعر"
    assert merged["branch_query"] == "مدينة نصر"
    assert merged["doctor_query"] == "احمد محمود"
    assert merged["requested_date"] == "2026-08-25"
    assert "requested_start_time" not in merged
    assert "not_before_time" not in merged
    assert "not_after_time" not in merged
    assert "requested_time_window" not in merged


def test_omitted_fields_do_not_clear_known_requirements_without_semantic_clear() -> None:
    existing = {
        "service_query": "ليزر إزالة الشعر",
        "requested_date": "2026-08-25",
        "not_before_time": "18:00",
        "not_after_time": "18:00",
    }
    turn = _turn(hints=_hints(), clear_entity_fields=[])

    assert _merge_flow_entity_state(existing, turn) == existing


def test_rejecting_presented_date_advances_next_availability_search() -> None:
    existing = {
        "service_query": "ليزر إزالة الشعر",
        "requested_date": "2026-09-08",
        "date": "2026-09-08",
        "not_before_time": "17:00",
    }
    turn = _turn(
        hints=_hints(requested_date="2026-09-08", not_before_time="17:00"),
        clear_entity_fields=["requested_date"],
    )

    merged = _merge_flow_entity_state(existing, turn)

    assert merged["availability_search_after_date"] == "2026-09-08"
    assert "requested_date" not in merged
    assert "date" not in merged
    assert merged["not_before_time"] == "17:00"


def test_new_time_requirement_invalidates_derived_old_time_window() -> None:
    existing = {
        "requested_date": "2026-08-25",
        "not_before_time": "18:00",
        "not_after_time": "18:00",
        "requested_time_window": {
            "not_before_time": "18:00",
            "not_after_time": "18:00",
        },
    }
    turn = _turn(
        hints=_hints(requested_date="2026-08-25", not_before_time="20:00"),
        clear_entity_fields=["not_after_time"],
    )

    merged = _merge_flow_entity_state(existing, turn)

    assert merged["not_before_time"] == "20:00"
    assert "not_after_time" not in merged
    assert "requested_time_window" not in merged


def test_exact_unavailable_time_formats_verified_nearby_slots_immediately() -> None:
    reply = format_verified_tool_fallback(
        "get_booking_options",
        {
            "ok": True,
            "date": "2026-08-25",
            "requested_start_time": "18:00",
            "requested_time_window": {
                "not_before_time": None,
                "not_after_time": None,
            },
            "requested_time_unavailable": True,
            "matching_slot_count": 0,
            "slots": [
                {
                    "start_time_24h": "17:45",
                    "doctor_name": "أحمد محمود",
                    "branch_name": "مدينة نصر",
                },
                {
                    "start_time_24h": "20:00",
                    "doctor_name": "أحمد محمود",
                    "branch_name": "مدينة نصر",
                },
            ],
        },
    )

    assert reply is not None
    assert "18:00" in reply
    assert "الثلاثاء 25 أغسطس" in reply
    assert "17:45" in reply
    assert "20:00" in reply
    assert "اختار الوقت" in reply


def test_flow_decision_defaults_to_no_clear_for_backward_compatibility() -> None:
    turn = FlowTurnDecision(
        action="continue",
        capabilities=["availability_discovery"],
        risk_flags=[],
        entity_hints=_hints(requested_date="2026-08-25"),
        selection_index=None,
        selection_time=None,
        missing_information=[],
        recommended_handoff_category="other",
        recommended_handoff_priority="normal",
        confidence=0.9,
        reason="continue",
    )
    assert turn.clear_entity_fields == []


def test_exact_unavailable_time_handles_adapter_slots(monkeypatch) -> None:
    from datetime import UTC, date, datetime, time, timedelta
    from uuid import uuid4

    from app.agents.tools import clinic_tools
    from app.integrations.clinic.base import AvailabilityResult, AvailabilitySlot

    branch_id = uuid4()
    service_id = uuid4()
    doctor_id = uuid4()

    def slot_at(hour: int, minute: int) -> AvailabilitySlot:
        start = datetime(2026, 8, 25, hour, minute, tzinfo=UTC)
        end = start + timedelta(minutes=30)
        return AvailabilitySlot(
            branch_id=str(branch_id),
            branch_name="مدينة نصر",
            doctor_id=str(doctor_id),
            doctor_name="أحمد محمود",
            service_id=str(service_id),
            service_name="ليزر إزالة الشعر",
            start_at=start,
            end_at=end,
            duration_minutes=30,
            price_minor=150000,
            currency="EGP",
        )

    raw_slots = (slot_at(17, 45), slot_at(20, 0))
    monkeypatch.setattr(
        clinic_tools,
        "_adapter_availability",
        lambda *args, **kwargs: AvailabilityResult(
            timezone="UTC",
            branch_id=str(branch_id),
            branch_name="مدينة نصر",
            service_id=str(service_id),
            service_name="ليزر إزالة الشعر",
            service_duration_minutes=30,
            service_price_minor=150000,
            service_currency="EGP",
            slots=raw_slots,
        ),
    )

    ctx = SimpleNamespace(db=object(), workspace=SimpleNamespace(id=uuid4()))
    branch = SimpleNamespace(id=branch_id, name="مدينة نصر")
    service = SimpleNamespace(
        id=service_id,
        name="ليزر إزالة الشعر",
        duration_minutes=30,
        price_minor=150000,
        currency="EGP",
    )

    payload = clinic_tools._availability_payload(
        ctx,
        branch=branch,
        service=service,
        booking_date=date(2026, 8, 25),
        doctor_id=doctor_id,
        requested_start=time(18, 0),
        lower_bound=None,
        upper_bound=None,
    )

    assert payload["requested_time_unavailable"] is True
    assert payload["matching_slot_count"] == 0
    assert [slot["start_time_24h"] for slot in payload["slots"]] == ["17:45", "20:00"]


def test_exact_available_start_returns_only_that_start_and_preserves_service_duration(monkeypatch) -> None:
    from datetime import UTC, date, datetime, time, timedelta
    from uuid import uuid4

    from app.agents.tools import clinic_tools
    from app.integrations.clinic.base import AvailabilityResult, AvailabilitySlot

    branch_id = uuid4()
    service_id = uuid4()
    doctor_id = uuid4()

    def slot_at(hour: int, minute: int) -> AvailabilitySlot:
        start = datetime(2026, 8, 25, hour, minute, tzinfo=UTC)
        end = start + timedelta(minutes=60)
        return AvailabilitySlot(
            branch_id=str(branch_id),
            branch_name="مدينة نصر",
            doctor_id=str(doctor_id),
            doctor_name="أحمد محمود",
            service_id=str(service_id),
            service_name="ليزر جسم كامل",
            start_at=start,
            end_at=end,
            duration_minutes=60,
            price_minor=350000,
            currency="EGP",
        )

    raw_slots = (slot_at(18, 0), slot_at(18, 15))
    monkeypatch.setattr(
        clinic_tools,
        "_adapter_availability",
        lambda *args, **kwargs: AvailabilityResult(
            timezone="UTC",
            branch_id=str(branch_id),
            branch_name="مدينة نصر",
            service_id=str(service_id),
            service_name="ليزر جسم كامل",
            service_duration_minutes=60,
            service_price_minor=350000,
            service_currency="EGP",
            slots=raw_slots,
        ),
    )
    ctx = SimpleNamespace(db=object(), workspace=SimpleNamespace(id=uuid4()))
    branch = SimpleNamespace(id=branch_id, name="مدينة نصر")
    service = SimpleNamespace(
        id=service_id,
        name="ليزر جسم كامل",
        duration_minutes=60,
        price_minor=350000,
        currency="EGP",
    )

    payload = clinic_tools._availability_payload(
        ctx,
        branch=branch,
        service=service,
        booking_date=date(2026, 8, 25),
        doctor_id=doctor_id,
        requested_start=time(18, 0),
        lower_bound=None,
        upper_bound=None,
    )

    assert payload["requested_time_unavailable"] is False
    assert payload["matching_slot_count"] == 1
    assert payload["slots"][0]["start_time_24h"] == "18:00"
    assert payload["slots"][0]["end_time_24h"] == "19:00"
    assert payload["slots"][0]["duration_minutes"] == 60