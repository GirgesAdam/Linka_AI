from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import guard


def pr(number: int, branch: str, sha: str, *, state: str = "open", draft: bool = True, merged: bool = False):
    return {
        "number": number,
        "state": state,
        "draft": draft,
        "merged": merged,
        "base": {"ref": "main" if number == guard.QA_HARNESS_PR else "refactor/spoken-clock-contract-backend-authority-20261014"},
        "user": {"login": "pr-author"},
        "head": {
            "ref": branch,
            "sha": sha,
            "repo": {"full_name": guard.REPOSITORY},
        },
    }


def approved_review(login: str, sha: str):
    return [{
        "user": {"login": login},
        "state": "APPROVED",
        "commit_id": sha,
        "submitted_at": "2026-10-18T12:00:00Z",
    }]


class TrustedDispatchGuardTests(unittest.TestCase):
    def setUp(self):
        self.qa_pr = pr(guard.QA_HARNESS_PR, guard.QA_HARNESS_BRANCH, guard.QA_HARNESS_SHA)
        self.agent_pr = pr(guard.AGENT_PR, guard.AGENT_BRANCH, guard.AGENT_SHA)

    def validate_inputs(self, **overrides):
        values = {
            "workflow_ref": "refs/heads/main",
            "mode": "validate_only",
            "qa_harness_sha": guard.QA_HARNESS_SHA,
            "agent_under_test_sha": guard.AGENT_SHA,
            "scenario_ids": "RM04,RM06,RM20,RM29",
            "confirm_live": False,
        }
        values.update(overrides)
        return guard.validate_inputs(**values)

    def validate_prs(self, **overrides):
        values = {
            "mode": "validate_only",
            "qa_harness_sha": guard.QA_HARNESS_SHA,
            "agent_under_test_sha": guard.AGENT_SHA,
            "qa_pr": self.qa_pr,
            "agent_pr": self.agent_pr,
            "qa_reviews": [],
            "agent_reviews": [],
        }
        values.update(overrides)
        return guard.validate_pr_metadata(**values)

    def test_approved_read_only_batch_is_selected_in_requested_order(self):
        self.assertEqual(self.validate_inputs(), ["RM04", "RM06", "RM20", "RM29"])

    def test_wrong_qa_sha_is_rejected(self):
        with self.assertRaisesRegex(guard.DispatchRejected, "QA harness SHA"):
            self.validate_inputs(qa_harness_sha="0" * 40)

    def test_wrong_agent_sha_is_rejected(self):
        with self.assertRaisesRegex(guard.DispatchRejected, "Agent-under-test SHA"):
            self.validate_inputs(agent_under_test_sha="1" * 40)

    def test_non_main_dispatch_ref_is_rejected(self):
        with self.assertRaisesRegex(guard.DispatchRejected, "refs/heads/main"):
            self.validate_inputs(workflow_ref="refs/heads/feature/untrusted")

    def test_stale_qa_pr_head_is_rejected(self):
        stale_qa = dict(self.qa_pr)
        stale_qa["head"] = dict(self.qa_pr["head"], sha="5" * 40)
        with self.assertRaisesRegex(guard.DispatchRejected, "stale"):
            self.validate_prs(qa_pr=stale_qa)

    def test_stale_agent_pr_head_is_rejected(self):
        stale_agent = dict(self.agent_pr)
        stale_agent["head"] = dict(self.agent_pr["head"], sha="2" * 40)
        with self.assertRaisesRegex(guard.DispatchRejected, "stale"):
            self.validate_prs(agent_pr=stale_agent)

    def test_wrong_agent_branch_is_rejected(self):
        wrong_branch = dict(self.agent_pr)
        wrong_branch["head"] = dict(self.agent_pr["head"], ref="other-branch")
        with self.assertRaisesRegex(guard.DispatchRejected, "branch"):
            self.validate_prs(agent_pr=wrong_branch)

    def test_closed_agent_pr_is_rejected(self):
        with self.assertRaisesRegex(guard.DispatchRejected, "must remain open"):
            self.validate_prs(agent_pr=dict(self.agent_pr, state="closed"))

    def test_qa_pr_must_target_main(self):
        wrong_base = dict(self.qa_pr, base={"ref": "other-branch"})
        with self.assertRaisesRegex(guard.DispatchRejected, "target main"):
            self.validate_prs(qa_pr=wrong_base)

    def test_merged_qa_harness_pr_is_still_valid_at_its_pinned_head(self):
        merged = dict(self.qa_pr, state="closed", merged=True, draft=False)
        merged["head"] = dict(self.qa_pr["head"], repo=None)
        self.validate_prs(qa_pr=merged)

    def test_closed_unmerged_qa_pr_is_rejected(self):
        with self.assertRaisesRegex(guard.DispatchRejected, "open or already merged"):
            self.validate_prs(qa_pr=dict(self.qa_pr, state="closed", merged=False))

    def test_validate_only_may_inspect_open_draft_prs_without_secrets(self):
        self.validate_prs()

    def test_live_mode_rejects_draft_or_unapproved_prs(self):
        with self.assertRaisesRegex(guard.DispatchRejected, "out of Draft"):
            self.validate_prs(mode="live")

    def test_live_mode_requires_current_human_approval_for_both_exact_heads(self):
        self.validate_prs(
            mode="live",
            qa_pr=dict(self.qa_pr, draft=False),
            agent_pr=dict(self.agent_pr, draft=False),
            qa_reviews=approved_review("qa-reviewer", guard.QA_HARNESS_SHA),
            agent_reviews=approved_review("agent-reviewer", guard.AGENT_SHA),
        )

    def test_stale_review_approval_does_not_authorize_current_head(self):
        with self.assertRaisesRegex(guard.DispatchRejected, "exact head SHA"):
            self.validate_prs(
                mode="live",
                qa_pr=dict(self.qa_pr, draft=False),
                agent_pr=dict(self.agent_pr, draft=False),
                qa_reviews=approved_review("qa-reviewer", guard.QA_HARNESS_SHA),
                agent_reviews=approved_review("agent-reviewer", "3" * 40),
            )

    def test_latest_changes_requested_review_blocks_live_dispatch(self):
        reviews = approved_review("agent-reviewer", guard.AGENT_SHA) + [{
            "user": {"login": "agent-reviewer"},
            "state": "CHANGES_REQUESTED",
            "commit_id": guard.AGENT_SHA,
            "submitted_at": "2026-10-18T13:00:00Z",
        }]
        with self.assertRaisesRegex(guard.DispatchRejected, "exact head SHA"):
            self.validate_prs(
                mode="live",
                qa_pr=dict(self.qa_pr, draft=False),
                agent_pr=dict(self.agent_pr, draft=False),
                qa_reviews=approved_review("qa-reviewer", guard.QA_HARNESS_SHA),
                agent_reviews=reviews,
            )

    def test_unknown_scenario_is_rejected(self):
        with self.assertRaisesRegex(guard.DispatchRejected, "not authorized"):
            self.validate_inputs(scenario_ids="RM30")

    def test_rm11_is_explicitly_excluded(self):
        with self.assertRaisesRegex(guard.DispatchRejected, "RM11"):
            self.validate_inputs(scenario_ids="RM11")

    def test_unauthorized_write_case_is_rejected(self):
        with self.assertRaisesRegex(guard.DispatchRejected, "RM09"):
            self.validate_inputs(scenario_ids="RM09")

    def test_duplicate_scenario_is_rejected(self):
        with self.assertRaisesRegex(guard.DispatchRejected, "Duplicate"):
            self.validate_inputs(scenario_ids="RM04,RM04")

    def test_live_mode_requires_explicit_confirmation(self):
        with self.assertRaisesRegex(guard.DispatchRejected, "confirm_live=true"):
            self.validate_inputs(mode="live", confirm_live=False)

    def test_validate_only_rejects_irrelevant_live_confirmation(self):
        with self.assertRaisesRegex(guard.DispatchRejected, "only valid with mode=live"):
            self.validate_inputs(mode="validate_only", confirm_live=True)

    def test_workflow_defaults_match_pinned_sha_approvals(self):
        workflow = (Path(__file__).resolve().parents[1] / "workflows" / "qa-real-agent-dispatch.yml").read_text(encoding="utf-8")
        self.assertIn(f"default: {guard.QA_HARNESS_SHA}", workflow)
        self.assertIn(f"default: {guard.AGENT_SHA}", workflow)
        self.assertIn("default: RM04,RM06,RM20,RM29", workflow)

    def test_workflow_uses_protected_environment_without_pull_request_target(self):
        workflow = (Path(__file__).resolve().parents[1] / "workflows" / "qa-real-agent-dispatch.yml").read_text(encoding="utf-8")
        self.assertIn("environment:" + chr(10) + "      name: agent-qa", workflow)
        self.assertNotIn("pull_request_target", workflow)

    def test_live_job_requires_environment_protection_attestation(self):
        workflow = (Path(__file__).resolve().parents[1] / "workflows" / "qa-real-agent-dispatch.yml").read_text(encoding="utf-8")
        self.assertIn("LINKA_QA_ENVIRONMENT_PROTECTION_CONFIRMED", workflow)
        self.assertIn('!= "true"', workflow)

    def test_environment_secrets_are_not_referenced_before_the_gated_runner_step(self):
        workflow = (Path(__file__).resolve().parents[1] / "workflows" / "qa-real-agent-dispatch.yml").read_text(encoding="utf-8")
        before_runner = workflow.split("      - name: Execute selected real Agent V2 scenarios", 1)[0]
        self.assertNotIn("${{ secrets.", before_runner)
    def test_report_annotation_keeps_harness_and_agent_shas_separate(self):
        source = {
            "git_sha": guard.AGENT_SHA,
            "mode": "validate_only",
            "results": [{"scenario_id": "RM04", "git_sha": guard.AGENT_SHA}],
            "real_model_executions": 0,
            "database_connections": 0,
            "database_writes": 0,
        }
        with tempfile.TemporaryDirectory() as tmp:
            report_path = Path(tmp) / "report.json"
            report_path.write_text(json.dumps(source), encoding="utf-8")
            guard.annotate_report(
                report_path=str(report_path),
                qa_harness_sha=guard.QA_HARNESS_SHA,
                agent_under_test_sha=guard.AGENT_SHA,
                workflow_sha="4" * 40,
                scenario_ids="RM04",
            )
            report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(report["git_sha"], guard.AGENT_SHA)
        self.assertEqual(report["qa_harness_sha"], guard.QA_HARNESS_SHA)
        self.assertEqual(report["agent_under_test_sha"], guard.AGENT_SHA)
        self.assertEqual(report["dispatch_metadata"]["selected_scenario_ids"], ["RM04"])
        self.assertEqual(report["results"][0]["qa_harness_sha"], guard.QA_HARNESS_SHA)
        self.assertEqual(report["results"][0]["agent_under_test_sha"], guard.AGENT_SHA)
        self.assertEqual(report["real_model_executions"], 0)
        self.assertEqual(report["database_writes"], 0)


if __name__ == "__main__":
    unittest.main()
