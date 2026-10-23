from datetime import UTC, datetime
from pathlib import Path

from app.agents.v2.semantic_context import SemanticContext
from app.agents.v2.turn_contract import TiaTurnUnderstanding, TurnEntities, TurnOperation
from app.agents.v2.turn_interpreter import _interpreter_system_prompt
from app.services.agent_v2.planner import PlannerContext, plan_turn

UNDERARM_SERVICE_ID = "3e5f1909-c8d2-5985-a116-8f7379399635"


class _ReferenceTarget:
    def __init__(self, *, kind: str, canonical_id: str) -> None:
        self.kind = kind
        self.canonical_id = canonical_id


def _pricing_context() -> PlannerContext:
    return PlannerContext(
        semantic_context=SemanticContext(
            model_input={},
            reference_map={
                "S1": _ReferenceTarget(kind="service", canonical_id=UNDERARM_SERVICE_ID),
                "V1": _ReferenceTarget(kind="device", canonical_id="prime_lase"),
            },
        ),
        active_task=None,
        now=datetime(2026, 9, 13, 12, 0, tzinfo=UTC),
    )


def test_semantic_matrix_covers_package_session_intent_contract() -> None:
    backend = Path(__file__).resolve().parent.parent
    source = (backend / "scripts/run_agent_e2e_matrix.py").read_text(encoding="utf-8")

    required_cases = (
        "package_purchase_explicit",
        "package_purchase_multisession_plan",
        "package_inquiry_compare",
        "package_use_existing",
        "package_avoid_existing",
        "single_session_no_package_intent",
    )
    for case in required_cases:
        assert case in source

    for intent in ("purchase", "inquire", "use_existing", "avoid_existing", "none"):
        assert f'_package_intent("{intent}")' in source


def test_package_semantic_matrix_does_not_use_runtime_lexical_routing() -> None:
    backend = Path(__file__).resolve().parent.parent
    source = (backend / "scripts/run_agent_e2e_matrix.py").read_text(encoding="utf-8").lower()

    assert "re.compile" not in source
    assert "re.search" not in source
    assert "re.match" not in source


def test_package_session_contract_preserves_offer_dimension_for_pricing() -> None:
    schema = TurnEntities.model_json_schema()
    description = str(schema["properties"]["package_sessions"].get("description") or "").lower()

    assert "multi-session" in description
    assert "pricing questions" in description


def test_six_session_package_pricing_uses_verified_package_offer_read() -> None:
    turn = TiaTurnUnderstanding(
        operations=[
            TurnOperation(
                type="pricing",
                entities=TurnEntities(
                    service={"ref": "S1"},
                    device={"ref": "V1"},
                    package_sessions=6,
                ),
                execution_intent="informational",
            )
        ]
    )

    plan = plan_turn(turn, _pricing_context())

    assert len(plan.steps) == 1
    step = plan.steps[0]
    assert step.disposition == "read"
    assert step.write_intent is None
    assert [read.kind for read in step.reads] == ["package_offers"]
    assert step.reads[0].parameters["service_id"] == UNDERARM_SERVICE_ID
    assert step.reads[0].parameters["device_key"] == "prime_lase"
    assert step.reads[0].parameters["package_sessions"] == 6


def test_single_session_pricing_stays_on_service_catalog_read() -> None:
    turn = TiaTurnUnderstanding(
        operations=[
            TurnOperation(
                type="pricing",
                entities=TurnEntities(service={"ref": "S1"}),
                execution_intent="informational",
            )
        ]
    )

    plan = plan_turn(turn, _pricing_context())

    assert len(plan.steps) == 1
    step = plan.steps[0]
    assert step.disposition == "read"
    assert step.write_intent is None
    assert [read.kind for read in step.reads] == ["service_catalog"]
    assert step.reads[0].parameters == {"service_id": UNDERARM_SERVICE_ID}


def test_history_package_impact_prompt_preserves_both_semantic_domains() -> None:
    prompt = _interpreter_system_prompt(
        timezone_name="Africa/Cairo",
        local_now=datetime(2026, 9, 13, 12, 0, tzinfo=UTC),
    ).lower()

    assert "past appointment outcome" in prompt
    assert "owned-package balance" in prompt
    assert "customer_history" in prompt
    assert "package_info" in prompt


def test_recent_package_correction_prompt_uses_typed_cancel_operation() -> None:
    prompt = _interpreter_system_prompt(
        timezone_name="Africa/Cairo",
        local_now=datetime(2026, 10, 22, 12, 0, tzinfo=UTC),
    ).lower()

    assert "cancel_package only" in prompt
    assert "recent_verified_action" in prompt
    assert "continues_previous=true" in prompt
    assert "general/historical purchased-package" in prompt
    assert "human_support" in prompt
    assert "do not replay that completed action" not in prompt


def test_history_package_impact_plans_both_verified_reads_without_writes() -> None:
    turn = TiaTurnUnderstanding(
        operations=[
            TurnOperation(
                type="customer_history",
                entities=TurnEntities(),
                execution_intent="informational",
            ),
            TurnOperation(
                type="package_info",
                entities=TurnEntities(),
                execution_intent="informational",
            ),
        ]
    )
    context = PlannerContext(
        semantic_context=SemanticContext(model_input={}, reference_map={}),
        active_task=None,
        now=datetime(2026, 9, 13, 12, 0, tzinfo=UTC),
    )

    plan = plan_turn(turn, context)

    assert [read.kind for step in plan.steps for read in step.reads] == [
        "customer_history",
        "customer_packages",
    ]
    assert all(step.disposition == "read" for step in plan.steps)
    assert all(step.write_intent is None for step in plan.steps)
