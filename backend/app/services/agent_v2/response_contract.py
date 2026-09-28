from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.services.agent_v2.outcome import OutcomeChoice, ResponseGoal, TurnOutcome

FactRequirement = Literal["required", "optional"]
FactSemanticType = Literal[
    "entity_name",
    "date",
    "time",
    "money",
    "count",
    "status",
    "boolean",
    "text",
    "choice",
]
TerminalAction = Literal[
    "booking",
    "reschedule",
    "cancel_appointment",
    "confirm_appointment",
    "buy_package",
    "buy_pulse_pack",
    "follow_up",
    "marketing_update",
]
AvailabilityState = Literal[
    "options_available",
    "requested_time_unavailable",
    "no_availability",
]


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
    action: TerminalAction
    succeeded: bool


class AvailabilityTruth(StrictResponseContractModel):
    state: AvailabilityState


class CustomerResponseUnit(StrictResponseContractModel):
    response_goal: ResponseGoal
    status: str
    action_truth: ActionTruth | None = None
    availability_truth: AvailabilityTruth | None = None
    facts: tuple[ResponseFact, ...] = ()
    choices: tuple[ResponseChoice, ...] = ()


class CustomerResponseContract(StrictResponseContractModel):
    units: tuple[CustomerResponseUnit, ...]


