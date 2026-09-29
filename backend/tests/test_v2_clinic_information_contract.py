from __future__ import annotations

from datetime import UTC, datetime
from inspect import getsource
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from app.agents.v2 import responder
from app.agents.v2.clinic_info_composer import deterministic_clinic_contract_reply
from app.agents.v2.responder import _build_responder_messages, compose_v2_customer_reply
from app.agents.v2.semantic_context import build_semantic_context
from app.agents.v2.turn_contract import TiaTurnUnderstanding, TurnEntities, TurnOperation
from app.integrations.clinic.tia_database import TiaDatabaseClinicAdapter
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


def _raw_clinic_payload() -> dict[str, object]:
    return {
        "clinic_name": "Tia Verified Clinic",
        "timezone": "Africa/Cairo",
        "locations": [
            {
                "name": "Main",
                "phone": "01012345678",
                "email": "hello@example.test",
                "address": "10 Verified Street, Cairo",
                "address_line1": "10 Verified Street",
                "city": "Cairo",
                "country_code": "EG",
                "timezone": "Africa/Cairo",
                "working_hours": [
                    {"weekday": 0, "start": "10:00", "end": "20:00"},
                    {"weekday": 1, "start": "11:00", "end": "19:00"},
                ],
                "branch_id": "internal-branch",
                "provider_token": "secret-provider-token",
            }
        ],
        "knowledge": "Clinic-authored policy text.",
        "workspace_id": "internal-workspace",
        "api_key": "secret-api-key",
    }


def _operation(*details: str) -> TurnOperation:
    return TurnOperation(
        type="clinic_info",
        entities=TurnEntities(),
        requested_clinic_details=list(details),
        execution_intent="informational",
    )


def _build_outcome(*details: str) -> TurnOutcome:
    operation = _operation(*details)
    step = PlanStep(
        operation_index=0,
        operation_type="clinic_info",
        disposition="read",
        reads=[ReadRequest(kind="clinic_info")],
        response_goal="answer_clinic_info",
    )
    return build_step_outcome(
        step,
        turn=TiaTurnUnderstanding(operations=[operation], safety_signals=[]),
        semantic_context=build_semantic_context(
            {"services": [], "doctors": [], "branches": []}
        ),
        reads=ReadExecutionBundle(
            results=[
                ReadResult(
                    kind="clinic_info",
                    ok=True,
                    payload=_raw_clinic_payload(),
                )
            ]
        ),
    )


def test_exact_clinic_address_and_contact_are_backend_owned() -> None:
    outcome = _build_outcome("name", "address", "contact")
    contract = build_customer_response_contract([outcome])
    truth = contract.units[0].clinic_truth

    assert truth is not None
    assert truth.clinic_name == "Tia Verified Clinic"
    assert truth.location is not None
    assert truth.location.name == "Main"
    assert truth.location.address == "10 Verified Street, Cairo"
    assert truth.location.phone == "01012345678"
    assert "email" not in truth.location.model_dump()

    text = deterministic_clinic_contract_reply(contract, arabic=False)
    assert "Tia Verified Clinic" in text
    assert "10 Verified Street, Cairo" in text
    assert "01012345678" in text
    assert "hello@example.test" not in text


def test_working_hours_are_exact_and_do_not_create_availability_claim() -> None:
    outcome = _build_outcome("working_hours")
    contract = build_customer_response_contract([outcome])
    truth = contract.units[0].clinic_truth

    assert truth is not None
    assert truth.location is not None
    assert [(row.weekday, row.start, row.end) for row in truth.location.working_hours] == [
        (0, "10:00", "20:00"),
        (1, "11:00", "19:00"),
    ]

    text = deterministic_clinic_contract_reply(contract, arabic=False)
    assert "Monday: 10:00–20:00" in text
    assert "Tuesday: 11:00–19:00" in text
    assert "appointment available" not in text.casefold()
    assert "bookable" not in text.casefold()


def test_open_now_is_scoped_and_never_invents_live_open_state() -> None:
    outcome = _build_outcome("open_now")
    contract = build_customer_response_contract([outcome])

    text = deterministic_clinic_contract_reply(contract, arabic=False)

    assert "Registered weekly working hours" in text
    assert "cannot confirm that the clinic is open right now" in text
    assert "appointment" not in text.casefold()
    assert "bookable" not in text.casefold()


