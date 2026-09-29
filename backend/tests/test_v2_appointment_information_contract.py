from __future__ import annotations

from datetime import UTC, datetime

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from app.agents.v2 import responder
from app.agents.v2.appointment_info_composer import (
    deterministic_appointment_info_reply,
)
from app.agents.v2.responder import (
    _build_responder_messages,
    compose_v2_customer_reply,
)
from app.agents.v2.semantic_context import build_semantic_context
from app.agents.v2.turn_contract import (
    TiaTurnUnderstanding,
    TurnEntities,
    TurnOperation,
)
from app.services.agent_v2.outcome import TurnOutcome
from app.services.agent_v2.outcome_builder import build_step_outcome
from app.services.agent_v2.planner import PlanStep, ReadRequest
from app.services.agent_v2.read_executor import ReadExecutionBundle, ReadResult
from app.services.agent_v2.response_contract import (
    build_customer_response_contract,
    is_pure_supported_appointment_contract,
)

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
def _service(
    name: str,
    *,
    device: str | None = None,
) -> dict[str, object]:
    result: dict[str, object] = {"service_name": name}
    if device is not None:
        result["laser_device_name"] = device
    return result


def _visit(
    *,
    status: str = "confirmed",
    start: str = "2026-10-01T18:00:00+03:00",
    end: str = "2026-10-01T18:30:00+03:00",
    doctor: str = "Dr Mona",
    services: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "status": status,
        "start_local": start,
        "end_local": end,
        "doctor_name": doctor,
        "services": services or [_service("Laser Underarm", device="Candela Gentle")],
    }


def _outcome(
    visits: list[dict[str, object]],
    *,
    complete_set: bool = True,
) -> TurnOutcome:
    return TurnOutcome(
        status="answered",
        response_goal="answer_customer_history",
        facts={
            "appointments": {
                "visits": visits,
                "visit_count": len(visits),
                "presentation_unit": "visit",
                "complete_set": complete_set,
            }
        },
    )
def _broad_read() -> ReadExecutionBundle:
    appointment = {
        "appointment_id": "internal-appointment-a",
        "visit_group_id": "internal-visit-a",
        "patient_id": "internal-patient",
        "workspace_id": "internal-workspace",
        "status": "confirmed",
        "service_name": "Laser Underarm",
        "doctor_name": "Dr Mona",
        "start_local": "2026-10-01T18:00:00+03:00",
        "end_local": "2026-10-01T18:30:00+03:00",
        "price_minor": 65_000,
        "currency": "EGP",
        "payment_status": "paid",
        "amount_paid_minor": 65_000,
        "payment_method": "cash",
        "billing_context": "package_prepaid",
        "patient_package_id": "internal-package",
        "package_external_id": "external-package",
        "laser_device_name": "Candela Gentle",
    }
    visit = {
        "visit_group_id": "internal-visit-a",
        "appointment_ids": ["internal-appointment-a"],
        "status": "confirmed",
        "start_local": "2026-10-01T18:00:00+03:00",
        "end_local": "2026-10-01T18:30:00+03:00",
        "doctor_name": "Dr Mona",
        "price_minor": 65_000,
        "currency": "EGP",
        "services": [
            {
                "appointment_id": "internal-appointment-a",
                "service_id": "internal-service",
                "service_name": "Laser Underarm",
                "laser_device_key": "candela",
                "laser_device_name": "Candela Gentle",
            }
        ],
    }
    return ReadExecutionBundle(
        results=[
            ReadResult(
                kind="appointments",
                ok=True,
                payload={
                    "appointments": [appointment],
                    "visits": [visit],
                    "visit_count": 1,
                    "presentation_unit": "visit",
                },
            )
        ]
    )


def _build_appointment_outcome() -> TurnOutcome:
    turn = TiaTurnUnderstanding(
        operations=[
            TurnOperation(
                type="appointment_list",
                entities=TurnEntities(),
                execution_intent="informational",
            )
        ],
        safety_signals=[],
    )
    step = PlanStep(
        operation_index=0,
        operation_type="appointment_list",
        disposition="read",
        reads=[ReadRequest(kind="appointments")],
        response_goal="answer_customer_history",
    )
    return build_step_outcome(
        step,
        turn=turn,
        semantic_context=build_semantic_context({"services": [], "doctors": []}),
        reads=_broad_read(),
    )


