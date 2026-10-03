from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from langchain_core.messages import HumanMessage
from pydantic import ValidationError

from app.agents.v2.turn_contract import (
    DateConstraint,
    EntityReference,
    TiaTurnUnderstanding,
    TurnEntities,
    TurnOperation,
)
from app.integrations.clinic.base import AppointmentReadResult, AppointmentRecord
from app.services.agent_v2 import orchestrator as runtime
from app.services.agent_v2.planner import (
    PlannerContext,
    plan_turn,
    resolve_same_turn_verified_service_dependency,
)
from app.services.agent_v2.response_contract import build_customer_response_contract

TZ = ZoneInfo("Africa/Cairo")
NOW = datetime(2026, 10, 3, 4, 40, tzinfo=TZ)
HYDRA_ID = "service-hydra"
PRP_ID = "service-prp"
LASER_ID = "service-laser"
DEKA = "device_deka_again"
OTHER = "device_other"


def _catalog() -> dict[str, object]:
    return {
        "services": [
            {
                "id": HYDRA_ID,
                "name": "Hydrafacial",
                "category": "facial",
                "price_minor": 150_000,
                "currency": "EGP",
                "price": "1,500 EGP",
                "requires_laser_device": False,
            },
            {
                "id": PRP_ID,
                "name": "PRP",
                "category": "facial",
                "price_minor": 220_000,
                "currency": "EGP",
                "price": "2,200 EGP",
                "requires_laser_device": False,
            },
            {
                "id": LASER_ID,
                "name": "Full Body Laser",
                "category": "laser",
                "price_minor": 0,
                "currency": "EGP",
                "price": "0 EGP",
                "requires_laser_device": True,
                "laser_devices": [
                    {
                        "device_key": DEKA,
                        "device_name": "DEKA Again",
                        "price_minor": 300_000,
                        "duration_minutes": 60,
                        "currency": "EGP",
                        "configured": True,
                    },
                    {
                        "device_key": OTHER,
                        "device_name": "Other Laser",
                        "price_minor": 450_000,
                        "duration_minutes": 75,
                        "currency": "EGP",
                        "configured": True,
                    },
                ],
            },
        ],
        "doctors": [
            {
                "id": "doctor-mariam",
                "name": "Mariam",
                "service_ids": [HYDRA_ID, PRP_ID, LASER_ID],
            }
        ],
        "branches": [{"id": "branch-1", "name": "Linka Clinic"}],
    }


def _appointment(
    appointment_id: str,
    *,
    service_id: str,
    service_name: str,
    day: int,
    device_key: str | None = None,
    device_name: str | None = None,
) -> AppointmentRecord:
    start = datetime(2026, 10, day, 14, 0, tzinfo=TZ)
    return AppointmentRecord(
        appointment_id=appointment_id,
        patient_id="patient-1",
        status="confirmed",
        service_id=service_id,
        service_name=service_name,
        branch_id="branch-1",
        branch_name="Linka Clinic",
        doctor_id="doctor-mariam",
        doctor_name="Mariam",
        start_at=start,
        end_at=start.replace(hour=15),
        timezone="Africa/Cairo",
        price_minor=999_999,
        currency="EGP",
        laser_device_key=device_key,
        laser_device_name=device_name,
    )


class _AppointmentAdapter:
    def __init__(self, appointments: list[AppointmentRecord]) -> None:
        self.appointments = appointments

    def require_capability(self, capability: object) -> None:
        return None

    def get_patient_appointments(self, request: object) -> AppointmentReadResult:
        return AppointmentReadResult(appointments=tuple(self.appointments))


def _appointment_op(*, day: int | None = None) -> TurnOperation:
    return TurnOperation(
        type="appointment_list",
        entities=TurnEntities(
            date=(
                DateConstraint(mode="exact", start_date=f"2026-10-{day:02d}")
                if day is not None
                else None
            )
        ),
        execution_intent="informational",
    )


def _relational_price_op() -> TurnOperation:
    return TurnOperation(
        type="pricing",
        entities=TurnEntities(),
        requested_service_details=["price"],
        same_turn_service_source="verified_appointment",
        execution_intent="informational",
    )


def _explicit_price_op(ref: str) -> TurnOperation:
    return TurnOperation(
        type="pricing",
        entities=TurnEntities(service=EntityReference(ref=ref)),
        requested_service_details=["price"],
        execution_intent="informational",
    )


