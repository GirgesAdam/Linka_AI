from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from langchain_core.messages import HumanMessage

from app.agents.v2 import responder
from app.agents.v2.patient_composer import (
    deterministic_patient_contract_reply,
)
from app.agents.v2.responder import compose_v2_customer_reply
from app.services.agent_v2.outcome import TurnOutcome
from app.services.agent_v2.outcome_builder import _patient_crm_response_facts
from app.services.agent_v2.planner import PlanStep, ReadRequest
from app.services.agent_v2.read_executor import (
    ReadExecutionContext,
    execute_step_reads,
)
from app.services.agent_v2.response_contract import (
    build_customer_response_contract,
    is_pure_supported_patient_contract,
)

NOW = datetime(2026, 9, 28, 20, 0, tzinfo=UTC)


def _profile_outcome(
    *,
    first_name: str = "Mona",
    last_name: str | None = "Ali",
    phone: str | None = "01012345678",
    preferred_language: str = "ar",
    status: str = "blocked",
    requested_details: list[str] | None = None,
) -> TurnOutcome:
    patient: dict[str, object] = {
        "first_name": first_name,
        "preferred_language": preferred_language,
        "status": status,
    }
    if last_name is not None:
        patient["last_name"] = last_name
    if phone is not None:
        patient["phone"] = phone
    facts = _patient_crm_response_facts(
        {
            "customer_profile": {
                "patient": patient,
                "requested_details": requested_details
                or ["name", "phone", "preferred_language"],
            }
        },
        response_goal="answer_customer_profile",
    )
    return TurnOutcome(
        status="answered",
        response_goal="answer_customer_profile",
        facts=facts,
    )


def test_patient_truth_owns_exact_customer_safe_profile_fields() -> None:
    contract = build_customer_response_contract([_profile_outcome()])
    truth = contract.units[0].patient_truth

    assert truth is not None
    assert truth.requested_details == ("name", "phone", "preferred_language")
    assert truth.first_name == "Mona"
    assert truth.last_name == "Ali"

    assert truth.phone == "01012345678"
    assert truth.preferred_language == "ar"
    assert is_pure_supported_patient_contract(contract) is True

    payload = truth.model_dump_json()
    assert "blocked" not in payload
    assert "patient_id" not in payload
    assert "workspace_id" not in payload
    assert "phone_normalized" not in payload


def test_deterministic_patient_reply_preserves_name_and_phone_exactly() -> None:
    contract = build_customer_response_contract(
        [_profile_outcome(first_name="Mona", last_name="El-Sayed", phone="+20 101 234 5678")]
    )
    text = deterministic_patient_contract_reply(contract, arabic=True)

    assert "Mona El-Sayed" in text
    assert "+20 101 234 5678" in text
    assert "العربية" in text
    assert "blocked" not in text


def test_patient_reply_handles_missing_optional_contact_without_invention() -> None:
    contract = build_customer_response_contract(
        [_profile_outcome(last_name=None, phone=None)]
    )
    text = deterministic_patient_contract_reply(contract, arabic=False)

    assert "Name on file: Mona" in text

    assert "phone on file" not in text
    assert "010" not in text


def test_profile_response_shaping_removes_internal_crm_status() -> None:
    shaped = _patient_crm_response_facts(
        {
            "customer_profile": {
                "patient": {
                    "first_name": "Mona",
                    "last_name": "Ali",
                    "phone": "01012345678",
                    "phone_normalized": "201012345678",
                    "preferred_language": "ar",
                    "status": "blocked",
                    "source": "campaign",
                    "marketing_consent": True,
                }
            }
        },
        response_goal="answer_customer_profile",
    )

    assert shaped == {
        "customer_profile": {
            "patient": {
                "first_name": "Mona",
                "last_name": "Ali",
                "phone": "01012345678",
                "preferred_language": "ar",
            },
            "requested_details": ["name", "phone", "preferred_language"],
        }
    }


def test_profile_response_shaping_preserves_requested_missing_phone() -> None:
    shaped = _patient_crm_response_facts(
        {
            "customer_profile": {
                "requested_details": ["phone"],
            }
        },
        response_goal="answer_customer_profile",
    )

    assert shaped == {
        "customer_profile": {
            "patient": {},
            "requested_details": ["phone"],
        }
    }
    outcome = TurnOutcome(
        status="answered",
        response_goal="answer_customer_profile",
        facts=shaped,
    )
    contract = build_customer_response_contract([outcome])
    truth = contract.units[0].patient_truth

    assert truth is not None
    assert truth.requested_details == ("phone",)
    assert truth.phone is None
    assert is_pure_supported_patient_contract(contract) is True


def test_history_response_shaping_removes_profile_and_financial_fields() -> None:
    history = {
        "profile": {
            "first_name": "Mona",
            "phone": "01012345678",
            "gender": "female",
            "birth_date": "1990-01-01",
        },
        "total_appointments": 4,
        "completed_appointments": 3,
        "money": [{"currency": "EGP", "net_paid": "900.00 EGP"}],
        "recent_visits": [
            {
                "status": "completed",
                "services": ["PRP"],
                "price_minor": 100_000,
                "net_paid_minor": 90_000,
            }
        ],
        "recent_appointments": [
            {
                "status": "completed",
                "service_name": "PRP",
                "payment_status": "paid",
                "payment_method": "cash",
                "billing": "standard",
                "price": "1000.00 EGP",
                "net_paid": "900.00 EGP",
            }
        ],
    }
    shaped = _patient_crm_response_facts(
        {"customer_history": {"history": history}},
        response_goal="answer_customer_history",
    )

    visible = shaped["customer_history"]["history"]
    assert "profile" not in visible
    assert "money" not in visible
    assert visible["total_appointments"] == 4
    assert visible["recent_visits"] == [
        {"status": "completed", "services": ["PRP"]}
    ]
    assert visible["recent_appointments"] == [
        {"status": "completed", "service_name": "PRP"}
    ]


