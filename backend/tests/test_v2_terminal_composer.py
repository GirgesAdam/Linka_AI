from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from langchain_core.messages import HumanMessage
from pydantic import ValidationError

from app.agents.llm_runtime import LLMProviderError
from app.agents.v2 import responder, terminal_composer
from app.agents.v2.responder import compose_v2_customer_reply
from app.agents.v2.terminal_composer import (
    TerminalComposerDraft,
    TerminalComposerUnitDraft,
    TerminalComposerValidationError,
    _build_terminal_composer_messages,
    compose_terminal_contract_reply,
    deterministic_terminal_fallback,
    format_customer_datetime,
    resolve_terminal_composer_draft,
    validate_terminal_composer_draft,
)
from app.services.agent_v2.outcome import TurnOutcome
from app.services.agent_v2.response_contract import (
    ActionTruth,
    CustomerResponseContract,
    CustomerResponseUnit,
    ResponseFact,
    build_customer_response_contract,
    is_pure_supported_terminal_contract,
)

NOW = datetime(2026, 9, 28, 16, 0, tzinfo=UTC)


def test_customer_datetime_formatter_uses_workspace_timezone() -> None:
    text = format_customer_datetime(
        "2026-10-05T07:00:00+00:00",
        arabic=True,
        timezone_name="Africa/Cairo",
    )

    assert text == "الاثنين 5 أكتوبر الساعة 10 صباحًا"



@pytest.mark.parametrize(
    ("iso_value", "expected"),
    [
        ("2026-08-25T10:00:00+03:00", "الثلاثاء 25 أغسطس"),
        ("2026-09-09T10:00:00+03:00", "الأربعاء 9 سبتمبر"),
        ("2026-08-20T10:00:00+03:00", "الخميس 20 أغسطس"),
    ],
)
def test_customer_datetime_formatter_uses_weekday_customer_date_contract(
    iso_value: str,
    expected: str,
) -> None:
    text = format_customer_datetime(
        iso_value,
        arabic=True,
        timezone_name="Africa/Cairo",
    )
    assert expected in text
    assert "/2026" not in text


def _terminal(
    goal: str,
    *,
    facts: dict[str, object] | None = None,
    action_result: dict[str, object] | None = None,
) -> TurnOutcome:
    return TurnOutcome(
        status="completed",
        response_goal=goal,
        facts=facts or {},
        action_result={"ok": True, **(action_result or {})},
    )


def _draft(
    *fact_keys: str,
    unit_index: int = 0,
    style: str = "warm",
    transition: str = "sentence",
) -> TerminalComposerDraft:
    return TerminalComposerDraft(
        units=[
            TerminalComposerUnitDraft(
                unit_index=unit_index,
                action_ref="unit_action",
                style=style,
                fact_keys=list(fact_keys),
                transition=transition,
            )
        ]
    )


def _fact_map(unit: CustomerResponseUnit) -> dict[str, ResponseFact]:
    return {fact.key: fact for fact in unit.facts}


@pytest.mark.parametrize(
    ("goal", "expected_action"),
    [
        ("booking_completed", "booking"),
        ("reschedule_completed", "reschedule"),
        ("cancellation_completed", "cancel_appointment"),
        ("appointment_confirmed", "confirm_appointment"),
        ("package_purchased", "buy_package"),
        ("pulse_pack_purchased", "buy_pulse_pack"),
        ("follow_up_created", "follow_up"),
        ("marketing_updated", "marketing_update"),
    ],
)
def test_all_terminal_write_goals_are_supported(
    goal: str,
    expected_action: str,
) -> None:
    contract = build_customer_response_contract([_terminal(goal)])

    assert is_pure_supported_terminal_contract(contract) is True
    assert contract.units[0].action_truth is not None
    assert contract.units[0].action_truth.action == expected_action
    assert contract.units[0].action_truth.succeeded is True


