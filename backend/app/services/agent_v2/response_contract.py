from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.services.agent_v2.outcome import OutcomeChoice, ResponseGoal, TurnOutcome

FactRequirement = Literal["required", "optional"]
FactSemanticType = Literal["entity_name", "date", "time", "money", "count", "status", "boolean", "text", "choice"]


class StrictResponseContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ResponseFact(StrictResponseContractModel):
    key: str
    semantic_type: FactSemanticType
    requirement: FactRequirement
    value: object
    complete_set: bool = False


class ResponseChoice(StrictResponseContractModel):
    label: str
    facts: tuple[ResponseFact, ...] = ()


class ActionTruth(StrictResponseContractModel):
    action: str
    succeeded: bool


class CustomerResponseUnit(StrictResponseContractModel):
    response_goal: ResponseGoal
    status: str
    action_truth: ActionTruth | None = None
    facts: tuple[ResponseFact, ...] = ()
    choices: tuple[ResponseChoice, ...] = ()


class CustomerResponseContract(StrictResponseContractModel):
    units: tuple[CustomerResponseUnit, ...]


_TERMINAL_GOALS = frozenset({
    "booking_completed", "reschedule_completed", "cancellation_completed",
    "appointment_confirmed", "package_purchased", "pulse_pack_purchased",
    "follow_up_created", "marketing_updated",
})
_REQUIRED_KEYS_BY_GOAL: dict[ResponseGoal, frozenset[str]] = {
    "answer_price": frozenset({"service_name", "price", "device_price_options"}),
    "answer_doctor": frozenset({"doctors"}),
    "present_availability": frozenset({"availability_windows"}),
    "requested_time_unavailable": frozenset({"requested_time_unavailable"}),
    "no_availability": frozenset({"no_availability"}),
    "active_task_cancelled": frozenset({"active_task_cancelled"}),
    "handoff": frozenset({"category"}),
}
_COMPLETE_SET_KEYS_BY_GOAL: dict[ResponseGoal, frozenset[str]] = {
    "answer_doctor": frozenset({"doctors"}),
    "present_availability": frozenset({"availability_windows"}),
}
_INTERNAL_EXACT_KEYS = frozenset({
    "id", "ref", "workspace_id", "patient_id", "appointment_id", "service_id",
    "doctor_id", "branch_id", "package_id", "transaction_id",
    "purchase_transaction_id", "reference_transaction_id", "patient_package_id",
    "patient_pulse_pack_id", "package_offer_id", "pulse_pack_offer_id", "external_id",
})
_TERMINAL_CANONICAL_KEYS = (
    "service_name", "doctor_name", "device_name", "laser_device_name", "start_local",
    "date", "time", "status", "package_used", "package_name", "pulses_remaining",
    "pulses_count", "follow_up_at", "marketing_consent", "consent",
)


def _safe_value(value: object) -> object:
    if isinstance(value, dict):
        cleaned: dict[str, object] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            if (
                key in _INTERNAL_EXACT_KEYS
                or key.endswith("_id")
                or key.endswith("_ids")
                or key.endswith("_ref")
                or key.endswith("_refs")
            ):
                continue
            safe = _safe_value(item)
            if safe not in (None, {}, []):
                cleaned[key] = safe
        return cleaned
    if isinstance(value, (list, tuple)):
        return [safe for item in value if (safe := _safe_value(item)) not in (None, {}, [])]
    return value


def _fact_type(key: str, value: object) -> FactSemanticType:
    lowered = key.casefold()
    if isinstance(value, bool):
        return "boolean"
    if any(token in lowered for token in ("price", "amount", "currency", "money", "refund")):
        return "money"
    if any(token in lowered for token in ("count", "remaining", "consumed", "sessions", "pulses")):
        return "count"
    if "date" in lowered:
        return "date"
    if any(token in lowered for token in ("time", "start_local", "follow_up_at", "expires_at", "purchased_at")):
        return "time"
    if "status" in lowered or "state" in lowered:
        return "status"
    if any(token in lowered for token in ("name", "doctor", "service", "device", "branch", "package")):
        return "entity_name"
    if "choice" in lowered or "option" in lowered or isinstance(value, list):
        return "choice"
    return "text"