def test_general_information_uses_only_saved_clinic_knowledge() -> None:
    outcome = _build_outcome("general_info")
    contract = build_customer_response_contract([outcome])

    text = deterministic_clinic_contract_reply(contract, arabic=False)

    assert text == "Clinic-authored policy text."
    assert "cancellation" not in text.casefold()
    assert "refund" not in text.casefold()


def test_broad_clinic_request_defaults_to_customer_safe_information() -> None:
    outcome = _build_outcome()
    assert outcome.facts["clinic_requested_details"] == [
        "name",
        "address",
        "contact",
        "working_hours",
        "general_info",
    ]

    contract = build_customer_response_contract([outcome])
    truth = contract.units[0].clinic_truth
    assert truth is not None
    assert truth.requested_details == (
        "name",
        "address",
        "contact",
        "working_hours",
        "general_info",
    )


def test_clinic_outcome_shaping_removes_internal_config_and_unrequested_fields() -> None:
    outcome = _build_outcome("contact")
    dumped = str(outcome.model_dump(mode="json"))

    assert outcome.facts["clinic_info"] == {
        "clinic_name": "Tia Verified Clinic",
        "locations": [
            {
                "name": "Main",
                "phone": "01012345678",
            }
        ],
    }
    for forbidden in (
        "workspace_id",
        "branch_id",
        "provider_token",
        "api_key",
        "working_hours",
        "address_line1",
        "knowledge",
        "hello@example.test",
    ):
        assert forbidden not in dumped


def test_mixed_legacy_path_receives_only_safe_clinic_facts() -> None:
    clinic = _build_outcome("contact")
    other = TurnOutcome(
        status="answered",
        response_goal="answer_service",
        facts={"service_catalog": {"service": {"name": "Hydrafacial"}}},
    )
    messages = _build_responder_messages(
        clinic_name="Tia",
        timezone_name="Africa/Cairo",
        local_now=NOW,
        history=[HumanMessage(content="رقم العيادة وقولي عندكم Hydrafacial؟")],
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
        "secret-provider-token",
        "secret-api-key",
        "internal-workspace",
        "internal-branch",
        "working_hours",
        "Clinic-authored policy text.",
    ):
        assert forbidden not in content


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
        clinic_name="Tia",
        timezone_name="Africa/Cairo",
        local_now=NOW,
        history=[HumanMessage(content="رقم العيادة إيه؟")],
        outcomes=[_build_outcome("contact")],
    )

    assert source == "deterministic:clinic-information-contract"
    assert "01012345678" in text


def test_explicit_clinic_email_question_returns_phone_only_truth() -> None:
    text, source = compose_v2_customer_reply(
        clinic_name="Tia",
        timezone_name="Africa/Cairo",
        local_now=NOW,
        history=[HumanMessage(content="إيميل العيادة إيه؟")],
        outcomes=[_build_outcome("contact")],
    )

    assert source == "deterministic:clinic-information-contract"
    assert "01012345678" in text
    assert "hello@example.test" not in text
    assert "email" not in text.casefold()


def test_stale_assistant_contact_cannot_override_verified_truth() -> None:
    text, source = compose_v2_customer_reply(
        clinic_name="Tia",
        timezone_name="Africa/Cairo",
        local_now=NOW,
        history=[
            AIMessage(
                content=(
                    "رقم العيادة 01199999999 والعنوان Old Street "
                    "والإيميل old-clinic@example.test."
                )
            ),
            HumanMessage(content="طب الرقم والعنوان ووسيلة التواصل المسجلين إيه؟"),
        ],
        outcomes=[_build_outcome("address", "contact")],
    )

    assert source == "deterministic:clinic-information-contract"
    assert "01012345678" in text
    assert "10 Verified Street, Cairo" in text
    assert "01199999999" not in text
    assert "Old Street" not in text
    assert "old-clinic@example.test" not in text


def test_payment_info_is_not_claimed_by_clinic_truth() -> None:
    outcome = TurnOutcome(
        status="answered",
        response_goal="answer_clinic_info",
        facts={
            "booking_requires_payment": False,
            "payment_execution_owner": "reception",
            "clinic_info": {"knowledge": "Cash and card are accepted."},
        },
    )
    contract = build_customer_response_contract([outcome])

    assert contract.units[0].clinic_truth is None
    assert is_pure_supported_clinic_contract(contract) is False


