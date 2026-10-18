from __future__ import annotations

import ast
import json
import re
import unittest
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONTRACT_PATH = ROOT / ".github" / "benchmark" / "v2_rm_scenario_contracts.json"
DOCUMENT = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
CONTRACTS = DOCUMENT["contracts"]
EXPECTED_IDS = [f"RM{i:02d}" for i in range(1, 30)]


def assembled_benchmark_tree() -> ast.Module:
    parts = sorted((ROOT / ".github" / "benchmark").glob("v2_benchmark_scenarios.part.*"))
    source = b"".join(path.read_bytes() for path in parts).decode("utf-8-sig")
    return ast.parse(source)


def current_benchmark_scenarios() -> list[str]:
    tree = assembled_benchmark_tree()
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "TOPICS"
            for target in node.targets
        ):
            topics = ast.literal_eval(node.value)
            return list(topics)
    raise AssertionError("TOPICS registry was not found in the assembled benchmark scenario source.")


def current_benchmark_handlers() -> set[str]:
    return {node.name for node in assembled_benchmark_tree().body if isinstance(node, ast.FunctionDef)}


class CanonicalScenarioContractTests(unittest.TestCase):
    def test_registry_has_exactly_rm01_through_rm29(self):
        self.assertEqual([item["scenario_id"] for item in CONTRACTS], EXPECTED_IDS)
        self.assertEqual(len({item["scenario_id"] for item in CONTRACTS}), 29)

    def test_every_existing_scenario_name_has_a_real_handler_function(self):
        handlers = current_benchmark_handlers()
        for scenario_name in current_benchmark_scenarios():
            with self.subTest(scenario_name=scenario_name):
                self.assertIn(f"_{scenario_name}", handlers)

    def test_all_26_existing_scenario_names_are_accounted_for_exactly_once(self):
        current = current_benchmark_scenarios()
        audited = [item["scenario_name"] for item in DOCUMENT["benchmark_scenario_audit"]]
        self.assertEqual(len(current), 26)
        self.assertEqual(set(audited), set(current))
        self.assertEqual(len(audited), len(set(audited)))
        self.assertEqual(DOCUMENT["current_benchmark_scenario_names"], current)

    def test_every_source_mapping_points_to_a_real_benchmark_scenario(self):
        valid = set(current_benchmark_scenarios())
        audit = {item["scenario_name"]: item for item in DOCUMENT["benchmark_scenario_audit"]}
        contract_source_map = defaultdict(set)
        for contract in CONTRACTS:
            with self.subTest(scenario_id=contract["scenario_id"]):
                self.assertTrue(set(contract["source_scenarios"]).issubset(valid))
                for source in contract["source_scenarios"]:
                    contract_source_map[source].add(contract["scenario_id"])
                    self.assertIn(contract["scenario_id"], audit[source]["mapped_rm_ids"])
        for source, item in audit.items():
            with self.subTest(source_scenario=source):
                self.assertEqual(set(item["mapped_rm_ids"]), contract_source_map[source])

    def test_related_code_and_test_references_exist(self):
        for contract in CONTRACTS:
            for reference in contract["related_code"]:
                path = reference.split("::", 1)[0]
                with self.subTest(scenario_id=contract["scenario_id"], code_reference=reference):
                    self.assertTrue((ROOT / path).is_file())
            for reference in contract["related_tests"]:
                path, separator, test_name = reference.partition("::")
                file_path = ROOT / path
                with self.subTest(scenario_id=contract["scenario_id"], test_reference=reference):
                    self.assertTrue(file_path.is_file())
                    if separator:
                        source = file_path.read_text(encoding="utf-8", errors="replace")
                        pattern = r"^\s*(?:async\s+)?def\s+" + re.escape(test_name) + r"\s*\("
                        self.assertIsNotNone(re.search(pattern, source, flags=re.MULTILINE), msg=reference)

    def test_every_contract_has_a_clear_goal_and_required_fields(self):
        required = {
            "scenario_id", "scenario_name", "capability_under_test", "source_scenarios",
            "preconditions", "customer_messages", "expected_behavior", "required_assertions",
            "forbidden_outcomes", "expected_database_delta", "expected_read_write_policy",
            "evidence_requirements", "specification_status",
        }
        for contract in CONTRACTS:
            with self.subTest(scenario_id=contract["scenario_id"]):
                self.assertTrue(required.issubset(contract))
                self.assertTrue(contract["scenario_name"].strip())
                self.assertTrue(contract["capability_under_test"].strip())
                self.assertIsInstance(contract["expected_database_delta"], dict)
                self.assertIn(contract["expected_read_write_policy"]["mode"],
                              {"read_only", "write_after_explicit_authorization", "handoff_only", "blocked"})

    def test_ready_contracts_have_messages_preconditions_assertions_and_evidence(self):
        for contract in CONTRACTS:
            if contract["specification_status"] != "SPEC_READY":
                continue
            with self.subTest(scenario_id=contract["scenario_id"]):
                self.assertTrue(contract["preconditions"])
                self.assertTrue(contract["customer_messages"])
                self.assertTrue(contract["expected_behavior"])
                self.assertTrue(contract["required_assertions"])
                self.assertTrue(contract["evidence_requirements"])
                self.assertTrue(contract["forbidden_outcomes"])
                for assertion in contract["required_assertions"]:
                    self.assertTrue(assertion["assertion"].strip())
                    self.assertTrue(assertion["evidence_source"].strip())
                    self.assertIn("available_in_current_benchmark_artifact", assertion)
                    if not assertion["available_in_current_benchmark_artifact"]:
                        self.assertTrue(assertion.get("capture_gap"))
                self.assertEqual(
                    [item["assertion_id"] for item in contract["required_assertions"]],
                    [item["assertion_id"] for item in contract["evidence_requirements"]],
                )

    def test_variants_have_independent_fixture_inputs_and_expected_behavior(self):
        for contract in CONTRACTS:
            variants = contract["variants"]
            ids = [variant["variant_id"] for variant in variants]
            with self.subTest(scenario_id=contract["scenario_id"]):
                self.assertEqual(len(ids), len(set(ids)))
                for variant in variants:
                    self.assertTrue(variant["preconditions"])
                    self.assertTrue(variant["customer_messages"])
                    self.assertTrue(variant["expected_behavior"])

    def test_spec_blocked_is_not_treated_as_complete_or_executable(self):
        blocked = [item for item in CONTRACTS if item["specification_status"] == "SPEC_BLOCKED"]
        self.assertEqual([item["scenario_id"] for item in blocked], ["RM22"])
        for item in blocked:
            self.assertIsNone(item["execution_group"])
            self.assertTrue(item["open_gaps"])
            self.assertEqual(item["expected_read_write_policy"]["mode"], "blocked")
        self.assertEqual(DOCUMENT["proposed_deduplication"]["blocked_rm_ids"], ["RM22"])

    def test_proposed_execution_groups_cover_every_ready_rm_once_and_are_fewer_than_29(self):
        plan = DOCUMENT["proposed_deduplication"]
        groups = plan["groups"]
        grouped_ids = [scenario_id for group in groups for scenario_id in group["covers_rm_ids"]]
        ready_ids = [item["scenario_id"] for item in CONTRACTS if item["specification_status"] == "SPEC_READY"]
        self.assertEqual(sorted(grouped_ids), sorted(ready_ids))
        self.assertEqual(len(grouped_ids), len(set(grouped_ids)))
        self.assertEqual(plan["proposed_execution_template_count"], 24)
        self.assertLess(len(groups), 29)
        self.assertEqual(plan["status"], "PROPOSAL_ONLY_NOT_ACTIVATED")
        self.assertFalse(plan["existing_26_registry_changed"])

    def test_duplicate_goal_keys_cannot_be_split_across_execution_groups(self):
        grouped = defaultdict(list)
        for item in CONTRACTS:
            grouped[item["deduplication_key"]].append(item)
        for key, items in grouped.items():
            if len(items) > 1:
                with self.subTest(deduplication_key=key):
                    self.assertEqual(len({item["execution_group"] for item in items}), 1)
                    self.assertTrue(all(item["deduplication_rationale"].strip() for item in items))

    def test_current_benchmark_scenarios_are_not_misreported_as_semantic_pass_fail(self):
        self.assertFalse(DOCUMENT["assertion_model"]["existing_benchmark_semantic_assertions_present"])
        for item in DOCUMENT["benchmark_scenario_audit"]:
            self.assertFalse(item["scenario_level_semantic_assertions_present"])
        for item in CONTRACTS:
            if item["specification_status"] == "SPEC_READY":
                self.assertEqual(item["assertion_implementation_status"], "SPECIFIED_NOT_WIRED_TO_LIVE_RUNNER")
                self.assertIn("BLOCKED_UNTIL_EVIDENCE_CAPTURE", item["execution_readiness"])

    def test_no_live_model_or_database_execution_is_authorized_by_spec(self):
        model = DOCUMENT["assertion_model"]
        self.assertFalse(model["live_execution_authorized"])
        self.assertEqual(model["real_model_executions_during_specification"], 0)
        self.assertEqual(model["database_connections_during_specification"], 0)
        self.assertEqual(model["database_writes_during_specification"], 0)

    def test_read_only_contracts_expect_zero_business_row_deltas(self):
        for item in CONTRACTS:
            if item["expected_read_write_policy"]["mode"] != "read_only":
                continue
            delta = item["expected_database_delta"]
            with self.subTest(scenario_id=item["scenario_id"]):
                for table in ("appointments", "patient_packages", "package_usages", "payments", "crm_tasks"):
                    self.assertEqual(delta[table]["count_delta"], 0)
                self.assertIn("unchanged", str(delta["patient_marketing_consent"]))

    def test_write_contracts_define_an_explicit_allowed_mutation(self):
        for item in CONTRACTS:
            if item["expected_read_write_policy"]["mode"] == "write_after_explicit_authorization":
                with self.subTest(scenario_id=item["scenario_id"]):
                    self.assertTrue(item["expected_read_write_policy"]["authorization_boundary"])
                    self.assertTrue(item["expected_read_write_policy"]["allowed_mutations"])


if __name__ == "__main__":
    unittest.main()
