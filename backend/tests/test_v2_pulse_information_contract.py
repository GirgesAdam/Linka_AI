from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from langchain_core.messages import HumanMessage

from app.agents.v2 import responder
from app.agents.v2.pulse_composer import deterministic_pulse_contract_reply
from app.agents.v2.responder import compose_v2_customer_reply
from app.agents.v2.turn_contract import TiaTurnUnderstanding, TurnEntities, TurnOperation
from app.agents.v2.turn_interpreter import _interpreter_system_prompt
from app.services.agent_v2.outcome import TurnOutcome
from app.services.agent_v2.outcome_builder import (
    _pulse_information_response_facts,
    build_step_outcome,
)
from app.services.agent_v2.planner import PlanStep, ReadRequest
from app.services.agent_v2.read_executor import (
    ReadExecutionBundle,
    ReadExecutionContext,
    ReadResult,
    execute_step_reads,
)
from app.services.agent_v2.response_contract import (
    build_customer_response_contract,
    is_pure_supported_pulse_contract,
)

NOW = datetime(2026, 9, 30, 0, 0, tzinfo=UTC)


def _pulse_outcome(
    *,
    requested: list[str],
    balances: list[dict[str, object]] | None = None,
    packs: list[dict[str, object]] | None = None,
    offers: list[dict[str, object]] | None = None,
    overage: dict[str, object] | None = None,
) -> TurnOutcome:
    facts: dict[str, object] = {"pulse_requested_details": requested}
    if balances is not None:
        facts["pulse_balance"] = {"balances": balances}
    if packs is not None:
        facts["pulse_packs"] = {"packs": packs}
    if offers is not None:
        facts["pulse_pack_offers"] = {"offers": offers}
    if overage is not None:
        facts["pulse_billing_settings"] = overage
    return TurnOutcome(
        status="answered",
        response_goal="pulse_information",
        facts=facts,
    )


def _balance(device: str = "Candela Gentle", remaining: int = 1250) -> dict[str, object]:
    return {
        "device_name": device,
        "pulses_remaining": remaining,
        "active_pack_count": 2,
    }


def _owned(
    *,
    device: str = "Candela Gentle",
    purchased: int = 2000,
    consumed: int = 750,
    remaining: int = 1250,
) -> dict[str, object]:
    return {
        "device_name": device,
        "pulses_purchased": purchased,
        "pulses_consumed": consumed,
        "pulses_remaining": remaining,
        "purchased_at": "2026-09-01T10:00:00+00:00",
        "expires_at": "2027-09-01",
        "status": "active",
        "effective_status": "active",
    }


def _offer(
    *,
    device: str = "Candela Gentle",
    count: int = 1000,
    price: str = "1500.00 EGP",
) -> dict[str, object]:
    return {
        "device_name": device,
        "pulses_count": count,
        "price": price,
        "currency": "EGP",
        "is_active": True,
    }


def test_pulse_truth_preserves_exact_balance_as_backend_integer() -> None:
    contract = build_customer_response_contract(
        [_pulse_outcome(requested=["balance"], balances=[_balance(remaining=1250)])]
    )
    truth = contract.units[0].pulse_truth

    assert truth is not None
    assert truth.requested_details == ("balance",)
    assert truth.balance_complete_set is True
    assert truth.balances[0].device_name == "Candela Gentle"
    assert truth.balances[0].pulses_remaining == 1250
    assert is_pure_supported_pulse_contract(contract) is True


def test_stale_conversation_cannot_override_canonical_balance() -> None:
    outcome = _pulse_outcome(requested=["balance"], balances=[_balance(remaining=1250)])

    text, source = compose_v2_customer_reply(
        clinic_name="Linka Clinic",
        timezone_name="Africa/Cairo",
        local_now=NOW,
        history=[
            HumanMessage(content="آخر مرة قلتلي رصيدي 900 Pulse، دلوقتي كام؟"),
        ],
        outcomes=[outcome],
    )

    assert source == "deterministic:pulse-contract"
    assert "1250" in text
    assert "900" not in text


