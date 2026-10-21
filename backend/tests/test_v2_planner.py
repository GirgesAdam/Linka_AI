from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.agents.v2.semantic_context import build_semantic_context
from app.agents.v2.turn_contract import (
    DateConstraint,
    EntityReference,
    Selection,
    TiaTurnUnderstanding,
    TimeConstraint,
    TurnEntities,
    TurnOperation,
)
from app.services.agent_v2.planner import (
    PlannerContext,
    ReadRequest,
    VerificationFacts,
    advance_step_after_verification,
    plan_turn,
)
from app.services.agent_v2.state import (
    BookingTaskState,
    CustomerConstraints,
    DerivedBookingState,
    OptionChoice,
    OptionSnapshot,
    RescheduleTarget,
    RescheduleTaskState,
    WriteAuthorization,
)

NOW = datetime(2026, 9, 11, 15, 0, tzinfo=UTC)


def _context(*, active_task=None) -> PlannerContext:
    semantic = build_semantic_context(
        {
            "services": [
                {"id": "service-underarm", "name": "ليزر إبط"},
                {"id": "service-bikini", "name": "ليزر بكيني"},
            ],
            "doctors": [
                {"id": "doctor-maryam", "name": "مريم"},
                {"id": "doctor-sarah", "name": "سارة"},
            ],
            "appointments": [
                {
                    "appointment_id": "appointment-1",
                    "service_id": "service-underarm",
                    "doctor_id": "doctor-maryam",
                    "status": "confirmed",
                    "start_local": "2026-09-17T19:00:00+03:00",
                }
            ],
        }
    )
    return PlannerContext(semantic_context=semantic, active_task=active_task, now=NOW)


def _operation(operation_type: str, **updates) -> TurnOperation:
    entities = TurnEntities(**updates)
    return TurnOperation(
        type=operation_type,
        entities=entities,
        selection=None,
        package_usage="unspecified",
    )


def test_availability_can_never_create_booking_write_intent() -> None:
    turn = TiaTurnUnderstanding(
        operations=[
            _operation(
                "availability",
                service=EntityReference(text=None, ref="S1", candidate_refs=[]),
                date=DateConstraint(mode="exact", start_date="2026-09-17", end_date=None),
                time=TimeConstraint(mode="exact", start_time="19:00", end_time=None),
            )
        ],
        safety_signals=[],
    )
    step = plan_turn(turn, _context()).steps[0]

    assert step.disposition == "read"
    assert step.write_intent is None
    assert [item.kind for item in step.reads] == ["availability"]


def test_appointment_list_preserves_temporal_scope_in_read_request() -> None:
    turn = TiaTurnUnderstanding(
        operations=[
            _operation(
                "appointment_list",
                date=DateConstraint(
                    mode="range",
                    start_date="2026-09-07",
                    end_date="2026-09-13",
                ),
                time=TimeConstraint(
                    mode="after",
                    start_time="12:00",
                    end_time=None,
                ),
            )
        ],
        safety_signals=[],
    )

    step = plan_turn(turn, _context()).steps[0]

    assert step.disposition == "read"
    assert step.response_goal == "answer_customer_history"
    assert [read.kind for read in step.reads] == ["appointments"]
    assert step.reads[0].parameters["date"] == turn.operations[0].entities.date.model_dump(
        mode="json"
    )
    assert step.reads[0].parameters["time"] == turn.operations[0].entities.time.model_dump(
        mode="json"
    )


def test_broad_appointment_list_keeps_unscoped_upcoming_read() -> None:
    turn = TiaTurnUnderstanding(
        operations=[_operation("appointment_list")],
        safety_signals=[],
    )

    step = plan_turn(turn, _context()).steps[0]

    assert step.reads == [ReadRequest(kind="appointments", parameters={})]


def test_exact_booking_first_presents_commercial_basis_and_verified_slot_without_write() -> None:
    turn = TiaTurnUnderstanding(
        operations=[
            _operation(
                "book",
                service=EntityReference(text=None, ref="S1", candidate_refs=[]),
                date=DateConstraint(mode="exact", start_date="2026-09-17", end_date=None),
                time=TimeConstraint(mode="exact", start_time="19:00", end_time=None),
            )
        ],
        safety_signals=[],
    )
    step = plan_turn(turn, _context()).steps[0]

    assert step.disposition == "read"
    assert step.write_intent is None
    assert step.response_goal == "answer_price"
    assert [read.kind for read in step.reads] == ["service_catalog", "availability"]
    assert step.facts["exact_time_requested"] is True


