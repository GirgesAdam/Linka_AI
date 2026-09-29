from __future__ import annotations

import json
import logging
from datetime import datetime
from time import perf_counter
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from langchain_core.messages import AIMessage, BaseMessage, SystemMessage, ToolMessage
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import tools_condition

from app.agents.availability_presentation import customer_visible_verified_data
from app.agents.llm_runtime import LLMProviderError, invoke_model, invoke_with_fallback
from app.agents.model_provider import build_chat_fallback_model, build_chat_model, model_label
from app.agents.prompts.customer_service import build_customer_service_system_prompt
from app.agents.semantic_actions import format_verified_tool_fallback
from app.agents.tools.clinic_tools import AgentToolContext, build_clinic_tools
from app.core.config import settings

logger = logging.getLogger(__name__)

_READ_ONLY_TOOL_NAMES = frozenset(
    {
        "get_customer_profile",
        "get_customer_history",
        "search_services",
        "list_branches",
        "list_doctors",
        "get_booking_options",
        "get_reschedule_options",
        "get_available_slots",
        "get_customer_appointments",
    }
)
_COMPOSITE_DISCOVERY_TOOLS = frozenset({"get_booking_options", "get_reschedule_options"})

def _tool_payload(message: ToolMessage) -> dict | None:
    content = message.content
    if isinstance(content, dict):
        return content
    if not isinstance(content, str):
        return None
    try:
        value = json.loads(content)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None

def _verified_tool_fallback(messages: list[BaseMessage]) -> str | None:
    for message in reversed(messages):
        if not isinstance(message, ToolMessage) or not message.name:
            continue
        payload = _tool_payload(message)
        if payload is None:
            continue
        reply = format_verified_tool_fallback(message.name, payload)
        if reply:
            return reply
    return None

def _finalizer_tool_context(messages: list[BaseMessage]) -> str | None:
    """Serialize current-turn tool results without replaying function-call history.
    Gemini 3 validates function-call/function-response turns strictly. The finalizer
    only needs verified tool outputs, not the intermediate AI function-call message,
    so we convert ToolMessages into hidden operational context and start a clean text
    generation request. This also keeps cross-model failover safe after a tool call.
    """
    rows: list[dict[str, object]] = []
    for message in messages:
        if not isinstance(message, ToolMessage):
            continue
        payload = _tool_payload(message)
        rows.append(
            {
                "tool": message.name or "unknown",
                "result": customer_visible_verified_data(payload) if payload is not None else str(message.content),
            }
        )
    if not rows:
        return None
    return json.dumps(
        {"current_turn_tool_results": rows},
        ensure_ascii=False,
        default=str,
        separators=(",", ":"),
    )

def _extract_text(message: AIMessage) -> str:
    if isinstance(message.content, str):
        return message.content.strip()
    parts: list[str] = []
    if isinstance(message.content, list):
        for block in message.content:
            if isinstance(block, str):
                parts.append(block)
                continue
            if not isinstance(block, dict):
                continue
            text = block.get("text") or block.get("content")
            if isinstance(text, str):
                parts.append(text)

    return "\n".join(part.strip() for part in parts if part.strip()).strip()

def _tool_call_names(message: AIMessage | None) -> list[str]:
    if message is None:
        return []
    names: list[str] = []
    for call in message.tool_calls or []:
        name = call.get("name") if isinstance(call, dict) else None
        if isinstance(name, str) and name:
            names.append(name)
    return names

