from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from langchain_core.messages import HumanMessage

from app.agents import turn_interpreter
from app.agents.semantic_actions import format_verified_tool_fallback
from app.agents.turn_models import FlowTurnDecision, SemanticCapabilityDecision, SemanticEntityHints
from app.services.agent_chat import _uuid_from_metadata


def _hints(**values: str | None) -> SemanticEntityHints:
    return SemanticEntityHints(
        service_query=values.get("service_query"),
        branch_query=values.get("branch_query"),
        doctor_query=values.get("doctor_query"),
        requested_date=values.get("requested_date"),
        not_before_time=values.get("not_before_time"),
        not_after_time=values.get("not_after_time"),
        appointment_reference=values.get("appointment_reference"),
    )


def _semantic_decision() -> SemanticCapabilityDecision:
    return SemanticCapabilityDecision(
        domains=["booking"],
        capabilities=["availability_discovery", "appointment_creation"],
        risk_flags=[],
        flow_signal="start_booking",
        entity_hints=_hints(
            service_query="ليزر",
            requested_date="2026-08-20",
            not_before_time="18:00",
        ),
        missing_information=[],
        recommended_handoff_category="other",
        recommended_handoff_priority="normal",
        confidence=0.99,
        reason="test",
    )


def test_unified_turn_interpreter_receives_clinic_local_clock() -> None:
    local_now = datetime.fromisoformat("2026-08-19T18:30:00+03:00")
    prompt = turn_interpreter._interpreter_system_prompt(
        timezone_name="Africa/Cairo",
        local_now=local_now,
        active_flow=False,
    )

    assert "Africa/Cairo" in prompt
    assert "2026-08-19T18:30:00+03:00" in prompt
    assert "Resolve clear relative dates/times" in prompt
    assert "requested_start_time" in prompt


def test_unified_active_flow_prompt_receives_clinic_local_clock() -> None:
    local_now = datetime.fromisoformat("2026-08-19T18:30:00+03:00")
    prompt = turn_interpreter._interpreter_system_prompt(
        timezone_name="Africa/Cairo",
        local_now=local_now,
        active_flow=True,
    )

    assert "Africa/Cairo" in prompt
    assert "2026-08-19T18:30:00+03:00" in prompt
    assert "Active workflow present: true" in prompt


def test_verified_booking_tool_fallback_formats_slots_without_internal_ids() -> None:
    reply = format_verified_tool_fallback(
        "get_booking_options",
        {
            "ok": True,
            "date": "2026-08-20",
            "requested_time_window": {"not_before_time": "18:00", "not_after_time": None},
            "slots": [
                {
                    "branch_id": str(uuid4()),
                    "doctor_id": str(uuid4()),
                    "service_id": str(uuid4()),
                    "start_time_24h": "18:30",
                    "doctor_name": "أحمد محمود",
                    "branch_name": "مدينة نصر",
                }
            ],
        },
    )

    assert reply is not None
    assert "الخميس 20 أغسطس" in reply
    assert "18:30" in reply
    assert "أحمد محمود" in reply
    assert "مدينة نصر" not in reply
    assert "branch_id" not in reply
    assert "doctor_id" not in reply


def test_verified_booking_tool_fallback_handles_empty_requested_window() -> None:
    reply = format_verified_tool_fallback(
        "get_booking_options",
        {
            "ok": True,
            "date": "2026-08-20",
            "requested_time_window": {"not_before_time": "18:00", "not_after_time": "20:00"},
            "slots": [],
        },
    )
    assert reply is not None
    assert "الوقت المطلوب" in reply
    assert "الخميس 20 أغسطس" in reply


def test_existing_inbound_run_id_is_parseable_and_reusable() -> None:
    run_id = uuid4()
    assert _uuid_from_metadata(str(run_id)) == run_id
    assert _uuid_from_metadata("not-a-uuid") is None


