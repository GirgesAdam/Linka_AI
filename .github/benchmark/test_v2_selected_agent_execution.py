from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import v2_selected_agent_execution as execution
import v2_selected_agent_runner as runner

DOCUMENT = json.loads((Path(__file__).with_name("v2_rm_scenario_contracts.json")).read_text(encoding="utf-8"))


def full_live_env() -> dict[str, str]:
    url = "postgresql://qa_user:placeholder@qa-db.example.invalid/linka_qa_test"
    return {
        "LINKA_QA_LIVE_APPROVED": "true",
        "LINKA_QA_EXTERNAL_SIDE_EFFECTS_DISABLED": "true",
        "OPENAI_API_KEY": "test-only-placeholder",
        "AGENT_V2_LIVE_ENABLED": "true",
        "ENVIRONMENT": "qa",
        "LINKA_QA_DATABASE_URL": url,
        "DATABASE_URL": url,
        "MIGRATION_DATABASE_URL": url,
        "LINKA_QA_EXPECTED_DATABASE_NAME": "linka_qa_test",
        "LINKA_QA_EXPECTED_DATABASE_HOST": "qa-db.example.invalid",
        "LINKA_QA_RUNTIME_MARKER": "selected-agent-test-marker",
        "LINKA_QA_EXPECTED_WORKSPACE_ID": "00000000-0000-0000-0000-000000000022",
        "LINKA_QA_EXPECTED_WORKSPACE_SLUG": "qa-agent-v2",
    }


def raw_stub(*, write: bool = False, secret: bool = False) -> dict:
    turn = {
        "customer": "customer message",
        "assistant": "assistant reply",
        "model": "stub-model",
        "duration_ms": 12,
        "token_usage": {
            "input_tokens": 5,
            "output_tokens": 3,
            "total_tokens": 8,
            "calls": [{"model": "stub-model", "input_tokens": 5, "output_tokens": 3, "total_tokens": 8}],
        },
        "understanding": {"operations": [{"type": "availability"}]},
        "plan": {"steps": [{"disposition": "read"}]},
        "traces": [{"read_kinds": ["availability"], "verified_parameters": {"service_id": "canonical"}}],
        "outcomes": [{"status": "completed", "facts": {"availability": []}}],
        "active_task": {"task_type": "booking", "requested_service_ids": ["canonical"]},
        "persisted_task": {"flow_version": 2},
        "pending_write": None,
        "verified_action_context": {"candidate_mode": "verified"},
        "pending_choice": {"options": []},
        "reference_semantic_path_used": False,
        "full_interpreter_called": True,
        "reference_action": None,
        "selected_option_ref": None,
        "reference_structured_output_error": False,
        "state_after": {"appointments": 1 if write else 0},
    }
    if secret:
        turn["traces"].append({
            "api_key": "sk-THISMUSTNOTAPPEAR12345",
            "database_url": "postgresql://user:password@host/db",
            "authorization": "Bearer abcdefghijklmnop",
        })
    return {
        "turns": [turn],
        "db_before": {"appointments": 0, "payment_transactions": 0},
        "db_after": {"appointments": 1 if write else 0, "payment_transactions": 0},
        "db_delta": {"appointments": 1 if write else 0, "payment_transactions": 0},
        "observations": ["raw evidence"],
        "runtime_error": None,
    }


class FakeEngine:
    def __init__(self):
        self.disposed = False

    def dispose(self):
        self.disposed = True


