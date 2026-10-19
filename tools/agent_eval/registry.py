from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from importlib import import_module
from typing import Any

BatchName = str

_BATCH_MODULES = {
    "batch_01": "tools.agent_eval.run_batch_01",
    "batch_02": "tools.agent_eval.run_batch_02",
    "batch_03": "tools.agent_eval.run_batch_03",
    "batch_04": "tools.agent_eval.run_batch_04",
    "batch_05": "tools.agent_eval.run_batch_05",
    "batch_06": "tools.agent_eval.run_batch_06",
}

_BATCH1 = (
    ("case_price", "service_price_prp"),
    ("case_ambiguous_laser", "booking_ambiguous_laser"),
    ("case_full_booking", "full_booking"),
    ("case_unavailable", "unavailable_time"),
    ("case_doctor_preference", "doctor_preference"),
    ("case_change_mind", "change_mind_booking"),
    ("case_reschedule", "reschedule_existing"),
    ("case_cancel", "cancel_existing"),
    ("case_package_remaining", "package_remaining"),
    ("case_package_other_service", "package_holder_other_service"),
    ("case_device_price", "device_specific_price"),
    ("case_multi_question", "multi_question"),
    ("case_context_followup", "contextual_followup"),
    ("case_unknown_fact", "unknown_clinic_fact"),
    ("case_handoff", "human_handoff"),
)

_BATCH2_NAMES = (
    "case_01_compound_price_booking", "case_02_two_services_same_turn",
    "case_03_buy_pulse_and_book", "case_04_explicit_use_pulses",
    "case_05_pulse_and_session_package", "case_06_pulse_overage",
    "case_07_pulse_pack_vs_overage", "case_08_pulse_financial_ledger_boundary",
    "case_09_pulse_billed_reschedule", "case_10_pulse_billed_cancel",
    "case_11_change_service_mid_flow", "case_12_change_doctor_mid_flow",
    "case_13_change_device_mid_flow", "case_14_conditional_fallback",
    "case_15_egyptian_time_ambiguity", "case_16_compare_doctors_then_select",
    "case_17_topic_switch_and_resume", "case_18_change_mind_completely",
    "case_19_duplicate_customer_message", "case_20_ambiguous_correction",
    "case_21_long_returning_customer", "case_22_long_db_wins_over_stale_chat",
    "case_23_long_old_context_no_leak", "case_24_short_control",
)

_BATCH3_NAMES = (
    "case_01_handoff_during_active_booking", "case_02_staff_takeover_then_handback",
    "case_03_financial_handoff_then_new_booking", "case_04_lifecycle_while_human_owns",
    "case_05_two_appointments_explicit_date", "case_06_two_same_service_explicit_date",
    "case_07_real_appointment_ambiguity", "case_08_candidate_selection_followup",
    "case_09_cancel_one_of_multiple", "case_10_one_session_remaining",
    "case_11_exhausted_package", "case_12_expired_package",
    "case_13_two_eligible_packages", "case_14_package_booking_then_cancel",
    "case_15_package_booking_then_reschedule", "case_16_old_cancelled_workflow_isolation",
    "case_17_old_doctor_preference_isolation", "case_18_old_device_isolation",
    "case_19_price_package_booking", "case_20_side_query_during_reschedule",
    "case_21_repeated_corrections", "case_22_abandon_then_different_booking",
)

_BATCH4_NAMES = (
    "case_01_book_then_modify_after_gap",
    "case_02_cancel_after_long_gap",
    "case_03_completed_appointment_not_actionable",
    "case_04_financial_handoff_then_return_later",
    "case_05_reception_modifies_then_handback",
    "case_06_multiple_messages_while_human_owns",
    "case_07_package_reschedule_cancel_chain",
    "case_08_package_then_nonpackage_service",
    "case_09_exhausted_package_after_gap",
    "case_10_two_appointments_one_package",
    "case_11_cancel_one_then_modify_other",
    "case_12_same_service_different_dates_devices",
    "case_13_price_availability_existing_package",
    "case_14_change_service_after_availability",
    "case_15_incremental_doctor_date_service_corrections",
    "case_16_repeat_reschedule_confirmation",
    "case_17_repeat_package_booking_confirmation",
)