def test_active_task_cancelled_is_not_in_phase2_terminal_cutover() -> None:
    contract = build_customer_response_contract(
        [
            TurnOutcome(
                status="answered",
                response_goal="active_task_cancelled",
                facts={"active_task_cancelled": True},
            )
        ]
    )

    assert is_pure_supported_terminal_contract(contract) is False
    assert contract.units[0].action_truth is None


def test_booking_exact_values_are_backend_resolved_from_fact_refs() -> None:
    contract = build_customer_response_contract(
        [
            _terminal(
                "booking_completed",
                facts={
                    "service_name": "Full Legs Laser",
                    "device_name": "Prime Lase",
                    "start_local": "2026-09-28T19:00:00+03:00",
                },
            )
        ]
    )
    draft = _draft("service_name", "start_local", "device_name")

    assert "Full Legs Laser" not in draft.model_dump_json()
    assert "Prime Lase" not in draft.model_dump_json()
    assert "19:00" not in draft.model_dump_json()

    text = resolve_terminal_composer_draft(
        contract,
        draft,
        arabic=True,
    )

    assert "حجزك اتأكد" in text
    assert "Full Legs Laser" in text
    assert "Prime Lase" in text
    assert "الاثنين 28 سبتمبر" in text
    assert "7 مساءً" in text


def test_booking_with_doctor_and_device_uses_backend_fact_values() -> None:
    contract = build_customer_response_contract(
        [
            _terminal(
                "booking_completed",
                facts={
                    "doctor_name": "د. مريم",
                    "device_name": "Candela Gentle",
                },
            )
        ]
    )
    text = resolve_terminal_composer_draft(
        contract,
        _draft("doctor_name", "device_name"),
        arabic=True,
    )

    assert "د. مريم" in text
    assert "Candela Gentle" in text


def test_package_backed_booking_acknowledges_package_without_pulse_claim() -> None:
    contract = build_customer_response_contract(
        [
            _terminal(
                "booking_completed",
                facts={
                    "service_name": "Laser",
                    "package_used": True,
                    "package_name": "6 Sessions",
                },
            )
        ]
    )
    text = deterministic_terminal_fallback(contract, arabic=True)

    assert "حجزك اتأكد" in text
    assert "الباكدج الحالية" in text
    assert "Pulse" not in text
    assert "دفع" not in text
    assert "خصم" not in text


def test_minimal_booking_still_has_successful_action_acknowledgment() -> None:
    contract = build_customer_response_contract(
        [_terminal("booking_completed")]
    )

    text = deterministic_terminal_fallback(contract, arabic=True)

    assert text == "تمام، حجزك اتأكد."


def test_booking_availability_evidence_never_becomes_availability_response() -> None:
    contract = build_customer_response_contract(
        [
            _terminal(
                "booking_completed",
                facts={
                    "availability": {
                        "service_name": "Laser",
                        "availability_windows": [
                            {
                                "doctor_name": "د. مريم",
                                "laser_device_name": "Prime Lase",
                                "start_local": "2026-09-28T19:00:00+03:00",
                            }
                        ],
                        "checked_dates": ["2026-09-28"],
                        "available_option_count": 1,
                    }
                },
            )
        ]
    )
    unit = contract.units[0]
    keys = {fact.key for fact in unit.facts}

    assert unit.response_goal == "booking_completed"
    assert "start_local" in keys
    assert "device_name" in keys
    assert "availability_windows" not in keys
    assert "checked_dates" not in keys
    assert "available_option_count" not in keys


@pytest.mark.parametrize(
    "facts",
    [
        {"date": {"mode": "exact", "start_date": "2026-10-03"}},
        {"time": {"mode": "exact", "start_time": "18:30"}},
        {
            "date": {"mode": "exact", "start_date": "2026-10-03"},
            "time": {"mode": "exact", "start_time": "18:30"},
            "service_name": "Hydrafacial",
        },
    ],
)
def test_reschedule_preserves_new_date_time_facts(
    facts: dict[str, object],
) -> None:
    contract = build_customer_response_contract(
        [_terminal("reschedule_completed", facts=facts)]
    )
    available = {fact.key for fact in contract.units[0].facts}
    selected = [
        key
        for key in ("service_name", "date", "time")
        if key in available
    ]
    text = resolve_terminal_composer_draft(
        contract,
        _draft(*selected),
        arabic=True,
    )

    assert "ميعادك اتغيّر" in text
    if "date" in available:
        assert "السبت 3 أكتوبر" in text
    if "time" in available:
        assert "6:30 مساءً" in text