def _make_fact(
    *, goal: ResponseGoal, key: str, value: object,
    required: bool | None = None, complete_set: bool | None = None,
) -> ResponseFact:
    is_required = key in _REQUIRED_KEYS_BY_GOAL.get(goal, frozenset()) if required is None else required
    is_complete = key in _COMPLETE_SET_KEYS_BY_GOAL.get(goal, frozenset()) if complete_set is None else complete_set
    return ResponseFact(
        key=key,
        semantic_type=_fact_type(key, value),
        requirement="required" if is_required else "optional",
        value=_safe_value(value),
        complete_set=is_complete,
    )


def _choice_contract(choice: OutcomeChoice, goal: ResponseGoal) -> ResponseChoice:
    safe = _safe_value(choice.facts)
    choice_facts = (
        tuple(_make_fact(goal=goal, key=key, value=value) for key, value in safe.items())
        if isinstance(safe, dict)
        else ()
    )
    return ResponseChoice(label=choice.label, facts=choice_facts)


def _service_price_facts(goal: ResponseGoal, facts: dict[str, object]) -> list[ResponseFact]:
    catalog = facts.get("service_catalog")
    if not isinstance(catalog, dict):
        return []
    service_wrapper = catalog.get("service")
    if not isinstance(service_wrapper, dict):
        return []
    service = _safe_value(service_wrapper)
    if not isinstance(service, dict):
        return []

    result: list[ResponseFact] = []
    name = service.get("name")
    if name not in (None, ""):
        result.append(_make_fact(goal=goal, key="service_name", value=name, required=True))
    selected = service.get("selected_laser_device")
    if isinstance(selected, dict):
        result.append(_make_fact(
            goal=goal, key="device_price_options", value=[selected], required=True, complete_set=True
        ))
    devices = service.get("laser_devices")
    if isinstance(devices, list) and devices:
        result.append(_make_fact(
            goal=goal, key="device_price_options", value=devices, required=True, complete_set=True
        ))
    price = service.get("price")
    if price not in (None, ""):
        result.append(_make_fact(goal=goal, key="price", value=price, required=True))
    currency = service.get("currency")
    if currency not in (None, ""):
        result.append(_make_fact(goal=goal, key="currency", value=currency))
    for key in ("description", "duration_minutes", "customer_duration_text"):
        if service.get(key) not in (None, ""):
            result.append(_make_fact(goal=goal, key=key, value=service[key]))
    return result


def _doctor_facts(goal: ResponseGoal, facts: dict[str, object]) -> list[ResponseFact]:
    doctors = facts.get("doctors")
    if not isinstance(doctors, dict):
        return []
    rows = doctors.get("doctors")
    if not isinstance(rows, list):
        return []
    return [_make_fact(goal=goal, key="doctors", value=_safe_value(rows), required=True, complete_set=True)]


def _availability_facts(goal: ResponseGoal, facts: dict[str, object]) -> list[ResponseFact]:
    availability = facts.get("availability")
    if not isinstance(availability, dict):
        if goal == "requested_time_unavailable":
            return [_make_fact(goal=goal, key="requested_time_unavailable", value=True, required=True)]
        if goal == "no_availability":
            return [_make_fact(goal=goal, key="no_availability", value=True, required=True)]
        return []
    safe = _safe_value(availability)
    if not isinstance(safe, dict):
        return []
    result: list[ResponseFact] = []
    if goal == "present_availability":
        windows = safe.get("availability_windows")
        if isinstance(windows, list):
            result.append(_make_fact(
                goal=goal, key="availability_windows", value=windows, required=True, complete_set=True
            ))
        for key in (
            "service_name", "price", "laser_device_options", "checked_dates",
            "available_option_count", "search_truncated",
        ):
            if safe.get(key) not in (None, "", [], {}):
                result.append(_make_fact(goal=goal, key=key, value=safe[key]))
        return result
    marker = "requested_time_unavailable" if goal == "requested_time_unavailable" else "no_availability"
    result.append(_make_fact(goal=goal, key=marker, value=True, required=True))
    for key in ("service_name", "checked_dates"):
        if safe.get(key) not in (None, "", [], {}):
            result.append(_make_fact(goal=goal, key=key, value=safe[key]))
    return result


