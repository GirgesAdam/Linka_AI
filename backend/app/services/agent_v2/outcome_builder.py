from __future__ import annotations

from typing import Any

from app.agents.availability_presentation import availability_windows_from_slots
from app.agents.v2.semantic_context import SemanticContext
from app.agents.v2.turn_contract import TiaTurnUnderstanding
from app.services.agent_v2.outcome import OutcomeChoice, ResponseGoal, TurnOutcome
from app.services.agent_v2.planner import PlanStep, TurnPlan
from app.services.agent_v2.read_executor import ReadExecutionBundle, ReadResult


class OutcomeBuildError(RuntimeError):
    pass


_INTERNAL_KEYS = frozenset(
    {
        "id",
        "workspace_id",
        "patient_id",
        "appointment_id",
        "service_id",
        "doctor_id",
        "branch_id",
        "package_id",
        "package_offer_id",
        "pulse_pack_offer_id",
        "patient_package_id",
        "patient_pulse_pack_id",
        "purchase_transaction_id",
        "reference_transaction_id",
        "transaction_id",
        "external_id",
        "package_external_id",
        "laser_device_key",
        "device_key",
        "pulse_usage",
        "doctor_ids",
        "service_ids",
        "branch_ids",
    }
)

_COMPLETED_GOAL_BY_WRITE_KIND: dict[str, ResponseGoal] = {
    "booking": "booking_completed",
    "confirm_appointment": "appointment_confirmed",
    "cancel_appointment": "cancellation_completed",
    "reschedule": "reschedule_completed",
    "buy_package": "package_purchased",
    "buy_pulse_pack": "pulse_pack_purchased",
    "follow_up": "follow_up_created",
    "marketing_update": "marketing_updated",
}

_FAILED_GOAL_BY_WRITE_KIND: dict[str, ResponseGoal] = {
    "booking": "clarification",
    "confirm_appointment": "clarification",
    "cancel_appointment": "clarification",
    "reschedule": "clarification",
    "buy_package": "package_information",
    "buy_pulse_pack": "pulse_information",
    "follow_up": "clarification",
    "marketing_update": "clarification",
}


def _money(minor: object, currency: object) -> str | None:
    if minor is None or not currency:
        return None
    try:
        value = int(minor)
    except (TypeError, ValueError):
        return None
    return f"{value / 100:.2f} {str(currency).upper()}"


def _visible_dict(value: dict[str, Any]) -> dict[str, object]:
    currency = value.get("currency") or value.get("service_currency")
    visible: dict[str, object] = {}
    for key, item in value.items():
        if key in _INTERNAL_KEYS or key.endswith("_ids"):
            continue
        if key.endswith("_id"):
            continue
        if key.endswith("_minor") and currency:
            converted = _money(item, currency)
            if converted is not None:
                visible[key.removesuffix("_minor")] = converted
            continue
        if key == "billing_context":
            visible["billing"] = {
                "package_prepaid": "package",
                "pulse_prepaid": "pulse_balance",
            }.get(str(item), "standard")
            continue
        normalized = _visible_value(item)
        if normalized not in (None, {}, []):
            visible[key] = normalized
    return visible


def _visible_value(value: object) -> object:
    if isinstance(value, dict):
        return _visible_dict(value)
    if isinstance(value, list):
        return [item for item in (_visible_value(row) for row in value) if item not in (None, {}, [])]
    if isinstance(value, tuple):
        return [item for item in (_visible_value(row) for row in value) if item not in (None, {}, [])]
    return value


def customer_visible_outcome(outcome: TurnOutcome) -> dict[str, object]:
    """Return the responder payload with choice refs/internal metadata removed."""
    return {
        "status": outcome.status,
        "response_goal": outcome.response_goal,
        "facts": _visible_value(outcome.facts),
        "choices": [
            {
                "label": choice.label,
                "facts": _visible_value(choice.facts),
            }
            for choice in outcome.choices
        ],
        "action_result": _visible_value(outcome.action_result),
        "active_task_summary": _visible_value(outcome.active_task_summary),
    }