def test_reschedule_supporting_availability_stays_reschedule_semantics() -> None:
    contract = build_customer_response_contract(
        [
            _terminal(
                "reschedule_completed",
                facts={
                    "service_name": "Laser",
                    "availability": {
                        "availability_windows": [
                            {
                                "start_local": "2026-10-03T18:30:00+03:00",
                                "doctor_name": "د. سارة",
                            }
                        ],
                        "available_option_count": 1,
                    },
                },
            )
        ]
    )
    text = deterministic_terminal_fallback(contract, arabic=True)

    assert "ميعادك اتغيّر" in text
    assert "متاح" not in text
    assert "اختار" not in text


def test_minimal_reschedule_still_acknowledges_completed_reschedule() -> None:
    contract = build_customer_response_contract(
        [_terminal("reschedule_completed")]
    )

    assert (
        deterministic_terminal_fallback(contract, arabic=True)
        == "تمام، ميعادك اتغيّر."
    )


def test_cancellation_identity_cannot_become_confirmation() -> None:
    cancellation = build_customer_response_contract(
        [_terminal("cancellation_completed")]
    )
    confirmation = build_customer_response_contract(
        [_terminal("appointment_confirmed")]
    )

    cancel_text = deterministic_terminal_fallback(
        cancellation,
        arabic=True,
    )
    confirm_text = deterministic_terminal_fallback(
        confirmation,
        arabic=True,
    )

    assert "اتلغى" in cancel_text
    assert "اتأكد" not in cancel_text
    assert "اتأكد" in confirm_text
    assert "اتلغى" not in confirm_text


def test_appointment_confirmation_is_not_rendered_as_new_booking() -> None:
    contract = build_customer_response_contract(
        [_terminal("appointment_confirmed")]
    )
    text = deterministic_terminal_fallback(contract, arabic=True)

    assert "ميعادك اتأكد" in text
    assert "حجزك" not in text


@pytest.mark.parametrize(
    "goal",
    ["package_purchased", "pulse_pack_purchased"],
)
def test_purchase_acknowledgment_never_implies_payment(
    goal: str,
) -> None:
    contract = build_customer_response_contract(
        [
            _terminal(
                goal,
                action_result={
                    "amount_paid_minor": 0,
                    "sale_price_minor": 250_000,
                    "currency": "EGP",
                },
            )
        ]
    )
    text = deterministic_terminal_fallback(contract, arabic=True)
    payload = contract.model_dump_json()

    assert "دفع" not in text
    assert "مدفوع" not in text
    assert "تسوية" not in text
    assert "خصم" not in text
    assert "amount_paid" not in payload
    assert "sale_price" not in payload


def test_pulse_purchase_is_not_rendered_as_session_package() -> None:
    contract = build_customer_response_contract(
        [
            _terminal(
                "pulse_pack_purchased",
                facts={
                    "pulse_count": 2000,
                    "device_name": "Candela Gentle",
                },
            )
        ]
    )
    text = deterministic_terminal_fallback(contract, arabic=True)

    assert "باقة الـPulses" in text
    assert "2000 Pulse" in text
    assert "Candela Gentle" in text
    assert "جلسة" not in text


def test_follow_up_aliases_due_at_to_safe_terminal_fact() -> None:
    contract = build_customer_response_contract(
        [
            _terminal(
                "follow_up_created",
                action_result={"due_at": "2026-10-05T11:30:00+03:00"},
            )
        ]
    )
    facts = {fact.key: fact for fact in contract.units[0].facts}

    assert facts["follow_up_at"].value == "2026-10-05T11:30:00+03:00"
    assert facts["follow_up_at"].requirement == "required"
    text = deterministic_terminal_fallback(contract, arabic=True)
    assert "المتابعة اتسجلت" in text
    assert "الاثنين 5 أكتوبر" in text
    assert "11:30 صباحًا" in text


