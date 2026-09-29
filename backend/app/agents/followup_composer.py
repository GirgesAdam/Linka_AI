from __future__ import annotations

import json
from datetime import datetime

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from app.agents.llm_runtime import LLMProviderError, invoke_model, invoke_with_model_chain
from app.agents.model_provider import (
    build_realtime_composer_fallback_model,
    build_realtime_composer_model,
    model_label,
)
from app.core.config import settings


def _extract_text(message: AIMessage) -> str:
    if isinstance(message.content, str):
        return message.content.strip()
    parts: list[str] = []
    if isinstance(message.content, list):
        for block in message.content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                text = block.get("text") or block.get("content")
                if isinstance(text, str):
                    parts.append(text)
    return "\n".join(part.strip() for part in parts if part.strip()).strip()


def compose_followup_message(
    *,
    clinic_name: str,
    timezone_name: str,
    local_now: datetime,
    patient_name: str,
    goal_title: str,
    goal_note: str | None,
    recent_messages: list[dict[str, str]],
) -> tuple[str, str]:
    """Compose one fresh proactive follow-up from grounded conversation context.

    This model is a wording layer only. Python already decided that the follow-up
    is due and authorized; the composer cannot call tools or mutate state.
    """
    system = SystemMessage(
        content=(
            "You write one proactive WhatsApp follow-up for Linka, an aesthetic clinic assistant. "
            "Write only the customer-visible message. Keep it concise and natural, like a skilled "
            "Egyptian clinic receptionist continuing the same conversation. Match the customer's "
            "language and tone; if Arabic, use natural Egyptian Arabic, not formal Arabic.\n\n"
            "RULES:\n"
            "- This is a continuation, not an ad or marketing blast.\n"
            "- Use the follow-up goal and recent conversation only. Do not invent clinic facts.\n"
            "- Do not claim a call, booking, payment, treatment, or staff action happened unless the "
            "recent conversation explicitly says it happened.\n"
            "- If the customer explicitly asked for a reminder, remind them naturally rather than "
            "saying the team is 'following up'.\n"
            "- Avoid robotic phrases like 'نود المتابعة مع حضرتك' unless that wording genuinely fits.\n"
            "- Do not mention internal tasks, automation, AI, IDs, databases, or scheduling systems.\n"
            "- Usually 1-3 short sentences. At most one light emoji, and only if it fits the tone.\n"
            "- Do not ask multiple questions. End with one clear next step only when useful.\n\n"
            f"Clinic: {clinic_name}\n"
            f"Clinic timezone: {timezone_name}\n"
            f"Current clinic-local time: {local_now.isoformat()}\n"
            f"Customer first/display name: {patient_name}"
        )
    )
    payload = {
        "follow_up_goal": {
            "title": goal_title,
            "note": goal_note,
        },
        "recent_conversation": recent_messages,
    }
    user = HumanMessage(
        content=(
            "Compose the follow-up now from this grounded context:\n"
            + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        )
    )
    messages = [system, user]

    primary_name = settings.gemini_realtime_composer_model
    fallback_name = settings.gemini_realtime_composer_fallback_model
    primary = build_realtime_composer_model()
    fallback_model = None

    def primary_call() -> AIMessage:
        return invoke_model(lambda: primary.invoke(messages))

    def fallback_call() -> AIMessage:
        nonlocal fallback_model
        if fallback_model is None:
            fallback_model = build_realtime_composer_fallback_model()
        if fallback_model is None:
            raise RuntimeError("Follow-up composer fallback model is not configured.")
        return invoke_model(lambda: fallback_model.invoke(messages))

    model_calls = [(primary_name, primary_call)]
    if fallback_name and fallback_name != primary_name:
        model_calls.append((fallback_name, fallback_call))

    invocation = invoke_with_model_chain(
        model_calls=model_calls,
        operation="ai-followup-composer",
        circuit_breaker_cooldown_seconds=settings.llm_realtime_circuit_breaker_cooldown_seconds,
    )
    text = _extract_text(invocation.value)
    if not text:
        raise LLMProviderError("AI follow-up composer returned no customer-visible text.")
    if len(text) > 2000:
        raise LLMProviderError("AI follow-up composer returned an unexpectedly long message.")
    return text, model_label(invocation.model_name)