def test_appointment_list_outcome_is_safe_shaped_before_response_contract() -> None:
    outcome = _build_appointment_outcome()
    payload = outcome.model_dump(mode="json")
    text = str(payload)
    appointments = outcome.facts["appointments"]
    assert isinstance(appointments, dict)
    assert appointments["complete_set"] is True
    assert appointments["visit_count"] == 1
    assert "appointments" not in appointments
    assert appointments["visits"] == [
        {
            "status": "confirmed",
            "start_local": "2026-10-01T18:00:00+03:00",
            "end_local": "2026-10-01T18:30:00+03:00",
            "doctor_name": "Dr Mona",
            "services": [
                {
                    "service_name": "Laser Underarm",
                    "laser_device_name": "Candela Gentle",
                }
            ],
        }
    ]

    for forbidden in (
        "price",
        "amount_paid",
        "payment_status",
        "payment_method",
        "billing",
        "package",
        "appointment_id",
        "patient_id",
        "workspace_id",
        "visit_group_id",
        "service_id",
        "laser_device_key",
    ):
        assert forbidden not in text


def test_single_upcoming_appointment_truth_preserves_exact_bindings() -> None:
    contract = build_customer_response_contract([_outcome([_visit()])])
    truth = contract.units[0].appointment_truth

    assert truth is not None
    assert truth.complete_set is True
    assert len(truth.visits) == 1
    visit = truth.visits[0]
    assert visit.status == "confirmed"
    assert visit.start_local == "2026-10-01T18:00:00+03:00"
    assert visit.doctor_name == "Dr Mona"
    assert len(visit.services) == 1
    assert visit.services[0].service_name == "Laser Underarm"
    assert visit.services[0].device_name == "Candela Gentle"
    assert is_pure_supported_appointment_contract(contract) is True


def test_multiple_upcoming_appointments_keep_independent_bindings_and_complete_set() -> None:
    visits = [
        _visit(
            start="2026-10-01T18:00:00+03:00",
            doctor="Dr Mona",
            services=[_service("Laser Underarm", device="Candela Gentle")],
        ),
        _visit(
            start="2026-10-03T11:30:00+03:00",
            end="2026-10-03T12:30:00+03:00",
            doctor="Dr Karim",
            services=[_service("Hydrafacial")],
        ),
    ]
    contract = build_customer_response_contract([_outcome(visits)])
    truth = contract.units[0].appointment_truth
    assert truth is not None
    assert truth.complete_set is True
    assert len(truth.visits) == 2

    first, second = truth.visits
    assert (first.doctor_name, first.services[0].service_name) == (
        "Dr Mona",
        "Laser Underarm",
    )
    assert first.services[0].device_name == "Candela Gentle"
    assert (second.doctor_name, second.services[0].service_name) == (
        "Dr Karim",
        "Hydrafacial",
    )
    assert second.services[0].device_name is None
    text = deterministic_appointment_info_reply(contract, arabic=False)
    assert text.count("Dr Mona") == 1
    assert text.count("Dr Karim") == 1
    assert text.count("Laser Underarm") == 1
    assert text.count("Hydrafacial") == 1
    assert "Candela Gentle" in text


def test_grouped_multi_service_visit_remains_one_visit() -> None:
    grouped = _visit(
        doctor="Dr Mona",
        services=[
            _service("Laser Underarm", device="Candela Gentle"),
            _service("Laser Bikini", device="Candela Gentle"),
        ],
    )
    contract = build_customer_response_contract([_outcome([grouped])])
    truth = contract.units[0].appointment_truth
    assert truth is not None
    assert len(truth.visits) == 1
    assert [item.service_name for item in truth.visits[0].services] == [
        "Laser Underarm",
        "Laser Bikini",
    ]

    text = deterministic_appointment_info_reply(contract, arabic=False)
    assert text.count("October 1, 2026") == 1
    assert text.count("Dr Mona") == 1
    assert "Laser Underarm" in text
    assert "Laser Bikini" in text


def test_empty_upcoming_set_uses_upcoming_scoped_claim() -> None:
    contract = build_customer_response_contract([_outcome([])])
    text = deterministic_appointment_info_reply(contract, arabic=True)

    assert text == "مفيش مواعيد جاية في السجل المؤكد الحالي."
    assert "أي مواعيد" not in text