@pytest.mark.parametrize(
    ("consent", "expected"),
    [
        (True, "تفعيل التواصل التسويقي"),
        (False, "إيقاف التواصل التسويقي"),
    ],
)
def test_marketing_update_only_states_backend_consent(
    consent: bool,
    expected: str,
) -> None:
    contract = build_customer_response_contract(
        [
            _terminal(
                "marketing_updated",
                action_result={"marketing_consent": consent},
            )
        ]
    )
    fact = _fact_map(contract.units[0])["marketing_consent"]
    text = deterministic_terminal_fallback(contract, arabic=True)

    assert fact.requirement == "required"
    assert "تفضيلات التواصل التسويقي اتحدثت" in text
    assert expected in text


def test_compound_terminal_units_keep_contract_order() -> None:
    contract = build_customer_response_contract(
        [
            _terminal(
                "package_purchased",
                facts={"package_name": "Gold"},
            ),
            _terminal(
                "booking_completed",
                facts={
                    "service_name": "Laser",
                    "start_local": "2026-10-06T17:00:00+03:00",
                },
            ),
        ]
    )

    text = deterministic_terminal_fallback(contract, arabic=True)

    assert text.index("الباكدج") < text.index("حجز")
    assert "Laser" in text
    assert "5 مساءً" in text


def test_compound_model_draft_cannot_reorder_units() -> None:
    contract = build_customer_response_contract(
        [
            _terminal("package_purchased"),
            _terminal("booking_completed"),
        ]
    )
    draft = TerminalComposerDraft(
        units=[
            TerminalComposerUnitDraft(
                unit_index=1,
                action_ref="unit_action",
                style="plain",
                fact_keys=[],
                transition="sentence",
            ),
            TerminalComposerUnitDraft(
                unit_index=0,
                action_ref="unit_action",
                style="plain",
                fact_keys=[],
                transition="and",
            ),
        ]
    )

    with pytest.raises(
        TerminalComposerValidationError,
        match="ordering",
    ):
        validate_terminal_composer_draft(contract, draft)


def test_cross_unit_fact_reference_fails_closed() -> None:
    contract = build_customer_response_contract(
        [
            _terminal("package_purchased"),
            _terminal(
                "booking_completed",
                facts={"start_local": "2026-10-06T17:00:00+03:00"},
            ),
        ]
    )
    draft = TerminalComposerDraft(
        units=[
            TerminalComposerUnitDraft(
                unit_index=0,
                action_ref="unit_action",
                style="plain",
                fact_keys=["start_local"],
                transition="sentence",
            ),
            TerminalComposerUnitDraft(
                unit_index=1,
                action_ref="unit_action",
                style="plain",
                fact_keys=[],
                transition="and",
            ),
        ]
    )

    with pytest.raises(
        TerminalComposerValidationError,
        match="unavailable fact",
    ):
        validate_terminal_composer_draft(contract, draft)


def test_unknown_fact_reference_fails_closed() -> None:
    contract = build_customer_response_contract(
        [_terminal("booking_completed")]
    )

    with pytest.raises(
        TerminalComposerValidationError,
        match="unavailable fact",
    ):
        validate_terminal_composer_draft(
            contract,
            _draft("invented_fact"),
        )


def test_required_fact_must_be_selected() -> None:
    contract = CustomerResponseContract(
        units=(
            CustomerResponseUnit(
                response_goal="booking_completed",
                status="completed",
                action_truth=ActionTruth(
                    action="booking",
                    succeeded=True,
                ),
                facts=(
                    ResponseFact(
                        key="service_name",
                        semantic_type="entity_name",
                        requirement="required",
                        value="Laser",
                    ),
                ),
            ),
        )
    )

    with pytest.raises(
        TerminalComposerValidationError,
        match="required",
    ):
        validate_terminal_composer_draft(
            contract,
            _draft(),
        )