def test_exact_booking_commercial_gate_never_auto_promotes_to_write() -> None:
    turn = TiaTurnUnderstanding(
        operations=[
            _operation(
                "book",
                service=EntityReference(text=None, ref="S1", candidate_refs=[]),
                date=DateConstraint(mode="exact", start_date="2026-09-17", end_date=None),
                time=TimeConstraint(mode="exact", start_time="19:00", end_time=None),
            )
        ],
        safety_signals=[],
    )
    step = plan_turn(turn, _context()).steps[0]
    unchanged = advance_step_after_verification(
        step,
        VerificationFacts(exact_slot_match_count=0),
    )

    assert unchanged == step
    assert unchanged.write_intent is None


def test_broad_booking_window_never_auto_promotes_to_write() -> None:
    turn = TiaTurnUnderstanding(
        operations=[
            _operation(
                "book",
                service=EntityReference(text=None, ref="S1", candidate_refs=[]),
                date=DateConstraint(mode="exact", start_date="2026-09-17", end_date=None),
                time=TimeConstraint(mode="after", start_time="18:00", end_time=None),
            )
        ],
        safety_signals=[],
    )
    step = plan_turn(turn, _context()).steps[0]
    unchanged = advance_step_after_verification(
        step,
        VerificationFacts(exact_slot_match_count=1),
    )
    assert unchanged.disposition == "read"


def test_verified_booking_snapshot_selection_requires_persisted_write_authorization() -> None:
    snapshot = OptionSnapshot(
        snapshot_id="snapshot-1",
        purpose="booking_slot",
        task_version=5,
        created_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(minutes=9),
        options=[
            OptionChoice(
                ref="slot-7",
                label="7:00",
                payload={"start_time_24h": "19:00", "start_at": "2026-09-17T19:00:00+03:00"},
            )
        ],
    )
    authorized_task = BookingTaskState(
        status="awaiting_choice",
        write_authorization=WriteAuthorization(
            operation="booking",
            authorized=True,
            source_turn_id="turn-1",
            granted_at=NOW - timedelta(minutes=2),
        ),
        constraints=CustomerConstraints(service_id="service-underarm"),
        derived=DerivedBookingState(
            commercial_basis_presented_key="service-underarm|none|standalone"
        ),
        option_snapshot=snapshot,
        version=5,
    )
    selection = TurnOperation(
        type="select_active",
        entities=TurnEntities(),
        selection=Selection(kind="time", index=None, time="19:00", ref=None),
        package_usage="unspecified",
    )
    turn = TiaTurnUnderstanding(operations=[selection], safety_signals=[])

    ready = plan_turn(turn, _context(active_task=authorized_task)).steps[0]
    assert ready.disposition == "write_ready"
    assert ready.write_intent is not None
    assert ready.write_intent.kind == "booking"

    unauthorized_task = authorized_task.model_copy(
        update={
            "write_authorization": WriteAuthorization(
                operation="booking",
                authorized=False,
                source_turn_id=None,
                granted_at=None,
            )
        }
    )
    no_write = plan_turn(turn, _context(active_task=unauthorized_task)).steps[0]
    assert no_write.disposition == "respond"
    assert no_write.write_intent is None


def _authorized_reschedule_task(
    *,
    appointment_id: str = "appointment-1",
    slot_appointment_id: str | None = None,
) -> RescheduleTaskState:
    payload: dict[str, object] = {
        "start_time_24h": "10:00",
        "start_at": "2026-09-30T10:00:00+03:00",
        "service_id": "service-underarm",
        "doctor_id": "doctor-maryam",
    }
    if slot_appointment_id is not None:
        payload["appointment_id"] = slot_appointment_id
    snapshot = OptionSnapshot(
        snapshot_id="reschedule-snapshot-1",
        purpose="reschedule_slot",
        task_version=4,
        created_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(minutes=9),
        options=[OptionChoice(ref="slot-1", label="10:00", payload=payload)],
    )
    return RescheduleTaskState(
        status="awaiting_choice",
        write_authorization=WriteAuthorization(
            operation="reschedule",
            authorized=True,
            source_turn_id="turn-reschedule",
            granted_at=NOW - timedelta(minutes=2),
        ),
        target=RescheduleTarget(
            appointment_id=appointment_id,
            service_id="service-underarm",
            doctor_id="doctor-maryam",
            start_local="2026-09-28T10:00:00+03:00",
        ),
        replacement=CustomerConstraints(
            service_id="service-underarm",
            doctor_id="doctor-maryam",
            date=DateConstraint(mode="exact", start_date="2026-09-30"),
        ),
        option_snapshot=snapshot,
        version=4,
    )


