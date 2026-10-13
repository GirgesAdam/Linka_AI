from __future__ import annotations

from pathlib import Path

from app.services.agent_v2.planner import PlanStep, ReadRequest
from app.services.agent_v2.test_harness import V2FixtureEnvironment, execute_fixture_reads


def test_v2_fixture_availability_verifies_one_exact_slot_with_doctor() -> None:
    step = PlanStep(
        operation_index=0,
        operation_type="book",
        disposition="read",
        reads=[
            ReadRequest(
                kind="availability",
                parameters={
                    "service_id": "svc-underarm",
                    "doctor_id": "doc-maryam",
                    "date": {
                        "mode": "exact",
                        "start_date": "2026-09-12",
                        "end_date": None,
                    },
                    "time": {
                        "mode": "exact",
                        "start_time": "19:00",
                        "end_time": None,
                    },
                },
            )
        ],
        response_goal="present_availability",
    )
    bundle = execute_fixture_reads(step, V2FixtureEnvironment())

    assert bundle.verification.exact_slot_match_count == 1
    assert bundle.verification.verified_parameters["doctor_id"] == "doc-maryam"
    assert bundle.results[0].kind == "availability"
    assert len(bundle.results[0].payload["slots"]) == 1


def test_v2_fixture_exact_slot_without_doctor_stays_ambiguous() -> None:
    step = PlanStep(
        operation_index=0,
        operation_type="book",
        disposition="read",
        reads=[
            ReadRequest(
                kind="availability",
                parameters={
                    "service_id": "svc-underarm",
                    "date": {
                        "mode": "exact",
                        "start_date": "2026-09-12",
                        "end_date": None,
                    },
                    "time": {
                        "mode": "exact",
                        "start_time": "19:00",
                        "end_time": None,
                    },
                },
            )
        ],
        response_goal="present_availability",
    )
    bundle = execute_fixture_reads(step, V2FixtureEnvironment())

    assert bundle.verification.exact_slot_match_count == 2
    assert bundle.verification.verified_parameters == {}


def test_v2_fixture_package_offer_is_deterministically_verifiable() -> None:
    step = PlanStep(
        operation_index=0,
        operation_type="buy_package",
        disposition="read",
        reads=[
            ReadRequest(
                kind="package_offers",
                parameters={
                    "service_id": "svc-underarm",
                    "device_key": "candela_gentle",
                    "package_sessions": 6,
                },
            )
        ],
        response_goal="package_purchased",
    )
    bundle = execute_fixture_reads(step, V2FixtureEnvironment())

    assert bundle.verification.package_offer_match_count == 1
    assert bundle.verification.verified_parameters["package_sessions"] == 6


def test_v2_fixture_service_catalog_respects_allowed_device_keys() -> None:
    step = PlanStep(
        operation_index=0,
        operation_type="continue_active",
        disposition="read",
        reads=[
            ReadRequest(
                kind="service_catalog",
                parameters={
                    "service_id": "svc-underarm",
                    "device_keys": ["candela_gentle"],
                },
            )
        ],
        response_goal="answer_price",
    )
    bundle = execute_fixture_reads(step, V2FixtureEnvironment())

    devices = bundle.results[0].payload["service"]["laser_devices"]
    assert [device["device_key"] for device in devices] == ["candela_gentle"]


def test_v2_only_runner_has_no_v1_or_database_runtime_dependencies() -> None:
    backend = Path(__file__).resolve().parent.parent
    harness = (backend / "app/services/agent_v2/test_harness.py").read_text(encoding="utf-8")
    runner = (backend / "scripts/run_v2_only_chat.py").read_text(encoding="utf-8")
    source = f"{harness}\n{runner}".lower()

    forbidden = (
        "app.services.agent_chat",
        "app.agents.turn_interpreter",
        "app.agents.tia_customer_agent",
        "compose_grounded_customer_reply",
        "sqlalchemy",
        "get_clinic_adapter",
        "db.commit",
        "db.flush",
        "db.add",
        "book_appointment(",
        "cancel_appointment(",
        "reschedule_appointment(",
        "purchase_package_offer(",
    )
    for token in forbidden:
        assert token not in source