def test_customer_profile_read_uses_only_context_patient() -> None:
    patient = SimpleNamespace(
        first_name="Mona",
        last_name="Ali",
        phone="01012345678",
        phone_normalized="201012345678",
        preferred_language="ar",
        status="active",
    )
    context = ReadExecutionContext(
        db=object(),
        workspace=SimpleNamespace(id=uuid4()),
        patient=patient,
        now=NOW,
    )
    step = PlanStep(
        operation_index=0,
        operation_type="customer_profile",
        disposition="read",
        reads=[ReadRequest(kind="customer_profile")],
        response_goal="answer_customer_profile",
    )

    bundle = execute_step_reads(step, context)
    payload = bundle.results[0].payload["patient"]

    assert payload["first_name"] == "Mona"
    assert payload["phone"] == "01012345678"
    assert "phone_normalized" not in payload


def test_customer_profile_phone_only_read_minimizes_pii() -> None:
    patient = SimpleNamespace(
        first_name="Mona",
        last_name="Ali",
        phone="+20 101 234 5678",
        phone_normalized="201012345678",
        preferred_language="ar",
        status="active",
    )
    context = ReadExecutionContext(
        db=object(),
        workspace=SimpleNamespace(id=uuid4()),
        patient=patient,
        now=NOW,
    )
    step = PlanStep(
        operation_index=0,
        operation_type="customer_profile",
        disposition="read",
        reads=[
            ReadRequest(
                kind="customer_profile",
                parameters={"requested_patient_details": ["phone"]},
            )
        ],
        response_goal="answer_customer_profile",
    )

    bundle = execute_step_reads(step, context)

    assert bundle.results[0].payload == {
        "patient": {"phone": "+20 101 234 5678"},
        "requested_details": ["phone"],
    }


def test_customer_history_read_is_scoped_to_workspace_and_current_patient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = uuid4()
    patient_id = uuid4()
    patient = SimpleNamespace(id=patient_id)
    captured: dict[str, object] = {}

    class History:
        def model_dump(self, *, mode: str) -> dict[str, object]:
            assert mode == "json"
            return {
                "profile": {"patient_id": str(patient_id), "first_name": "Mona"},
                "total_appointments": 0,
                "recent_appointments": [],
            }

    def fake_history(*_args: object, **kwargs: object) -> History:
        captured["workspace_id"] = kwargs.get("workspace_id")
        captured["patient"] = kwargs.get("patient")
        return History()

    monkeypatch.setattr(
        "app.services.agent_v2.read_executor.build_patient_history_context",
        fake_history,
    )

    context = ReadExecutionContext(
        db=object(),
        workspace=SimpleNamespace(id=workspace_id),
        patient=patient,
        now=NOW,
    )
    step = PlanStep(
        operation_index=0,
        operation_type="customer_history",
        disposition="read",
        reads=[ReadRequest(kind="customer_history")],
        response_goal="answer_customer_history",
    )

    execute_step_reads(step, context)

    assert captured["workspace_id"] == workspace_id
    assert captured["patient"] is patient


def test_pure_profile_bypasses_generic_responder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
        history=[HumanMessage(content="إيه البيانات المسجلة عندكم ليا؟")],
        outcomes=[_profile_outcome()],
    )

    assert source == "deterministic:patient-contract"
    assert "Mona Ali" in text
    assert "01012345678" in text
    assert "blocked" not in text


def test_customer_history_does_not_enter_patient_truth() -> None:
    outcome = TurnOutcome(
        status="answered",
        response_goal="answer_customer_history",
        facts={
            "customer_history": {
                "history": {
                    "total_appointments": 2,
                    "recent_visits": [{"status": "completed"}],
                }
            }
        },
    )
    contract = build_customer_response_contract([outcome])

    assert contract.units[0].patient_truth is None
    assert is_pure_supported_patient_contract(contract) is False


def test_mixed_patient_and_unsupported_family_stays_legacy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcomes = [
        _profile_outcome(),
        TurnOutcome(
            status="answered",
            response_goal="answer_service",
            facts={"service_catalog": {"service": {"name": "Hydrafacial"}}},
        ),
    ]
    called = {"legacy": False}

    monkeypatch.setattr(
        responder,
        "_build_responder_messages",
        lambda **_kwargs: (
            called.__setitem__("legacy", True)
            or [HumanMessage(content="legacy")]
        ),
    )
    monkeypatch.setattr(responder, "build_realtime_composer_model", lambda: object())
    monkeypatch.setattr(responder, "model_label", lambda name: str(name))

    monkeypatch.setattr(
        responder,
        "invoke_with_model_chain",
        lambda **_kwargs: SimpleNamespace(
            value=responder.ResponderDraft(
                reply="رد legacy grounded",
                availability_claim="not_applicable",
            ),
            model_name="test-model",
        ),
    )

    text, _source = compose_v2_customer_reply(
        clinic_name="Linka Clinic",
        timezone_name="Africa/Cairo",
        local_now=NOW,
        history=[HumanMessage(content="قولّي بياناتي والخدمة")],
        outcomes=outcomes,
    )

    assert called["legacy"] is True
    assert text == "رد legacy grounded"