def _select_reschedule_time() -> TiaTurnUnderstanding:
    return TiaTurnUnderstanding(
        operations=[
            TurnOperation(
                type="select_active",
                entities=TurnEntities(),
                selection=Selection(kind="time", time="10:00"),
                package_usage="unspecified",
                execution_intent="execute",
                continues_previous=True,
            )
        ],
        safety_signals=[],
    )


def test_reschedule_slot_selection_preserves_persisted_target_appointment_id() -> None:
    state = _authorized_reschedule_task()

    step = plan_turn(_select_reschedule_time(), _context(active_task=state)).steps[0]

    assert step.disposition == "write_ready"
    assert step.write_intent is not None
    assert step.write_intent.kind == "reschedule"
    assert step.write_intent.parameters["appointment_id"] == "appointment-1"
    assert step.write_intent.parameters["start_at"] == "2026-09-30T10:00:00+03:00"
    assert step.write_intent.parameters["_reschedule_required_fields_resolved"] is True


def test_reschedule_slot_payload_does_not_need_appointment_id() -> None:
    state = _authorized_reschedule_task()
    assert state.option_snapshot is not None
    assert "appointment_id" not in state.option_snapshot.options[0].payload

    step = plan_turn(_select_reschedule_time(), _context(active_task=state)).steps[0]

    assert step.disposition == "write_ready"
    assert step.write_intent is not None
    assert step.write_intent.parameters["appointment_id"] == state.target.appointment_id


def test_reschedule_slot_cannot_override_persisted_target_identity() -> None:
    state = _authorized_reschedule_task(slot_appointment_id="appointment-2")

    step = plan_turn(_select_reschedule_time(), _context(active_task=state)).steps[0]

    assert step.disposition == "blocked"
    assert step.write_intent is None
    assert step.clarification_field == "appointment"
    assert step.facts["reason"] == "reschedule_target_conflict"


def test_reschedule_slot_without_valid_persisted_target_fails_closed() -> None:
    state = _authorized_reschedule_task()
    invalid = state.model_copy(update={"target": None})

    step = plan_turn(_select_reschedule_time(), _context(active_task=invalid)).steps[0]

    assert step.disposition == "clarify"
    assert step.clarification_field == "appointment"
    assert step.write_intent is None


def test_stale_snapshot_selection_never_writes() -> None:
    snapshot = OptionSnapshot(
        snapshot_id="snapshot-old",
        purpose="booking_slot",
        task_version=4,
        created_at=NOW - timedelta(minutes=20),
        expires_at=NOW - timedelta(minutes=10),
        options=[OptionChoice(ref="slot-old", label="7:00", payload={"start_time_24h": "19:00"})],
    )
    task = BookingTaskState(
        status="awaiting_choice",
        write_authorization=WriteAuthorization(
            operation="booking",
            authorized=True,
            source_turn_id="turn-1",
            granted_at=NOW - timedelta(minutes=30),
        ),
        constraints=CustomerConstraints(service_id="service-underarm"),
        option_snapshot=snapshot,
        version=4,
    )
    turn = TiaTurnUnderstanding(
        operations=[
            TurnOperation(
                type="select_active",
                entities=TurnEntities(),
                selection=Selection(kind="index", index=1, time=None, ref=None),
                package_usage="unspecified",
            )
        ],
        safety_signals=[],
    )
    step = plan_turn(turn, _context(active_task=task)).steps[0]
    assert step.disposition == "clarify"
    assert step.write_intent is None


def test_cancel_active_is_not_appointment_cancellation() -> None:
    turn = TiaTurnUnderstanding(
        operations=[_operation("cancel_active")],
        safety_signals=[],
    )
    step = plan_turn(turn, _context()).steps[0]
    assert step.disposition == "state_update"
    assert step.state_action == "cancel_active"
    assert step.write_intent is None


