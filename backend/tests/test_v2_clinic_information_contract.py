from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from app.agents.v2 import responder
from app.agents.v2.clinic_info_composer import deterministic_clinic_contract_reply
from app.agents.v2.responder import _build_responder_messages, compose_v2_customer_reply
from app.agents.v2.semantic_context import build_semantic_context
from app.agents.v2.turn_contract import TiaTurnUnderstanding, TurnEntities, TurnOperation
from app.services.agent_v2 import read_executor
from app.services.agent_v2.outcome import TurnOutcome
from app.services.agent_v2.outcome_builder import build_step_outcome
from app.services.agent_v2.planner import PlanStep, ReadRequest
from app.services.agent_v2.read_executor import (
    ReadExecutionBundle,
    ReadExecutionContext,
    ReadResult,
)
from app.services.agent_v2.response_contract import (
    build_customer_response_contract,
    is_pure_supported_clinic_contract,
)

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)


def _operation(*details: str) -> TurnOperation:
    return TurnOperation(
        type="clinic_info",
        entities=TurnEntities(),
        requested_clinic_details=list(details),
        execution_intent="informational",
    )


def _step() -> PlanStep:
    return PlanStep(
        operation_index=0,
        operation_type="clinic_info",
        disposition="read",
        reads=[ReadRequest(kind="clinic_info")],
        response_goal="answer_clinic_info",
    )


def _reads() -> ReadExecutionBundle:
    return ReadExecutionBundle(
        results=[
            ReadResult(
                kind="clinic_info",
                ok=True,
                payload={
                    "clinic_name": "Tia Clinic",
                    "timezone": "Africa/Cairo",
                    "locations": [
                        {
                            "id": "branch-internal",
                            "workspace_id": "workspace-internal",
                            "name": "Main Branch",
                            "phone": "01012345678",
                            "email": "hello@example.com",
                            "address_line1": "10 Nile St",
                            "city": "Cairo",
                            "country_code": "EG",
                            "timezone": "Africa/Cairo",
                            "provider_account_id": "provider-secret-id",
                            "access_token": "secret-token",
                        }
                    ],
                    "knowledge": "Clinic-authored visitor policy.",
                    "integration_config": {"api_key": "secret-key"},
                },
            )
        ]
    )


def _outcome(*details: str) -> TurnOutcome:
    turn = TiaTurnUnderstanding(operations=[_operation(*details)], safety_signals=[])
    return build_step_outcome(
        _step(),
        turn=turn,
        semantic_context=build_semantic_context({"services": [], "doctors": []}),
        reads=_reads(),
    )


def test_exact_clinic_name_is_backend_owned() -> None:
    contract = build_customer_response_contract([_outcome("name")])
    truth = contract.units[0].clinic_truth

    assert truth is not None
    assert truth.clinic_name == "Tia Clinic"
    assert truth.requested_details == ("name",)
    text = deterministic_clinic_contract_reply(contract, arabic=False)
    assert text == "Clinic: Tia Clinic."


def test_address_and_contact_remain_bound_to_same_primary_location() -> None:
    contract = build_customer_response_contract([_outcome("location", "contact")])
    truth = contract.units[0].clinic_truth

    assert truth is not None
    assert len(truth.locations) == 1
    location = truth.locations[0]
    assert location.name == "Main Branch"
    assert location.phone == "01012345678"
    assert location.email == "hello@example.com"
    assert location.address_line1 == "10 Nile St"
    assert location.city == "Cairo"

    text = deterministic_clinic_contract_reply(contract, arabic=False)
    for expected in (
        "Main Branch",
        "10 Nile St",
        "Cairo",
        "01012345678",
        "hello@example.com",
    ):
        assert expected in text


def test_clinic_response_shaping_removes_internal_config_and_unrequested_facts() -> None:
    outcome = _outcome("contact")
    dumped = str(outcome.model_dump(mode="json"))

    assert outcome.facts["clinic_requested_details"] == ["contact"]
    assert outcome.facts["clinic_info"] == {
        "locations": [
            {
                "name": "Main Branch",
                "phone": "01012345678",
                "email": "hello@example.com",
            }
        ]
    }
    for forbidden in (
        "branch-internal",
        "workspace-internal",
        "provider-secret-id",
        "secret-token",
        "secret-key",
        "api_key",
        "integration_config",
        "timezone",
        "address_line1",
        "knowledge",
    ):
        assert forbidden not in dumped


def test_missing_saved_knowledge_does_not_invite_policy_invention() -> None:
    outcome = TurnOutcome(
        status="answered",
        response_goal="answer_clinic_info",
        facts={
            "clinic_info": {},
            "clinic_requested_details": ["knowledge"],
        },
    )
    contract = build_customer_response_contract([outcome])
    text = deterministic_clinic_contract_reply(contract, arabic=False)

    assert text == "No additional clinic information is currently stored."
    for invented in ("cancel", "refund", "deposit", "24 hours", "fee"):
        assert invented not in text.casefold()


