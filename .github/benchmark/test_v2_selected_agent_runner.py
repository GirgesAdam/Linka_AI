import contextlib
import importlib.util
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).with_name("v2_selected_agent_runner.py")
SPEC = importlib.util.spec_from_file_location("v2_selected_agent_runner", MODULE_PATH)
runner = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(runner)

RM22_SUBCASES = [f"RM22-{letter}" for letter in "ABCDEF"]


class ScenarioSelectionTests(unittest.TestCase):
    def test_registry_has_exactly_rm01_through_rm29(self):
        self.assertEqual(tuple(runner.SCENARIOS), runner.ALLOWED_IDS)
        self.assertEqual(len(runner.SCENARIOS), 29)

    def test_rm22_registry_has_exactly_six_approved_subcases(self):
        self.assertEqual(list(runner.SUBCASES), RM22_SUBCASES)
        self.assertTrue(all(runner.SUBCASE_PARENTS[item] == "RM22" for item in RM22_SUBCASES))
        self.assertNotIn("RM22-G", runner.ALLOWED_SELECTION_IDS)

    def test_single_and_subset_selection(self):
        self.assertEqual(runner.parse_scenario_ids("RM01"), ["RM01"])
        self.assertEqual(runner.parse_scenario_ids("rm01, RM04", 2), ["RM01", "RM04"])

    def test_rm22_umbrella_expands_to_six_unique_subcases(self):
        self.assertEqual(runner.parse_scenario_ids("RM22"), RM22_SUBCASES)

    def test_rm22_subcases_can_be_selected_individually(self):
        for subcase in RM22_SUBCASES:
            with self.subTest(subcase=subcase):
                self.assertEqual(runner.parse_scenario_ids(subcase), [subcase])

    def test_unknown_id_and_unknown_rm22_variant_rejected(self):
        for raw, cap in (("RM01,RM30", 2), ("RM22-G", 1)):
            with self.subTest(raw=raw):
                with self.assertRaises(runner.SelectionError):
                    runner.parse_scenario_ids(raw, cap)

    def test_duplicate_explicit_id_rejected(self):
        with self.assertRaises(runner.SelectionError):
            runner.parse_scenario_ids("RM01,rm01", 2)

    def test_umbrella_plus_subcase_overlap_is_normalized_without_double_run(self):
        selected = runner.parse_scenario_ids("RM22,RM22-A", 2)
        self.assertEqual(selected, RM22_SUBCASES)
        self.assertEqual(selected.count("RM22-A"), 1)

    def test_selection_cap_enforced_on_explicit_selectors_before_expansion(self):
        with self.assertRaises(runner.SelectionError):
            runner.parse_scenario_ids("RM01,RM02", 1)
        self.assertEqual(runner.parse_scenario_ids("RM22", 1), RM22_SUBCASES)

    def test_empty_selection_rejected(self):
        with self.assertRaises(runner.SelectionError):
            runner.parse_scenario_ids("  ", 1)

    def test_validate_only_report_never_claims_execution(self):
        report = runner.make_report(["RM01", "RM18"], "validate_only", "abc123", "validation only")
        self.assertEqual(report["overall_status"], "NOT_RUN")
        self.assertEqual(report["real_model_executions"], 0)
        self.assertEqual(report["database_connections"], 0)
        self.assertEqual(report["database_writes"], 0)
        self.assertTrue(all(row["status"] == "NOT_RUN" for row in report["results"]))
        self.assertEqual(report["results"][0]["coverage_status"], "EXISTING_PROBE_PARTIAL")
        self.assertEqual(report["results"][1]["coverage_status"], "NEW_SCENARIO_REQUIRED")
        self.assertEqual(report["results"][0]["specification_status"], "SPEC_READY")
        self.assertEqual(report["results"][1]["specification_status"], "SPEC_READY")
        self.assertEqual(report["results"][0]["required_assertion_count"], 3)

    def test_rm22_report_is_six_independently_attributable_not_run_rows(self):
        selected = runner.parse_scenario_ids("RM22")
        report = runner.make_report(selected, "validate_only", "abc123", "validation only", requested_ids=["RM22"])
        self.assertEqual(report["requested_ids"], ["RM22"])
        self.assertEqual(report["selected_ids"], RM22_SUBCASES)
        self.assertEqual(report["selector_count_requested"], 1)
        self.assertEqual(report["scenario_count_requested"], 6)
        self.assertEqual([row["scenario_id"] for row in report["results"]], RM22_SUBCASES)
        self.assertTrue(all(row["coverage_id"] == "RM22" for row in report["results"]))
        self.assertTrue(all(row["subcase_id"] in RM22_SUBCASES for row in report["results"]))
        self.assertTrue(all(row["specification_status"] == "SPEC_READY" for row in report["results"]))
        self.assertTrue(all(row["status"] == "NOT_RUN" for row in report["results"]))
        self.assertEqual(report["real_model_executions"], 0)
        self.assertEqual(report["database_connections"], 0)
        self.assertEqual(report["database_writes"], 0)

    def test_rm22_f_report_keeps_real_execution_unauthorized(self):
        report = runner.make_report(["RM22-F"], "validate_only", "abc123", "validation only")
        row = report["results"][0]
        self.assertEqual(row["scenario_id"], "RM22-F")
        self.assertFalse(row["real_execution_authorized"])
        self.assertIn("real execution is not authorized", row["reason"])

    def test_validate_only_main_accepts_pulse_plus_non_pulse_subset_without_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            report_path = Path(tmp) / "subset.json"
            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                exit_code = runner.main([
                    "--scenario-ids", "RM22-A,RM04",
                    "--max-scenarios", "2",
                    "--mode", "validate_only",
                    "--report", str(report_path),
                ])
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(exit_code, 0)
            self.assertEqual(report["selected_ids"], ["RM22-A", "RM04"])
            self.assertEqual(report["real_model_executions"], 0)
            self.assertEqual(report["database_connections"], 0)
            self.assertEqual(report["database_writes"], 0)
            self.assertEqual(report["overall_status"], "NOT_RUN")
            self.assertEqual(stderr.getvalue(), "")

    def test_live_mode_refuses_before_model_or_database_access(self):
        names = ["LINKA_QA_LIVE_APPROVED", "LINKA_QA_EXTERNAL_SIDE_EFFECTS_DISABLED", "OPENAI_API_KEY", "LINKA_QA_DATABASE_URL", "LINKA_QA_EXPECTED_DATABASE_NAME", "LINKA_QA_EXPECTED_DATABASE_HOST"]
        saved = {name: os.environ.pop(name, None) for name in names}
        try:
            with tempfile.TemporaryDirectory() as tmp:
                report_path = Path(tmp) / "live-refused.json"
                stdout = io.StringIO()
                stderr = io.StringIO()
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    exit_code = runner.main(["--scenario-ids", "RM01", "--max-scenarios", "1", "--mode", "live", "--live-approved", "--report", str(report_path)])
                report = json.loads(report_path.read_text(encoding="utf-8"))
                self.assertEqual(exit_code, 3)
                self.assertEqual(report["real_model_executions"], 0)
                self.assertEqual(report["database_connections"], 0)
                self.assertEqual(report["database_writes"], 0)
                self.assertEqual(report["overall_status"], "NOT_RUN")
                self.assertEqual(report["results"][0]["status"], "NOT_RUN")
                self.assertIn("LIVE_REFUSED", stderr.getvalue())
        finally:
            for name, value in saved.items():
                if value is not None:
                    os.environ[name] = value

    def test_live_mode_stays_disabled_even_when_static_env_checks_pass(self):
        values = {
            "LINKA_QA_LIVE_APPROVED": "true",
            "LINKA_QA_EXTERNAL_SIDE_EFFECTS_DISABLED": "true",
            "OPENAI_API_KEY": "test-only-placeholder",
            "LINKA_QA_DATABASE_URL": "postgresql://qa_user:placeholder@qa-db.example.invalid/linka_qa_test",
            "LINKA_QA_EXPECTED_DATABASE_NAME": "linka_qa_test",
            "LINKA_QA_EXPECTED_DATABASE_HOST": "qa-db.example.invalid",
        }
        saved = {name: os.environ.get(name) for name in values}
        os.environ.update(values)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                report_path = Path(tmp) / "live-still-disabled.json"
                stdout = io.StringIO()
                stderr = io.StringIO()
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    exit_code = runner.main(["--scenario-ids", "RM22", "--max-scenarios", "1", "--mode", "live", "--live-approved", "--report", str(report_path)])
                report = json.loads(report_path.read_text(encoding="utf-8"))
                self.assertEqual(exit_code, 3)
                self.assertIn("isolation have not been proven", stderr.getvalue())
                self.assertEqual(report["selected_ids"], RM22_SUBCASES)
                self.assertEqual(report["real_model_executions"], 0)
                self.assertEqual(report["database_connections"], 0)
                self.assertEqual(report["database_writes"], 0)
                self.assertEqual(report["overall_status"], "NOT_RUN")
        finally:
            for name, value in saved.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

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