def _availability_device_price_facts(
    slots: list[object],
) -> tuple[list[dict[str, object]], list[str]]:
    grouped: dict[str, dict[str, object]] = {}
    for raw_slot in slots:
        if not isinstance(raw_slot, dict):
            continue
        device_key = str(raw_slot.get("laser_device_key") or "").strip()
        device_name = str(raw_slot.get("laser_device_name") or "").strip()
        if not device_key and not device_name:
            continue
        identity = f"key:{device_key}" if device_key else f"name:{device_name.casefold()}"
        entry = grouped.setdefault(
            identity,
            {
                "device_name": device_name,
                "prices": set(),
            },
        )
        if not entry.get("device_name") and device_name:
            entry["device_name"] = device_name
        price = _money(raw_slot.get("price_minor"), raw_slot.get("currency"))
        if price is not None:
            prices = entry["prices"]
            assert isinstance(prices, set)
            prices.add(price)

    options: list[dict[str, object]] = []
    conflicts: list[str] = []
    for entry in grouped.values():
        device_name = str(entry.get("device_name") or "").strip()
        if not device_name:
            continue
        prices = entry["prices"]
        assert isinstance(prices, set)
        option: dict[str, object] = {"device_name": device_name}
        if len(prices) == 1:
            option["price"] = next(iter(prices))
        elif len(prices) > 1:
            conflicts.append(device_name)
        options.append(option)

    options.sort(key=lambda row: str(row["device_name"]))
    conflicts.sort()
    return options, conflicts


def _availability_facts(result: ReadResult) -> dict[str, object]:
    payload = result.payload
    raw_slots = payload.get("slots")
    slots = raw_slots if isinstance(raw_slots, list) else []
    windows = availability_windows_from_slots(slots)
    visible_windows = [
        {
            key: window.get(key)
            for key in (
                "doctor_name",
                "laser_device_name",
                "start_local",
                "end_local",
                "start_time_24h",
                "end_time_24h",
            )
            if window.get(key) not in (None, "")
        }
        for window in windows
    ]
    laser_device_options, device_price_conflicts = _availability_device_price_facts(slots)
    prices = {
        str(_money(slot.get("price_minor"), slot.get("currency")))
        for slot in slots
        if isinstance(slot, dict) and _money(slot.get("price_minor"), slot.get("currency"))
    }
    facts: dict[str, object] = {
        "service_name": payload.get("service_name"),
        "checked_dates": payload.get("checked_dates") or [],
        "availability_windows": visible_windows,
        "available_option_count": len(slots),
        "search_truncated": bool(payload.get("search_truncated")),
    }
    if laser_device_options:
        facts["laser_device_options"] = laser_device_options
        if device_price_conflicts:
            facts["device_price_conflicts"] = device_price_conflicts
        if len(laser_device_options) == 1 and "price" in laser_device_options[0]:
            facts["price"] = laser_device_options[0]["price"]
    elif len(prices) == 1:
        facts["price"] = next(iter(prices))
    elif prices:
        facts["prices"] = sorted(prices)
    return {key: value for key, value in facts.items() if value not in (None, "", [])}


def _requested_service_details(step: PlanStep, turn: TiaTurnUnderstanding) -> set[str]:
    try:
        operation = turn.operations[step.operation_index]
    except IndexError:
        return set()
    details = set(operation.requested_service_details)
    if operation.type == "pricing":
        details.add("price")
    if operation.type == "service_info" and not details:
        details.add("description")
    return details


_CLINIC_DETAIL_ORDER = (
    "name",
    "address",
    "contact",
    "working_hours",
    "general_info",
    "open_now",
)


def _requested_clinic_details(
    step: PlanStep,
    turn: TiaTurnUnderstanding,
) -> tuple[str, ...]:
    try:
        operation = turn.operations[step.operation_index]
    except IndexError:
        return ()
    if operation.type != "clinic_info":
        return ()
    requested = list(dict.fromkeys(operation.requested_clinic_details))
    if not requested:
        requested = [
            "name",
            "address",
            "contact",
            "working_hours",
            "general_info",
        ]
    return tuple(detail for detail in _CLINIC_DETAIL_ORDER if detail in requested)


