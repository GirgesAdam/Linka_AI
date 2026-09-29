from __future__ import annotations

from datetime import datetime

import pytest
from langchain_core.messages import HumanMessage

from app.agents.v2 import responder
from app.agents.v2.turn_contract import TiaTurnUnderstanding
from app.services.agent_v2.outcome import TurnOutcome
from app.services.agent_v2.planner import PlannerContext, plan_turn


def _medical_outcome(priority: str) -> TurnOutcome:
    return TurnOutcome(
        status="handoff",
        response_goal="handoff",
        facts={"category": "medical", "priority": priority},
    )


def test_regular_medical_handoff_is_deterministic_and_matches_safe_v1_reply(monkeypatch) -> None:
    monkeypatch.setattr(
        responder,
        "build_realtime_composer_model",
        lambda: pytest.fail("medical handoff must not require a responder LLM call"),
    )

    reply, model = responder.compose_v2_customer_reply(
        clinic_name="Linka Test Clinic",
        timezone_name="Africa/Cairo",
        local_now=datetime(2026, 9, 12, 12, 0, 0),
        history=[HumanMessage(content="أنا حامل، ينفع أعمل بوتوكس؟")],
        outcomes=[_medical_outcome("high")],
    )

    assert reply == "الموضوع ده محتاج تقييم من الفريق الطبي، فحوّلت المحادثة لفريق العيادة للمراجعة."
    assert model == "deterministic:medical-handoff"


def test_urgent_medical_handoff_is_deterministic_and_directs_emergency_help(monkeypatch) -> None:
    monkeypatch.setattr(
        responder,
        "build_realtime_composer_model",
        lambda: pytest.fail("urgent medical handoff must not require a responder LLM call"),
    )

    reply, model = responder.compose_v2_customer_reply(
        clinic_name="Linka Test Clinic",
        timezone_name="Africa/Cairo",
        local_now=datetime(2026, 9, 12, 12, 0, 0),
        history=[HumanMessage(content="I am having severe symptoms after the procedure")],
        outcomes=[_medical_outcome("urgent")],
    )

    assert "urgent medical attention" in reply
    assert "emergency services" in reply
    assert "clinic medical team" in reply
    assert model == "deterministic:medical-handoff"


def test_planner_preserves_medical_and_urgent_priorities() -> None:
    context = PlannerContext(
        semantic_context=object(),
        active_task=None,
        now=datetime(2026, 9, 12, 12, 0, 0),
    )

    medical = plan_turn(
        TiaTurnUnderstanding(operations=[], safety_signals=["medical"]),
        context,
    )
    urgent = plan_turn(
        TiaTurnUnderstanding(operations=[], safety_signals=["urgent_medical"]),
        context,
    )

    assert medical.steps == []
    assert medical.handoff_category == "medical"
    assert medical.handoff_priority == "high"
    assert urgent.steps == []
    assert urgent.handoff_category == "medical"
    assert urgent.handoff_priority == "urgent"


def test_live_v2_handoff_uses_planned_priority_not_a_hardcoded_normal_priority() -> None:
    from app.services.agent_v2 import live_chat

    source = open(live_chat.__file__, encoding="utf-8").read()
    assert 'priority=turn.plan.handoff_priority or "normal"' in source
    assert 'category=turn.plan.handoff_category or "other"' in source
