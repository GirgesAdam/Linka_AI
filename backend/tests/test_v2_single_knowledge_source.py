from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from app.agents.v2.semantic_context import build_semantic_context
from app.services.agent_v2 import read_executor
from app.services.agent_v2.planner import PlanStep, ReadRequest
from app.services.agent_v2.read_executor import ReadExecutionContext, execute_step_reads


def _read_context() -> ReadExecutionContext:
    workspace = SimpleNamespace(
        id="workspace-1",
        name="Linka Clinic",
        timezone="Africa/Cairo",
        primary_branch_id=None,
    )
    patient = SimpleNamespace(id="patient-1")
    return ReadExecutionContext(
        db=object(),
        workspace=workspace,
        patient=patient,
        now=datetime.now(UTC),
        catalog={
            "services": [
                {
                    "id": "service-1",
                    "name": "ليزر إبط",
                    "description": "وصف قديم من جدول الخدمات لا يجب استخدامه.",
                    "duration_minutes": 15,
                    "price_minor": 50000,
                    "currency": "EGP",
                }
            ],
            "branches": [
                {
                    "id": "branch-1",
                    "name": "العيادة",
                    "address_line1": "القاهرة",
                    "city": "Cairo",
                    "timezone": "Africa/Cairo",
                }
            ],
            "doctors": [],
            "appointments": [],
        },
    )


def test_semantic_context_never_exposes_legacy_service_description() -> None:
    context = build_semantic_context(
        {
            "services": [
                {
                    "id": "service-1",
                    "name": "ليزر إبط",
                    "category": "Laser",
                    "description": "legacy prose",
                }
            ],
            "doctors": [],
            "appointments": [],
        }
    )

    service = context.model_input["services"][0]
    assert service["name"] == "ليزر إبط"
    assert "description" not in service
    assert "legacy prose" not in str(context.model_input)


def test_operational_price_read_does_not_load_knowledge(monkeypatch) -> None:
    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("Pricing must not load explanatory clinic knowledge.")

    monkeypatch.setattr(read_executor, "relevant_knowledge_context", fail_if_called)
    step = PlanStep(
        operation_index=0,
        operation_type="pricing",
        disposition="read",
        reads=[ReadRequest(kind="service_catalog", parameters={"service_id": "service-1"})],
        response_goal="answer_price",
    )

    bundle = execute_step_reads(step, _read_context())

    service = bundle.results[0].payload["service"]
    assert service["price_minor"] == 50000
    assert "description" not in service
    assert "وصف قديم" not in str(service)


def test_service_explanation_uses_only_saved_clinic_knowledge(monkeypatch) -> None:
    monkeypatch.setattr(
        read_executor,
        "relevant_knowledge_context",
        lambda *_args, **_kwargs: {
            "ok": True,
            "source": "clinic_knowledge_text",
            "authority": "explanatory_only",
            "entries": [
                {
                    "scope_type": "clinic",
                    "title": "معلومات العيادة",
                    "content": "ده الشرح المحفوظ في معلومات Linka.",
                }
            ],
        },
    )
    context = _read_context()

    result = read_executor._read_service_catalog(
        ReadRequest(kind="service_catalog", parameters={"service_id": "service-1"}),
        context,
        include_explanation=True,
    )

    assert result.ok is True
    service = result.payload["service"]
    assert service["description"] == "ده الشرح المحفوظ في معلومات Linka."
    assert "وصف قديم" not in str(result.payload)


def test_clinic_info_includes_saved_knowledge_for_device_and_policy_questions(monkeypatch) -> None:
    monkeypatch.setattr(
        read_executor,
        "relevant_knowledge_context",
        lambda *_args, **_kwargs: {
            "entries": [
                {
                    "scope_type": "clinic",
                    "title": "معلومات العيادة",
                    "content": "Prime Lase وCandela Gentle متاحين حسب الخدمة وتقييم المختص.",
                }
            ]
        },
    )
    context = _read_context()

    result = read_executor._read_clinic_info(ReadRequest(kind="clinic_info"), context)

    assert result.ok is True
    assert result.payload["knowledge"] == (
        "Prime Lase وCandela Gentle متاحين حسب الخدمة وتقييم المختص."
    )