def _priced_laser_devices(service: dict[str, object]) -> list[dict[str, object]]:
    raw_devices = service.get("laser_devices")
    if not isinstance(raw_devices, list):
        return []
    priced: list[dict[str, object]] = []
    for device in raw_devices:
        if not isinstance(device, dict) or device.get("configured") is False:
            continue
        price = _money(device.get("price_minor"), device.get("currency"))
        if price is None:
            continue
        priced.append(
            {
                "device_key": device.get("device_key"),
                "device_name": device.get("device_name"),
                "price": price,
            }
        )
    return priced


def _service_catalog_facts(
    result: ReadResult,
    requested_details: set[str],
    *,
    selected_device_key: str | None = None,
) -> dict[str, object]:
    raw_service = result.payload.get("service")
    if not isinstance(raw_service, dict):
        visible = _visible_value(result.payload)
        return visible if isinstance(visible, dict) else {}

    shaped: dict[str, object] = {}
    if raw_service.get("name") not in (None, ""):
        shaped["name"] = raw_service["name"]

    priced_devices = _priced_laser_devices(raw_service)
    requires_device = bool(raw_service.get("requires_laser_device")) or bool(priced_devices)
    if "price" in requested_details:
        if requires_device:
            shaped["requires_laser_device"] = True
            selected = next(
                (
                    device
                    for device in priced_devices
                    if selected_device_key is not None
                    and str(device.get("device_key")) == selected_device_key
                ),
                None,
            )
            if selected is None and selected_device_key is None and len(priced_devices) == 1:
                selected = priced_devices[0]
            if selected is not None:
                shaped["selected_laser_device"] = {
                    "device_name": selected.get("device_name"),
                    "price": selected["price"],
                }
            elif selected_device_key is None and priced_devices:
                shaped["laser_devices"] = [
                    {
                        "device_name": device.get("device_name"),
                        "price": device["price"],
                    }
                    for device in priced_devices
                ]
        else:
            price = _money(raw_service.get("price_minor"), raw_service.get("currency"))
            if price is None and raw_service.get("price") not in (None, ""):
                price = str(raw_service["price"])
            if price is not None:
                shaped["price"] = price
                if raw_service.get("currency") not in (None, ""):
                    shaped["currency"] = str(raw_service["currency"]).upper()

    visible_service = _visible_value(raw_service)
    if isinstance(visible_service, dict):
        if "duration" in requested_details:
            if visible_service.get("customer_duration_text") not in (None, ""):
                shaped["customer_duration_text"] = visible_service["customer_duration_text"]
            elif visible_service.get("duration_minutes") not in (None, ""):
                shaped["duration_minutes"] = visible_service["duration_minutes"]
        if "description" in requested_details and visible_service.get("description") not in (None, ""):
            shaped["description"] = visible_service["description"]
        if "devices" in requested_details and visible_service.get("laser_devices") not in (None, [], {}):
            raw_devices = visible_service["laser_devices"]
            if isinstance(raw_devices, list):
                shaped["laser_devices"] = [
                    {"device_name": row["device_name"]}
                    for row in raw_devices
                    if isinstance(row, dict) and row.get("device_name") not in (None, "")
                ]
    return {"service": shaped}


def _read_facts(
    result: ReadResult,
    *,
    requested_service_details: set[str],
    selected_device_key: str | None = None,
) -> dict[str, object]:
    if result.kind == "availability":
        compatibility_failure = result.payload.get("compatibility_failure")
        if isinstance(compatibility_failure, dict):
            visible = _visible_value(compatibility_failure)
            return {
                "compatibility_failure": (
                    visible if isinstance(visible, dict) else {}
                )
            }
        return {"availability": _availability_facts(result)}
    if result.kind == "service_catalog":
        return {
            "service_catalog": _service_catalog_facts(
                result,
                requested_service_details,
                selected_device_key=selected_device_key,
            )
        }
    return {result.kind: _visible_value(result.payload)}