def test_duplicate_or_invalid_working_hours_fail_closed_from_pure_contract() -> None:
    outcome = TurnOutcome(
        status="answered",
        response_goal="answer_clinic_info",
        facts={
            "clinic_requested_details": ["working_hours"],
            "clinic_info": {
                "clinic_name": "Tia",
                "timezone": "Africa/Cairo",
                "locations": [
                    {
                        "name": "Main",
                        "working_hours": [
                            {"weekday": 0, "start": "10:00", "end": "20:00"},
                            {"weekday": 0, "start": "10:00", "end": "20:00"},
                        ],
                    }
                ],
            },
        },
    )
    contract = build_customer_response_contract([outcome])

    assert contract.units[0].clinic_truth is None
    assert is_pure_supported_clinic_contract(contract) is False


def test_clinic_read_binds_primary_location_and_filters_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(read_executor, "_explanatory_knowledge", lambda _context: None)
    workspace = SimpleNamespace(
        id="workspace-1",
        name="Verified Clinic",
        timezone="Africa/Cairo",
        primary_branch_id="branch-b",
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
                    "id": "branch-a",
                    "name": "A",
                    "phone": "01000000001",
                    "address": "Address A",
                    "working_hours": [
                        {"weekday": 0, "start": "09:00", "end": "17:00"}
                    ],
                },
                {
                    "id": "branch-b",
                    "name": "B",
                    "phone": "01000000002",
                    "address": "Address B",
                    "working_hours": [
                        {"weekday": 1, "start": "10:00", "end": "18:00"}
                    ],
                    "access_token": "never-expose",
                    "integration_id": "internal-integration",
                },
            ],
        },
    )

    result = read_executor._read_clinic_info(ReadRequest(kind="clinic_info"), context)

    assert result.payload["clinic_name"] == "Verified Clinic"
    assert result.payload["locations"] == [
        {
            "name": "B",
            "phone": "01000000002",
            "address": "Address B",
            "working_hours": [
                {"weekday": 1, "start": "10:00", "end": "18:00"}
            ],
        }
    ]
    assert "01000000001" not in str(result.payload)
    assert "never-expose" not in str(result.payload)
    assert "internal-integration" not in str(result.payload)


def test_clinic_read_does_not_pick_first_branch_when_single_location_is_ambiguous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(read_executor, "_explanatory_knowledge", lambda _context: None)
    context = ReadExecutionContext(
        db=object(),
        workspace=SimpleNamespace(
            id="workspace-1",
            name="Verified Clinic",
            timezone="Africa/Cairo",
            primary_branch_id=None,
        ),
        patient=SimpleNamespace(id="patient-1"),
        now=NOW,
        catalog={
            "services": [],
            "doctors": [],
            "branches": [
                {"id": "branch-a", "name": "A", "address": "Address A"},
                {"id": "branch-b", "name": "B", "address": "Address B"},
            ],
        },
    )

    result = read_executor._read_clinic_info(ReadRequest(kind="clinic_info"), context)

    assert result.payload["locations"] == []
    assert "Address A" not in str(result.payload)
    assert "Address B" not in str(result.payload)


def test_legacy_clinic_public_info_payload_is_phone_only() -> None:
    from app.services.agent_chat import _clinic_public_info_payload

    source = getsource(_clinic_public_info_payload)

    assert '"phone": location.phone' in source
    assert '"email": location.email' not in source


def test_native_catalog_projects_customer_contact_without_internal_config() -> None:
    source = getsource(TiaDatabaseClinicAdapter.build_catalog)

    for expected in (
        '"phone": row.phone',
        '"address_line1": row.address_line1',
        '"address_line2": row.address_line2',
        '"state": row.state',
        '"country_code": row.country_code',
        '"timezone": row.timezone',
        '"working_hours": hours_by_branch.get(row.id, [])',
    ):
        assert expected in source
    for forbidden in (
        '"email": row.email',
        "access_token",
        "api_key",
        "webhook_secret",
        "phone_number_id",
        "database_url",
    ):
        assert forbidden not in source