def test_mismatched_action_truth_fails_structural_validation() -> None:
    contract = CustomerResponseContract(
        units=(
            CustomerResponseUnit(
                response_goal="booking_completed",
                status="completed",
                action_truth=ActionTruth(
                    action="cancel_appointment",
                    succeeded=True,
                ),
            ),
        )
    )

    with pytest.raises(
        TerminalComposerValidationError,
        match="action identity",
    ):
        validate_terminal_composer_draft(
            contract,
            _draft(),
        )


def test_action_ref_schema_cannot_name_another_action() -> None:
    with pytest.raises(ValidationError):
        TerminalComposerUnitDraft(
            unit_index=0,
            action_ref="cancel_appointment",
            style="plain",
            fact_keys=[],
            transition="sentence",
        )


def test_composer_input_contains_fact_keys_but_not_fact_values_or_ids() -> None:
    contract = build_customer_response_contract(
        [
            _terminal(
                "booking_completed",
                facts={
                    "service_name": "Secret Service Name",
                    "device_name": "Secret Device Name",
                    "appointment_id": "appointment-secret",
                    "service_id": "service-secret",
                    "selected_ref": "S1",
                },
            )
        ]
    )

    messages = _build_terminal_composer_messages(
        history=[HumanMessage(content="تم؟")],
        contract=contract,
    )
    payload = str(messages[-2].content)

    assert "service_name" in payload
    assert "device_name" in payload
    assert "Secret Service Name" not in payload
    assert "Secret Device Name" not in payload
    assert "appointment-secret" not in payload
    assert "service-secret" not in payload
    assert "S1" not in payload


def test_final_resolved_text_never_exposes_internal_ids_or_refs() -> None:
    outcome = _terminal(
        "booking_completed",
        facts={
            "service_name": "Laser",
            "appointment_id": "appointment-secret",
            "doctor_id": "doctor-secret",
            "patient_id": "patient-secret",
            "workspace_id": "workspace-secret",
            "transaction_id": "transaction-secret",
            "selected_ref": "S1",
        },
    )
    contract = build_customer_response_contract([outcome])
    text = deterministic_terminal_fallback(contract, arabic=True)

    assert "Laser" in text
    for secret in (
        "appointment-secret",
        "doctor-secret",
        "patient-secret",
        "workspace-secret",
        "transaction-secret",
        "S1",
    ):
        assert secret not in text
        assert secret not in contract.model_dump_json()


def test_provider_failure_uses_terminal_contract_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = build_customer_response_contract(
        [
            _terminal(
                "booking_completed",
                facts={"service_name": "Laser"},
            )
        ]
    )
    monkeypatch.setattr(
        terminal_composer,
        "build_realtime_composer_model",
        lambda: object(),
    )
    monkeypatch.setattr(
        terminal_composer,
        "invoke_with_model_chain",
        lambda **_kwargs: (_ for _ in ()).throw(
            LLMProviderError(
                "provider unavailable",
                status_code=503,
                retryable=True,
            )
        ),
    )

    text, source = compose_terminal_contract_reply(
        history=[HumanMessage(content="تم الحجز؟")],
        contract=contract,
    )

    assert source == "deterministic:terminal-contract-fallback"
    assert "حجزك اتأكد" in text
    assert "Laser" in text


def test_invalid_structured_draft_uses_terminal_contract_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = build_customer_response_contract(
        [_terminal("booking_completed")]
    )
    invalid = _draft("invented_fact")
    monkeypatch.setattr(
        terminal_composer,
        "build_realtime_composer_model",
        lambda: object(),
    )
    monkeypatch.setattr(
        terminal_composer,
        "invoke_with_model_chain",
        lambda **_kwargs: SimpleNamespace(
            value=invalid,
            model_name="test-model",
        ),
    )

    text, source = compose_terminal_contract_reply(
        history=[HumanMessage(content="تم؟")],
        contract=contract,
    )

    assert source == "deterministic:terminal-contract-fallback"
    assert "حجزك اتأكد" in text