_TERMINAL_GOALS = frozenset({
    "booking_completed", "reschedule_completed", "cancellation_completed",
    "appointment_confirmed", "package_purchased", "pulse_pack_purchased",
    "follow_up_created", "marketing_updated",
})
_AVAILABILITY_GOALS = frozenset({
    "present_availability",
    "requested_time_unavailable",
    "no_availability",
})
_AVAILABILITY_STATE_BY_GOAL: dict[ResponseGoal, AvailabilityState] = {
    "present_availability": "options_available",
    "requested_time_unavailable": "requested_time_unavailable",
    "no_availability": "no_availability",
}
_REQUIRED_KEYS_BY_GOAL: dict[ResponseGoal, frozenset[str]] = {
    "answer_price": frozenset({"service_name", "price", "device_price_options"}),
    "answer_doctor": frozenset({"doctors"}),
    "present_availability": frozenset({"availability_windows"}),
    "requested_time_unavailable": frozenset({"requested_time_unavailable"}),
    "no_availability": frozenset({"no_availability"}),
    "active_task_cancelled": frozenset({"active_task_cancelled"}),
    "follow_up_created": frozenset({"follow_up_at"}),
    "marketing_updated": frozenset({"marketing_consent"}),
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
SUPPORTED_TERMINAL_RESPONSE_GOALS = _TERMINAL_GOALS
SUPPORTED_AVAILABILITY_RESPONSE_GOALS = _AVAILABILITY_GOALS
AVAILABILITY_STATE_BY_GOAL = _AVAILABILITY_STATE_BY_GOAL

TERMINAL_ACTION_BY_GOAL: dict[ResponseGoal, TerminalAction] = {
    "booking_completed": "booking",
    "reschedule_completed": "reschedule",
    "cancellation_completed": "cancel_appointment",
    "appointment_confirmed": "confirm_appointment",
    "package_purchased": "buy_package",
    "pulse_pack_purchased": "buy_pulse_pack",
    "follow_up_created": "follow_up",
    "marketing_updated": "marketing_update",
}

_TERMINAL_FACT_SOURCES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("service_name", ("service_name",)),
    ("doctor_name", ("doctor_name",)),
    ("device_name", ("device_name", "laser_device_name")),
    ("start_local", ("start_local",)),
    ("date", ("date",)),
    ("time", ("time",)),
    ("status", ("status",)),
    ("package_used", ("package_used",)),
    ("package_name", ("package_name",)),
    ("pulses_remaining", ("pulses_remaining",)),
    ("pulses_count", ("pulses_count", "pulse_count")),
    ("follow_up_at", ("follow_up_at", "follow_up_at_local", "due_at")),
    ("marketing_consent", ("marketing_consent", "consent")),
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
    safe = _safe_value(availability) if isinstance(availability, dict) else {}
    if not isinstance(safe, dict):
        safe = {}

    result: list[ResponseFact] = []
    if goal == "present_availability":
        windows = safe.get("availability_windows")
        if isinstance(windows, list):
            result.append(
                _make_fact(
                    goal=goal,
                    key="availability_windows",
                    value=windows,
                    required=True,
                    complete_set=True,
                )
            )
        for key in (
            "service_name",
            "checked_dates",
            "available_option_count",
            "search_truncated",
        ):
            if safe.get(key) not in (None, "", [], {}):
                result.append(_make_fact(goal=goal, key=key, value=safe[key]))
        return result

    marker = (
        "requested_time_unavailable"
        if goal == "requested_time_unavailable"
        else "no_availability"
    )
    result.append(_make_fact(goal=goal, key=marker, value=True, required=True))

    if goal == "requested_time_unavailable":
        requested_time = facts.get("time")
        if requested_time not in (None, "", [], {}):
            result.append(
                _make_fact(
                    goal=goal,
                    key="requested_time",
                    value=requested_time,
                    required=True,
                )
            )

    for key in ("service_name", "checked_dates", "search_truncated"):
        if safe.get(key) not in (None, "", [], {}):
            result.append(_make_fact(goal=goal, key=key, value=safe[key]))
    return result


def _terminal_facts(goal: ResponseGoal, outcome: TurnOutcome) -> list[ResponseFact]:
    result: list[ResponseFact] = []
    seen: set[str] = set()
    for target_key, source_keys in _TERMINAL_FACT_SOURCES:
        for source in (outcome.action_result, outcome.facts):
            value = next(
                (
                    source[source_key]
                    for source_key in source_keys
                    if source.get(source_key) not in (None, "", [], {})
                ),
                None,
            )
            if value is None or target_key in seen:
                continue
            result.append(_make_fact(goal=goal, key=target_key, value=value))
            seen.add(target_key)
            break

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
    availability_truth: AvailabilityTruth | None = None
    if goal in _TERMINAL_GOALS:
        expected_action = TERMINAL_ACTION_BY_GOAL[goal]
        source_action = outcome.action_result.get("action")
        if source_action not in (None, expected_action):
            raise ValueError(
                f"Terminal action mismatch for {goal}: expected {expected_action}."
            )
        succeeded = (
            outcome.status == "completed"
            and outcome.action_result.get("ok") is True
        )
        action_truth = ActionTruth(
            action=expected_action,
            succeeded=succeeded,
        )
        facts = _terminal_facts(goal, outcome)
    elif goal == "answer_price":
        facts = _service_price_facts(goal, outcome.facts)
    elif goal == "answer_doctor":
        facts = _doctor_facts(goal, outcome.facts)
    elif goal in _AVAILABILITY_GOALS:
        availability_truth = AvailabilityTruth(
            state=_AVAILABILITY_STATE_BY_GOAL[goal]
        )
        facts = _availability_facts(goal, outcome.facts)
    else:
        facts = _generic_facts(goal, outcome.facts)

    return CustomerResponseUnit(
        response_goal=goal,
        status=outcome.status,
        action_truth=action_truth,
        availability_truth=availability_truth,
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


def is_pure_supported_terminal_contract(
    contract: CustomerResponseContract,
) -> bool:
    """Whether every unit is a completed terminal write supported by Phase 2."""
    if not contract.units:
        return False
    return all(
        unit.response_goal in SUPPORTED_TERMINAL_RESPONSE_GOALS
        and unit.status == "completed"
        and unit.action_truth is not None
        and unit.action_truth.succeeded is True
        for unit in contract.units
    )


def is_pure_supported_availability_contract(
    contract: CustomerResponseContract,
) -> bool:
    """Whether every unit has the verified shape required by the Phase 3A composer."""
    if not contract.units:
        return False

    for unit in contract.units:
        if (
            unit.response_goal not in SUPPORTED_AVAILABILITY_RESPONSE_GOALS
            or unit.availability_truth is None
            or unit.availability_truth.state
            != AVAILABILITY_STATE_BY_GOAL[unit.response_goal]
        ):
            return False

        facts = {fact.key: fact for fact in unit.facts}
        if unit.response_goal == "present_availability":
            windows = facts.get("availability_windows")
            if (
                windows is None
                or windows.complete_set is not True
                or not isinstance(windows.value, list)
                or not windows.value
            ):
                return False
        elif unit.response_goal == "requested_time_unavailable":
            marker = facts.get("requested_time_unavailable")
            requested_time = facts.get("requested_time")
            if (
                marker is None
                or marker.value is not True
                or requested_time is None
            ):
                return False
        elif unit.response_goal == "no_availability":
            marker = facts.get("no_availability")
            if marker is None or marker.value is not True:
                return False

    return True
