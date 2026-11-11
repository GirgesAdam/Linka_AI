import pytest

from tools.agent_eval.registry import scenarios_for_batch, select_scenarios
from tools.agent_eval.run import main
from tools.agent_eval.run_batch_07 import _replacement_chain

EXPECTED = [
    "b7_01_booking_doctor_info_price_resume",
    "b7_02_booking_multiple_corrections",
    "b7_03_laser_device_correction_after_side_reads",
    "b7_04_service_replacement_invalidates_old_device",
    "b7_05_standard_to_laser_requires_device",
    "b7_06_package_side_read_financial_boundary_resume",
    "b7_07_buy_package_then_continue_booking",
    "b7_08_package_changes_externally_before_booking",
    "b7_09_booking_detour_then_reschedule",
    "b7_10_two_appointments_persisted_reschedule_target",
    "b7_11_cancel_one_then_modify_other",
    "b7_12_reception_edits_appointment_during_conversation",
    "b7_13_external_cancel_before_followup_action",
    "b7_14_abandon_old_booking_start_new",
    "b7_15_ambiguous_new_intent_preserves_active_task",
    "b7_16_completed_booking_long_detour_repeat_confirmation",
]


def test_batch7_registered_with_stable_scenarios() -> None:
    rows = scenarios_for_batch("batch_07")
    assert len(rows) == 16
    assert [row.id for row in rows] == [f"S{index}" for index in range(1, 17)]
    assert [row.canonical_id for row in rows] == EXPECTED


def test_batch7_quick_profile_selects_all(capsys) -> None:
    assert main(["--batch", "batch_07", "--profile", "quick"]) == 0
    output = capsys.readouterr().out
    assert "Selected scenarios: 16" in output
    assert "no DB or LLM execution" in output


def test_batch7_single_scenario_selector() -> None:
    rows = select_scenarios("batch_07", selectors={"S12"})
    assert [row.canonical_id for row in rows] == [EXPECTED[11]]


def test_batch7_unknown_selector_rejected() -> None:
    with pytest.raises(ValueError, match="Unknown scenario selector"):
        select_scenarios("batch_07", selectors={"S99"})


def test_replacement_chain_follows_transitive_reschedules() -> None:
    snapshot = {
        "appointments": [
            {"id": "source", "rescheduled_from_appointment_id": None},
            {"id": "replacement-1", "rescheduled_from_appointment_id": "source"},
            {"id": "replacement-2", "rescheduled_from_appointment_id": "replacement-1"},
            {"id": "unrelated", "rescheduled_from_appointment_id": None},
        ]
    }

    assert [row["id"] for row in _replacement_chain(snapshot, "source")] == [
        "replacement-1",
        "replacement-2",
    ]