def _facts_from_reads(
    bundle: ReadExecutionBundle | None,
    *,
    requested_service_details: set[str],
    selected_device_key: str | None = None,
) -> dict[str, object]:
    if bundle is None:
        return {}
    facts: dict[str, object] = {}
    for result in bundle.results:
        current = _read_facts(
            result,
            requested_service_details=requested_service_details,
            selected_device_key=selected_device_key,
        )
        for key, value in current.items():
            if key not in facts:
                facts[key] = value
                continue
            existing = facts[key]
            if isinstance(existing, list):
                existing.append(value)
            else:
                facts[key] = [existing, value]
    return facts


def _clinic_information_response_facts(
    facts: dict[str, object],
    *,
    requested_details: tuple[str, ...],
) -> dict[str, object]:
    shaped = dict(facts)
    wrapper = shaped.get("clinic_info")
    if not isinstance(wrapper, dict):
        return shaped

    safe: dict[str, object] = {}
    clinic_name = str(wrapper.get("clinic_name") or "").strip()
    if clinic_name:
        safe["clinic_name"] = clinic_name

    raw_locations = wrapper.get("locations")
    location = (
        raw_locations[0]
        if isinstance(raw_locations, list)
        and len(raw_locations) == 1
        and isinstance(raw_locations[0], dict)
        else None
    )
    if isinstance(location, dict):
        safe_location: dict[str, object] = {}
        if location.get("name") not in (None, ""):
            safe_location["name"] = location["name"]

        if "address" in requested_details:
            for key in (
                "address",
                "address_line1",
                "address_line2",
                "city",
                "state",
                "country_code",
            ):
                if location.get(key) not in (None, ""):
                    safe_location[key] = location[key]

        if "contact" in requested_details:
            if location.get("phone") not in (None, ""):
                safe_location["phone"] = location["phone"]

        if (
            "working_hours" in requested_details
            or "open_now" in requested_details
        ):
            if location.get("timezone") not in (None, ""):
                safe_location["timezone"] = location["timezone"]
            raw_hours = location.get("working_hours")
            if isinstance(raw_hours, list):
                safe_location["working_hours"] = [
                    {
                        "weekday": row["weekday"],
                        "start": row["start"],
                        "end": row["end"],
                    }
                    for row in raw_hours
                    if isinstance(row, dict)
                    and row.get("weekday") is not None
                    and row.get("start") not in (None, "")
                    and row.get("end") not in (None, "")
                ]

        if safe_location:
            safe["locations"] = [safe_location]

    if (
        "working_hours" in requested_details
        or "open_now" in requested_details
    ) and wrapper.get("timezone") not in (None, ""):
        safe["timezone"] = wrapper["timezone"]

    if "general_info" in requested_details and wrapper.get("knowledge") not in (None, ""):
        safe["knowledge"] = wrapper["knowledge"]

    shaped["clinic_info"] = safe
    shaped["clinic_requested_details"] = list(requested_details)
    return shaped


def _package_information_response_facts(
    facts: dict[str, object],
) -> dict[str, object]:
    """Keep read-only package info separate from Phase 3B commercial ownership."""
    shaped = dict(facts)

    owned = shaped.get("customer_packages")
    if isinstance(owned, dict):
        rows = owned.get("packages")
        if isinstance(rows, list):
            shaped["customer_packages"] = {
                "packages": [
                    {
                        key: row[key]
                        for key in (
                            "name",
                            "sessions_purchased",
                            "sessions_reserved",
                            "sessions_consumed",
                            "sessions_remaining",
                            "laser_device_name",
                            "purchased_at",
                            "expires_at",
                            "status",
                            "effective_status",
                        )
                        if isinstance(row, dict)
                        and row.get(key) not in (None, "", [], {})
                    }
                    for row in rows
                    if isinstance(row, dict)
                ]
            }

    offers = shaped.get("package_offers")
    if isinstance(offers, dict):
        rows = offers.get("offers")
        if isinstance(rows, list):
            shaped["package_offers"] = {
                "offers": [
                    {
                        key: row[key]
                        for key in (
                            "service_name",
                            "device_name",
                            "sessions_count",
                            "is_active",
                        )
                        if isinstance(row, dict)
                        and row.get(key) not in (None, "", [], {})
                    }
                    for row in rows
                    if isinstance(row, dict)
                ]
            }

    return shaped