def test_realtime_agent_contract_finalizes_after_composite_read_and_dedupes_reads() -> None:
    backend = Path(__file__).resolve().parent.parent
    source = (backend / "app/agents/tia_customer_agent.py").read_text(encoding="utf-8")
    agent_chat = (backend / "app/services/agent_chat.py").read_text(encoding="utf-8")

    assert '_COMPOSITE_DISCOVERY_TOOLS = frozenset({"get_booking_options", "get_reschedule_options"})' in source
    assert "_COMPOSITE_DISCOVERY_TOOLS.intersection(last_tool_round_names)" in source
    assert "stage=tool-dedup" in source
    assert "finalizer-empty-fallback" in source
    assert "existing_run_id = _uuid_from_metadata" in agent_chat
    assert "run_id = existing_run_id or uuid4()" in agent_chat
    assert "_existing_agent_response_for_inbound" in agent_chat


def test_composite_tool_round_goes_straight_to_finalizer_and_empty_finalizer_is_safe(
    monkeypatch,
) -> None:
    from langchain_core.messages import AIMessage

    from app.agents import tia_customer_agent

    class FakeTool:
        name = "get_booking_options"

        def __init__(self) -> None:
            self.calls = 0

        def invoke(self, args):
            self.calls += 1
            return (
                '{"ok":true,"date":"2026-08-20","requested_time_window":'
                '{"not_before_time":"18:00","not_after_time":null},"slots":['
                '{"start_time_24h":"18:30","doctor_name":"أحمد محمود",'
                '"branch_name":"مدينة نصر"}]}'
            )

    class FakeBoundModel:
        def __init__(self) -> None:
            self.calls = 0

        def invoke(self, messages):
            self.calls += 1
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "get_booking_options",
                        "args": {
                            "service_search": "ليزر",
                            "booking_date": "2026-08-20",
                        },
                        "id": "booking-call-1",
                        "type": "tool_call",
                    }
                ],
            )

    class FakeBaseModel:
        def __init__(self, bound) -> None:
            self.bound = bound
            self.finalizer_calls = 0

        def bind_tools(self, tools):
            return self.bound

        def invoke(self, messages):
            self.finalizer_calls += 1
            return AIMessage(content="")

    tool = FakeTool()
    bound = FakeBoundModel()
    base = FakeBaseModel(bound)
    monkeypatch.setattr(tia_customer_agent, "build_clinic_tools", lambda ctx: [tool])
    monkeypatch.setattr(tia_customer_agent, "build_chat_model", lambda: base)

    context = SimpleNamespace(
        workspace=SimpleNamespace(name="Linka", timezone="Africa/Cairo"),
        run_id=uuid4(),
    )
    reply, _ = tia_customer_agent.run_tia_customer_agent(
        history=[HumanMessage(content="عايز أحجز بكرة بعد 6")],
        tool_context=context,
    )

    assert bound.calls == 1
    assert tool.calls == 1
    assert base.finalizer_calls == 1
    assert "18:30" in reply
    assert "أحمد محمود" in reply


def test_duplicate_read_tool_is_not_executed_twice(monkeypatch) -> None:
    from langchain_core.messages import AIMessage

    from app.agents import tia_customer_agent

    class FakeTool:
        name = "search_services"

        def __init__(self) -> None:
            self.calls = 0

        def invoke(self, args):
            self.calls += 1
            return '{"ok":true,"services":[{"service_name":"ليزر"}]}'

    class FakeBoundModel:
        def __init__(self) -> None:
            self.calls = 0

        def invoke(self, messages):
            self.calls += 1
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "search_services",
                        "args": {"search": "ليزر"},
                        "id": f"service-call-{self.calls}",
                        "type": "tool_call",
                    }
                ],
            )

    class FakeBaseModel:
        def __init__(self, bound) -> None:
            self.bound = bound

        def bind_tools(self, tools):
            return self.bound

        def invoke(self, messages):
            return AIMessage(content="تمام، لقيت الخدمة في بيانات العيادة.")

    tool = FakeTool()
    bound = FakeBoundModel()
    base = FakeBaseModel(bound)
    monkeypatch.setattr(tia_customer_agent, "build_clinic_tools", lambda ctx: [tool])
    monkeypatch.setattr(tia_customer_agent, "build_chat_model", lambda: base)

    context = SimpleNamespace(
        workspace=SimpleNamespace(name="Linka", timezone="Africa/Cairo"),
        run_id=uuid4(),
    )
    reply, _ = tia_customer_agent.run_tia_customer_agent(
        history=[HumanMessage(content="عايز أعرف الخدمة")],
        tool_context=context,
    )

    assert bound.calls == 2
    assert tool.calls == 1
    assert "لقيت الخدمة" in reply


