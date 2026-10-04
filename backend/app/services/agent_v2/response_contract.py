from __future__ import annotations

from decimal import Decimal, InvalidOperation
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
CommercialPriceKind = Literal[
    "service_base_price",
    "service_device_price",
    "service_device_price_options",
    "package_price_options",
    "device_price_clarification",
    "price_unavailable",
]
CommercialPriceQualifier = Literal["base", "device", "package"]
DoctorTruthKind = Literal["doctor_result_set", "doctor_choice"]


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


class CommercialPriceOption(StrictResponseContractModel):
    qualifier: CommercialPriceQualifier
    service_name: str
    amount: str
    currency: str
    device_name: str | None = None
    sessions_count: int | None = None


class CommercialTruth(StrictResponseContractModel):
    kind: CommercialPriceKind
    service_name: str | None = None
    options: tuple[CommercialPriceOption, ...] = ()
    complete_set: bool = False


class DoctorOption(StrictResponseContractModel):
    name: str
    specialization: str | None = None


class DoctorTruth(StrictResponseContractModel):
    kind: DoctorTruthKind
    options: tuple[DoctorOption, ...] = ()
    complete_set: bool = True


class OwnedPackageInfo(StrictResponseContractModel):
    name: str
    device_name: str | None = None
    sessions_purchased: int
    sessions_remaining: int
    effective_status: str
    expires_at: str | None = None


class PackageOfferInfo(StrictResponseContractModel):
    service_name: str
    device_name: str | None = None
    sessions_count: int


class PackageTruth(StrictResponseContractModel):
    owned_requested: bool = False
    offers_requested: bool = False
    owned_packages: tuple[OwnedPackageInfo, ...] = ()
    package_offers: tuple[PackageOfferInfo, ...] = ()
    owned_complete_set: bool = False
    offers_complete_set: bool = False


class PulseBalanceInfo(StrictResponseContractModel):
    device_name: str
    pulses_remaining: int
    active_pack_count: int


class OwnedPulsePackInfo(StrictResponseContractModel):
    device_name: str
    pulses_purchased: int
    pulses_consumed: int
    pulses_remaining: int
    effective_status: str
    purchased_at: str | None = None
    expires_at: str | None = None


class PulseOfferInfo(StrictResponseContractModel):
    device_name: str
    pulses_count: int
    price: str
    currency: str


class PulseOverageInfo(StrictResponseContractModel):
    device_name: str
    unit_price: str | None = None
    currency: str | None = None
    requested_pulse_count: int | None = None
    total_price: str | None = None


class PulseTruth(StrictResponseContractModel):
    requested_details: tuple[str, ...]
    balances: tuple[PulseBalanceInfo, ...] = ()
    owned_packs: tuple[OwnedPulsePackInfo, ...] = ()
    available_offers: tuple[PulseOfferInfo, ...] = ()
    overage_options: tuple[PulseOverageInfo, ...] = ()
    balance_complete_set: bool = False
    owned_complete_set: bool = False
    offers_complete_set: bool = False
    overage_complete_set: bool = False


class PatientTruth(StrictResponseContractModel):
    requested_details: tuple[str, ...]
    first_name: str | None = None
    last_name: str | None = None
    phone: str | None = None
    preferred_language: str | None = None


class ServiceInfo(StrictResponseContractModel):
    name: str
    description: str | None = None
    customer_duration_text: str | None = None
    duration_minutes: int | None = None
    device_names: tuple[str, ...] = ()
    devices_complete_set: bool = False


class ServiceTruth(StrictResponseContractModel):
    kind: Literal["service_detail", "service_list"]
    services: tuple[ServiceInfo, ...] = ()
    requested_details: tuple[str, ...] = ()
    complete_set: bool = False


class ClinicWorkingHour(StrictResponseContractModel):
    weekday: int
    start: str
    end: str


class ClinicLocationInfo(StrictResponseContractModel):
    name: str | None = None
    phone: str | None = None
    address: str | None = None
    address_line1: str | None = None
    address_line2: str | None = None
    city: str | None = None
    state: str | None = None
    country_code: str | None = None
    timezone: str | None = None
    working_hours: tuple[ClinicWorkingHour, ...] = ()


class ClinicTruth(StrictResponseContractModel):
    clinic_name: str
    timezone: str | None = None
    location: ClinicLocationInfo | None = None
    knowledge: str | None = None
    requested_details: tuple[str, ...] = ()


class AppointmentServiceInfo(StrictResponseContractModel):
    service_name: str | None = None
    device_name: str | None = None


class AppointmentVisitInfo(StrictResponseContractModel):
    status: str
    start_local: str
    end_local: str | None = None
    doctor_name: str | None = None
    services: tuple[AppointmentServiceInfo, ...] = ()


class AppointmentFactChallengeInfo(StrictResponseContractModel):
    field: Literal["time"]
    claimed_time: str