def test_owned_packs_and_available_offers_are_structurally_distinct() -> None:
    contract = build_customer_response_contract(
        [
            _pulse_outcome(
                requested=["owned_packs", "offers"],
                packs=[_owned(remaining=1250)],
                offers=[_offer(count=1000)],
            )
        ]
    )
    truth = contract.units[0].pulse_truth

    assert truth is not None
    assert truth.owned_complete_set is True
    assert truth.offers_complete_set is True
    assert truth.owned_packs[0].pulses_remaining == 1250
    assert truth.available_offers[0].pulses_count == 1000
    assert truth.available_offers[0].price == "1500.00 EGP"


def test_deterministic_renderer_keeps_owned_and_offer_language_separate() -> None:
    contract = build_customer_response_contract(
        [
            _pulse_outcome(
                requested=["owned_packs", "offers"],
                packs=[_owned(remaining=1250)],
                offers=[_offer(count=1000)],
            )
        ]
    )

    text = deterministic_pulse_contract_reply(contract, arabic=True)

    assert "باقات الـPulses اللي عندك" in text
    assert "المتبقي 1250" in text
    assert "عروض باقات الـPulses المتاحة للشراء" in text
    assert "1000 Pulse" in text
    assert "1500.00 EGP" in text


def test_offer_device_price_pairs_cannot_be_swapped() -> None:
    contract = build_customer_response_contract(
        [
            _pulse_outcome(
                requested=["offers"],
                offers=[
                    _offer(device="Candela Gentle", count=1000, price="1500.00 EGP"),
                    _offer(device="Prime Lase", count=2000, price="2600.00 EGP"),
                ],
            )
        ]
    )

    text = deterministic_pulse_contract_reply(contract, arabic=False)

    assert "1000 Pulses for Candela Gentle for 1500.00 EGP" in text
    assert "2000 Pulses for Prime Lase for 2600.00 EGP" in text
    assert "1000 Pulses for Prime Lase" not in text
    assert "2000 Pulses for Candela Gentle" not in text


def test_overage_device_unit_price_and_total_stay_bound() -> None:
    contract = build_customer_response_contract(
        [
            _pulse_outcome(
                requested=["overage_price"],
                overage={
                    "devices": [
                        {
                            "device_name": "Candela Gentle",
                            "overage_price": "1.50 EGP",
                            "currency": "EGP",
                        }
                    ],
                    "requested_pulse_count": 500,
                    "overage_total": "750.00 EGP",
                    "currency": "EGP",
                },
            )
        ]
    )
    truth = contract.units[0].pulse_truth

    assert truth is not None
    option = truth.overage_options[0]
    assert option.device_name == "Candela Gentle"
    assert option.unit_price == "1.50 EGP"
    assert option.requested_pulse_count == 500
    assert option.total_price == "750.00 EGP"

    text = deterministic_pulse_contract_reply(contract, arabic=True)
    assert "Candela Gentle" in text
    assert "1.50 EGP" in text
    assert "500 Pulse = 750.00 EGP" in text


def test_pure_pulse_information_bypasses_generic_responder_and_additional_llm_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        responder,
        "build_realtime_composer_model",
        lambda: (_ for _ in ()).throw(AssertionError("LLM must be unreachable")),
    )
    monkeypatch.setattr(
        responder,
        "_build_responder_messages",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("generic responder must be unreachable")
        ),
    )

    text, source = compose_v2_customer_reply(
        clinic_name="Linka Clinic",
        timezone_name="Africa/Cairo",
        local_now=NOW,
        history=[HumanMessage(content="رصيدي كام؟")],
        outcomes=[_pulse_outcome(requested=["balance"], balances=[_balance()])],
    )

    assert source == "deterministic:pulse-contract"
    assert "1250" in text


def test_read_only_pulse_reply_never_claims_payment_settlement_or_automatic_billing() -> None:
    contract = build_customer_response_contract(
        [
            _pulse_outcome(
                requested=["balance", "offers"],
                balances=[_balance()],
                offers=[_offer()],
            )
        ]
    )

    text = deterministic_pulse_contract_reply(contract, arabic=True)

    for forbidden in (
        "خصمت",
        "هخصم",
        "دفعت",
        "تم الدفع",
        "سجلت الدفع",
        "تمت التسوية",
        "اتحاسبت",
        "هنستخدم الباقة تلقائيًا",
    ):
        assert forbidden not in text