def test_verified_prefetch_direct_reply_skips_customer_llm_for_booking_read() -> None:
    from app.agents.capability_policy import CapabilityPolicyDecision
    from app.services.agent_chat import _verified_prefetch_direct_reply

    policy = CapabilityPolicyDecision(
        capabilities=frozenset({"availability_discovery", "appointment_creation"}),
        allowed_tools=frozenset({"get_booking_options", "escalate_to_human"}),
        write_capabilities=frozenset({"appointment_creation"}),
        requires_human=False,
        handoff_category="other",
        handoff_priority="normal",
        risk_flags=frozenset(),
    )
    result = _verified_prefetch_direct_reply(
        policy=policy,
        prefetched_results={
            "get_booking_options": {
                "ok": True,
                "date": "2026-08-20",
                "requested_time_window": {
                    "not_before_time": "18:00",
                    "not_after_time": None,
                },
                "slots": [
                    {
                        "start_time_24h": "18:30",
                        "doctor_name": "أحمد محمود",
                        "branch_name": "مدينة نصر",
                    }
                ],
            }
        },
    )

    assert result is not None
    reply, model = result
    assert "18:30" in reply
    assert "أحمد محمود" in reply
    assert model == "deterministic:verified-get_booking_options"


def test_verified_prefetch_direct_reply_does_not_hide_uncovered_capability() -> None:
    from app.agents.capability_policy import CapabilityPolicyDecision
    from app.services.agent_chat import _verified_prefetch_direct_reply

    policy = CapabilityPolicyDecision(
        capabilities=frozenset(
            {"availability_discovery", "appointment_creation", "pricing"}
        ),
        allowed_tools=frozenset(
            {"get_booking_options", "search_services", "escalate_to_human"}
        ),
        write_capabilities=frozenset({"appointment_creation"}),
        requires_human=False,
        handoff_category="other",
        handoff_priority="normal",
        risk_flags=frozenset(),
    )
    result = _verified_prefetch_direct_reply(
        policy=policy,
        prefetched_results={"get_booking_options": {"ok": True, "slots": []}},
    )
    assert result is None


def test_unavailable_model_tool_call_gets_matching_response_and_clean_finalizer(
    monkeypatch,
) -> None:
    from langchain_core.messages import AIMessage, ToolMessage

    from app.agents import tia_customer_agent

    class FakeTool:
        name = "escalate_to_human"

        def invoke(self, args):
            raise AssertionError("Unavailable tool call must not execute another tool")

    class FakeBaseModel:
        def __init__(self) -> None:
            self.calls = 0
            self.finalizer_messages = None

        def bind_tools(self, tools):
            raise AssertionError("A turn with no allowed tools must stay unbound")

        def invoke(self, messages):
            self.calls += 1
            if self.calls == 1:
                # Even an unbound provider response can theoretically contain a stale or
                # hallucinated function call. The runtime must reject it deterministically.
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "get_booking_options",
                            "args": {
                                "service_search": "ليزر",
                                "booking_date": "2026-08-20",
                            },
                            "id": "unavailable-booking-call",
                            "type": "tool_call",
                        }
                    ],
                )

            self.finalizer_messages = list(messages)
            # The finalizer must be a clean text request: no function-call AIMessage
            # and no ToolMessage are replayed to Gemini.
            assert not any(
                isinstance(message, AIMessage) and message.tool_calls
                for message in messages
            )
            assert not any(isinstance(message, ToolMessage) for message in messages)
            assert any(
                "tool_not_available" in str(getattr(message, "content", ""))
                for message in messages
            )
            return AIMessage(content="تمام، استخدمت البيانات المتاحة من نفس الطلب.")

    base = FakeBaseModel()
    monkeypatch.setattr(tia_customer_agent, "build_clinic_tools", lambda ctx: [FakeTool()])
    monkeypatch.setattr(tia_customer_agent, "build_chat_model", lambda: base)

    context = SimpleNamespace(
        workspace=SimpleNamespace(name="Linka", timezone="Africa/Cairo"),
        run_id=uuid4(),
    )
    reply, _ = tia_customer_agent.run_tia_customer_agent(
        history=[HumanMessage(content="عايز أحجز بكرة بعد 6")],
        tool_context=context,
        operational_context=(
            '{"turn_prefetch":{"get_booking_options":{"ok":true,'
            '"date":"2026-08-20","slots":[]}}}'
        ),
        allowed_tool_names=set(),
    )

    assert "استخدمت البيانات" in reply
    assert base.calls == 2
    assert base.finalizer_messages is not None