def test_valid_composer_source_is_observable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = build_customer_response_contract(
        [
            _terminal(
                "booking_completed",
                facts={"service_name": "Laser"},
            )
        ]
    )
    monkeypatch.setattr(
        terminal_composer,
        "build_realtime_composer_model",
        lambda: object(),
    )
    monkeypatch.setattr(
        terminal_composer,
        "model_label",
        lambda name: str(name),
    )
    monkeypatch.setattr(
        terminal_composer,
        "invoke_with_model_chain",
        lambda **_kwargs: SimpleNamespace(
            value=_draft("service_name"),
            model_name="test-model",
        ),
    )

    text, source = compose_terminal_contract_reply(
        history=[HumanMessage(content="تم؟")],
        contract=contract,
    )

    assert source == "contract-composer:test-model"
    assert "Laser" in text


def test_pure_terminal_responder_routes_to_contract_composer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    def fake_terminal(**kwargs):
        seen.update(kwargs)
        return "terminal reply", "contract-composer:test-model"

    monkeypatch.setattr(
        responder,
        "compose_terminal_contract_reply",
        fake_terminal,
    )
    monkeypatch.setattr(
        responder,
        "build_realtime_composer_model",
        lambda: (_ for _ in ()).throw(
            AssertionError("legacy responder should not run")
        ),
    )

    text, source = compose_v2_customer_reply(
        clinic_name="Linka Clinic",
        timezone_name="Africa/Cairo",
        local_now=NOW,
        history=[HumanMessage(content="تم الحجز؟")],
        outcomes=[
            _terminal(
                "booking_completed",
                facts={
                    "availability": {
                        "availability_windows": [
                            {
                                "start_local": "2026-09-28T19:00:00+03:00",
                            }
                        ]
                    }
                },
            )
        ],
    )

    assert text == "terminal reply"
    assert source == "contract-composer:test-model"
    contract = seen["contract"]
    assert isinstance(contract, CustomerResponseContract)
    assert contract.units[0].response_goal == "booking_completed"
    assert "availability_windows" not in {
        fact.key for fact in contract.units[0].facts
    }



def test_mixed_terminal_and_nonterminal_preserves_terminal_truth_without_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        responder,
        "build_realtime_composer_model",
        lambda: (_ for _ in ()).throw(
            AssertionError("mixed typed terminal response must not invoke the generic model")
        ),
    )

    text, source = compose_v2_customer_reply(
        clinic_name="Linka Clinic",
        timezone_name="Africa/Cairo",
        local_now=NOW,
        history=[HumanMessage(content="تم الحجز ومعلومة العيادة؟")],
        outcomes=[
            _terminal("booking_completed"),
            TurnOutcome(
                status="answered",
                response_goal="answer_clinic_info",
                facts={"clinic_info": {"phone": "123"}},
            ),
        ],
    )

    assert source == "deterministic:mixed-typed-contract"
    assert "حجزك اتأكد" in text
    assert "123" not in text

def test_active_task_cancelled_stays_on_legacy_responder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        responder,
        "compose_terminal_contract_reply",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("active task cancellation is not Phase 2")
        ),
    )
    monkeypatch.setattr(
        responder,
        "build_realtime_composer_model",
        lambda: object(),
    )
    monkeypatch.setattr(
        responder,
        "model_label",
        lambda name: str(name),
    )
    monkeypatch.setattr(
        responder,
        "invoke_with_model_chain",
        lambda **_kwargs: SimpleNamespace(
            value=responder.ResponderDraft(
                reply="تمام، وقفت الخطوة اللي كنا مكملين فيها.",
                availability_claim="not_applicable",
            ),
            model_name="legacy-model",
        ),
    )

    text, source = compose_v2_customer_reply(
        clinic_name="Linka Clinic",
        timezone_name="Africa/Cairo",
        local_now=NOW,
        history=[HumanMessage(content="خلاص بلاش نكمل")],
        outcomes=[
            TurnOutcome(
                status="answered",
                response_goal="active_task_cancelled",
                facts={"active_task_cancelled": True},
            )
        ],
    )

    assert "وقفت" in text
    assert source == "legacy-model"