def test_cancel_appointment_requires_verified_unique_patient_appointment() -> None:
    turn = TiaTurnUnderstanding(
        operations=[
            _operation(
                "cancel_appointment",
                appointment=EntityReference(text=None, ref="A1", candidate_refs=[]),
            )
        ],
        safety_signals=[],
    )
    step = plan_turn(turn, _context()).steps[0]
    assert step.disposition == "read"
    assert step.write_intent is not None

    ambiguous = advance_step_after_verification(
        step,
        VerificationFacts(appointment_match_count=2),
    )
    assert ambiguous.disposition == "clarify"
    assert ambiguous.clarification_field == "appointment"

    ready = advance_step_after_verification(
        step,
        VerificationFacts(
            appointment_match_count=1,
            verified_parameters={"appointment_id": "appointment-1"},
        ),
    )
    assert ready.disposition == "write_ready"


def test_medical_safety_overrides_simultaneous_booking() -> None:
    turn = TiaTurnUnderstanding(
        operations=[
            _operation(
                "book",
                service=EntityReference(text=None, ref="S1", candidate_refs=[]),
                date=DateConstraint(mode="exact", start_date="2026-09-17", end_date=None),
            )
        ],
        safety_signals=["urgent_medical", "medical"],
    )
    plan = plan_turn(turn, _context())
    assert plan.steps == []
    assert plan.handoff_category == "medical"
    assert plan.handoff_priority == "urgent"


def test_customer_profile_preserves_requested_field_scope() -> None:
    operation = TurnOperation(
        type="customer_profile",
        entities=TurnEntities(),
        requested_patient_details=["phone"],
        execution_intent="informational",
    )
    turn = TiaTurnUnderstanding(operations=[operation], safety_signals=[])

    plan = plan_turn(turn, _context())

    assert plan.steps[0].response_goal == "answer_customer_profile"
    assert plan.steps[0].reads == [
        ReadRequest(
            kind="customer_profile",
            parameters={"requested_patient_details": ["phone"]},
        )
    ]


def test_payment_question_and_payment_dispute_are_separate_semantics() -> None:
    safe_turn = TiaTurnUnderstanding(
        operations=[_operation("customer_history")],
        safety_signals=[],
    )
    safe = plan_turn(safe_turn, _context())
    assert safe.handoff_category is None
    assert safe.steps[0].reads[0].kind == "customer_history"

    dispute_turn = TiaTurnUnderstanding(
        operations=[_operation("customer_history")],
        safety_signals=["payment_dispute"],
    )
    dispute = plan_turn(dispute_turn, _context())
    assert dispute.steps == []
    assert dispute.handoff_category == "payment"


def _pulse_operation(*details: str) -> TurnOperation:
    return TurnOperation(
        type="pulse_info",
        entities=TurnEntities(),
        requested_pulse_details=list(details),
        execution_intent="informational",
    )


def test_safe_pulse_read_is_preserved_with_ordinary_human_support() -> None:
    turn = TiaTurnUnderstanding(
        operations=[
            _pulse_operation("balance"),
            _operation("human_support"),
        ],
        safety_signals=[],
    )

    plan = plan_turn(turn, _context())

    assert plan.handoff_category == "customer_request"
    assert [step.operation_type for step in plan.steps] == ["pulse_info", "human_support"]
    assert [read.kind for read in plan.steps[0].reads] == ["pulse_balance"]
    assert plan.steps[0].write_intent is None
    assert plan.steps[1].disposition == "handoff"


def test_safe_pulse_offer_read_is_preserved_with_ordinary_human_support() -> None:
    turn = TiaTurnUnderstanding(
        operations=[
            _pulse_operation("offers"),
            _operation("human_support"),
        ],
        safety_signals=[],
    )

    plan = plan_turn(turn, _context())

    assert [step.operation_type for step in plan.steps] == ["pulse_info", "human_support"]
    assert [read.kind for read in plan.steps[0].reads] == ["pulse_pack_offers"]
    assert plan.steps[1].disposition == "handoff"


def test_open_ended_service_info_reads_catalog_without_forcing_service_choice() -> None:
    turn = TiaTurnUnderstanding(
        operations=[_operation("service_info")],
        safety_signals=[],
    )

    plan = plan_turn(turn, _context())

    assert len(plan.steps) == 1
    step = plan.steps[0]
    assert step.disposition == "read"
    assert step.response_goal == "answer_service"
    assert [read.kind for read in step.reads] == ["service_catalog"]
    assert step.reads[0].parameters == {}


def test_multiple_safe_reads_keep_order_before_one_handoff() -> None:
    turn = TiaTurnUnderstanding(
        operations=[
            _operation(
                "service_info",
                service=EntityReference(text=None, ref="S1", candidate_refs=[]),
            ),
            _pulse_operation("balance"),
            _operation("human_support"),
        ],
        safety_signals=[],
    )

    plan = plan_turn(turn, _context())

    assert [step.operation_type for step in plan.steps] == [
        "service_info",
        "pulse_info",
        "human_support",
    ]
    assert sum(step.disposition == "handoff" for step in plan.steps) == 1