def run_tia_customer_agent(
    *,
    history: list[BaseMessage],
    tool_context: AgentToolContext,
    operational_context: str | None = None,
    allowed_tool_names: set[str] | None = None,
) -> tuple[str, str]:
    started = perf_counter()
    timezone_name = tool_context.workspace.timezone
    try:
        tz = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        timezone_name = "Africa/Cairo"
        tz = ZoneInfo(timezone_name)
    system_prompt = build_customer_service_system_prompt(
        clinic_name=tool_context.workspace.name,
        timezone_name=timezone_name,
        local_now=datetime.now(tz),
    )
    internal_messages: list[SystemMessage] = []
    if operational_context:
        internal_messages.append(
            SystemMessage(
                content=(
                    "INTERNAL OPERATIONAL CONTEXT FROM EARLIER OR PREFETCHED TOOL CALLS. "
                    "This is hidden from the customer. Use it as source-of-truth data for "
                    "this turn and do not repeat a read tool when the needed result is "
                    "already present here. Never print internal IDs. If the customer "
                    "selects a shown slot, use the exact hidden IDs and start_local value; "
                    "the write tool revalidates before commit.\n"
                    f"{operational_context}"
                )
            )
        )
    all_tools = build_clinic_tools(tool_context)
    if allowed_tool_names is None:
        tools = all_tools
    else:
        tools = [tool for tool in all_tools if tool.name in allowed_tool_names]
    primary_base_model = build_chat_model()
    # Do not bind an empty function declaration set. Some provider/model versions
    # can still emit remembered/hallucinated function calls after bind_tools([]).
    # An unbound model makes a no-tool turn genuinely text-only.
    primary_model = primary_base_model.bind_tools(tools) if tools else primary_base_model
    fallback_name = settings.gemini_agent_fallback_model
    fallback_base_model = None
    fallback_model = None
    last_model_name = settings.gemini_agent_model

    def get_fallback_base_model():
        nonlocal fallback_base_model
        if fallback_base_model is None:
            fallback_base_model = build_chat_fallback_model()
        return fallback_base_model
    def get_fallback_bound_model():
        nonlocal fallback_model
        if fallback_model is None:
            base = get_fallback_base_model()
            fallback_model = (
                base.bind_tools(tools) if base is not None and tools else base
            )
        return fallback_model
    model_call_index = 0
    finalizer_calls = 0
    tool_rounds = 0
    executed_read_tools: set[str] = set()
    last_tool_round_names: list[str] = []
    last_tool_round_had_unavailable = False
    def _invoke_bound_model(
        request_messages: list[BaseMessage],
        *,
        operation: str,
    ) -> AIMessage:
        nonlocal last_model_name

        def invoke_fallback() -> AIMessage:
            model = get_fallback_bound_model()
            if model is None:
                raise RuntimeError("Customer-agent fallback model is not configured.")
            return invoke_model(lambda: model.invoke(request_messages))
        has_fallback = bool(fallback_name and fallback_name != settings.gemini_agent_model)
        invocation = invoke_with_fallback(
            primary_call=lambda: invoke_model(lambda: primary_model.invoke(request_messages)),
            primary_model_name=settings.gemini_agent_model,
            fallback_call=invoke_fallback if has_fallback else None,
            fallback_model_name=fallback_name if has_fallback else None,
            operation=operation,
            circuit_breaker_cooldown_seconds=settings.llm_realtime_circuit_breaker_cooldown_seconds,
        )
        last_model_name = invocation.model_name
        return invocation.value
    def _invoke_unbound_finalizer(request_messages: list[BaseMessage]) -> AIMessage:
        nonlocal last_model_name

        def invoke_fallback() -> AIMessage:
            model = get_fallback_base_model()
            if model is None:
                raise RuntimeError("Customer-agent fallback model is not configured.")
            return invoke_model(lambda: model.invoke(request_messages))
        has_fallback = bool(fallback_name and fallback_name != settings.gemini_agent_model)
        invocation = invoke_with_fallback(
            primary_call=lambda: invoke_model(lambda: primary_base_model.invoke(request_messages)),
            primary_model_name=settings.gemini_agent_model,
            fallback_call=invoke_fallback if has_fallback else None,
            fallback_model_name=fallback_name if has_fallback else None,
            operation="customer-agent-finalizer",
            circuit_breaker_cooldown_seconds=settings.llm_realtime_circuit_breaker_cooldown_seconds,
        )
        last_model_name = invocation.model_name
        return invocation.value
    def call_model(state: MessagesState) -> dict[str, list[AIMessage]]:
        nonlocal model_call_index
        model_call_index += 1
        request_messages: list[BaseMessage] = [
            SystemMessage(content=system_prompt),
            *internal_messages,
            *state["messages"],
        ]
        response = _invoke_bound_model(
            request_messages,
            operation=f"customer-agent-round-{model_call_index}",
        )
        logger.info(
            "Linka agent run_id=%s stage=model round=%s tool_calls=%s",
            tool_context.run_id,
            model_call_index,
            _tool_call_names(response),
        )
        return {"messages": [response]}
    tool_by_name = {tool.name: tool for tool in tools}
    def call_tools(state: MessagesState) -> dict[str, list[ToolMessage]]:
        nonlocal tool_rounds, last_tool_round_names, last_tool_round_had_unavailable
        tool_rounds += 1
        latest = state["messages"][-1] if state["messages"] else None
        latest_ai = latest if isinstance(latest, AIMessage) else None
        calls = list(latest_ai.tool_calls or []) if latest_ai is not None else []
        names = _tool_call_names(latest_ai)
        last_tool_round_names = names
        last_tool_round_had_unavailable = False
        stage_started = perf_counter()
        outputs: list[ToolMessage] = []
        for index, call in enumerate(calls):
            name = call.get("name") if isinstance(call, dict) else None
            args = call.get("args") if isinstance(call, dict) else {}
            call_id = call.get("id") if isinstance(call, dict) else None
            if not isinstance(name, str) or not name:
                continue
            if not isinstance(args, dict):
                args = {}
            call_id = str(call_id or f"{name}-{tool_rounds}-{index}")
            if name not in tool_by_name:
                last_tool_round_had_unavailable = True
                # Never leave a Gemini function call unmatched. A model can still
                # emit a tool name that was deliberately removed from this turn
                # (for example because get_booking_options was already prefetched).
                # Returning a matching ToolMessage keeps the provider transcript
                # structurally valid while preserving the deterministic policy boundary.
                payload = {
                    "ok": False,
                    "reason": "tool_not_available",
                    "message": (
                        "This tool is not available in the current turn. Use verified "
                        "prefetched context or another currently available tool."
                    ),
                }
                outputs.append(
                    ToolMessage(
                        content=json.dumps(payload, ensure_ascii=False),
                        tool_call_id=call_id,
                        name=name,
                    )
                )
                logger.warning(
                    "Linka agent run_id=%s stage=tool-unavailable round=%s name=%s",
                    tool_context.run_id,
                    tool_rounds,
                    name,
                )
                continue
            if name in _READ_ONLY_TOOL_NAMES and name in executed_read_tools:
                payload = {
                    "ok": False,
                    "reason": "duplicate_read_blocked",
                    "message": "This read tool already ran in the current turn; use its prior result.",
                }
                outputs.append(
                    ToolMessage(
                        content=json.dumps(payload, ensure_ascii=False),
                        tool_call_id=call_id,
                        name=name,
                    )
                )
                logger.warning(
                    "Linka agent run_id=%s stage=tool-dedup round=%s name=%s",
                    tool_context.run_id,
                    tool_rounds,
                    name,
                )
                continue
            tool = tool_by_name[name]
            try:
                raw = tool.invoke(args)
            except (ValueError, RuntimeError) as exc:
                raw = json.dumps(
                    {"ok": False, "error": str(exc)},
                    ensure_ascii=False,
                )

            if name in _READ_ONLY_TOOL_NAMES:
                executed_read_tools.add(name)
            if isinstance(raw, str):
                content = raw
            else:
                content = json.dumps(raw, ensure_ascii=False, default=str)
            outputs.append(
                ToolMessage(
                    content=content,
                    tool_call_id=call_id,
                    name=name,
                )
            )
        logger.info(
            "Linka agent run_id=%s stage=tools round=%s names=%s duration_ms=%s",
            tool_context.run_id,
            tool_rounds,
            names,
            int((perf_counter() - stage_started) * 1000),
        )
        return {"messages": outputs}
    def route_after_tools(_: MessagesState) -> str:
        # One unavailable tool is enough evidence that the model tried to leave the
        # policy surface. Do not give it another chance to invent a second legacy
        # tool name; finalize from verified context or ask for missing information.
        if last_tool_round_had_unavailable:
            return "finalize"
        if _COMPOSITE_DISCOVERY_TOOLS.intersection(last_tool_round_names):
            return "finalize"
        if tool_rounds >= settings.agent_max_tool_rounds:
            return "finalize"
        return "agent"

    def finalize(state: MessagesState) -> dict[str, list[AIMessage]]:
        nonlocal finalizer_calls
        finalizer_calls += 1
        # Do not replay this turn's AI function-call transcript into an unbound
        # finalizer request. Gemini 3 requires exact function-call/response matching
        # and thought-signature circulation. We only need the verified outputs here,
        # so flatten ToolMessages into hidden operational context and reuse the clean
        # customer conversation history.
        turn_messages = list(state["messages"][len(history) :])
        finalizer_tool_context = _finalizer_tool_context(turn_messages)
        finalizer_internal_messages = list(internal_messages)
        if finalizer_tool_context:
            finalizer_internal_messages.append(
                SystemMessage(
                    content=(
                        "INTERNAL VERIFIED TOOL RESULTS FROM THIS TURN. These are "
                        "source-of-truth execution results. Use them to answer the "
                        "customer, never expose internal IDs, and do not request a tool.\n"
                        f"{finalizer_tool_context}"
                    )
                )
            )
        request_messages: list[BaseMessage] = [
            SystemMessage(content=system_prompt),
            *finalizer_internal_messages,
            SystemMessage(
                content=(
                    "INTERNAL EXECUTION BUDGET: the tool-call budget for this customer "
                    "turn is exhausted or a composite source-of-truth discovery tool has "
                    "already returned enough data. Do not request another tool. Answer "
                    "the customer now using the verified tool results and conversation "
                    "state already available. If required information is still missing, "
                    "ask only for that missing information; never invent clinic data or "
                    "claim an action succeeded."
                )
            ),
            *history,
        ]
        try:
            response = _invoke_unbound_finalizer(request_messages)
        except LLMProviderError:
            safe_reply = _verified_tool_fallback(state["messages"])
            if not safe_reply:
                raise
            logger.warning(
                "Linka agent run_id=%s stage=finalizer-provider-fallback tool_rounds=%s",
                tool_context.run_id,
                tool_rounds,
            )
            response = AIMessage(content=safe_reply)
        if not _extract_text(response):
            safe_reply = _verified_tool_fallback(state["messages"])
            if safe_reply:
                logger.warning(
                    "Linka agent run_id=%s stage=finalizer-empty-fallback tool_rounds=%s",
                    tool_context.run_id,
                    tool_rounds,
                )
                response = AIMessage(content=safe_reply)
        logger.info(
            "Linka agent run_id=%s stage=finalizer tool_rounds=%s",
            tool_context.run_id,
            tool_rounds,
        )
        return {"messages": [response]}
    builder = StateGraph(MessagesState)
    builder.add_node("agent", call_model)
    builder.add_node("tools", call_tools)
    builder.add_node("finalize", finalize)
    builder.add_edge(START, "agent")
    builder.add_conditional_edges("agent", tools_condition)
    builder.add_conditional_edges(
        "tools",
        route_after_tools,
        {"agent": "agent", "finalize": "finalize"},
    )
    builder.add_edge("finalize", END)
    graph = builder.compile()
    result = graph.invoke(
        {"messages": history},
        config={"recursion_limit": settings.agent_recursion_limit},
    )

    final_message = result["messages"][-1]
    if not isinstance(final_message, AIMessage):
        raise RuntimeError("Agent finished without an AI response.")

    reply = _extract_text(final_message)
    if not reply:
        reply = _verified_tool_fallback(result["messages"]) or ""
    if not reply:
        # Gemini can rarely return an empty bound-tool message without a tool call.
        # Recover once with a clean unbound text request instead of failing the whole
        # customer turn. Verified operational context is preserved in internal_messages.
        recovery_messages: list[BaseMessage] = [
            SystemMessage(content=system_prompt),
            *internal_messages,
            SystemMessage(
                content=(
                    "The previous generation returned no customer-visible text. "
                    "Answer the latest customer message now. Do not request a tool. "
                    "Use verified operational context when present; otherwise ask only "
                    "for genuinely missing information and do not invent clinic facts."
                )
            ),
            *history,
        ]
        try:
            recovery = _invoke_unbound_finalizer(recovery_messages)
            reply = _extract_text(recovery)
        except (LLMProviderError, RuntimeError):
            reply = ""
        if reply:
            logger.warning(
                "Linka agent run_id=%s stage=empty-response-recovered",
                tool_context.run_id,
            )
    if not reply:
        # Keep the channel reliable even during a provider edge case. This message
        # intentionally makes no clinic-data claim and authorizes no action.
        reply = "معلش، حصلت مشكلة مؤقتة وأنا بجهز الرد. ممكن تبعت طلبك تاني؟"
        logger.error(
            "Linka agent run_id=%s stage=empty-response-safe-fallback",
            tool_context.run_id,
        )
    logger.info(
        "Linka agent run_id=%s completed model_calls=%s tool_rounds=%s duration_ms=%s model=%s",
        tool_context.run_id,
        model_call_index + finalizer_calls,
        tool_rounds,
        int((perf_counter() - started) * 1000),
        last_model_name,
    )
    return reply, model_label(last_model_name)