def _appointment_information_response_facts(
    facts: dict[str, object],
) -> dict[str, object]:
    """Expose only read-only upcoming-visit facts needed for customer presentation."""
    shaped = dict(facts)
    wrapper = shaped.get("appointments")
    if not isinstance(wrapper, dict):
        return shaped

    raw_visits = wrapper.get("visits")
    visits: list[dict[str, object]] = []
    if isinstance(raw_visits, list):
        for raw_visit in raw_visits:
            if not isinstance(raw_visit, dict):
                continue
            visit = {
                key: raw_visit[key]
                for key in ("status", "start_local", "end_local", "doctor_name")
                if raw_visit.get(key) not in (None, "", [], {})
            }
            raw_services = raw_visit.get("services")
            if isinstance(raw_services, list):
                visit["services"] = [
                    {
                        key: raw_service[key]
                        for key in ("service_name", "laser_device_name")
                        if isinstance(raw_service, dict)
                        and raw_service.get(key) not in (None, "", [], {})
                    }
                    for raw_service in raw_services
                    if isinstance(raw_service, dict)
                ]
            visits.append(visit)

    shaped["appointments"] = {
        "visits": visits,
        "visit_count": len(visits),
        "presentation_unit": "visit",
        "complete_set": True,
    }
    return shaped


def _patient_crm_response_facts(
    facts: dict[str, object],
    *,
    response_goal: ResponseGoal | None,
) -> dict[str, object]:
    """Expose only response-safe patient fields without moving financial ownership."""
    shaped = dict(facts)

    if response_goal == "answer_customer_profile":
        wrapper = shaped.get("customer_profile")
        if isinstance(wrapper, dict):
            patient = wrapper.get("patient")
            safe_patient = patient if isinstance(patient, dict) else {}
            requested = list(
                wrapper.get("requested_details")
                or ["name", "phone", "preferred_language"]
            )
            visible_keys: list[str] = []
            if "name" in requested:
                visible_keys.extend(("first_name", "last_name"))
            if "phone" in requested:
                visible_keys.append("phone")
            if "preferred_language" in requested:
                visible_keys.append("preferred_language")
            shaped["customer_profile"] = {
                "patient": {
                    key: safe_patient[key]
                    for key in visible_keys
                    if safe_patient.get(key) not in (None, "", [], {})
                },
                "requested_details": requested,
            }

    if response_goal == "answer_customer_history":
        wrapper = shaped.get("customer_history")
        if isinstance(wrapper, dict):
            history = wrapper.get("history")
            if isinstance(history, dict):
                visible_history: dict[str, object] = {
                    key: history[key]
                    for key in (
                        "first_clinic_activity_at",
                        "last_clinic_activity_at",
                        "total_appointments",
                        "completed_appointments",
                        "cancelled_appointments",
                        "no_show_appointments",
                        "recent_visit_count",
                        "presentation_unit",
                    )
                    if history.get(key) not in (None, "", [], {})
                }
                services = history.get("services")
                if isinstance(services, list):
                    visible_history["services"] = [
                        {
                            key: row[key]
                            for key in (
                                "service_name",
                                "completed_visits",
                                "first_completed_at",
                                "last_completed_at",
                            )
                            if isinstance(row, dict)
                            and row.get(key) not in (None, "", [], {})
                        }
                        for row in services
                        if isinstance(row, dict)
                    ]
                recent_appointments = history.get("recent_appointments")
                if isinstance(recent_appointments, list):
                    visible_history["recent_appointments"] = [
                        {
                            key: row[key]
                            for key in (
                                "status",
                                "start_at",
                                "end_at",
                                "service_name",
                                "branch_name",
                                "doctor_name",
                            )
                            if isinstance(row, dict)
                            and row.get(key) not in (None, "", [], {})
                        }
                        for row in recent_appointments
                        if isinstance(row, dict)
                    ]
                recent_visits = history.get("recent_visits")
                if isinstance(recent_visits, list):
                    visible_history["recent_visits"] = [
                        {
                            key: row[key]
                            for key in (
                                "status",
                                "start_at",
                                "end_at",
                                "services",
                                "service_name",
                                "branch_name",
                                "doctor_name",
                                "date",
                            )
                            if isinstance(row, dict)
                            and row.get(key) not in (None, "", [], {})
                        }
                        for row in recent_visits
                        if isinstance(row, dict)
                    ]
                shaped["customer_history"] = {"history": visible_history}

    return shaped


