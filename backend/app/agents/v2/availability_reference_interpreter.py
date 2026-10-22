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
from app.agents.v2.turn_contract import TimeConstraint
from app.core.config import settings

ReferenceAction = Literal[
    "select_presented_option",
    "refresh_availability",
    "new_search",
    "clarify",
    "normal",
]
ReferenceSelectionScope = Literal["none", "doctor", "device"]


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
            "For action=new_search, use the customer's explicit colon-formatted HH:MM when it is not displayed. "
            "For action=select_presented_option on a compressed window, exact_time may carry the "
            "semantically understood 24-hour clock. For action=clarify, exact_time may preserve a clock "
            "the customer explicitly expressed when multiple displayed options share that same clock. "
            "Python validates every carried clock against the current snapshot."
        ),
    )
    time_constraint: TimeConstraint | None = Field(
        default=None,
        description=(
            "For action=new_search only, carry the existing typed time constraint when the latest customer "
            "meaning changes availability by time: nearest, before, or after. For before/after, copy the "
            "authoritative exact start_time supplied in current_time_constraint or by the selected concrete "
            "anchor; never invent an anchor. Use null for other actions or non-time search changes."
        ),
    )
    selection_scope: ReferenceSelectionScope = Field(
        default="none",
        description=(
            "For select_presented_option only: set doctor when the customer selected the displayed option "
            "by doctor identity, device when they selected it by device identity, otherwise none. Python "
            "validates the corresponding canonical value from the current server-owned option."
        ),
    )

    @model_validator(mode="after")
    def validate_option_ref(self) -> ReferenceDecision:
        if self.action == "select_presented_option":
            if not self.option_ref:
                raise ValueError("select_presented_option requires option_ref.")
        elif self.option_ref is not None:
            raise ValueError("option_ref is valid only for select_presented_option.")
        if self.exact_time is not None and self.action not in {
            "new_search",
            "select_presented_option",
            "clarify",
        }:
            raise ValueError(
                "exact_time is valid only for new_search, select_presented_option, or clarify."
            )
        if self.time_constraint is not None and self.action != "new_search":
            raise ValueError("time_constraint is valid only for new_search.")
        if self.selection_scope != "none" and self.action != "select_presented_option":
            raise ValueError(
                "selection_scope is valid only for select_presented_option."
            )
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
Do not use keyword matching rules and never invent an option_ref. If pending_exact_time is supplied, it is a
server-validated clock explicitly expressed on the immediately unresolved selection path. A later doctor/device
selection may use it to finish selecting one current displayed window; a new explicit clock overrides it.

Actions:
- select_presented_option: the customer is selecting/correcting/referencing one displayed option and
  the intended option is unambiguous. Return that supplied option_ref. This includes references by
  position, displayed clock time, doctor/device when they identify one displayed window, or wording that is
  explicitly relative to the presented-option sequence. A next/previous-one request may use
  last_selected_option_ref as its anchor. Do not use this action merely because an availability-timeline
  request could be satisfied by one of the currently displayed neighboring options.
  Set selection_scope=device only when the customer selected that option by its displayed device identity;
  set selection_scope=doctor only when they selected it by displayed doctor identity; otherwise use none.
  Do not infer a scope dimension merely because the displayed option happens to contain one.
  For a compressed window plus a customer clock, exact_time may carry the semantically understood 24-hour
  clock; Python validates it against the current server-owned exact slots.
- refresh_availability: the customer wants Linka to check availability again / refresh the same search, OR asks
  for additional/next availability results beyond the items already displayed. A request for more availability does
  not require last_selected_option_ref; it continues the verified search scope and lets existing pagination exclude
  already-presented windows. This is different from selecting the next displayed option relative to a chosen anchor.
