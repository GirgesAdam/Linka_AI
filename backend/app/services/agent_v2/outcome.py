from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

OutcomeStatus = Literal[
    "answered",
    "needs_input",
    "completed",
    "blocked",
    "handoff",
    "partial_success",
]
ResponseGoal = Literal[
    "answer_service",
    "answer_price",
    "answer_doctor",
    "answer_clinic_info",
    "answer_customer_profile",
    "answer_customer_history",
    "present_availability",
    "requested_time_unavailable",
    "no_availability",
    "ask_service_choice",
    "ask_doctor_choice",
    "ask_device_choice",
    "ask_time_choice",
    "ask_appointment_choice",
    "ask_package_choice",
    "clarification",
    "booking_completed",
    "reschedule_completed",
    "cancellation_completed",
    "appointment_confirmed",
    "active_task_cancelled",
    "package_information",
    "package_comparison",
    "availability_reference",
    "package_purchased",
    "pulse_information",
    "pulse_pack_purchased",
    "package_refund_quote",
    "follow_up_created",
    "marketing_updated",
    "handoff",
    "social_ack",
]

_COMPLETED_RESPONSE_GOALS = frozenset(
    {
        "booking_completed",
        "reschedule_completed",
        "cancellation_completed",
        "appointment_confirmed",
        "package_purchased",
        "pulse_pack_purchased",
        "follow_up_created",
        "marketing_updated",
    }
)
_COMPLETED_ACTION_BY_GOAL = {
    "booking_completed": "booking",
    "reschedule_completed": "reschedule",
    "cancellation_completed": "cancel_appointment",
    "appointment_confirmed": "confirm_appointment",
    "package_purchased": "buy_package",
    "pulse_pack_purchased": "buy_pulse_pack",
    "follow_up_created": "follow_up",
    "marketing_updated": "marketing_update",
}


def _strip_reference_metadata(value: object) -> object:
    """Remove ephemeral/internal reference tokens before any outcome reaches language rendering."""
    if isinstance(value, dict):
        visible: dict[str, object] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            if key == "ref" or key.endswith("_ref") or key.endswith("_refs"):
                continue
            visible[key] = _strip_reference_metadata(item)
        return visible
    if isinstance(value, list):
        return [_strip_reference_metadata(item) for item in value]
    if isinstance(value, tuple):
        return [_strip_reference_metadata(item) for item in value]
    return value


class StrictOutcomeModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OutcomeChoice(StrictOutcomeModel):
    ref: str
    label: str
    facts: dict[str, object] = Field(default_factory=dict)


class TurnOutcome(StrictOutcomeModel):
    """Structured business result consumed by the customer-facing V2 responder."""

    status: OutcomeStatus
    response_goal: ResponseGoal
    facts: dict[str, object] = Field(default_factory=dict)
    choices: list[OutcomeChoice] = Field(default_factory=list)
    action_result: dict[str, object] = Field(default_factory=dict)
    active_task_summary: dict[str, object] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_shape(self) -> TurnOutcome:
        choice_goals = {
            "ask_service_choice",
            "ask_doctor_choice",
            "ask_device_choice",
            "ask_time_choice",
            "ask_appointment_choice",
            "ask_package_choice",
        }
        if self.response_goal in choice_goals and not self.choices:
            raise ValueError("choice response goals require verified choices.")
        if self.status == "handoff" and self.response_goal != "handoff":
            raise ValueError("handoff status requires handoff response goal.")
        if self.status == "completed" and self.response_goal not in _COMPLETED_RESPONSE_GOALS:
            raise ValueError("completed outcomes require a terminal write response goal.")
        if self.status != "completed" and self.response_goal in _COMPLETED_RESPONSE_GOALS:
            raise ValueError("terminal write response goals require completed status.")
        if self.response_goal == "active_task_cancelled" and self.status != "answered":
            raise ValueError("active task cancellation is a terminal state result, not a clinic write.")

        stripped_facts = _strip_reference_metadata(self.facts)
        stripped_action = _strip_reference_metadata(self.action_result)
        stripped_summary = _strip_reference_metadata(self.active_task_summary)
        assert isinstance(stripped_facts, dict)
        assert isinstance(stripped_action, dict)
        assert isinstance(stripped_summary, dict)
        self.facts = stripped_facts
        self.action_result = stripped_action
        self.active_task_summary = stripped_summary
        self.choices = [
            choice.model_copy(
                update={
                    "facts": (
                        cleaned
                        if isinstance(
                            cleaned := _strip_reference_metadata(choice.facts),
                            dict,
                        )
                        else {}
                    )
                }
            )
            for choice in self.choices
        ]

        if self.status == "completed" and self.action_result.get("ok") is True:
            self.action_result = {
                **self.action_result,
                "completed": True,
                "action": _COMPLETED_ACTION_BY_GOAL[self.response_goal],
            }
        return self