def test_pulse_purchase_terminal_is_not_owned_by_pulse_information_contract() -> None:
    outcome = TurnOutcome(
        status="completed",
        response_goal="pulse_pack_purchased",
        facts={"pulse_count": 1000, "device_name": "Candela Gentle"},
        action_result={
            "ok": True,
            "action": "buy_pulse_pack",
            "amount_paid_minor": 0,
        },
    )
    contract = build_customer_response_contract([outcome])

    assert contract.units[0].pulse_truth is None
    assert is_pure_supported_pulse_contract(contract) is False

    text, source = compose_v2_customer_reply(
        clinic_name="Linka Clinic",
        timezone_name="Africa/Cairo",
        local_now=NOW,
        history=[HumanMessage(content="ضيفلي باقة 1000 Pulse")],
        outcomes=[outcome],
    )
    assert source.startswith("deterministic:terminal-contract")
    assert "اتضافت" in text or "إضافة" in text
    assert "دفع" not in text
    assert "خصم" not in text
    assert "تسوية" not in text


def test_pulse_outcome_shaping_filters_internal_and_financial_metadata() -> None:
    shaped = _pulse_information_response_facts(
        {
            "pulse_packs": {
                "packs": [
                    {
                        **_owned(),
                        "patient_pulse_pack_id": str(uuid4()),
                        "purchase_transaction_id": str(uuid4()),
                        "sale_price": "2500.00 EGP",
                        "amount_paid": "1000.00 EGP",
                        "balance_due": "1500.00 EGP",
                        "ledger_id": str(uuid4()),
                    }
                ]
            },
            "pulse_pack_offers": {
                "offers": [
                    {
                        **_offer(),
                        "id": str(uuid4()),
                        "workspace_id": str(uuid4()),
                        "created_at": NOW.isoformat(),
                        "updated_at": NOW.isoformat(),
                    }
                ]
            },
        },
        requested_details=("owned_packs", "offers"),
    )

    dumped = str(shaped)
    assert "patient_pulse_pack_id" not in dumped
    assert "purchase_transaction_id" not in dumped
    assert "sale_price" not in dumped
    assert "amount_paid" not in dumped
    assert "balance_due" not in dumped
    assert "ledger_id" not in dumped
    assert "workspace_id" not in dumped
    assert "created_at" not in dumped
    assert "updated_at" not in dumped


def test_patient_scoped_balance_read_uses_current_workspace_and_patient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[tuple[object, object]] = []

    def fake_balances(_db, *, workspace_id, patient_id):
        captured.append((workspace_id, patient_id))
        return []

    monkeypatch.setattr(
        "app.services.agent_v2.read_executor.list_patient_pulse_balances",
        fake_balances,
    )
    workspace_a = uuid4()
    patient_a = uuid4()
    workspace_b = uuid4()
    patient_b = uuid4()
    step = PlanStep(
        operation_index=0,
        operation_type="pulse_info",
        disposition="read",
        reads=[ReadRequest(kind="pulse_balance")],
        response_goal="pulse_information",
    )

    for workspace_id, patient_id in (
        (workspace_a, patient_a),
        (workspace_b, patient_b),
    ):
        execute_step_reads(
            step,
            ReadExecutionContext(
                db=object(),
                workspace=SimpleNamespace(id=workspace_id),
                patient=SimpleNamespace(id=patient_id),
                now=NOW,
            ),
        )

    assert captured == [(workspace_a, patient_a), (workspace_b, patient_b)]


def test_workspace_scoped_offer_read_never_uses_patient_or_other_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[object] = []

    def fake_offers(_db, *, workspace_id, active_only):
        captured.append(workspace_id)
        assert active_only is True
        return []

    monkeypatch.setattr(
        "app.services.agent_v2.read_executor.list_pulse_pack_offers",
        fake_offers,
    )
    workspace_a = uuid4()
    workspace_b = uuid4()
    step = PlanStep(
        operation_index=0,
        operation_type="pulse_info",
        disposition="read",
        reads=[ReadRequest(kind="pulse_pack_offers")],
        response_goal="pulse_information",
    )

    for workspace_id in (workspace_a, workspace_b):
        execute_step_reads(
            step,
            ReadExecutionContext(
                db=object(),
                workspace=SimpleNamespace(id=workspace_id),
                patient=SimpleNamespace(id=uuid4()),
                now=NOW,
            ),
        )

    assert captured == [workspace_a, workspace_b]