class SelectedAgentExecutionTests(unittest.TestCase):
    def test_current_live_safe_subset_is_explicitly_small_and_read_only(self):
        safe = []
        blocked = []
        execution_ids = [item for item in runner.ALLOWED_IDS if item != "RM22"] + list(runner.SUBCASES)
        for execution_id in execution_ids:
            readiness = execution.execution_readiness(DOCUMENT, execution_id)
            (safe if readiness["safe_to_execute"] else blocked).append(execution_id)
        self.assertEqual(safe, ["RM04", "RM06", "RM11", "RM20", "RM29"])
        self.assertEqual(len(safe), 5)
        self.assertEqual(len(blocked), 29)
        for execution_id in safe:
            self.assertEqual(execution.execution_readiness(DOCUMENT, execution_id)["policy"], "read_only")

    def test_read_only_scenario_executes_through_stub_and_preserves_raw_evidence(self):
        artifact = execution.execute_with_backend(
            document=DOCUMENT,
            execution_id="RM04",
            git_sha="abc123",
            backend=lambda _execution_id, _source: raw_stub(),
            environment_evidence={"rollback": {"rollback_verified": True}},
        )
        self.assertEqual(artifact["execution_status"], "COMPLETED")
        self.assertEqual(artifact["coverage_id"], "RM04")
        self.assertEqual(artifact["source_scenario"], "nearest_available")
        self.assertEqual(artifact["customer_messages"], ["customer message"])
        turn = artifact["turns"][0]
        for field in ("understanding", "plan", "traces", "outcomes", "assistant", "active_task", "pending_write"):
            self.assertIn(field, turn)
        self.assertEqual(artifact["db_delta"]["appointments"], 0)
        self.assertEqual(artifact["token_usage"]["call_count"], 1)
        self.assertNotIn("semantic_result", artifact)

    def test_write_scenario_can_use_same_capture_contract_with_stub_but_is_not_live_authorized(self):
        readiness = execution.execution_readiness(DOCUMENT, "RM09")
        self.assertFalse(readiness["safe_to_execute"])
        self.assertEqual(readiness["policy"], "write_after_explicit_authorization")
        artifact = execution.execute_with_backend(
            document=DOCUMENT,
            execution_id="RM09",
            git_sha="abc123",
            backend=lambda _execution_id, _source: raw_stub(write=True),
            enforce_live_readiness=False,
        )
        self.assertEqual(artifact["execution_status"], "COMPLETED")
        self.assertEqual(artifact["db_before"]["appointments"], 0)
        self.assertEqual(artifact["db_after"]["appointments"], 1)
        self.assertEqual(artifact["db_delta"]["appointments"], 1)

    def test_secret_redaction_preserves_evidence_shape_without_credentials(self):
        artifact = execution.execute_with_backend(
            document=DOCUMENT,
            execution_id="RM04",
            git_sha="abc123",
            backend=lambda _execution_id, _source: raw_stub(secret=True),
        )
        encoded = json.dumps(artifact)
        self.assertNotIn("THISMUSTNOTAPPEAR", encoded)
        self.assertNotIn("user:password", encoded)
        self.assertNotIn("abcdefghijklmnop", encoded)
        self.assertIn("[REDACTED]", encoded)
        self.assertIn("understanding", artifact["turns"][0])
        self.assertIn("traces", artifact["turns"][0])

    def test_existing_benchmark_capture_contains_required_raw_state_and_business_counters(self):
        source = (Path(__file__).with_name("v2_benchmark_core.py")).read_text(encoding="utf-8")
        for field in (
            "understanding", "plan", "traces", "outcomes", "active_task", "persisted_task",
            "pending_write", "verified_action_context", "pending_choice", "selected_option_ref",
        ):
            with self.subTest(field=field):
                self.assertIn(field, source)
        for counter in (
            "appointments", "patient_packages", "package_usages", "patient_pulse_packs",
            "pulse_usages", "appointment_pulse_settlements", "payment_transactions",
            "crm_tasks", "handoff_requests", "activity_events", "patient_marketing_consent",
        ):
            with self.subTest(counter=counter):
                self.assertIn(f'"{counter}"', source)
        self.assertIn('"visit_group_id"', source)

    def test_external_effect_isolation_proof_matches_direct_agent_api_path(self):
        evidence = execution.verify_external_effect_isolation()
        self.assertTrue(evidence["benchmark_uses_direct_agent_api"])
        self.assertTrue(evidence["direct_api_outbound_not_queued"])
        self.assertTrue(evidence["dispatch_flag_depends_on_queue"])
        self.assertFalse(evidence["write_execution_currently_allowed"])

    def test_live_safe_read_only_path_is_structurally_executable_with_stub_only(self):
        values = full_live_env()
        saved = {name: os.environ.get(name) for name in values}
        os.environ.update(values)
        engine = FakeEngine()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                report_path = Path(tmp) / "live-stub.json"
                with (
                    mock.patch.object(runner.execution, "verify_external_effect_isolation", return_value={"dispatch": "disabled"}),
                    mock.patch.object(runner.execution, "verify_runtime_database_identity", return_value=(engine, {"database_name": "linka_qa_test", "qa_marker_verified": True})),
                    mock.patch.object(runner.execution, "verify_outer_transaction_rollback", return_value={"rollback_verified": True, "persisted_after_outer_rollback": False}),
                    mock.patch.object(runner.execution, "execute_real_agent_probe", return_value=raw_stub()),
                    contextlib.redirect_stdout(io.StringIO()),
                    contextlib.redirect_stderr(io.StringIO()),
                ):
                    exit_code = runner.main([
                        "--scenario-ids", "RM04", "--max-scenarios", "1",
                        "--mode", "live", "--live-approved", "--report", str(report_path),
                    ])
                report = json.loads(report_path.read_text(encoding="utf-8"))
                self.assertEqual(exit_code, 0)
                self.assertEqual(report["overall_status"], "COMPLETED")
                self.assertEqual(report["real_model_executions"], 1)
                self.assertEqual(report["results"][0]["execution_status"], "COMPLETED")
                self.assertEqual(report["results"][0]["environment_evidence"]["rollback"]["rollback_verified"], True)
                self.assertTrue(engine.disposed)
        finally:
            for name, value in saved.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

    def test_live_write_contract_refuses_before_database_or_model(self):
        values = full_live_env()
        saved = {name: os.environ.get(name) for name in values}
        os.environ.update(values)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                report_path = Path(tmp) / "write-refused.json"
                with (
                    mock.patch.object(runner.execution, "verify_runtime_database_identity") as db_check,
                    mock.patch.object(runner.execution, "execute_real_agent_probe") as real_probe,
                    contextlib.redirect_stdout(io.StringIO()),
                    contextlib.redirect_stderr(io.StringIO()),
                ):
                    exit_code = runner.main([
                        "--scenario-ids", "RM09", "--max-scenarios", "1",
                        "--mode", "live", "--live-approved", "--report", str(report_path),
                    ])
                report = json.loads(report_path.read_text(encoding="utf-8"))
                self.assertEqual(exit_code, 3)
                self.assertEqual(report["overall_status"], "LIVE_REFUSED")
                self.assertEqual(report["real_model_executions"], 0)
                self.assertEqual(report["database_connections"], 0)
                db_check.assert_not_called()
                real_probe.assert_not_called()
        finally:
            for name, value in saved.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value


if __name__ == "__main__":
    unittest.main()