def _facts_for_completed_write(facts: dict[str, object], write_kind: str) -> dict[str, object]:
    """Hide scheduling end-times after booking/reschedule completion.

    End-times are useful when presenting availability ranges, but after an exact write they can make
    a language model infer a session duration the customer never asked for. The completed action only
    needs the verified start time, doctor/device, service, and other explicitly relevant facts.
    """

    if write_kind not in {"booking", "reschedule"}:
        return facts
    shaped = dict(facts)
    availability = shaped.get("availability")
    if not isinstance(availability, dict):
        return shaped
    visible_availability = dict(availability)
    windows = visible_availability.get("availability_windows")
    if isinstance(windows, list):
        visible_availability["availability_windows"] = [
            {
                key: item
                for key, item in window.items()
                if key not in {"end_local", "end_time_24h"}
            }
            if isinstance(window, dict)
            else window
            for window in windows
        ]
    shaped["availability"] = visible_availability
    return shaped


def _model_rows(context: SemanticContext, collection: str) -> list[dict[str, object]]:
    rows = context.model_input.get(collection)
    if not isinstance(rows, list):
        return []
    return [dict(row) for row in rows if isinstance(row, dict)]


def _row_by_ref(context: SemanticContext, ref: str) -> dict[str, object] | None:
    for collection in ("services", "doctors", "appointments", "packages"):
        for row in _model_rows(context, collection):
            if row.get("ref") == ref:
                return row
    for service in _model_rows(context, "services"):
        devices = service.get("devices")
        if not isinstance(devices, list):
            continue
        for device in devices:
            if isinstance(device, dict) and device.get("ref") == ref:
                return dict(device)
    return None


def _label_for_ref(context: SemanticContext, ref: str) -> str:
    row = _row_by_ref(context, ref)
    if row is None:
        return "خيار متاح"
    name = row.get("name")
    if name:
        return str(name)
    service_ref = row.get("service_ref")
    doctor_ref = row.get("doctor_ref")
    pieces: list[str] = []
    if isinstance(service_ref, str):
        service = _row_by_ref(context, service_ref)
        if service and service.get("name"):
            pieces.append(str(service["name"]))
    if isinstance(doctor_ref, str):
        doctor = _row_by_ref(context, doctor_ref)
        if doctor and doctor.get("name"):
            pieces.append(str(doctor["name"]))
    if row.get("start_local"):
        pieces.append(str(row["start_local"]))
    return " · ".join(pieces) if pieces else "خيار متاح"


def _semantic_choices(
    *,
    step: PlanStep,
    turn: TiaTurnUnderstanding,
    semantic_context: SemanticContext,
) -> list[OutcomeChoice]:
    if step.clarification_field not in {"service", "doctor", "device", "appointment", "package"}:
        return []
    try:
        operation = turn.operations[step.operation_index]
    except IndexError:
        return []
    entity = getattr(operation.entities, step.clarification_field)
    if entity is None:
        return []
    return [
        OutcomeChoice(
            ref=ref,
            label=_label_for_ref(semantic_context, ref),
            facts={},
        )
        for ref in entity.candidate_refs
        if semantic_context.resolve(ref) is not None
    ]


def _choices_from_appointments(bundle: ReadExecutionBundle | None) -> list[OutcomeChoice]:
    if bundle is None:
        return []
    choices: list[OutcomeChoice] = []
    for result in bundle.results:
        if result.kind != "appointments":
            continue
        rows = result.payload.get("appointments")
        if not isinstance(rows, list):
            continue
        for index, row in enumerate(rows, start=1):
            if not isinstance(row, dict):
                continue
            label_parts = [
                str(row.get("service_name") or "موعد"),
                str(row.get("doctor_name") or ""),
                str(row.get("start_local") or ""),
            ]
            choices.append(
                OutcomeChoice(
                    ref=f"appointment-choice-{index}",
                    label=" · ".join(part for part in label_parts if part),
                    facts={"appointment_id": row.get("appointment_id")},
                )
            )
    return choices