- new_search: the customer changes or starts an availability search scope such as doctor/service/date/time.
  This includes asking for the nearest available appointment/time and asking for availability earlier or later
  than an authoritative exact time. These are availability-timeline searches, not previous/next displayed-option
  navigation, even when a currently displayed option happens to satisfy the requested direction. Carry the same
  meaning in time_constraint using the existing typed modes: nearest for nearest-time search, before/after with
  the authoritative exact start_time for earlier/later. If the new search changes a different dimension and does
  not change time, use time_constraint=null. An explicit
  clock time that does not match any displayed option is a time change/new search, not an ambiguous
  displayed-option reference.
- clarify: the customer is referring to one of the displayed options but the intended option cannot be determined
  safely, including a relative option-selection request with no usable selected-option anchor. Do not use clarify
  merely because a request for additional availability has no selected anchor; that meaning is refresh_availability.
  Also clarify an
  earlier/later availability-timeline request when there is no authoritative exact clock anchor. A range/window
  in current_time_constraint is not an exact anchor and must not be converted into one of its boundaries or a
  displayed option. If the customer explicitly expressed a clock and
  multiple displayed options share that same clock, preserve that semantically understood 24-hour clock in
  exact_time while leaving option_ref null. Do not preserve a clock the customer did not express.
- normal: the latest message is not merely resolving the displayed availability (for example a side question,
  social turn, or a separate lifecycle/action request that needs the normal V2 interpreter).

Safety:
- option_ref is semantic intent only. Python validates it against server-owned options.
- A displayed compressed window with concrete=false is not a canonical single slot. If the customer selects
  that window by doctor/device only, return its option_ref with exact_time=null so Python can narrow the
  current task without inventing a slot. If the customer also supplies a clock, return that option_ref and
  the semantically understood exact_time; Python must match it to exactly one current slot.
- For availability-timeline earlier/later meaning, current_time_constraint may be used only when its mode is
  exact and it carries start_time. A concrete last_selected_option_ref can also identify the exact displayed
  time the customer is referring to. If neither gives an exact anchor, clarify rather than guessing one. When
  action=clarify, time_constraint must be null.
- Natural 12-hour clock wording without a colon/explicit AM-PM marker is semantic, not 24-hour authority.
  If exactly one displayed option corresponds to that natural clock reading, select that supplied option_ref.
  If more than one displayed option could correspond to it (for example both 03:00 and 15:00), clarify rather
  than guessing. This preserves the conversational meaning of a displayed 3 PM option when the customer says
  "3", without Python parsing the phrase.
- A short/bare customer expression can sometimes reasonably denote both a displayed ordinal and a natural
  clock time relevant to the displayed availability (for example a bare "4" while a fourth option exists and
  4 PM is also plausible). In that genuinely bare case return clarify. Do not choose the ordinal or the clock
  interpretation, and do not mutate last_selected_option_ref based on a guess. Explicit ordinal wording remains
  a displayed-option selection. Explicit clock phrasing such as "at 4" or an equivalent explicit clock phrase
  in the customer's language is clock intent, not an ordinal, even when a fourth displayed option exists. If that
  clock maps to exactly one current displayed option/window after the already selected doctor/device context,
  select that supplied option_ref and return the semantically understood exact_time when needed. If multiple
  displayed options share that clock, clarify and preserve only the explicit clock in exact_time.
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
    raw_time = availability_context.get("time")
    current_time_constraint = None
    if isinstance(raw_time, dict):
        current_time_constraint = {
            key: raw_time.get(key)
            for key in ("mode", "start_time", "end_time")
            if raw_time.get(key) not in (None, "")
        }
    payload = {
        "last_presented_availability_options": options,
        "last_selected_option_ref": availability_context.get("last_selected_option_ref"),
        "current_time_constraint": current_time_constraint,
        "pending_exact_time": availability_context.get("pending_exact_time"),
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
        decision=ReferenceDecision(action="clarify", option_ref=None, selection_scope="none"),
        model_name=None,
        structured_output_error=saw_structured_error,
    )