def _run(
    monkeypatch: pytest.MonkeyPatch,
    *,
    turn: TiaTurnUnderstanding,
    appointments: list[AppointmentRecord],
    message: str,
):
    monkeypatch.setattr(runtime, "load_active_task", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        runtime,
        "interpret_customer_turn_v2",
        lambda **kwargs: turn,
    )
    return runtime.orchestrate_v2_turn(
        db=object(),
        workspace=SimpleNamespace(
            id=uuid4(),
            primary_branch_id="branch-1",
            timezone="Africa/Cairo",
        ),
        patient=SimpleNamespace(id="patient-1"),
        conversation_id=uuid4(),
        run_id=uuid4(),
        history=[HumanMessage(content=message)],
        local_now=NOW,
        timezone_name="Africa/Cairo",
        clinic_name="Linka Test Clinic",
        catalog=_catalog(),
        adapter=_AppointmentAdapter(appointments),
        turn_id="turn-f8",
    )


def test_relational_price_resolves_only_after_verified_appointment_read() -> None:
    turn = TiaTurnUnderstanding(
        operations=[_appointment_op(), _relational_price_op()]
    )
    from app.agents.v2.semantic_context import build_semantic_context

    context = build_semantic_context(_catalog())
    plan = plan_turn(
        turn,
        PlannerContext(semantic_context=context, active_task=None, now=NOW),
    )
    assert plan.steps[1].disposition == "clarify"
    assert plan.steps[1].clarification_field == "service"

    resolved = resolve_same_turn_verified_service_dependency(
        plan.steps[1],
        operation=turn.operations[1],
        verified_appointment_parameters={
            "service_id": LASER_ID,
            "device_key": DEKA,
        },
    )

    assert resolved.disposition == "read"
    assert resolved.response_goal == "answer_price"
    assert resolved.reads[0].kind == "service_catalog"
    assert resolved.reads[0].parameters == {"service_id": LASER_ID}
    assert resolved.facts["service_id"] == LASER_ID
    assert resolved.facts["device_key"] == DEKA
    assert resolved.write_intent is None
    assert resolved.state_action == "none"