def _choices_from_packages(bundle: ReadExecutionBundle | None) -> list[OutcomeChoice]:
    if bundle is None:
        return []
    choices: list[OutcomeChoice] = []
    for result in bundle.results:
        if result.kind not in {"customer_packages", "package_offers", "package_refund_quote"}:
            continue
        rows = (
            result.payload.get("packages")
            or result.payload.get("offers")
            or result.payload.get("quotes")
        )
        if not isinstance(rows, list):
            continue
        for index, row in enumerate(rows, start=1):
            if not isinstance(row, dict):
                continue
            name = row.get("name") or row.get("package_name") or row.get("service_name") or "باكيدج"
            choices.append(
                OutcomeChoice(
                    ref=f"package-choice-{index}",
                    label=str(name),
                    facts=_visible_dict(row),
                )
            )
    return choices


def _clarification_choices(
    *,
    step: PlanStep,
    turn: TiaTurnUnderstanding,
    semantic_context: SemanticContext,
    reads: ReadExecutionBundle | None,
) -> list[OutcomeChoice]:
    semantic = _semantic_choices(step=step, turn=turn, semantic_context=semantic_context)
    if semantic:
        return semantic
    if step.clarification_field == "appointment":
        return _choices_from_appointments(reads)
    if step.clarification_field == "package":
        return _choices_from_packages(reads)
    return []


def _availability_has_options(bundle: ReadExecutionBundle | None) -> bool | None:
    if bundle is None:
        return None
    for result in bundle.results:
        if result.kind != "availability":
            continue
        slots = result.payload.get("slots")
        return bool(slots) if isinstance(slots, list) else False
    return None


def _time_is_exact(step: PlanStep) -> bool:
    value = step.facts.get("time")
    return isinstance(value, dict) and value.get("mode") == "exact"


def _result_requires_staff(bundle: ReadExecutionBundle | None) -> bool:
    if bundle is None:
        return False
    return any(result.error_code == "refund_quote_requires_staff" for result in bundle.results)


def _refund_quote_choice_count(bundle: ReadExecutionBundle | None) -> int:
    if bundle is None:
        return 0
    for result in bundle.results:
        if result.kind != "package_refund_quote":
            continue
        if result.payload.get("needs_package_choice") is not True:
            return 0
        quotes = result.payload.get("quotes")
        return len(quotes) if isinstance(quotes, list) and len(quotes) > 1 else 0
    return 0


def _completed_write_goal(step: PlanStep) -> ResponseGoal:
    if step.write_intent is None:
        raise OutcomeBuildError("Completed write outcome requires a write intent.")
    goal = _COMPLETED_GOAL_BY_WRITE_KIND.get(step.write_intent.kind)
    if goal is None:
        raise OutcomeBuildError(f"No terminal response goal for write kind {step.write_intent.kind}.")
    return goal


def _failed_write_goal(step: PlanStep) -> ResponseGoal:
    if step.write_intent is None:
        return "clarification"
    return _FAILED_GOAL_BY_WRITE_KIND.get(step.write_intent.kind, "clarification")


def build_handoff_outcome(plan: TurnPlan) -> TurnOutcome:
    if plan.handoff_category is None:
        raise OutcomeBuildError("Turn plan does not require handoff.")
    return TurnOutcome(
        status="handoff",
        response_goal="handoff",
        facts={
            "category": plan.handoff_category,
            "priority": plan.handoff_priority or "normal",
        },
    )