def test_partial_upcoming_truth_does_not_claim_complete_customer_schedule() -> None:
    contract = build_customer_response_contract(
        [_outcome([_visit()], complete_set=False)]
    )
    truth = contract.units[0].appointment_truth
    assert truth is not None and truth.complete_set is False

    text = deterministic_appointment_info_reply(contract, arabic=True)
    assert "الموعد الجاي المتحقق" in text
    assert "ميعادك الجاي:" not in text


def test_full_upcoming_list_renders_every_verified_visit_once() -> None:
    visits = [
        _visit(
            start="2026-10-01T18:00:00+03:00",
            doctor="Dr Mona",
            services=[_service("Service A", device="Device A")],
        ),
        _visit(
            start="2026-10-02T19:00:00+03:00",
            doctor="Dr Karim",
            services=[_service("Service B", device="Device B")],
        ),
        _visit(
            start="2026-10-03T20:00:00+03:00",
            doctor="Dr Laila",
            services=[_service("Service C")],
        ),
    ]
    contract = build_customer_response_contract([_outcome(visits)])
    text = deterministic_appointment_info_reply(contract, arabic=False)

    for name in ("Service A", "Service B", "Service C"):
        assert text.count(name) == 1
    for doctor in ("Dr Mona", "Dr Karim", "Dr Laila"):
        assert text.count(doctor) == 1
    assert text.count("status: confirmed") == 3
def test_stale_assistant_wording_cannot_override_verified_appointment_truth() -> None:
    verified = _outcome(
        [
            _visit(
                start="2026-10-05T18:00:00+03:00",
                doctor="Dr Verified",
                services=[_service("Verified Service", device="Verified Device")],
            )
        ]
    )
    text, label = compose_v2_customer_reply(
        clinic_name="Linka Clinic",
        timezone_name="Africa/Cairo",
        local_now=NOW,
        history=[
            AIMessage(content="ميعادك الأحد الساعة 5 مع د. قديم."),
            HumanMessage(content="ميعادي الجاي إمتى؟"),
        ],
        outcomes=[verified],
    )

    assert label == "deterministic:appointment-info-contract"
    assert "5 أكتوبر 2026" in text
    assert "6:00 مساءً" in text
    assert "Dr Verified" in text
    assert "Verified Service" in text
    assert "الأحد الساعة 5" not in text
    assert "د. قديم" not in text


def test_pure_appointment_path_bypasses_generic_responder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        responder,
        "_build_responder_messages",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("generic responder must not run")
        ),
    )
    text, label = compose_v2_customer_reply(
        clinic_name="Linka Clinic",
        timezone_name="Africa/Cairo",
        local_now=NOW,
        history=[HumanMessage(content="معادي الجاي إمتى؟")],
        outcomes=[_outcome([_visit()])],
    )

    assert label == "deterministic:appointment-info-contract"
    assert "Laser Underarm" in text
    assert "Candela Gentle" in text


def test_mixed_generic_path_receives_only_safe_appointment_facts() -> None:
    appointment = _build_appointment_outcome()
    service = TurnOutcome(
        status="answered",
        response_goal="answer_service",
        facts={"service_catalog": {"service": {"name": "Hydrafacial"}}},
    )
    messages = _build_responder_messages(
        clinic_name="Linka Clinic",
        timezone_name="Africa/Cairo",
        local_now=NOW,
        history=[HumanMessage(content="ميعادي الجاي وإيه الهيدرافيشل؟")],
        outcomes=[appointment, service],
    )
    outcome_message = next(
        message
        for message in messages
        if isinstance(message.content, str)
        and message.content.startswith("TURN_OUTCOMES")
    )
    content = str(outcome_message.content)

    assert "Laser Underarm" in content
    assert "Candela Gentle" in content
    assert "Dr Mona" in content
    assert "Hydrafacial" in content
    for forbidden in (
        "payment_status",
        "payment_method",
        "amount_paid",
        "billing",
        "patient_package",
        "package_external",
        "appointment_id",
        "visit_group_id",
    ):
        assert forbidden not in content
def test_historical_customer_history_is_not_claimed_by_appointment_contract() -> None:
    historical = TurnOutcome(
        status="answered",
        response_goal="answer_customer_history",
        facts={
            "customer_history": {
                "history": {
                    "total_appointments": 2,
                    "recent_visits": [
                        {"status": "completed", "services": ["PRP"]}
                    ],
                }
            }
        },
    )
    contract = build_customer_response_contract([historical])

    assert contract.units[0].appointment_truth is None
    assert is_pure_supported_appointment_contract(contract) is False