def test_historical_mixed_non_laser_answers_appointment_and_current_price(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    appointment = _appointment(
        "appointment-a",
        service_id=HYDRA_ID,
        service_name="Hydrafacial",
        day=6,
    )
    result = _run(
        monkeypatch,
        turn=TiaTurnUnderstanding(
            operations=[_appointment_op(), _relational_price_op()]
        ),
        appointments=[appointment],
        message="ميعادي الجاي إمتى ومع مين؟ والخدمة اللي حاجزها سعرها كام؟",
    )

    assert result.plan.steps[1].disposition == "clarify"
    assert result.traces[0].verified_parameters["service_id"] == HYDRA_ID
    assert result.traces[1].read_kinds == ("service_catalog",)
    assert result.traces[1].outcome is not None
    assert result.traces[1].outcome.status == "answered"
    assert result.traces[1].outcome.response_goal == "answer_price"
    contract = build_customer_response_contract(list(result.outcomes))
    assert len(contract.units) == 2
    assert contract.units[0].appointment_truth is not None
    assert contract.units[1].commercial_truth is not None
    assert contract.units[1].commercial_truth.kind == "service_base_price"
    assert contract.units[1].commercial_truth.options[0].amount == "1500.00"
    assert "Hydrafacial" in (result.reply or "")
    assert "1500" in (result.reply or "").replace(",", "")
    assert "محتاج معلومة إضافية" not in (result.reply or "")
    assert result.pending_write is None
    assert all(step.write_intent is None for step in result.plan.steps)
    assert all(step.state_action == "none" for step in result.plan.steps)


def test_laser_relational_price_uses_verified_appointment_device(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    appointment = _appointment(
        "appointment-laser",
        service_id=LASER_ID,
        service_name="Full Body Laser",
        day=6,
        device_key=DEKA,
        device_name="DEKA Again",
    )
    result = _run(
        monkeypatch,
        turn=TiaTurnUnderstanding(
            operations=[_appointment_op(), _relational_price_op()]
        ),
        appointments=[appointment],
        message="ميعادي امتى والخدمة اللي حاجزها سعرها كام؟",
    )

    assert result.traces[0].verified_parameters["service_id"] == LASER_ID
    assert result.traces[0].verified_parameters["device_key"] == DEKA
    price_outcome = result.traces[1].outcome
    assert price_outcome is not None
    service = price_outcome.facts["service_catalog"]["service"]
    assert service["selected_laser_device"] == {
        "device_name": "DEKA Again",
        "price": "3000.00 EGP",
    }
    reply = (result.reply or "").replace(",", "")
    assert "DEKA Again" in reply
    assert "3000" in reply
    assert "4500" not in reply
    assert "Other Laser" not in reply
    assert "محتاج معلومة إضافية" not in reply
    assert result.pending_write is None


def test_selected_appointment_service_wins_over_other_patient_appointment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hydra = _appointment(
        "appointment-a",
        service_id=HYDRA_ID,
        service_name="Hydrafacial",
        day=6,
    )
    prp = _appointment(
        "appointment-b",
        service_id=PRP_ID,
        service_name="PRP",
        day=8,
    )
    result = _run(
        monkeypatch,
        turn=TiaTurnUnderstanding(
            operations=[_appointment_op(day=6), _relational_price_op()]
        ),
        appointments=[hydra, prp],
        message="ميعادي يوم 6 أكتوبر إمتى والخدمة اللي حاجزها سعرها كام؟",
    )

    assert result.traces[0].verified_parameters["service_id"] == HYDRA_ID
    assert "1500" in (result.reply or "").replace(",", "")
    assert "2200" not in (result.reply or "").replace(",", "")


def test_no_appointment_does_not_fabricate_service_or_price(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _run(
        monkeypatch,
        turn=TiaTurnUnderstanding(
            operations=[_appointment_op(), _relational_price_op()]
        ),
        appointments=[],
        message="ميعادي الجاي إمتى والخدمة اللي حاجزها سعرها كام؟",
    )

    assert result.traces[0].verified_parameters == {}
    assert result.traces[1].read_kinds == ()
    assert result.traces[1].outcome is not None
    assert result.traces[1].outcome.status == "needs_input"
    assert result.traces[1].outcome.facts["needed"] == "service"
    assert "1500" not in (result.reply or "").replace(",", "")
    assert "2200" not in (result.reply or "").replace(",", "")
    assert result.pending_write is None


def test_ambiguous_appointments_do_not_price_arbitrary_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _run(
        monkeypatch,
        turn=TiaTurnUnderstanding(
            operations=[_appointment_op(), _relational_price_op()]
        ),
        appointments=[
            _appointment(
                "appointment-a",
                service_id=HYDRA_ID,
                service_name="Hydrafacial",
                day=6,
            ),
            _appointment(
                "appointment-b",
                service_id=PRP_ID,
                service_name="PRP",
                day=8,
            ),
        ],
        message="مواعيدي الجاية إمتى والخدمة اللي حاجزها سعرها كام؟",
    )

    assert result.traces[0].verified_parameters == {}
    assert result.traces[1].read_kinds == ()
    assert result.traces[1].outcome is not None
    assert result.traces[1].outcome.status == "needs_input"
    assert "1500" not in (result.reply or "").replace(",", "")
    assert "2200" not in (result.reply or "").replace(",", "")


def test_explicit_service_price_overrides_relational_appointment_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _run(
        monkeypatch,
        turn=TiaTurnUnderstanding(
            operations=[_appointment_op(), _explicit_price_op("S2")]
        ),
        appointments=[
            _appointment(
                "appointment-a",
                service_id=HYDRA_ID,
                service_name="Hydrafacial",
                day=6,
            )
        ],
        message="ميعادي الجاي إمتى؟ وسعر الـPRP كام؟",
    )

    assert result.understanding.operations[1].same_turn_service_source == "none"
    assert result.traces[0].verified_parameters["service_id"] == HYDRA_ID
    assert result.plan.steps[1].reads[0].parameters == {"service_id": PRP_ID}
    reply = (result.reply or "").replace(",", "")
    assert "2200" in reply
    assert "1500" not in reply


def test_price_only_behavior_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    result = _run(
        monkeypatch,
        turn=TiaTurnUnderstanding(
            operations=[_explicit_price_op("S1")]
        ),
        appointments=[],
        message="سعر الـHydrafacial كام؟",
    )

    assert len(result.traces) == 1
    assert result.traces[0].read_kinds == ("service_catalog",)
    assert "1500" in (result.reply or "").replace(",", "")
    assert result.pending_write is None


def test_appointment_only_behavior_is_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _run(
        monkeypatch,
        turn=TiaTurnUnderstanding(operations=[_appointment_op()]),
        appointments=[
            _appointment(
                "appointment-a",
                service_id=HYDRA_ID,
                service_name="Hydrafacial",
                day=6,
            )
        ],
        message="ميعادي الجاي إمتى ومع مين؟",
    )

    assert len(result.traces) == 1
    assert result.traces[0].read_kinds == ("appointments",)
    assert "Hydrafacial" in (result.reply or "")
    assert result.pending_write is None


def test_marker_cannot_override_explicit_grounded_service() -> None:
    with pytest.raises(ValidationError):
        TurnOperation(
            type="pricing",
            entities=TurnEntities(service=EntityReference(ref="S1")),
            requested_service_details=["price"],
            same_turn_service_source="verified_appointment",
            execution_intent="informational",
        )