class AppointmentInfoTruth(StrictResponseContractModel):
    visits: tuple[AppointmentVisitInfo, ...] = ()
    complete_set: bool = True
    fact_challenge: AppointmentFactChallengeInfo | None = None


class CustomerResponseUnit(StrictResponseContractModel):
    response_goal: ResponseGoal
    status: str
    action_truth: ActionTruth | None = None
    availability_truth: AvailabilityTruth | None = None
    commercial_truth: CommercialTruth | None = None
    doctor_truth: DoctorTruth | None = None
    package_truth: PackageTruth | None = None
    pulse_truth: PulseTruth | None = None
    patient_truth: PatientTruth | None = None
    service_truth: ServiceTruth | None = None
    clinic_truth: ClinicTruth | None = None
    appointment_truth: AppointmentInfoTruth | None = None
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
_DOCTOR_GOALS = frozenset({
    "answer_doctor",
    "ask_doctor_choice",
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
SUPPORTED_DOCTOR_RESPONSE_GOALS = _DOCTOR_GOALS
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
    next_field = facts.get("booking_next_field")
    if next_field in {"date", "booking"}:
        result.append(_make_fact(goal=goal, key="booking_next_field", value=next_field))
    return result


def _price_parts(
    value: object,
    *,
    currency_hint: object = None,
) -> tuple[str, str] | None:
    if not isinstance(value, str) or not value.strip():
        return None
    parts = value.strip().split()
    if not parts:
        return None
    try:
        amount = Decimal(parts[0])
    except InvalidOperation:
        return None
    currency = str(currency_hint or (parts[1] if len(parts) > 1 else "")).strip().upper()
    if not currency:
        return None
    amount_text = format(amount, "f")
    return amount_text, currency


def _price_value(source: dict[str, object]) -> object:
    price = source.get("price")
    if price not in (None, ""):
        return price
    minor = source.get("price_minor")
    currency = source.get("currency")
    if minor is None or currency in (None, ""):
        return None
    try:
        amount = Decimal(int(minor)) / Decimal(100)
    except (TypeError, ValueError):
        return None
    return f"{format(amount, '.2f')} {str(currency).upper()}"


def _commercial_option(
    *,
    qualifier: CommercialPriceQualifier,
    service_name: object,
    price: object,
    currency_hint: object = None,
    device_name: object = None,
    sessions_count: object = None,
) -> CommercialPriceOption | None:
    clean_service = str(service_name or "").strip()
    parts = _price_parts(price, currency_hint=currency_hint)
    if not clean_service or parts is None:
        return None
    amount, currency = parts
    clean_device = str(device_name or "").strip() or None
    sessions: int | None = None
    if sessions_count not in (None, ""):
        try:
            sessions = int(sessions_count)
        except (TypeError, ValueError):
            return None
        if sessions <= 0:
            return None
    if qualifier == "device" and clean_device is None:
        return None
    if qualifier == "package" and sessions is None:
        return None
    return CommercialPriceOption(
        qualifier=qualifier,
        service_name=clean_service,
        amount=amount,
        currency=currency,
        device_name=clean_device,
        sessions_count=sessions,
    )


def _service_commercial_truth(facts: dict[str, object]) -> CommercialTruth | None:
    catalog = facts.get("service_catalog")
    if not isinstance(catalog, dict):
        return None
    raw_service = catalog.get("service")
    if not isinstance(raw_service, dict):
        return None
    service = _safe_value(raw_service)
    if not isinstance(service, dict):
        return None
    service_name = str(service.get("name") or "").strip()
    if not service_name:
        return None

    selected = service.get("selected_laser_device")
    if isinstance(selected, dict):
        option = _commercial_option(
            qualifier="device",
            service_name=service_name,
            price=_price_value(selected),
            device_name=selected.get("device_name"),
        )
        if option is not None:
            return CommercialTruth(
                kind="service_device_price",
                service_name=service_name,
                options=(option,),
                complete_set=False,
            )

    raw_devices = service.get("laser_devices")
    if isinstance(raw_devices, list) and raw_devices:
        options = tuple(
            option
            for row in raw_devices
            if isinstance(row, dict)
            and (
                option := _commercial_option(
                    qualifier="device",
                    service_name=service_name,
                    price=_price_value(row),
                    device_name=row.get("device_name"),
                )
            )
            is not None
        )
        if options and len(options) == len(raw_devices):
            return CommercialTruth(
                kind=(
                    "service_device_price"
                    if len(options) == 1
                    else "service_device_price_options"
                ),
                service_name=service_name,
                options=options,
                complete_set=len(options) > 1,
            )

    option = _commercial_option(
        qualifier="base",
        service_name=service_name,
        price=_price_value(service),
        currency_hint=service.get("currency"),
    )
    if option is not None:
        return CommercialTruth(
            kind="service_base_price",
            service_name=service_name,
            options=(option,),
            complete_set=False,
        )

    return CommercialTruth(
        kind="price_unavailable",
        service_name=service_name,
        options=(),
        complete_set=False,
    )


def _package_commercial_truth(facts: dict[str, object]) -> CommercialTruth | None:
    package_facts = facts.get("package_offers")
    if not isinstance(package_facts, dict):
        return None
    raw_offers = package_facts.get("offers")
    if not isinstance(raw_offers, list):
        return None

    options: list[CommercialPriceOption] = []
    for raw in raw_offers:
        if not isinstance(raw, dict):
            return None
        option = _commercial_option(
            qualifier="package",
            service_name=raw.get("service_name"),
            price=_price_value(raw),
            currency_hint=raw.get("currency"),
            device_name=raw.get("device_name"),
            sessions_count=raw.get("sessions_count"),
        )
        if option is None:
            return None
        options.append(option)

    service_names = {option.service_name for option in options}
    service_name = next(iter(service_names)) if len(service_names) == 1 else None
    return CommercialTruth(
        kind="package_price_options",
        service_name=service_name,
        options=tuple(options),
        complete_set=len(options) > 1,
    )


def _device_clarification_commercial_truth(
    outcome: TurnOutcome,
) -> CommercialTruth | None:
    if (
        outcome.status != "needs_input"
        or outcome.response_goal != "clarification"
        or outcome.facts.get("needed") != "device"
    ):
        return None
    availability = outcome.facts.get("availability")
    if not isinstance(availability, dict):
        return None
    if availability.get("device_price_conflicts") not in (None, [], {}):
        return None
    raw_options = availability.get("laser_device_options")
    if not isinstance(raw_options, list) or len(raw_options) < 2:
        return None
    service_name = str(availability.get("service_name") or "").strip()
    if not service_name:
        return None
    options = tuple(
        option
        for raw in raw_options
        if isinstance(raw, dict)
        and (
            option := _commercial_option(
                qualifier="device",
                service_name=service_name,
                price=_price_value(raw),
                device_name=raw.get("device_name"),
            )
        )
        is not None
    )
    if len(options) != len(raw_options):
        return None
    return CommercialTruth(
        kind="device_price_clarification",
        service_name=service_name,
        options=options,
        complete_set=True,
    )


def _commercial_truth(outcome: TurnOutcome) -> CommercialTruth | None:
    if outcome.status == "answered" and outcome.response_goal == "answer_price":
        return (
            _service_commercial_truth(outcome.facts)
            or _package_commercial_truth(outcome.facts)
        )
    return _device_clarification_commercial_truth(outcome)


def _doctor_option(raw: object) -> DoctorOption | None:
    if not isinstance(raw, dict):
        return None
    safe = _safe_value(raw)
    if not isinstance(safe, dict):
        return None
    name = str(safe.get("name") or safe.get("doctor_name") or "").strip()
    if not name:
        return None
    specialization = str(safe.get("specialization") or "").strip() or None
    return DoctorOption(
        name=name,
        specialization=specialization,
    )


def _doctor_truth(outcome: TurnOutcome) -> DoctorTruth | None:
    if outcome.status == "answered" and outcome.response_goal == "answer_doctor":
        wrapper = outcome.facts.get("doctors")
        if not isinstance(wrapper, dict):
            return None
        rows = wrapper.get("doctors")
        if not isinstance(rows, list):
            return None
        options: list[DoctorOption] = []
        for raw in rows:
            option = _doctor_option(raw)
            if option is None:
                return None
            options.append(option)
        return DoctorTruth(
            kind="doctor_result_set",
            options=tuple(options),
            complete_set=True,
        )

    if (
        outcome.status == "needs_input"
        and outcome.response_goal == "ask_doctor_choice"
        and outcome.choices
    ):
        options: list[DoctorOption] = []
        for choice in outcome.choices:
            name = str(choice.label or "").strip()
            if not name:
                return None
            options.append(DoctorOption(name=name))
        return DoctorTruth(
            kind="doctor_choice",
            options=tuple(options),
            complete_set=True,
        )

    return None


def _owned_package_info(raw: object) -> OwnedPackageInfo | None:
    if not isinstance(raw, dict):
        return None
    safe = _safe_value(raw)
    if not isinstance(safe, dict):
        return None
    name = str(safe.get("name") or "").strip()
    effective_status = str(safe.get("effective_status") or "").strip()
    if not name or not effective_status:
        return None
    try:
        sessions_purchased = int(safe.get("sessions_purchased"))
        sessions_remaining = int(safe.get("sessions_remaining"))
    except (TypeError, ValueError):
        return None
    if sessions_purchased < 0 or sessions_remaining < 0:
        return None
    device_name = str(safe.get("laser_device_name") or "").strip() or None
    expires_at = str(safe.get("expires_at") or "").strip() or None
    return OwnedPackageInfo(
        name=name,
        device_name=device_name,
        sessions_purchased=sessions_purchased,
        sessions_remaining=sessions_remaining,
        effective_status=effective_status,
        expires_at=expires_at,
    )


def _package_offer_info(raw: object) -> PackageOfferInfo | None:
    if not isinstance(raw, dict):
        return None
    safe = _safe_value(raw)
    if not isinstance(safe, dict):
        return None
    service_name = str(safe.get("service_name") or "").strip()
    if not service_name:
        return None
    try:
        sessions_count = int(safe.get("sessions_count"))
    except (TypeError, ValueError):
        return None
    if sessions_count <= 0:
        return None
    device_name = str(safe.get("device_name") or "").strip() or None
    return PackageOfferInfo(
        service_name=service_name,
        device_name=device_name,
        sessions_count=sessions_count,
    )


def _package_truth(outcome: TurnOutcome) -> PackageTruth | None:
    if outcome.status != "answered" or outcome.response_goal != "package_information":
        return None

    owned_requested = "customer_packages" in outcome.facts
    offers_requested = "package_offers" in outcome.facts
    if not owned_requested and not offers_requested:
        return None

    owned: list[OwnedPackageInfo] = []
    if owned_requested:
        wrapper = outcome.facts.get("customer_packages")
        if not isinstance(wrapper, dict):
            return None
        rows = wrapper.get("packages", [])
        if not isinstance(rows, list):
            return None
        for raw in rows:
            item = _owned_package_info(raw)
            if item is None:
                return None
            owned.append(item)

    offers: list[PackageOfferInfo] = []
    if offers_requested:
        wrapper = outcome.facts.get("package_offers")
        if not isinstance(wrapper, dict):
            return None
        rows = wrapper.get("offers", [])
        if not isinstance(rows, list):
            return None
        for raw in rows:
            item = _package_offer_info(raw)
            if item is None:
                return None
            offers.append(item)

    return PackageTruth(
        owned_requested=owned_requested,
        offers_requested=offers_requested,
        owned_packages=tuple(owned),
        package_offers=tuple(offers),
        owned_complete_set=owned_requested,
        offers_complete_set=offers_requested,
    )


def _pulse_balance_info(raw: object) -> PulseBalanceInfo | None:
    if not isinstance(raw, dict):
        return None
    safe = _safe_value(raw)
    if not isinstance(safe, dict):
        return None
    device_name = str(safe.get("device_name") or "").strip()
    if not device_name:
        return None
    try:
        remaining = int(safe.get("pulses_remaining"))
        active_pack_count = int(safe.get("active_pack_count"))
    except (TypeError, ValueError):
        return None
    if remaining < 0 or active_pack_count < 0:
        return None
    return PulseBalanceInfo(
        device_name=device_name,
        pulses_remaining=remaining,
        active_pack_count=active_pack_count,
    )


def _owned_pulse_pack_info(raw: object) -> OwnedPulsePackInfo | None:
    if not isinstance(raw, dict):
        return None
    safe = _safe_value(raw)
    if not isinstance(safe, dict):
        return None
    device_name = str(safe.get("device_name") or "").strip()
    effective_status = str(safe.get("effective_status") or "").strip()
    if not device_name or not effective_status:
        return None
    try:
        purchased = int(safe.get("pulses_purchased"))
        consumed = int(safe.get("pulses_consumed"))
        remaining = int(safe.get("pulses_remaining"))
    except (TypeError, ValueError):
        return None
    if min(purchased, consumed, remaining) < 0:
        return None
    return OwnedPulsePackInfo(
        device_name=device_name,
        pulses_purchased=purchased,
        pulses_consumed=consumed,
        pulses_remaining=remaining,
        effective_status=effective_status,
        purchased_at=str(safe.get("purchased_at") or "").strip() or None,
        expires_at=str(safe.get("expires_at") or "").strip() or None,
    )


def _pulse_offer_info(raw: object) -> PulseOfferInfo | None:
    if not isinstance(raw, dict):
        return None
    safe = _safe_value(raw)
    if not isinstance(safe, dict):
        return None
    device_name = str(safe.get("device_name") or "").strip()
    price = str(safe.get("price") or "").strip()
    currency = str(safe.get("currency") or "").strip().upper()
    try:
        count = int(safe.get("pulses_count"))
    except (TypeError, ValueError):
        return None
    if not device_name or not price or not currency or count <= 0:
        return None
    return PulseOfferInfo(
        device_name=device_name,
        pulses_count=count,
        price=price,
        currency=currency,
    )


def _pulse_overage_info(
    raw: object,
    *,
    requested_pulse_count: object = None,
    total_price: object = None,
) -> PulseOverageInfo | None:
    if not isinstance(raw, dict):
        return None
    safe = _safe_value(raw)
    if not isinstance(safe, dict):
        return None
    device_name = str(safe.get("device_name") or "").strip()
    if not device_name:
        return None
    unit_price = str(safe.get("overage_price") or "").strip() or None
    currency = str(safe.get("currency") or "").strip().upper() or None
    count: int | None = None
    if requested_pulse_count is not None:
        try:
            count = int(requested_pulse_count)
        except (TypeError, ValueError):
            return None
        if count <= 0:
            return None
    total = str(total_price or "").strip() or None
    if total is not None and count is None:
        return None
    return PulseOverageInfo(
        device_name=device_name,
        unit_price=unit_price,
        currency=currency,
        requested_pulse_count=count,
        total_price=total,
    )


def _pulse_truth(outcome: TurnOutcome) -> PulseTruth | None:
    if outcome.status != "answered" or outcome.response_goal != "pulse_information":
        return None

    raw_requested = outcome.facts.get("pulse_requested_details")
    if not isinstance(raw_requested, list):
        return None
    requested = tuple(
        str(detail)
        for detail in raw_requested
        if str(detail) in {"balance", "owned_packs", "offers", "overage_price"}
    )
    if not requested or len(requested) != len(raw_requested):
        return None

    balances: list[PulseBalanceInfo] = []
    if "balance" in requested:
        wrapper = outcome.facts.get("pulse_balance")
        if not isinstance(wrapper, dict):
            return None
        rows = wrapper.get("balances", [])
        if not isinstance(rows, list):
            return None
        for raw in rows:
            item = _pulse_balance_info(raw)
            if item is None:
                return None
            balances.append(item)

    owned: list[OwnedPulsePackInfo] = []
    if "owned_packs" in requested:
        wrapper = outcome.facts.get("pulse_packs")
        if not isinstance(wrapper, dict):
            return None
        rows = wrapper.get("packs", [])
        if not isinstance(rows, list):
            return None
        for raw in rows:
            item = _owned_pulse_pack_info(raw)
            if item is None:
                return None
            owned.append(item)

    offers: list[PulseOfferInfo] = []
    if "offers" in requested:
        wrapper = outcome.facts.get("pulse_pack_offers")
        if not isinstance(wrapper, dict):
            return None
        rows = wrapper.get("offers", [])
        if not isinstance(rows, list):
            return None
        for raw in rows:
            item = _pulse_offer_info(raw)
            if item is None:
                return None
            offers.append(item)

    overage: list[PulseOverageInfo] = []
    if "overage_price" in requested:
        wrapper = outcome.facts.get("pulse_billing_settings")
        if not isinstance(wrapper, dict):
            return None
        rows = wrapper.get("devices", [])
        if not isinstance(rows, list):
            return None
        shared_count = wrapper.get("requested_pulse_count")
        shared_total = wrapper.get("overage_total")
        for raw in rows:
            item = _pulse_overage_info(
                raw,
                requested_pulse_count=(shared_count if len(rows) == 1 else None),
                total_price=(shared_total if len(rows) == 1 else None),
            )
            if item is None:
                return None
            overage.append(item)

    return PulseTruth(
        requested_details=requested,
        balances=tuple(balances),
        owned_packs=tuple(owned),
        available_offers=tuple(offers),
        overage_options=tuple(overage),
        balance_complete_set="balance" in requested,
        owned_complete_set="owned_packs" in requested,
        offers_complete_set="offers" in requested,
        overage_complete_set="overage_price" in requested,
    )


def _appointment_truth(outcome: TurnOutcome) -> AppointmentInfoTruth | None:
    if outcome.status != "answered" or outcome.response_goal != "answer_customer_history":
        return None
    wrapper = outcome.facts.get("appointments")
    if not isinstance(wrapper, dict):
        return None
    raw_visits = wrapper.get("visits")
    if not isinstance(raw_visits, list):
        return None

    visits: list[AppointmentVisitInfo] = []
    for raw_visit in raw_visits:
        if not isinstance(raw_visit, dict):
            return None
        safe_visit = _safe_value(raw_visit)
        if not isinstance(safe_visit, dict):
            return None
        status = str(safe_visit.get("status") or "").strip()
        start_local = str(safe_visit.get("start_local") or "").strip()
        if not status or not start_local:
            return None
        raw_services = safe_visit.get("services", [])
        if not isinstance(raw_services, list):
            return None
        services: list[AppointmentServiceInfo] = []
        for raw_service in raw_services:
            if not isinstance(raw_service, dict):
                return None
            service_name = str(raw_service.get("service_name") or "").strip() or None
            device_name = str(raw_service.get("laser_device_name") or "").strip() or None
            if service_name is None and device_name is None:
                continue
            services.append(
                AppointmentServiceInfo(
                    service_name=service_name,
                    device_name=device_name,
                )
            )
        visits.append(
            AppointmentVisitInfo(
                status=status,
                start_local=start_local,
                end_local=str(safe_visit.get("end_local") or "").strip() or None,
                doctor_name=str(safe_visit.get("doctor_name") or "").strip() or None,
                services=tuple(services),
            )
        )

    fact_challenge = None
    raw_challenge = outcome.facts.get("appointment_fact_challenge")
    if isinstance(raw_challenge, dict):
        field = raw_challenge.get("field")
        claimed_time = raw_challenge.get("claimed_time")
        if field == "time" and isinstance(claimed_time, str) and claimed_time:
            fact_challenge = AppointmentFactChallengeInfo(
                field="time",
                claimed_time=claimed_time,
            )

    return AppointmentInfoTruth(
        visits=tuple(visits),
        complete_set=wrapper.get("complete_set") is True,
        fact_challenge=fact_challenge,
    )


def _service_truth(outcome: TurnOutcome) -> ServiceTruth | None:
    if outcome.status != "answered" or outcome.response_goal != "answer_service":
        return None
    wrapper = outcome.facts.get("service_catalog")
    if not isinstance(wrapper, dict) or "service_requested_details" not in outcome.facts:
        return None

    requested_raw = outcome.facts.get("service_requested_details", [])
    requested = tuple(str(item) for item in requested_raw) if isinstance(requested_raw, list) else ()
    allowed = {"duration", "description", "devices"}
    if not set(requested).issubset(allowed) or len(set(requested)) != len(requested):
        return None

    raw_service = wrapper.get("service")
    if isinstance(raw_service, dict):
        name = str(raw_service.get("name") or "").strip()
        if not name:
            return None
        description = str(raw_service.get("description") or "").strip() or None
        customer_duration_text = str(raw_service.get("customer_duration_text") or "").strip() or None
        duration_minutes = None
        if raw_service.get("duration_minutes") not in (None, ""):
            try:
                duration_minutes = int(raw_service["duration_minutes"])
            except (TypeError, ValueError):
                return None
            if duration_minutes <= 0:
                return None
        raw_devices = raw_service.get("laser_devices", [])
        if not isinstance(raw_devices, list):
            return None
        device_names: list[str] = []
        for row in raw_devices:
            if not isinstance(row, dict):
                return None
            device_name = str(row.get("device_name") or "").strip()
            if device_name:
                device_names.append(device_name)
        if len(device_names) != len(set(device_names)):
            return None
        return ServiceTruth(
            kind="service_detail",
            services=(ServiceInfo(
                name=name,
                description=description,
                customer_duration_text=customer_duration_text,
                duration_minutes=duration_minutes,
                device_names=tuple(device_names),
                devices_complete_set="devices" in requested,
            ),),
            requested_details=requested,
            complete_set=False,
        )

    raw_services = wrapper.get("services")
    if not isinstance(raw_services, list):
        return None
    services: list[ServiceInfo] = []
    for row in raw_services:
        if not isinstance(row, dict):
            return None
        name = str(row.get("name") or "").strip()
        if not name:
            return None
        services.append(ServiceInfo(name=name))
    names = [item.name.casefold() for item in services]
    if len(names) != len(set(names)):
        return None
    return ServiceTruth(
        kind="service_list",
        services=tuple(services),
        requested_details=(),
        complete_set=True,
    )


def _clinic_truth(outcome: TurnOutcome) -> ClinicTruth | None:
    if outcome.status != "answered" or outcome.response_goal != "answer_clinic_info":
        return None
    if "clinic_requested_details" not in outcome.facts:
        return None

    wrapper = outcome.facts.get("clinic_info")
    if not isinstance(wrapper, dict):
        return None
    requested_raw = outcome.facts.get("clinic_requested_details")
    if not isinstance(requested_raw, list) or not requested_raw:
        return None
    requested = tuple(str(item) for item in requested_raw)
    allowed = {
        "name",
        "address",
        "contact",
        "working_hours",
        "general_info",
        "open_now",
    }
    if not set(requested).issubset(allowed) or len(set(requested)) != len(requested):
        return None

    clinic_name = str(wrapper.get("clinic_name") or "").strip()
    if not clinic_name:
        return None
    timezone = str(wrapper.get("timezone") or "").strip() or None
    knowledge = str(wrapper.get("knowledge") or "").strip() or None
    if "general_info" not in requested and knowledge is not None:
        return None
    if (
        "working_hours" not in requested
        and "open_now" not in requested
        and timezone is not None
    ):
        return None

    raw_locations = wrapper.get("locations", [])
    if not isinstance(raw_locations, list) or len(raw_locations) > 1:
        return None

    location: ClinicLocationInfo | None = None
    if raw_locations:
        raw_location = raw_locations[0]
        if not isinstance(raw_location, dict):
            return None
        safe = _safe_value(raw_location)
        if not isinstance(safe, dict):
            return None

        address_fields = (
            "address",
            "address_line1",
            "address_line2",
            "city",
            "state",
            "country_code",
        )
        if "address" not in requested and any(
            safe.get(key) not in (None, "") for key in address_fields
        ):
            return None
        if "contact" not in requested and safe.get("phone") not in (None, ""):
            return None
        if (
            "working_hours" not in requested
            and "open_now" not in requested
            and (
                safe.get("timezone") not in (None, "")
                or safe.get("working_hours") not in (None, [], {})
            )
        ):
            return None

        hours: list[ClinicWorkingHour] = []
        raw_hours = safe.get("working_hours", [])
        if not isinstance(raw_hours, list):
            return None
        seen_hours: set[tuple[int, str, str]] = set()
        for row in raw_hours:
            if not isinstance(row, dict):
                return None
            weekday = row.get("weekday")
            start = str(row.get("start") or "").strip()
            end = str(row.get("end") or "").strip()
            if (
                not isinstance(weekday, int)
                or not 0 <= weekday <= 6
                or len(start) != 5
                or len(end) != 5
                or start[2:3] != ":"
                or end[2:3] != ":"
                or not (start[:2] + start[3:]).isdigit()
                or not (end[:2] + end[3:]).isdigit()
                or int(start[:2]) > 23
                or int(end[:2]) > 23
                or int(start[3:]) > 59
                or int(end[3:]) > 59
                or start >= end
            ):
                return None
            key = (weekday, start, end)
            if key in seen_hours:
                return None
            seen_hours.add(key)
            hours.append(
                ClinicWorkingHour(
                    weekday=weekday,
                    start=start,
                    end=end,
                )
            )

        location = ClinicLocationInfo(
            name=str(safe.get("name") or "").strip() or None,
            phone=str(safe.get("phone") or "").strip() or None,
            address=str(safe.get("address") or "").strip() or None,
            address_line1=str(safe.get("address_line1") or "").strip() or None,
            address_line2=str(safe.get("address_line2") or "").strip() or None,
            city=str(safe.get("city") or "").strip() or None,
            state=str(safe.get("state") or "").strip() or None,
            country_code=str(safe.get("country_code") or "").strip() or None,
            timezone=str(safe.get("timezone") or "").strip() or None,
            working_hours=tuple(hours),
        )

    return ClinicTruth(
        clinic_name=clinic_name,
        timezone=timezone,
        location=location,
        knowledge=knowledge,
        requested_details=requested,
    )


def _patient_truth(outcome: TurnOutcome) -> PatientTruth | None:
    if outcome.status != "answered" or outcome.response_goal != "answer_customer_profile":
        return None
    wrapper = outcome.facts.get("customer_profile")
    if not isinstance(wrapper, dict):
        return None
    raw_details = wrapper.get("requested_details")
    if not isinstance(raw_details, list) or not raw_details:
        return None
    requested = tuple(str(item) for item in raw_details)
    allowed = {"name", "phone", "preferred_language"}
    if not set(requested).issubset(allowed) or len(set(requested)) != len(requested):
        return None

    raw = wrapper.get("patient")
    if not isinstance(raw, dict):
        return None
    safe = _safe_value(raw)
    if not isinstance(safe, dict):
        return None

    first_name = str(safe.get("first_name") or "").strip() or None
    last_name = str(safe.get("last_name") or "").strip() or None
    phone = str(safe.get("phone") or "").strip() or None
    preferred_language = str(safe.get("preferred_language") or "").strip() or None

    if "name" in requested and first_name is None:
        return None
    if "preferred_language" in requested and preferred_language is None:
        return None
    if "name" not in requested and (first_name is not None or last_name is not None):
        return None
    if "phone" not in requested and phone is not None:
        return None
    if "preferred_language" not in requested and preferred_language is not None:
        return None

    return PatientTruth(
        requested_details=requested,
        first_name=first_name,
        last_name=last_name,
        phone=phone,
        preferred_language=preferred_language,
    )


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
    if goal == "answer_customer_history" and "appointment_fact_challenge" in safe:
        safe = {
            key: value
            for key, value in safe.items()
            if key != "appointment_fact_challenge"
        }
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
    commercial_truth = _commercial_truth(outcome)
    doctor_truth = _doctor_truth(outcome)
    package_truth = _package_truth(outcome)
    pulse_truth = _pulse_truth(outcome)
    patient_truth = _patient_truth(outcome)
    service_truth = _service_truth(outcome)
    clinic_truth = _clinic_truth(outcome)
    appointment_truth = _appointment_truth(outcome)
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
        commercial_truth=commercial_truth,
        doctor_truth=doctor_truth,
        package_truth=package_truth,
        pulse_truth=pulse_truth,
        patient_truth=patient_truth,
        service_truth=service_truth,
        clinic_truth=clinic_truth,
        appointment_truth=appointment_truth,
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


def is_pure_supported_price_device_contract(
    contract: CustomerResponseContract,
) -> bool:
    """Whether every unit has a verified commercial shape supported by Phase 3B."""
    if not contract.units:
        return False

    for unit in contract.units:
        truth = unit.commercial_truth
        if truth is None:
            return False
        if unit.response_goal == "answer_price":
            if unit.status != "answered":
                return False
            fact_keys = {fact.key for fact in unit.facts}
            if fact_keys & {
                "description",
                "duration_minutes",
                "customer_duration_text",
            }:
                return False
        elif unit.response_goal == "clarification":
            if (
                unit.status != "needs_input"
                or truth.kind != "device_price_clarification"
            ):
                return False
        else:
            return False

        signatures = [
            (
                option.qualifier,
                option.service_name,
                option.device_name,
                option.sessions_count,
                option.amount,
                option.currency,
            )
            for option in truth.options
        ]
        if len(signatures) != len(set(signatures)):
            return False

        if truth.kind == "price_unavailable":
            if truth.options:
                return False
            continue
        if truth.kind == "package_price_options":
            continue
        if not truth.options:
            return False
        if truth.kind in {
            "service_device_price_options",
            "device_price_clarification",
        }:
            if len(truth.options) < 2 or truth.complete_set is not True:
                return False
        elif len(truth.options) != 1 or truth.complete_set:
            return False

    return True


def is_pure_supported_doctor_contract(
    contract: CustomerResponseContract,
) -> bool:
    """Whether every unit has a verified doctor shape supported by Phase 3C."""
    if not contract.units:
        return False

    for unit in contract.units:
        truth = unit.doctor_truth
        if truth is None or truth.complete_set is not True:
            return False

        if unit.response_goal == "answer_doctor":
            if unit.status != "answered" or truth.kind != "doctor_result_set":
                return False
            facts = {fact.key: fact for fact in unit.facts}
            doctors = facts.get("doctors")
            if (
                doctors is None
                or doctors.complete_set is not True
                or not isinstance(doctors.value, list)
                or len(doctors.value) != len(truth.options)
            ):
                return False
        elif unit.response_goal == "ask_doctor_choice":
            if unit.status != "needs_input" or truth.kind != "doctor_choice":
                return False
            if not truth.options or len(unit.choices) != len(truth.options):
                return False
            if [choice.label for choice in unit.choices] != [
                option.name for option in truth.options
            ]:
                return False
        else:
            return False

    return True


def is_pure_supported_package_contract(
    contract: CustomerResponseContract,
) -> bool:
    """Whether every unit is read-only package information supported by Phase 3D."""
    if not contract.units:
        return False

    for unit in contract.units:
        truth = unit.package_truth
        if (
            unit.response_goal != "package_information"
            or unit.status != "answered"
            or truth is None
        ):
            return False
        if not truth.owned_requested and not truth.offers_requested:
            return False
        if truth.owned_requested is not truth.owned_complete_set:
            return False
        if truth.offers_requested is not truth.offers_complete_set:
            return False


    return True


def is_pure_supported_pulse_contract(
    contract: CustomerResponseContract,
) -> bool:
    """Whether every unit is verified read-only Pulse information."""
    if not contract.units:
        return False

    allowed_fact_keys = {
        "pulse_requested_details",
        "pulse_balance",
        "pulse_packs",
        "pulse_pack_offers",
        "pulse_billing_settings",
    }
    for unit in contract.units:
        truth = unit.pulse_truth
        if (
            unit.response_goal != "pulse_information"
            or unit.status != "answered"
            or truth is None
        ):
            return False
        if not truth.requested_details:
            return False
        if any(fact.key not in allowed_fact_keys for fact in unit.facts):
            return False
        if (
            ("balance" in truth.requested_details) is not truth.balance_complete_set
            or ("owned_packs" in truth.requested_details) is not truth.owned_complete_set
            or ("offers" in truth.requested_details) is not truth.offers_complete_set
            or ("overage_price" in truth.requested_details) is not truth.overage_complete_set
        ):
            return False

    return True


def is_pure_supported_patient_contract(
    contract: CustomerResponseContract,
) -> bool:
    """Whether every unit is a current-patient profile supported by Phase 3E."""
    if not contract.units:
        return False

    return all(
        unit.response_goal == "answer_customer_profile"
        and unit.status == "answered"
        and unit.patient_truth is not None
        for unit in contract.units
    )


def is_pure_supported_service_contract(
    contract: CustomerResponseContract,
) -> bool:
    """Whether every unit is verified read-only service information."""
    if not contract.units:
        return False
    return all(
        unit.response_goal == "answer_service"
        and unit.status == "answered"
        and unit.service_truth is not None
        and unit.commercial_truth is None
        and (
            "devices" not in unit.service_truth.requested_details
            or all(item.devices_complete_set for item in unit.service_truth.services)
        )
        for unit in contract.units
    )


def is_pure_supported_clinic_contract(
    contract: CustomerResponseContract,
) -> bool:
    """Whether every unit is verified read-only clinic information."""
    if not contract.units:
        return False

    return all(
        unit.response_goal == "answer_clinic_info"
        and unit.status == "answered"
        and unit.clinic_truth is not None
        and unit.commercial_truth is None
        and unit.availability_truth is None
        and all(
            fact.key in {"clinic_info", "clinic_requested_details"}
            for fact in unit.facts
        )
        for unit in contract.units
    )


def is_pure_supported_appointment_contract(
    contract: CustomerResponseContract,
) -> bool:
    """Whether every unit is read-only upcoming appointment information."""
    if not contract.units:
        return False

    return all(
        unit.response_goal == "answer_customer_history"
        and unit.status == "answered"
        and unit.appointment_truth is not None
        and all(fact.key == "appointments" for fact in unit.facts)
        for unit in contract.units
    )
