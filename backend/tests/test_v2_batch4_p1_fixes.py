from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services.agent_v2 import orchestrator
from app.services.agent_v2.planner import (
    PlanStep,
    ReadRequest,
    VerificationFacts,
    WriteIntent,
)
from app.services.agent_v2.read_executor import (
    ReadExecutionBundle,
    ReadExecutionContext,
    ReadResult,
    execute_step_reads,
)

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)


def _cancel_step() -> PlanStep:
    return PlanStep(
        operation_index=0,
        operation_type="cancel_appointment",
        disposition="read",
        reads=[ReadRequest(kind="appointments")],
        write_intent=WriteIntent(
            kind="cancel_appointment",
            authorized=True,
            parameters={},
        ),
        response_goal="cancellation_completed",
    )


def _package_appointment_bundle() -> ReadExecutionBundle:
    appointment = {
        "appointment_id": "apt-package-1",
        "payment_status": "paid",
        "amount_paid_minor": 0,
        "billing_context": "package_prepaid",
        "patient_package_id": "pkg-1",
        "package_external_id": None,
    }
    return ReadExecutionBundle(
        results=[
            ReadResult(
                kind="appointments",
                ok=True,
                payload={"appointments": [appointment]},
            )
        ],
        verification=VerificationFacts(
            appointment_match_count=1,
            verified_parameters={"appointment_id": "apt-package-1"},
        ),
    )


def test_live_read_advance_applies_financial_cancellation_policy() -> None:
    advanced = orchestrator._advance_after_reads(
        _cancel_step(),
        _package_appointment_bundle(),
    )

    assert advanced.disposition == "handoff"
    assert advanced.response_goal == "handoff"
    assert advanced.facts["reason"] == "financial_cancellation_requires_staff"


def test_customer_package_read_excludes_receptionist_ledger_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = {
        "name": "Laser 6 Sessions",
        "sessions_purchased": 6,
        "sessions_reserved": 1,
        "sessions_consumed": 2,
        "sessions_remaining": 3,
        "laser_device_name": "Candela Gentle",
        "purchased_at": NOW.isoformat(),
        "expires_at": None,
        "status": "active",
        "effective_status": "active",
        "source": "manual",
        "sale_price_minor": 900_000,
        "amount_paid_minor": 300_000,
        "amount_refunded_minor": 0,
        "balance_due_minor": 600_000,
        "purchase_transaction_id": str(uuid4()),
        "cancellation_default_charge_minor": 300_000,
        "standalone_session_price_minor_at_purchase": 150_000,
    }
    captured: dict[str, object] = {}

    def model_dump(*, mode: str, include: set[str]) -> dict[str, object]:
        assert mode == "json"
        captured["include"] = set(include)
        return {key: raw[key] for key in include if key in raw}

    def list_packages(*_args: object, **kwargs: object) -> list[object]:
        captured["workspace_id"] = kwargs.get("workspace_id")
        captured["patient_id"] = kwargs.get("patient_id")
        captured["include_financials"] = kwargs.get("include_financials")
        return [SimpleNamespace(model_dump=model_dump)]

    monkeypatch.setattr(
        "app.services.agent_v2.read_executor.list_patient_packages",
        list_packages,
    )
    step = PlanStep(
        operation_index=0,
        operation_type="package_info",
        disposition="read",
        reads=[ReadRequest(kind="customer_packages")],
        response_goal="package_information",
    )
    workspace_id = uuid4()
    patient_id = uuid4()
    context = ReadExecutionContext(
        db=object(),
        workspace=SimpleNamespace(id=workspace_id),
        patient=SimpleNamespace(id=patient_id),
        now=NOW,
    )

    bundle = execute_step_reads(step, context)
    package = bundle.results[0].payload["packages"][0]

    assert captured["workspace_id"] == workspace_id
    assert captured["patient_id"] == patient_id
    assert captured["include_financials"] is False
    assert package["sessions_remaining"] == 3
    assert package["sessions_consumed"] == 2
    assert package["name"] == "Laser 6 Sessions"
    for field in {
        "sale_price_minor",
        "amount_paid_minor",
        "amount_refunded_minor",
        "balance_due_minor",
        "purchase_transaction_id",
        "cancellation_default_charge_minor",
        "standalone_session_price_minor_at_purchase",
    }:
        assert field not in package