def _terminal_facts(goal: ResponseGoal, outcome: TurnOutcome) -> list[ResponseFact]:
    result: list[ResponseFact] = []
    seen: set[str] = set()
    for source in (outcome.action_result, outcome.facts):
        for key in _TERMINAL_CANONICAL_KEYS:
            value = source.get(key)
            if value in (None, "", [], {}) or key in seen:
                continue
            result.append(_make_fact(goal=goal, key=key, value=value))
            seen.add(key)

    availability = outcome.facts.get("availability")
    if isinstance(availability, dict):
        service_name = availability.get("service_name")
        if service_name not in (None, "") and "service_name" not in seen:
            result.append(_make_fact(goal=goal, key="service_name", value=service_name))
            seen.add("service_name")
        windows = availability.get("availability_windows")
        if isinstance(windows, list) and len(windows) == 1 and isinstance(windows[0], dict):
            window = windows[0]
            for source_key, target_key in (
                ("start_local", "start_local"),
                ("doctor_name", "doctor_name"),
                ("laser_device_name", "device_name"),
            ):
                value = window.get(source_key)
                if value not in (None, "") and target_key not in seen:
                    result.append(_make_fact(goal=goal, key=target_key, value=value))
                    seen.add(target_key)
    return result


def _generic_facts(goal: ResponseGoal, facts: dict[str, object]) -> list[ResponseFact]:
    safe = _safe_value(facts)
    if not isinstance(safe, dict):
        return []
    if (
        goal == "answer_clinic_info"
        and "payment_execution_owner" in safe
        and "booking_requires_payment" in safe
    ):
        safe = {
            key: value
            for key, value in safe.items()
            if key
            in {
                "booking_requires_payment",
                "payment_execution_owner",
                "active_booking_in_progress",
                "clinic_info",
            }
        }
    return [
        _make_fact(goal=goal, key=key, value=value)
        for key, value in safe.items()
        if value not in (None, "", [], {})
    ]


def _unit_from_outcome(outcome: TurnOutcome) -> CustomerResponseUnit:
    goal = outcome.response_goal
    action_truth: ActionTruth | None = None
    if goal in _TERMINAL_GOALS:
        action = outcome.action_result.get("action")
        succeeded = outcome.status == "completed" and outcome.action_result.get("ok") is True
        action_truth = ActionTruth(action=str(action or goal.removesuffix("_completed")), succeeded=succeeded)
        facts = _terminal_facts(goal, outcome)
    elif goal == "answer_price":
        facts = _service_price_facts(goal, outcome.facts)
    elif goal == "answer_doctor":
        facts = _doctor_facts(goal, outcome.facts)
    elif goal in {"present_availability", "requested_time_unavailable", "no_availability"}:
        facts = _availability_facts(goal, outcome.facts)
    else:
        facts = _generic_facts(goal, outcome.facts)

    return CustomerResponseUnit(
        response_goal=goal,
        status=outcome.status,
        action_truth=action_truth,
        facts=tuple(facts),
        choices=tuple(_choice_contract(choice, goal) for choice in outcome.choices),
    )


def build_customer_response_contract(
    outcomes: list[TurnOutcome] | tuple[TurnOutcome, ...],
) -> CustomerResponseContract:
    """Pure deterministic projection from business outcomes to the language boundary."""
    return CustomerResponseContract(
        units=tuple(_unit_from_outcome(outcome) for outcome in outcomes)
    )