def test_handoff_first_is_reordered_after_safe_read() -> None:
    turn = TiaTurnUnderstanding(
        operations=[
            _operation("human_support"),
            _pulse_operation("balance"),
        ],
        safety_signals=[],
    )

    plan = plan_turn(turn, _context())

    assert [step.operation_type for step in plan.steps] == ["pulse_info", "human_support"]
    assert [read.kind for read in plan.steps[0].reads] == ["pulse_balance"]
    assert plan.steps[1].disposition == "handoff"


def test_payment_dispute_remains_terminal_over_safe_read() -> None:
    turn = TiaTurnUnderstanding(
        operations=[_pulse_operation("balance")],
        safety_signals=["payment_dispute"],
    )

    plan = plan_turn(turn, _context())

    assert plan.steps == []
    assert plan.handoff_category == "payment"


def test_urgent_medical_remains_terminal_over_safe_read() -> None:
    turn = TiaTurnUnderstanding(
        operations=[_pulse_operation("balance")],
        safety_signals=["urgent_medical"],
    )

    plan = plan_turn(turn, _context())

    assert plan.steps == []
    assert plan.handoff_category == "medical"
    assert plan.handoff_priority == "urgent"


def test_privacy_issue_remains_terminal_over_safe_read() -> None:
    turn = TiaTurnUnderstanding(
        operations=[_pulse_operation("balance")],
        safety_signals=["privacy_issue"],
    )

    plan = plan_turn(turn, _context())

    assert plan.steps == []
    assert plan.handoff_category == "customer_request"
    assert plan.handoff_priority == "high"


def test_write_plus_human_support_fails_closed_without_write() -> None:
    turn = TiaTurnUnderstanding(
        operations=[
            TurnOperation(
                type="buy_pulse_pack",
                entities=TurnEntities(pulse_count=1000),
                execution_intent="execute",
            ),
            _operation("human_support"),
        ],
        safety_signals=[],
    )

    plan = plan_turn(turn, _context())

    assert plan.handoff_category == "customer_request"
    assert len(plan.steps) == 1
    assert plan.steps[0].operation_type == "human_support"
    assert plan.steps[0].disposition == "handoff"
    assert plan.steps[0].write_intent is None


def test_marketing_false_is_an_explicit_write_not_missing_data() -> None:
    turn = TiaTurnUnderstanding(
        operations=[_operation("marketing_update", marketing_consent=False)],
        safety_signals=[],
    )
    step = plan_turn(turn, _context()).steps[0]
    assert step.disposition == "write_ready"
    assert step.write_intent is not None
    assert step.write_intent.parameters["marketing_consent"] is False


def test_compound_turn_produces_ordered_independent_steps() -> None:
    turn = TiaTurnUnderstanding(
        operations=[
            _operation(
                "pricing",
                service=EntityReference(text=None, ref="S1", candidate_refs=[]),
            ),
            _operation(
                "availability",
                service=EntityReference(text=None, ref="S1", candidate_refs=[]),
                date=DateConstraint(mode="exact", start_date="2026-09-19", end_date=None),
            ),
        ],
        safety_signals=[],
    )
    plan = plan_turn(turn, _context())
    assert [step.operation_type for step in plan.steps] == ["pricing", "availability"]
    assert plan.steps[0].write_intent is None
    assert plan.steps[1].write_intent is None


def test_laser_device_requirement_stays_deterministic_outside_model_input() -> None:
    semantic = build_semantic_context(
        {
            "services": [
                {
                    "id": "service-laser",
                    "name": "ليزر إبط",
                    "requires_laser_device": True,
                    "laser_devices": [
                        {
                            "device_key": "candela_gentle",
                            "device_name": "Candela Gentle",
                        }
                    ],
                }
            ],
            "doctors": [],
            "appointments": [],
        }
    )
    assert "requires_laser_device" not in semantic.model_input["services"][0]

    turn = TiaTurnUnderstanding(
        operations=[
            _operation(
                "book",
                service=EntityReference(text=None, ref="S1", candidate_refs=[]),
                date=DateConstraint(mode="exact", start_date="2026-09-17", end_date=None),
                time=TimeConstraint(mode="exact", start_time="19:00", end_time=None),
            )
        ],
        safety_signals=[],
    )
    context = PlannerContext(semantic_context=semantic, active_task=None, now=NOW)

    step = plan_turn(turn, context).steps[0]

    assert step.facts["service_requires_laser_device"] is True
    assert step.facts["booking_device_price_step"] is True
    assert step.disposition == "read"
    assert step.response_goal == "answer_price"
    assert [read.kind for read in step.reads] == ["service_catalog"]
    assert step.write_intent is None