def test_broad_clinic_request_defaults_to_all_customer_safe_categories() -> None:
    outcome = _outcome()
    assert outcome.facts["clinic_requested_details"] == [
        "contact",
        "knowledge",
        "location",
        "name",
    ]
    contract = build_customer_response_contract([outcome])
    assert is_pure_supported_clinic_contract(contract) is True


def test_pure_clinic_path_bypasses_generic_responder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        responder,
        "_build_responder_messages",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("generic responder must not run")
        ),
    )
    text, source = compose_v2_customer_reply(
        clinic_name="Tia Clinic",
        timezone_name="Africa/Cairo",
        local_now=NOW,
        history=[HumanMessage(content="رقم العيادة إيه؟")],
        outcomes=[_outcome("contact")],
    )

    assert source == "deterministic:clinic-information-contract"
    assert "01012345678" in text


def test_stale_assistant_clinic_fact_cannot_override_verified_truth() -> None:
    text, source = compose_v2_customer_reply(
        clinic_name="Tia Clinic",
        timezone_name="Africa/Cairo",
        local_now=NOW,
        history=[
            AIMessage(content="رقم العيادة 99999999 والعنوان Old Address."),
            HumanMessage(content="طيب الرقم والعنوان الصح إيه؟"),
        ],
        outcomes=[_outcome("contact", "location")],
    )

    assert source == "deterministic:clinic-information-contract"
    assert "01012345678" in text
    assert "10 Nile St" in text
    assert "99999999" not in text
    assert "Old Address" not in text


def test_mixed_legacy_path_receives_only_safe_scoped_clinic_facts() -> None:
    clinic = _outcome("contact")
    other = TurnOutcome(
        status="answered",
        response_goal="answer_service",
        facts={"service_catalog": {"services": [{"name": "Hydrafacial"}]}},
    )

    messages = _build_responder_messages(
        clinic_name="Tia Clinic",
        timezone_name="Africa/Cairo",
        local_now=NOW,
        history=[HumanMessage(content="رقمكم والخدمات عندكم إيه؟")],
        outcomes=[clinic, other],
    )
    outcome_message = next(
        message
        for message in messages
        if isinstance(message.content, str)
        and message.content.startswith("TURN_OUTCOMES")
    )
    content = str(outcome_message.content)

    assert "01012345678" in content
    for forbidden in (
        "secret-token",
        "secret-key",
        "branch-internal",
        "workspace-internal",
        "10 Nile St",
        "Clinic-authored visitor policy.",
    ):
        assert forbidden not in content


def test_payment_info_answer_clinic_info_is_not_claimed_by_clinic_contract() -> None:
    outcome = TurnOutcome(
        status="answered",
        response_goal="answer_clinic_info",
        facts={
            "booking_requires_payment": False,
            "payment_execution_owner": "reception",
            "clinic_info": {"clinic_name": "Tia Clinic"},
        },
    )
    contract = build_customer_response_contract([outcome])

    assert contract.units[0].clinic_truth is None
    assert is_pure_supported_clinic_contract(contract) is False


def test_clinic_truth_never_owns_availability_prices_or_open_closed_state() -> None:
    contract = build_customer_response_contract([_outcome("name", "location", "contact")])
    dumped = str(contract.units[0].clinic_truth.model_dump(mode="json")).casefold()

    for forbidden in (
        "availability",
        "slot",
        "price",
        "currency",
        "doctor",
        "open_now",
        "closed_now",
        "working_hours",
    ):
        assert forbidden not in dumped


def test_read_clinic_info_is_single_location_and_secret_free(monkeypatch) -> None:
    monkeypatch.setattr(
        read_executor,
        "relevant_knowledge_context",
        lambda *_args, **_kwargs: {"entries": []},
    )
    workspace = SimpleNamespace(
        id="workspace-1",
        name="Clinic X",
        timezone="Africa/Cairo",
        primary_branch_id="branch-2",
    )
    context = ReadExecutionContext(
        db=object(),
        workspace=workspace,
        patient=SimpleNamespace(id="patient-1"),
        now=NOW,
        catalog={
            "services": [],
            "doctors": [],
            "branches": [
                {
                    "id": "branch-1",
                    "name": "A",
                    "phone": "111",
                    "secret": "do-not-expose",
                },
                {
                    "id": "branch-2",
                    "name": "B",
                    "phone": "222",
                    "address_line1": "B address",
                    "webhook_secret": "do-not-expose-2",
                },
            ],
        },
    )

    result = read_executor._read_clinic_info(ReadRequest(kind="clinic_info"), context)

    assert result.payload["clinic_name"] == "Clinic X"
    assert result.payload["locations"] == [
        {"name": "B", "phone": "222", "address_line1": "B address"}
    ]
    serialized = str(result.payload)
    assert "branch-1" not in serialized
    assert "branch-2" not in serialized
    assert "do-not-expose" not in serialized