def build_step_outcome(
    step: PlanStep,
    *,
    turn: TiaTurnUnderstanding,
    semantic_context: SemanticContext,
    reads: ReadExecutionBundle | None = None,
    action_result: dict[str, object] | None = None,
    active_task_summary: dict[str, object] | None = None,
) -> TurnOutcome:
    """Convert deterministic planning/execution facts into one responder-safe outcome."""
    requested_details = _requested_service_details(step, turn)
    requested_clinic_details = _requested_clinic_details(step, turn)
    selected_device_key = (
        str(step.facts["device_key"])
        if step.facts.get("device_key") not in (None, "")
        else None
    )
    read_facts = _facts_from_reads(
        reads,
        requested_service_details=requested_details,
        selected_device_key=selected_device_key,
    )
    base_facts = {**_visible_dict(step.facts), **read_facts}
    if step.operation_type == "service_info":
        base_facts["service_requested_details"] = sorted(requested_details)
    if step.operation_type == "clinic_info":
        base_facts = _clinic_information_response_facts(
            base_facts,
            requested_details=requested_clinic_details,
        )
    if step.response_goal == "package_information":
        base_facts = _package_information_response_facts(base_facts)
    if step.operation_type == "appointment_list":
        base_facts = _appointment_information_response_facts(base_facts)
    base_facts = _patient_crm_response_facts(
        base_facts,
        response_goal=step.response_goal,
    )
    active_summary = _visible_dict(dict(active_task_summary or {}))

    if step.disposition == "handoff":
        return TurnOutcome(
            status="handoff",
            response_goal="handoff",
            facts=base_facts,
            active_task_summary=active_summary,
        )

    if step.disposition == "clarify":
        choices = _clarification_choices(
            step=step,
            turn=turn,
            semantic_context=semantic_context,
            reads=reads,
        )
        requested_goal: ResponseGoal = step.response_goal or "clarification"
        goal = requested_goal if choices or not requested_goal.startswith("ask_") else "clarification"
        return TurnOutcome(
            status="needs_input",
            response_goal=goal,
            facts={**base_facts, "needed": step.clarification_field or "clarification"},
            choices=choices,
            active_task_summary=active_summary,
        )

    if step.disposition == "blocked":
        return TurnOutcome(
            status="blocked",
            response_goal=step.response_goal or "clarification",
            facts=base_facts,
            active_task_summary=active_summary,
        )

    if step.disposition == "write_ready":
        if action_result is None:
            raise OutcomeBuildError("A write-ready step cannot claim a customer outcome before execution.")
        if action_result.get("requires_human"):
            return TurnOutcome(
                status="handoff",
                response_goal="handoff",
                facts=base_facts,
                action_result=_visible_dict(action_result),
                active_task_summary=active_summary,
            )
        if action_result.get("ok") is not True:
            return TurnOutcome(
                status="blocked",
                response_goal=_failed_write_goal(step),
                facts=base_facts,
                action_result=_visible_dict(action_result),
                active_task_summary=active_summary,
            )
        assert step.write_intent is not None
        return TurnOutcome(
            status="completed",
            response_goal=_completed_write_goal(step),
            facts=_facts_for_completed_write(base_facts, step.write_intent.kind),
            action_result=_visible_dict(action_result),
            active_task_summary=active_summary,
        )

    if _result_requires_staff(reads):
        return TurnOutcome(
            status="blocked",
            response_goal=step.response_goal or "package_refund_quote",
            facts={**base_facts, "requires_staff_review": True},
            active_task_summary=active_summary,
        )

    refund_quote_choice_count = _refund_quote_choice_count(reads)
    if step.operation_type == "refund_quote" and refund_quote_choice_count > 1:
        return TurnOutcome(
            status="needs_input",
            response_goal="ask_package_choice",
            facts={
                "needed": "package",
                "available_quote_count": refund_quote_choice_count,
            },
            choices=_choices_from_packages(reads),
            active_task_summary=active_summary,
        )

    has_availability = _availability_has_options(reads)
    if step.operation_type in {"availability", "book", "reschedule"} and has_availability is False:
        goal: ResponseGoal = "requested_time_unavailable" if _time_is_exact(step) else "no_availability"
        return TurnOutcome(
            status="blocked",
            response_goal=goal,
            facts=base_facts,
            active_task_summary=active_summary,
        )

    return TurnOutcome(
        status="answered",
        response_goal=step.response_goal or "clarification",
        facts=base_facts,
        active_task_summary=active_summary,
    )
