from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import guard

AGENT_SHA = "a" * 40
OTHER_SHA = "b" * 40
WORKFLOW_SHA = "c" * 40
MAIN_SHA = "d" * 40


def qa_pr(*, state: str = "open", draft: bool = True, merged: bool = False):
    return {
        "number": guard.QA_HARNESS_PR,
        "state": state,
        "draft": draft,
        "merged": merged,
        "base": {"ref": "main"},
        "user": {"login": "qa-author"},
        "head": {
            "ref": guard.QA_HARNESS_BRANCH,
            "sha": guard.QA_HARNESS_SHA,
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


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


class TrustedDispatchGuardTests(unittest.TestCase):
    def setUp(self):
        self.qa = qa_pr()

    def validate_inputs(self, **overrides):
        values = {
            "workflow_ref": "refs/heads/main",
            "mode": "live",
            "qa_harness_sha": guard.QA_HARNESS_SHA,
            "agent_under_test_sha": AGENT_SHA,
            "approved_agent_sha": AGENT_SHA,
            "scenario_ids": "RM04,RM06,RM20,RM29",
            "confirm_live": True,
        }
        values.update(overrides)
        return guard.validate_inputs(**values)

    def validate_qa(self, **overrides):
        values = {
            "mode": "live",
            "qa_harness_sha": guard.QA_HARNESS_SHA,
            "qa_pr": self.qa,
            "qa_reviews": approved_review("independent-reviewer", guard.QA_HARNESS_SHA),
        }
        values.update(overrides)
        return guard.validate_qa_pr_metadata(**values)

    def test_approved_read_only_batch_is_selected_in_requested_order(self):
        self.assertEqual(self.validate_inputs(), ["RM04", "RM06", "RM20", "RM29"])

    def test_wrong_qa_sha_is_rejected(self):
        with self.assertRaisesRegex(guard.DispatchRejected, "QA harness SHA"):
            self.validate_inputs(qa_harness_sha="0" * 40)

    def test_agent_sha_must_match_repository_approved_pin(self):
        with self.assertRaisesRegex(guard.DispatchRejected, "approved SHA"):
            self.validate_inputs(agent_under_test_sha=OTHER_SHA)

    def test_missing_repository_approved_agent_sha_is_rejected(self):
        with self.assertRaisesRegex(guard.DispatchRejected, "Repository-approved Agent SHA"):
            self.validate_inputs(approved_agent_sha="")

    def test_malformed_agent_sha_is_rejected(self):
        with self.assertRaisesRegex(guard.DispatchRejected, "40-character"):
            self.validate_inputs(agent_under_test_sha="not-a-sha", approved_agent_sha="not-a-sha")

    def test_non_main_dispatch_ref_is_rejected(self):
        with self.assertRaisesRegex(guard.DispatchRejected, "refs/heads/main"):
            self.validate_inputs(workflow_ref="refs/heads/feature/untrusted")

    def test_live_mode_requires_explicit_confirmation(self):
        with self.assertRaisesRegex(guard.DispatchRejected, "confirm_live=true"):
            self.validate_inputs(confirm_live=False)

    def test_unknown_scenario_is_rejected(self):
        with self.assertRaisesRegex(guard.DispatchRejected, "not authorized"):
            self.validate_inputs(scenario_ids="RM30")

    def test_rm11_is_explicitly_excluded(self):
        with self.assertRaisesRegex(guard.DispatchRejected, "RM11"):
            self.validate_inputs(scenario_ids="RM11")

    def test_write_scenario_is_rejected(self):
        with self.assertRaisesRegex(guard.DispatchRejected, "RM09"):
            self.validate_inputs(scenario_ids="RM09")

    def test_duplicate_scenario_is_rejected(self):
        with self.assertRaisesRegex(guard.DispatchRejected, "Duplicate"):
            self.validate_inputs(scenario_ids="RM04,RM04")

    def test_empty_scenario_item_is_rejected(self):
        with self.assertRaisesRegex(guard.DispatchRejected, "empty item"):
            self.validate_inputs(scenario_ids="RM04,,RM06")

    def test_stale_qa_pr_head_is_rejected(self):
        stale = qa_pr()
        stale["head"] = dict(stale["head"], sha=OTHER_SHA)
        with self.assertRaisesRegex(guard.DispatchRejected, "stale"):
            self.validate_qa(qa_pr=stale)

    def test_wrong_qa_branch_is_rejected(self):
        wrong = qa_pr()
        wrong["head"] = dict(wrong["head"], ref="unapproved-branch")
        with self.assertRaisesRegex(guard.DispatchRejected, "branch"):
            self.validate_qa(qa_pr=wrong)

    def test_qa_pr_must_target_main(self):
        wrong = qa_pr()
        wrong["base"] = {"ref": "release"}
        with self.assertRaisesRegex(guard.DispatchRejected, "target main"):
            self.validate_qa(qa_pr=wrong)

    def test_closed_unmerged_qa_pr_is_rejected(self):
        with self.assertRaisesRegex(guard.DispatchRejected, "open or already merged"):
            self.validate_qa(qa_pr=qa_pr(state="closed", merged=False))

    def test_open_draft_qa_pr_is_rejected_for_live(self):
        with self.assertRaisesRegex(guard.DispatchRejected, "out of Draft"):
            self.validate_qa(qa_pr=qa_pr(draft=True))

    def test_current_human_approval_for_exact_qa_head_passes(self):
        self.validate_qa(qa_pr=qa_pr(draft=False))

    def test_stale_qa_approval_is_rejected(self):
        with self.assertRaisesRegex(guard.DispatchRejected, "no current human approval"):
            self.validate_qa(
                qa_pr=qa_pr(draft=False),
                qa_reviews=approved_review("reviewer", OTHER_SHA),
            )

    def test_latest_changes_requested_blocks_live_dispatch(self):
        reviews = approved_review("reviewer", guard.QA_HARNESS_SHA) + [{
            "user": {"login": "reviewer"},
            "state": "CHANGES_REQUESTED",
            "commit_id": guard.QA_HARNESS_SHA,
            "submitted_at": "2026-10-18T13:00:00Z",
        }]
        with self.assertRaisesRegex(guard.DispatchRejected, "no current human approval"):
            self.validate_qa(qa_pr=qa_pr(draft=False), qa_reviews=reviews)

    def test_merged_qa_pr_can_be_used_after_merge(self):
        merged = qa_pr(state="closed", merged=True, draft=False)
        merged["head"] = dict(merged["head"], repo=None)
        self.validate_qa(qa_pr=merged, mode="validate_only", qa_reviews=[])

    def test_agent_sha_must_be_ancestor_of_current_main(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            git(repo, "init", "-b", "main")
            git(repo, "config", "user.name", "QA guard tests")
            git(repo, "config", "user.email", "qa-guard@example.invalid")
            (repo / "tracked.txt").write_text("main\n", encoding="utf-8")
            git(repo, "add", "tracked.txt")
            git(repo, "commit", "-m", "main baseline")
            main_sha = git(repo, "rev-parse", "HEAD")
            git(repo, "update-ref", "refs/remotes/origin/main", main_sha)
            verified_main = guard.verify_agent_main_history(
                repo_path=str(repo),
                agent_sha=main_sha,
                approved_agent_sha=main_sha,
            )
            self.assertEqual(verified_main, main_sha)

    def test_agent_sha_outside_main_history_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            git(repo, "init", "-b", "main")
            git(repo, "config", "user.name", "QA guard tests")
            git(repo, "config", "user.email", "qa-guard@example.invalid")
            (repo / "tracked.txt").write_text("main\n", encoding="utf-8")
            git(repo, "add", "tracked.txt")
            git(repo, "commit", "-m", "main baseline")
            main_sha = git(repo, "rev-parse", "HEAD")
            git(repo, "update-ref", "refs/remotes/origin/main", main_sha)
            git(repo, "checkout", "-b", "unmerged-candidate")
            (repo / "candidate.txt").write_text("not merged\n", encoding="utf-8")
            git(repo, "add", "candidate.txt")
            git(repo, "commit", "-m", "unmerged candidate")
            candidate_sha = git(repo, "rev-parse", "HEAD")
            with self.assertRaisesRegex(guard.DispatchRejected, "not in the verified history"):
                guard.verify_agent_main_history(
                    repo_path=str(repo),
                    agent_sha=candidate_sha,
                    approved_agent_sha=candidate_sha,
                )

    def test_report_annotation_keeps_harness_and_agent_shas_separate(self):
        source = {
            "git_sha": AGENT_SHA,
            "mode": "live",
            "results": [{"scenario_id": "RM04", "git_sha": AGENT_SHA}],
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
                agent_under_test_sha=AGENT_SHA,
                approved_agent_sha=AGENT_SHA,
                workflow_sha=WORKFLOW_SHA,
                main_sha=MAIN_SHA,
                scenario_ids="RM04",
            )
            report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(report["git_sha"], AGENT_SHA)
        self.assertEqual(report["qa_harness_sha"], guard.QA_HARNESS_SHA)
        self.assertEqual(report["agent_under_test_sha"], AGENT_SHA)
        self.assertEqual(report["dispatch_metadata"]["verified_main_sha"], MAIN_SHA)
        self.assertEqual(report["dispatch_metadata"]["agent_sha_approval_source"], guard.APPROVED_AGENT_SHA_VARIABLE)
        self.assertEqual(report["dispatch_metadata"]["selected_scenario_ids"], ["RM04"])
        self.assertEqual(report["results"][0]["qa_harness_sha"], guard.QA_HARNESS_SHA)
        self.assertEqual(report["results"][0]["agent_under_test_sha"], AGENT_SHA)
        self.assertEqual(report["real_model_executions"], 0)
        self.assertEqual(report["database_writes"], 0)

    def test_workflow_targets_approved_main_sha_not_agent_pr(self):
        workflow = (Path(__file__).resolve().parents[1] / "workflows" / "qa-real-agent-dispatch.yml").read_text(encoding="utf-8")
        self.assertIn("LINKA_QA_APPROVED_AGENT_SHA", workflow)
        self.assertNotIn('pulls/262', workflow)
        self.assertNotIn("agent-pr.json", workflow)
        self.assertIn("verify-main-history", workflow)

    def test_workflow_keeps_protected_environment_and_no_pull_request_target(self):
        workflow = (Path(__file__).resolve().parents[1] / "workflows" / "qa-real-agent-dispatch.yml").read_text(encoding="utf-8")
        self.assertIn("name: agent-qa", workflow)
        self.assertIn("LINKA_QA_ENVIRONMENT_PROTECTION_CONFIRMED", workflow)
        self.assertNotIn("pull_request_target", workflow)

    def test_environment_secrets_are_only_passed_to_the_runner_step(self):
        workflow = (Path(__file__).resolve().parents[1] / "workflows" / "qa-real-agent-dispatch.yml").read_text(encoding="utf-8")
        before_runner = workflow.split("      - name: Execute selected real Agent V2 scenarios", 1)[0]
        self.assertNotIn("${{ secrets.", before_runner)

    def test_rm11_is_not_authorized_in_workflow_default(self):
        workflow = (Path(__file__).resolve().parents[1] / "workflows" / "qa-real-agent-dispatch.yml").read_text(encoding="utf-8")
        self.assertIn("default: RM04,RM06,RM20,RM29", workflow)
        self.assertNotIn("RM11", guard.AUTHORIZED_SCENARIOS)


if __name__ == "__main__":
    unittest.main()
