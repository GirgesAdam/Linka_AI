from __future__ import annotations

from app.agents.v2.semantic_context import build_semantic_context
from app.agents.v2.turn_contract import TiaTurnUnderstanding, TurnEntities, TurnOperation
from app.services.agent_v2.outcome_builder import build_step_outcome, customer_visible_outcome
from app.services.agent_v2.planner import PlanStep, ReadRequest
from app.services.agent_v2.read_executor import ReadExecutionBundle, ReadResult


def _context():
    return build_semantic_context(
        {
            "services": [{"id": "service-1", "name": "ليزر إبط"}],
            "doctors": [],
            "appointments": [],
        }
    )


def _service_reads() -> ReadExecutionBundle:
    return ReadExecutionBundle(
        results=[
            ReadResult(
                kind="service_catalog",
                ok=True,
                payload={
                    "service": {
                        "id": "service-1",
                        "name": "ليزر إبط",
                        "description": "جلسة إزالة شعر لمنطقة الإبط.",
                        "price_minor": 50000,
                        "currency": "EGP",
                        "customer_duration_text": "حوالي 15 دقيقة",
                        "laser_devices": [
                            {"device_key": "candela", "device_name": "Candela Gentle"}
                        ],
                    }
                },
            )
        ]
    )


def test_price_question_exposes_price_but_not_duration_or_extra_service_details() -> None:
    operation = TurnOperation(
        type="pricing",
        entities=TurnEntities(),
        selection=None,
        package_usage="unspecified",
        requested_service_details=["price"],
    )
    turn = TiaTurnUnderstanding(operations=[operation], safety_signals=[])
    step = PlanStep(
        operation_index=0,
        operation_type="pricing",
        disposition="read",
        reads=[ReadRequest(kind="service_catalog")],
        response_goal="answer_price",
    )

    outcome = build_step_outcome(
        step,
        turn=turn,
        semantic_context=_context(),
        reads=_service_reads(),
    )
    visible = customer_visible_outcome(outcome)
    service = visible["facts"]["service_catalog"]["service"]

    assert service == {"name": "ليزر إبط", "price": "500.00 EGP", "currency": "EGP"}
    assert "duration" not in str(visible).lower()
    assert "15" not in str(visible)
    assert "laser_devices" not in str(visible)
    assert "description" not in str(visible)


def test_duration_is_exposed_only_when_semantically_requested() -> None:
    operation = TurnOperation(
        type="service_info",
        entities=TurnEntities(),
        selection=None,
        package_usage="unspecified",
        requested_service_details=["duration"],
    )
    turn = TiaTurnUnderstanding(operations=[operation], safety_signals=[])
    step = PlanStep(
        operation_index=0,
        operation_type="service_info",
        disposition="read",
        reads=[ReadRequest(kind="service_catalog")],
        response_goal="answer_service",
    )

    outcome = build_step_outcome(
        step,
        turn=turn,
        semantic_context=_context(),
        reads=_service_reads(),
    )
    visible = customer_visible_outcome(outcome)
    service = visible["facts"]["service_catalog"]["service"]

    assert service == {"name": "ليزر إبط", "customer_duration_text": "حوالي 15 دقيقة"}


def test_open_ended_service_catalog_preserves_only_verified_service_list() -> None:
    operation = TurnOperation(
        type="service_info",
        entities=TurnEntities(),
        selection=None,
        package_usage="unspecified",
        requested_service_details=[],
    )
    turn = TiaTurnUnderstanding(operations=[operation], safety_signals=[])
    step = PlanStep(
        operation_index=0,
        operation_type="service_info",
        disposition="read",
        reads=[ReadRequest(kind="service_catalog")],
        response_goal="answer_service",
    )
    reads = ReadExecutionBundle(
        results=[ReadResult(kind="service_catalog", ok=True, payload={"services": [{"name": "Hydrafacial"}, {"name": "PRP للبشرة"}]})]
    )

    visible = customer_visible_outcome(
        build_step_outcome(step, turn=turn, semantic_context=_context(), reads=reads)
    )

    assert visible["facts"]["service_catalog"] == {
        "services": [{"name": "Hydrafacial"}, {"name": "PRP للبشرة"}]
    }


def test_generic_service_information_defaults_to_description_not_duration() -> None:
    operation = TurnOperation(
        type="service_info",
        entities=TurnEntities(),
        selection=None,
        package_usage="unspecified",
        requested_service_details=[],
    )
    turn = TiaTurnUnderstanding(operations=[operation], safety_signals=[])
    step = PlanStep(
        operation_index=0,
        operation_type="service_info",
        disposition="read",
        reads=[ReadRequest(kind="service_catalog")],
        response_goal="answer_service",
    )

    outcome = build_step_outcome(
        step,
        turn=turn,
        semantic_context=_context(),
        reads=_service_reads(),
    )
    visible = customer_visible_outcome(outcome)
    service = visible["facts"]["service_catalog"]["service"]

    assert service == {"name": "ليزر إبط", "description": "جلسة إزالة شعر لمنطقة الإبط."}