def test_build_step_outcome_uses_requested_detail_whitelist() -> None:
    turn = TiaTurnUnderstanding(
        operations=[
            TurnOperation(
                type="pulse_info",
                entities=TurnEntities(),
                requested_pulse_details=["balance"],
                execution_intent="informational",
            )
        ],
        safety_signals=[],
    )
    step = PlanStep(
        operation_index=0,
        operation_type="pulse_info",
        disposition="read",
        reads=[ReadRequest(kind="pulse_balance")],
        response_goal="pulse_information",
    )
    reads = ReadExecutionBundle(
        results=[
            ReadResult(
                kind="pulse_balance",
                ok=True,
                payload={
                    "balances": [
                        {
                            "device_key": "candela_gentle",
                            "device_name": "Candela Gentle",
                            "pulses_purchased": 2000,
                            "pulses_consumed": 750,
                            "pulses_remaining": 1250,
                            "active_pack_count": 2,
                            "unexpected_internal": "do-not-leak",
                        }
                    ]
                },
            )
        ]
    )

    outcome = build_step_outcome(
        step,
        turn=turn,
        semantic_context=SimpleNamespace(),
        reads=reads,
    )

    assert outcome.facts == {
        "pulse_requested_details": ["balance"],
        "pulse_balance": {
            "balances": [
                {
                    "device_name": "Candela Gentle",
                    "pulses_remaining": 1250,
                    "active_pack_count": 2,
                }
            ]
        },
    }



def test_mixed_pulse_and_other_family_preserves_verified_balance_without_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcomes = [
        _pulse_outcome(requested=["balance"], balances=[_balance()]),
        TurnOutcome(
            status="answered",
            response_goal="answer_service",
            facts={"service_catalog": {"service": {"name": "Hydrafacial", "description": "Verified"}}},
        ),
    ]
    monkeypatch.setattr(
        responder,
        "build_realtime_composer_model",
        lambda: (_ for _ in ()).throw(
            AssertionError("mixed typed Pulse response must not invoke the generic model")
        ),
    )

    text, source = compose_v2_customer_reply(
        clinic_name="Linka Clinic",
        timezone_name="Africa/Cairo",
        local_now=NOW,
        history=[HumanMessage(content="قولّي رصيدي والخدمة")],
        outcomes=outcomes,
    )

    assert source == "deterministic:mixed-typed-contract"
    assert "Candela Gentle" in text
    assert "1250" in text

def test_purchase_fallback_pulse_information_is_not_claimed_by_read_only_shaper() -> None:
    turn = TiaTurnUnderstanding(
        operations=[
            TurnOperation(
                type="buy_pulse_pack",
                entities=TurnEntities(pulse_count=1000),
                execution_intent="execute",
            )
        ],
        safety_signals=[],
    )
    step = PlanStep(
        operation_index=0,
        operation_type="buy_pulse_pack",
        disposition="blocked",
        reads=[ReadRequest(kind="pulse_pack_offers")],
        response_goal="pulse_information",
    )
    reads = ReadExecutionBundle(
        results=[
            ReadResult(
                kind="pulse_pack_offers",
                ok=True,
                payload={
                    "offers": [
                        {
                            "device_name": "Candela Gentle",
                            "pulses_count": 1000,
                            "price_minor": 150_000,
                            "currency": "EGP",
                            "is_active": True,
                        }
                    ]
                },
            )
        ]
    )

    outcome = build_step_outcome(
        step,
        turn=turn,
        semantic_context=SimpleNamespace(),
        reads=reads,
    )

    assert outcome.status == "blocked"
    assert "pulse_pack_offers" in outcome.facts
    assert "pulse_requested_details" not in outcome.facts
    assert build_customer_response_contract([outcome]).units[0].pulse_truth is None


def test_interpreter_contract_keeps_owned_pack_remaining_distinct_from_aggregate_balance() -> None:
    prompt = _interpreter_system_prompt(
        timezone_name="Africa/Cairo",
        local_now=NOW,
    )
    schema = TiaTurnUnderstanding.model_json_schema()
    description = schema["$defs"]["TurnOperation"]["properties"]["requested_pulse_details"][
        "description"
    ]

    normalized_prompt = " ".join(prompt.split())

    assert "particular pack they own" in normalized_prompt
    assert "الباقة اللي عندي على Candela فاضل فيها كام Pulse؟" in normalized_prompt
    assert '"what is my Candela Pulse balance?" are balance' in normalized_prompt
    assert "particular owned-pack remaining question is owned_packs, not balance" in description