_BATCH5_NAMES = (
    "case_01_same_name_different_patients",
    "case_02_phone_beats_ambiguous_name",
    "case_03_historical_name_not_current_identity",
    "case_04_slot_becomes_unavailable",
    "case_05_doctor_schedule_changes_between_turns",
    "case_06_cancelled_externally_before_reschedule",
    "case_07_same_day_current_time_boundary",
    "case_08_doctor_does_not_offer_service",
    "case_09_laser_requires_device",
    "case_10_explicit_incompatible_device",
    "case_11_doctor_change_invalidates_availability",
    "case_12_duplicate_booking_after_external_change",
    "case_13_repeat_cancel_after_external_change",
    "case_14_availability_then_competing_laser_booking",
    "case_15_two_rapid_customer_turns",
)

_BATCH6_NAMES = (
    "case_01_two_service_same_visit_success",
    "case_02_second_component_unavailable",
    "case_03_component_needs_device_clarification",
    "case_04_shared_anchor_sequences_components",
    "case_05_reschedule_entire_group",
    "case_06_cancel_entire_standard_group",
    "case_07_package_component_plus_standard_component",
    "case_08_financial_handoff_blocks_grouped_write",
    "case_09_buy_package_and_book_same_service",
    "case_10_buy_package_a_book_a_and_b",
    "case_11_replace_one_service_before_commit",
    "case_12_change_device_for_one_component",
    "case_13_side_price_query_preserves_compound",
    "case_14_remove_one_component",
    "case_15_sequence_crosses_resource_boundary",
    "case_16_canonical_state_changes_before_compound_commit",
)


@dataclass(frozen=True)
class Scenario:
    id: str
    canonical_id: str
    batch: BatchName
    tags: tuple[str, ...]
    runner_name: str

    def resolve_runner(self) -> Callable[..., Any]:
        return getattr(batch_module(self.batch), self.runner_name)


def batch_module(batch: BatchName):
    try:
        module_name = _BATCH_MODULES[batch]
    except KeyError as exc:
        raise ValueError(f"Unknown batch: {batch}") from exc
    return import_module(module_name)


def _tags(value: str) -> tuple[str, ...]:
    tags: set[str] = set()
    lowered = value.lower()
    for tag, needles in {
        "handoff": ("handoff", "human", "staff", "financial"),
        "lifecycle": ("reschedule", "cancel", "appointment", "lifecycle"),
        "package": ("package",),
        "pulse": ("pulse",),
        "booking": ("booking", "service", "doctor", "device", "time"),
        "financial": ("financial", "price", "payment", "pulse"),
    }.items():
        if any(needle in lowered for needle in needles):
            tags.add(tag)
    return tuple(sorted(tags or {"general"}))


def _rows_for(batch: BatchName) -> list[tuple[str, str]]:
    if batch == "batch_01":
        return list(_BATCH1)
    if batch == "batch_02":
        names, prefix = _BATCH2_NAMES, "b2"
    elif batch == "batch_03":
        names, prefix = _BATCH3_NAMES, "b3"
    elif batch == "batch_04":
        names, prefix = _BATCH4_NAMES, "b4"
    elif batch == "batch_05":
        names, prefix = _BATCH5_NAMES, "b5"
    else:
        names, prefix = _BATCH6_NAMES, "b6"
    return [
        (name, f"{prefix}_{name.removeprefix('case_')}")
        for name in names
    ]


def scenarios_for_batch(batch: BatchName) -> list[Scenario]:
    if batch not in _BATCH_MODULES:
        raise ValueError(f"Unknown batch: {batch}")
    return [
        Scenario(
            id=f"S{index}",
            canonical_id=canonical_id,
            batch=batch,
            tags=_tags(canonical_id),
            runner_name=runner_name,
        )
        for index, (runner_name, canonical_id) in enumerate(_rows_for(batch), start=1)
    ]


def select_scenarios(
    batch: BatchName,
    *,
    selectors: set[str] | None = None,
    tag: str | None = None,
) -> list[Scenario]:
    rows = scenarios_for_batch(batch)
    if selectors:
        known = {
            alias
            for row in rows
            for alias in (row.id, row.canonical_id, row.runner_name)
        }
        unknown = selectors - known
        if unknown:
            raise ValueError(f"Unknown scenario selector(s): {', '.join(sorted(unknown))}")
        rows = [
            row for row in rows
            if selectors & {row.id, row.canonical_id, row.runner_name}
        ]
    if tag:
        rows = [row for row in rows if tag in row.tags]
    return rows


def failed_ids_from_report(payload: dict[str, Any]) -> set[str]:
    failed: set[str] = set()
    for row in payload.get("scenario_results", ()):
        scenario_id = row.get("id")
        if not scenario_id:
            continue
        status = row.get("status")
        is_failed = (
            str(status).lower() != "passed"
            if status is not None
            else bool(row.get("issues") or row.get("execution_error"))
        )
        if is_failed:
            failed.add(str(scenario_id))
    return failed