def test_unified_interpreter_exposes_service_choices_for_follow_up_selection() -> None:
    flow = SimpleNamespace(
        option_snapshot={
            "ok": True,
            "needs_service_choice": True,
            "services": [
                {
                    "service_id": "11111111-1111-4111-8111-111111111111",
                    "service_name": "ليزر إزالة الشعر",
                },
                {
                    "service_id": "22222222-2222-4222-8222-222222222222",
                    "service_name": "ليزر إزالة الشعر — Demo",
                },
            ],
        },
    )

    summary = turn_interpreter._option_summary(flow)

    assert summary["services"] == [
        {
            "index": 1,
            "id": "11111111-1111-4111-8111-111111111111",
            "name": "ليزر إزالة الشعر",
        },
        {
            "index": 2,
            "id": "22222222-2222-4222-8222-222222222222",
            "name": "ليزر إزالة الشعر — Demo",
        },
    ]


def test_prerequisite_service_index_selection_updates_semantic_state_without_write(monkeypatch) -> None:
    from app.services import agent_chat

    flow = SimpleNamespace(
        entity_state={"service_query": "ليزر"},
        option_snapshot={
            "needs_service_choice": True,
            "services": [
                {
                    "service_id": "11111111-1111-4111-8111-111111111111",
                    "service_name": "ليزر إزالة الشعر",
                },
                {
                    "service_id": "22222222-2222-4222-8222-222222222222",
                    "service_name": "ليزر إزالة الشعر — Demo",
                },
            ],
        },
    )
    turn = FlowTurnDecision(
        action="select_option",
        capabilities=["availability_discovery", "appointment_creation"],
        risk_flags=[],
        entity_hints=_hints(),
        selection_index=1,
        selection_time=None,
        missing_information=[],
        recommended_handoff_category="other",
        recommended_handoff_priority="normal",
        confidence=0.99,
        reason="customer selected first service",
    )
    decision = _semantic_decision().model_copy(
        update={"entity_hints": _hints(service_query="ليزر", requested_date="2026-08-20")}
    )

    def fake_transition(db, current, **changes):
        current.entity_state = changes["entity_state"]
        current.option_snapshot = changes["option_snapshot"]
        current.status = changes["status"]
        return current

    monkeypatch.setattr(agent_chat, "transition_flow", fake_transition)

    updated_flow, updated_turn, updated_decision = agent_chat._apply_prerequisite_option_selection(
        db=object(),
        flow=flow,
        turn=turn,
        decision=decision,
        run_id=uuid4(),
    )

    assert updated_flow.entity_state["service_query"] == "ليزر إزالة الشعر"
    assert updated_flow.entity_state["service_id"] == "11111111-1111-4111-8111-111111111111"
    assert updated_flow.option_snapshot == {}
    assert updated_turn.action == "continue"
    assert updated_turn.selection_index is None
    assert updated_decision.entity_hints.service_query == "ليزر إزالة الشعر"
    assert updated_decision.entity_hints.service_id == "11111111-1111-4111-8111-111111111111"


def test_flow_sync_contract_persists_non_slot_choice_snapshots() -> None:
    backend = Path(__file__).resolve().parent.parent
    source = (backend / "app/services/conversation_flows.py").read_text(encoding="utf-8")

    assert "has_presented_options = has_slots or needs_choice" in source
    assert "option_snapshot=output if has_presented_options else {}" in source