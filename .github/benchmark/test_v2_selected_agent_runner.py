import importlib.util
import os
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).with_name("v2_selected_agent_runner.py")
SPEC = importlib.util.spec_from_file_location("v2_selected_agent_runner", MODULE_PATH)
runner = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(runner)


class ScenarioSelectionTests(unittest.TestCase):
    def test_registry_has_exactly_rm01_through_rm29(self):
        self.assertEqual(tuple(runner.SCENARIOS), runner.ALLOWED_IDS)
        self.assertEqual(len(runner.SCENARIOS), 29)

    def test_single_and_subset_selection(self):
        self.assertEqual(runner.parse_scenario_ids("RM01"), ["RM01"])
        self.assertEqual(runner.parse_scenario_ids("rm01, RM04", 2), ["RM01", "RM04"])

    def test_unknown_id_rejected(self):
        with self.assertRaises(runner.SelectionError):
            runner.parse_scenario_ids("RM01,RM30", 2)

    def test_duplicate_id_rejected(self):
        with self.assertRaises(runner.SelectionError):
            runner.parse_scenario_ids("RM01,rm01", 2)

    def test_selection_cap_enforced(self):
        with self.assertRaises(runner.SelectionError):
            runner.parse_scenario_ids("RM01,RM02", 1)

    def test_empty_selection_rejected(self):
        with self.assertRaises(runner.SelectionError):
            runner.parse_scenario_ids("  ", 1)

    def test_validate_only_report_never_claims_execution(self):
        report = runner.make_report(["RM01", "RM18"], "validate_only", "abc123", "validation only")
        self.assertEqual(report["overall_status"], "NOT_RUN")
        self.assertEqual(report["real_model_executions"], 0)
        self.assertEqual(report["database_writes"], 0)
        self.assertTrue(all(row["status"] == "NOT_RUN" for row in report["results"]))
        self.assertEqual(report["results"][0]["coverage_status"], "scenario_definition_missing")
        self.assertEqual(report["results"][1]["coverage_status"], "scenario_definition_missing")

    def test_live_environment_fails_closed_without_explicit_approval(self):
        names = ["LINKA_QA_LIVE_APPROVED", "LINKA_QA_EXTERNAL_SIDE_EFFECTS_DISABLED", "OPENAI_API_KEY", "LINKA_QA_DATABASE_URL", "LINKA_QA_EXPECTED_DATABASE_NAME", "LINKA_QA_EXPECTED_DATABASE_HOST"]
        saved = {name: os.environ.pop(name, None) for name in names}
        try:
            ok, message = runner.validate_live_environment()
            self.assertFalse(ok)
            self.assertIn("LIVE_APPROVED", message)
        finally:
            for name, value in saved.items():
                if value is not None:
                    os.environ[name] = value

    def test_live_environment_rejects_non_qa_database_name(self):
        names = {
            "LINKA_QA_LIVE_APPROVED": "true",
            "LINKA_QA_EXTERNAL_SIDE_EFFECTS_DISABLED": "true",
            "OPENAI_API_KEY": "test-only-placeholder",
            "LINKA_QA_DATABASE_URL": "postgresql://user:placeholder@db.test.internal/production",
            "LINKA_QA_EXPECTED_DATABASE_NAME": "production",
            "LINKA_QA_EXPECTED_DATABASE_HOST": "db.test.internal",
        }
        saved = {name: os.environ.get(name) for name in names}
        os.environ.update(names)
        try:
            ok, message = runner.validate_live_environment()
            self.assertFalse(ok)
            self.assertIn("QA/test/sandbox", message)
        finally:
            for name, value in saved.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

    def test_live_environment_requires_side_effects_disabled(self):
        names = {
            "LINKA_QA_LIVE_APPROVED": "true",
            "LINKA_QA_EXTERNAL_SIDE_EFFECTS_DISABLED": "false",
            "OPENAI_API_KEY": "test-only-placeholder",
            "LINKA_QA_DATABASE_URL": "postgresql://user:placeholder@db.test.internal/linka_qa_test",
            "LINKA_QA_EXPECTED_DATABASE_NAME": "linka_qa_test",
            "LINKA_QA_EXPECTED_DATABASE_HOST": "db.test.internal",
        }
        saved = {name: os.environ.get(name) for name in names}
        os.environ.update(names)
        try:
            ok, message = runner.validate_live_environment()
            self.assertFalse(ok)
            self.assertIn("side effects", message)
        finally:
            for name, value in saved.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value


if __name__ == "__main__":
    unittest.main()