def test_non_laser_booking_reads_verified_price_before_availability_write() -> None:
    operation = TurnOperation(
        type="book",
        entities=TurnEntities(
            service=EntityReference(text=None, ref="S1", candidate_refs=[]),
            date=DateConstraint(mode="exact", start_date="2026-09-17", end_date=None),
            time=TimeConstraint(mode="exact", start_time="19:00", end_time=None),
        ),
        execution_intent="execute",
    )
    step = plan_turn(
        TiaTurnUnderstanding(operations=[operation], safety_signals=[]),
        _context(),
    ).steps[0]

    assert step.disposition == "read"
    assert step.response_goal == "answer_price"
    assert [read.kind for read in step.reads] == ["service_catalog", "availability"]
    assert step.write_intent is None
    assert step.facts["commercial_basis_key"].endswith("|none|standalone")


def test_booking_planner_never_carries_pulse_billing_policy() -> None:
    operation = TurnOperation(
        type="book",
        entities=TurnEntities(
            service=EntityReference(text=None, ref="S1", candidate_refs=[]),
            date=DateConstraint(mode="exact", start_date="2026-09-17", end_date=None),
            time=TimeConstraint(mode="exact", start_time="19:00", end_time=None),
        ),
        selection=None,
        package_usage="unspecified",
    )
    step = plan_turn(
        TiaTurnUnderstanding(operations=[operation], safety_signals=[]),
        _context(),
    ).steps[0]

    assert step.disposition == "read"
    assert step.write_intent is None
    assert "pulse_usage" not in step.facts
    assert all("pulse_usage" not in read.parameters for read in step.reads)


def test_session_package_booking_is_independent_from_pulse_billing() -> None:
    operation = TurnOperation(
        type="book",
        entities=TurnEntities(
            service=EntityReference(text=None, ref="S1", candidate_refs=[]),
            date=DateConstraint(mode="exact", start_date="2026-09-17", end_date=None),
        ),
        selection=None,
        package_usage="use_existing",
    )
    step = plan_turn(
        TiaTurnUnderstanding(operations=[operation], safety_signals=[]),
        _context(),
    ).steps[0]

    assert step.disposition == "read"
    assert step.write_intent is None
    assert step.response_goal == "package_information"
    assert [read.kind for read in step.reads] == ["customer_packages", "availability"]
    assert step.facts["package_usage"] == "use_existing"
    assert "pulse_usage" not in step.facts


def test_package_info_reads_requested_offer_scope_only() -> None:
    operation = TurnOperation(
        type="package_info",
        entities=TurnEntities(),
        selection=None,
        package_usage="unspecified",
        requested_package_details=["offers"],
    )
    step = plan_turn(
        TiaTurnUnderstanding(operations=[operation], safety_signals=[]),
        _context(),
    ).steps[0]

    assert step.disposition == "read"
    assert step.write_intent is None
    assert [item.kind for item in step.reads] == ["package_offers"]


def test_package_info_keeps_owned_scope_and_backward_compatible_default() -> None:
    for requested_details in (["owned"], []):
        operation = TurnOperation(
            type="package_info",
            entities=TurnEntities(),
            selection=None,
            package_usage="unspecified",
            requested_package_details=requested_details,
        )
        step = plan_turn(
            TiaTurnUnderstanding(operations=[operation], safety_signals=[]),
            _context(),
        ).steps[0]

        assert step.disposition == "read"
        assert step.write_intent is None
        assert [item.kind for item in step.reads] == ["customer_packages"]


def test_package_info_can_read_owned_and_offers_when_both_are_requested() -> None:
    operation = TurnOperation(
        type="package_info",
        entities=TurnEntities(),
        selection=None,
        package_usage="unspecified",
        requested_package_details=["owned", "offers"],
    )
    step = plan_turn(
        TiaTurnUnderstanding(operations=[operation], safety_signals=[]),
        _context(),
    ).steps[0]

    assert step.write_intent is None
    assert [item.kind for item in step.reads] == ["customer_packages", "package_offers"]
