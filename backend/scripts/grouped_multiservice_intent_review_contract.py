from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

CAIRO = ZoneInfo("Africa/Cairo")


def _task_ids(record: dict[str, object]) -> list[str]:
    return [str(item) for item in record.get("requested_service_ids") or []]


def _has_response_goal(record: dict[str, object], goal: str) -> bool:
    return any(
        isinstance(outcome, dict) and outcome.get("response_goal") == goal
        for outcome in record.get("outcomes") or []
    )


def _availability_option_counts(record: dict[str, object]) -> list[int]:
    counts: list[int] = []
    for outcome in record.get("outcomes") or []:
        if not isinstance(outcome, dict):
            continue
        facts = outcome.get("facts")
        if not isinstance(facts, dict):
            continue
        availability = facts.get("availability")
        if not isinstance(availability, dict):
            continue
        count = availability.get("available_option_count")
        if isinstance(count, int):
            counts.append(count)
    return counts


def _fresh_slots_matching_proposal(
    record: dict[str, object],
    proposal: dict[str, object],
) -> list[dict[str, object]]:
    proposal_start = datetime.fromisoformat(str(proposal["start_at"]))
    doctor_id = str(proposal.get("doctor_id") or "")
    branch_id = str(proposal.get("branch_id") or "")
    device_key = str(proposal.get("device_key") or "")
    matches: list[dict[str, object]] = []
    for trace in record.get("traces") or []:
        if not isinstance(trace, dict) or "availability" not in (trace.get("read_kinds") or []):
            continue
        for slot in trace.get("availability_slots") or []:
            if not isinstance(slot, dict):
                continue
            raw_start = slot.get("start_at") or slot.get("start_local")
            try:
                slot_start = datetime.fromisoformat(str(raw_start))
            except (TypeError, ValueError):
                continue
            if (
                slot_start == proposal_start
                and str(slot.get("doctor_id") or "") == doctor_id
                and str(slot.get("branch_id") or "") == branch_id
                and str(slot.get("laser_device_key") or "") == device_key
            ):
                matches.append(slot)
    return matches


def validate_g6_visit_time_selection(records, ids):
    expected = {str(ids["underarm"]), str(ids["bikini"])}
    required_device_key = "candela_gentle"
    assert len(records) == 3
    for record in records:
        assert set(_task_ids(record)) == expected
        assert record["pending_write"] is None
        assert all(
            not bool(trace.get("pending_write"))
            for trace in record.get("traces") or []
            if isinstance(trace, dict)
        )

    presented = records[0].get("availability_reference_context")
    assert isinstance(presented, dict)
    assert set(presented.get("requested_service_ids") or []) == expected
    presented_options = presented.get("availability_reference_options")
    assert isinstance(presented_options, list) and presented_options

    after_side = records[1].get("availability_reference_context")
    assert isinstance(after_side, dict)
    assert set(after_side.get("requested_service_ids") or []) == expected

    final = records[2]
    assert final.get("reference_action") == "select_presented_option"
    selected_ref = str(final.get("selected_option_ref") or "")
    assert selected_ref
    assert final.get("full_interpreter_called") is False
    selected_options = [
        item
        for item in presented_options
        if isinstance(item, dict) and str(item.get("option_ref") or "") == selected_ref
    ]
    assert len(selected_options) == 1
    assert selected_options[0].get("concrete") is True
    assert int(selected_options[0].get("canonical_slot_count") or 0) == 1

    final_task = final.get("active_task")
    assert isinstance(final_task, dict)
    assert final_task.get("status") == "awaiting_confirmation"
    assert set(final_task.get("requested_service_ids") or []) == expected
    derived = final_task.get("derived")
    assert isinstance(derived, dict)
    assert str(derived.get("selected_slot_ref") or "") == selected_ref
    constraints = final_task.get("constraints")
    assert isinstance(constraints, dict)
    assert str(constraints.get("doctor_id") or "") == str(ids["doctor"])
    assert str(constraints.get("device_key") or "") == required_device_key
    assert str(constraints.get("date", {}).get("value") or "") == "2026-10-17"
    assert str(constraints.get("time", {}).get("value") or "") == "20:00"
    service_device_keys = {
        str(value)
        for value in (final_task.get("service_device_keys") or {}).values()
        if value
    }
    assert service_device_keys == {required_device_key}

    proposal = final.get("proposal")
    assert isinstance(proposal, dict)
    assert set(proposal.get("service_ids") or []) == expected
    assert not (proposal.get("visits") or []), "RA-G6 same-visit selection must remain one appointment"
    assert str(proposal.get("doctor_id") or "") == str(ids["doctor"])
    assert str(proposal.get("device_key") or "") == required_device_key
    proposal_device_keys = {
        str(value)
        for value in (proposal.get("additional_service_device_keys") or {}).values()
        if value
    }
    assert not proposal_device_keys or proposal_device_keys == {required_device_key}
    assert str(proposal.get("branch_id") or "") == str(constraints.get("branch_id") or "")
    proposal_start = datetime.fromisoformat(str(proposal["start_at"]))
    local_start = proposal_start.astimezone(CAIRO)
    assert local_start.date().isoformat() == "2026-10-17"
    assert local_start.strftime("%H:%M") == "20:00"

    fresh_matches = _fresh_slots_matching_proposal(final, proposal)
    assert fresh_matches, "RA-G6 must freshly re-read the canonical selected slot"
    assert 1 in _availability_option_counts(final), (
        "RA-G6 fresh availability verification must expose one canonical available option"
    )
    assert _has_response_goal(final, "booking_proposal")

    reply = str(final.get("reply") or "")
    assert "\u0627\u0644\u0645\u0648\u0639\u062f \u0627\u0644\u0645\u0642\u062a\u0631\u062d" in reply
    assert "\u062a\u0623\u0643\u062f\u064a" in reply and "\u062a\u0639\u062f" in reply
    assert reply.count("\u0644\u064a\u0632\u0631") >= 2
    assert "Candela Gentle" in reply
    assert "\u0645\u0631\u064a\u0645" in reply
    for forbidden in (
        "\u0645\u0634 \u0642\u0627\u062f\u0631 \u0623\u0631\u0628\u0637",
        "\u0623\u0646\u0647\u064a \u0648\u0642\u062a",
        "\u062a\u0645 \u0627\u0644\u062d\u062c\u0632",
        "\u062a\u0645 \u062a\u0623\u0643\u064a\u062f \u0627\u0644\u062d\u062c\u0632",
    ):
        assert forbidden not in reply
