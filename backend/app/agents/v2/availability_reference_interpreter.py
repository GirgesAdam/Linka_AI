from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.agents.llm_runtime import LLMProviderError, is_cross_model_failover_eligible
from app.agents.model_provider import (
    build_realtime_interpreter_fallback_model,
    build_realtime_interpreter_model,
)
from app.agents.structured_output import StructuredOutputError, invoke_typed_structured_output
from app.core.config import settings

ReferenceAction = Literal[
    "select_presented_option",
    "refresh_availability",
    "new_search",
    "clarify",
    "normal",
]


def _require_all_schema_fields(schema: dict) -> None:
    properties = schema.get("properties")
    if isinstance(properties, dict):
        schema["required"] = list(properties)


class ReferenceDecision(BaseModel):
    """Tiny semantic contract for a turn while displayed availability is still in focus."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra=_require_all_schema_fields,
    )

    action: ReferenceAction
    option_ref: str | None = Field(
        default=None,
        description=(
            "One supplied displayed option_ref only when action=select_presented_option. "
            "Never invent an option_ref."
        ),
    )
    exact_time: str | None = Field(
        default=None,
        description=(
            "The customer's exact colon-formatted HH:MM only when action=new_search and the explicit "
            "clock is not one of the displayed options. Never normalize, infer, or convert AM/PM."
        ),
    )

    @model_validator(mode="after")
    def validate_option_ref(self) -> ReferenceDecision:
        if self.action == "select_presented_option":
            if not self.option_ref:
                raise ValueError("select_presented_option requires option_ref.")
        elif self.option_ref is not None:
            raise ValueError("option_ref is valid only for select_presented_option.")
        if self.exact_time is not None and self.action != "new_search":
            raise ValueError("exact_time is valid only for new_search.")
        return self


@dataclass(frozen=True)
class ReferenceInterpretation:
    decision: ReferenceDecision
    model_name: str | None
    structured_output_error: bool = False


_SYSTEM_PROMPT = """You are Linka's narrow semantic resolver for the latest availability follow-up.
Return only the required structured schema. Do not answer the customer and do not perform any write.

You receive the exact availability items Linka most recently displayed, each with an opaque option_ref,
and an optional last_selected_option_ref. Understand the customer's latest message semantically.
Do not use keyword matching rules and never invent an option_ref.

Actions:
- select_presented_option: the customer is selecting/correcting/referencing one displayed option and
  the intended option is unambiguous. Return that supplied option_ref. This includes references by
  position, displayed clock time, or relative wording when last_selected_option_ref makes the target clear.
- refresh_availability: the customer wants Linka to check availability again / refresh the same search.
- new_search: the customer changes or starts an availability search scope such as doctor/service/date/time. An explicit clock time that does not match any displayed option is a time change/new search, not an ambiguous displayed-option reference.
- clarify: the customer is referring to the displayed options but the intended option cannot be determined
  safely, including a relative request with no usable anchor.
- normal: the latest message is not merely resolving the displayed availability (for example a side question,
  social turn, or a separate lifecycle/action request that needs the normal V2 interpreter).

Safety:
- option_ref is semantic intent only. Python validates it against server-owned options.
- A displayed compressed window with concrete=false is not a canonical single slot. If the customer selects
  that window, still return its option_ref; Python will ask for a specific time rather than invent one.
- Natural 12-hour clock wording without a colon/explicit AM-PM marker is semantic, not 24-hour authority.
  If exactly one displayed option corresponds to that natural clock reading, select that supplied option_ref.
  If more than one displayed option could correspond to it (for example both 03:00 and 15:00), clarify rather
  than guessing. This preserves the conversational meaning of a displayed 3 PM option when the customer says
  "3", without Python parsing the phrase.
- An explicit colon-formatted clock is exact: 03:00 is not 15:00. If that exact clock is one displayed option,
  select that option_ref. If it is not displayed, use new_search so the normal availability flow can verify
  that exact time. For this time-only new search, copy the exact colon clock into exact_time. Never map it to a different displayed clock.
- If the customer explicitly asks Linka to book/reschedule/cancel or otherwise execute a lifecycle action now,
  use normal so the full V2 interpreter preserves that action. Do not turn a write request into a mere selection.
"""


def _messages(
    *,
    latest_customer_text: str,
    availability_context: dict[str, object],
) -> list[BaseMessage]:
    raw_options = availability_context.get("availability_reference_options")
    options: list[dict[str, object]] = []
    if isinstance(raw_options, list):
        for item in raw_options:
            if not isinstance(item, dict):
                continue
            safe = {
                key: item.get(key)
                for key in (
                    "option_ref",
                    "concrete",
                    "start_time_24h",
                    "end_time_24h",
                    "start_local",
                    "end_local",
                    "doctor_name",
                    "laser_device_name",
                )
                if item.get(key) not in (None, "")
            }
            if safe.get("option_ref"):
                options.append(safe)
    payload = {
        "last_presented_availability_options": options,
        "last_selected_option_ref": availability_context.get("last_selected_option_ref"),
        "latest_customer_message": latest_customer_text,
    }
    return [
        SystemMessage(content=_SYSTEM_PROMPT),
        HumanMessage(content=json.dumps(payload, ensure_ascii=False, separators=(",", ":"))),
    ]


def _invoke_twice(model, messages: list[BaseMessage]) -> ReferenceDecision:
    try:
        return invoke_typed_structured_output(
            model=model,
            schema=ReferenceDecision,
            messages=messages,
        )
    except StructuredOutputError:
        return invoke_typed_structured_output(
            model=model,
            schema=ReferenceDecision,
            messages=messages,
        )


def interpret_availability_reference_turn(
    *,
    latest_customer_text: str,
    availability_context: dict[str, object],
) -> ReferenceInterpretation:
    """Resolve current displayed-option meaning before the full V2 schema.

    Schema failures are contained here: after bounded primary/fallback attempts the
    resolver fails closed to a semantic clarification, never a write or guessed ref.
    """
    messages = _messages(
        latest_customer_text=latest_customer_text,
        availability_context=availability_context,
    )
    attempts: list[tuple[str, object]] = [
        (settings.openai_model, build_realtime_interpreter_model()),
    ]
    fallback_name = settings.openai_fallback_model
    fallback_model = build_realtime_interpreter_fallback_model()
    if fallback_name and fallback_model is not None and fallback_name != settings.openai_model:
        attempts.append((fallback_name, fallback_model))

    saw_structured_error = False
    last_provider_error: LLMProviderError | None = None
    for model_name, model in attempts:
        try:
            decision = _invoke_twice(model, messages)
        except StructuredOutputError:
            saw_structured_error = True
            continue
        except LLMProviderError as exc:
            last_provider_error = exc
            if is_cross_model_failover_eligible(exc):
                continue
            raise
        return ReferenceInterpretation(
            decision=decision,
            model_name=model_name,
            structured_output_error=saw_structured_error,
        )

    if last_provider_error is not None and not saw_structured_error:
        raise last_provider_error
    return ReferenceInterpretation(
        decision=ReferenceDecision(action="clarify", option_ref=None),
        model_name=None,
        structured_output_error=saw_structured_error,
    )
