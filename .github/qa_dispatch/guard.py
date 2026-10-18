from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

REPOSITORY = "GirgesAdam/Linka_AI"
QA_HARNESS_PR = 264
QA_HARNESS_BRANCH = "qa/llm-29-selective-runner-20261018"
QA_HARNESS_SHA = "dbbfdff3df4bfe51866097870f9663bf58a28ca5"
APPROVED_AGENT_SHA_VARIABLE = "LINKA_QA_APPROVED_AGENT_SHA"
AUTHORIZED_SCENARIOS = ("RM04", "RM06", "RM20", "RM29")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class DispatchRejected(ValueError):
    """A dispatch request or its provenance failed a fail-closed guard."""


def _require_sha(value: str, label: str, expected: str | None = None) -> None:
    if not isinstance(value, str) or not SHA_RE.fullmatch(value):
        raise DispatchRejected(f"{label} must be a full 40-character lowercase commit SHA.")
    if expected is not None and value != expected:
        raise DispatchRejected(f"{label} is not the approved SHA.")


def parse_scenario_ids(raw: str) -> list[str]:
    if not isinstance(raw, str) or not raw.strip():
        raise DispatchRejected("Select one or more authorized scenario IDs.")
    parts = raw.split(",")
    if any(not part.strip() for part in parts):
        raise DispatchRejected("Scenario selection contains an empty item.")
    selected = [part.strip().upper() for part in parts]
    if len(selected) != len(set(selected)):
        raise DispatchRejected("Duplicate scenario IDs are not allowed.")
    unauthorized = [item for item in selected if item not in AUTHORIZED_SCENARIOS]
    if unauthorized:
        raise DispatchRejected(
            "Scenario ID(s) are not authorized for QA-REAL-03: " + ", ".join(unauthorized)
        )
    if len(selected) > len(AUTHORIZED_SCENARIOS):
        raise DispatchRejected("The selected batch exceeds the four-scenario authorization.")
    return selected


def validate_inputs(
    *,
    workflow_ref: str,
    mode: str,
    qa_harness_sha: str,
    agent_under_test_sha: str,
    approved_agent_sha: str,
    scenario_ids: str,
    confirm_live: bool,
) -> list[str]:
    if workflow_ref != "refs/heads/main":
        raise DispatchRejected("Trusted dispatch must run from refs/heads/main.")
    if mode not in {"validate_only", "live"}:
        raise DispatchRejected("Mode must be validate_only or live.")
    if mode == "live" and not confirm_live:
        raise DispatchRejected("Live mode requires confirm_live=true.")
    if mode == "validate_only" and confirm_live:
        raise DispatchRejected("confirm_live is only valid with mode=live.")

    _require_sha(qa_harness_sha, "QA harness SHA", QA_HARNESS_SHA)
    _require_sha(approved_agent_sha, "Repository-approved Agent SHA")
    _require_sha(agent_under_test_sha, "Agent-under-test SHA", approved_agent_sha)
    return parse_scenario_ids(scenario_ids)


def _pr_number(pr: dict[str, Any], label: str, expected: int) -> None:
    if pr.get("number") != expected:
        raise DispatchRejected(f"{label} PR number does not match the approved target.")
    head = pr.get("head")
    if not isinstance(head, dict):
        raise DispatchRejected(f"{label} PR has no readable head metadata.")
    repo = head.get("repo")
    if repo is None and label == "QA harness" and pr.get("state") == "closed" and pr.get("merged") is True:
        return
    if not isinstance(repo, dict) or repo.get("full_name") != REPOSITORY:
        raise DispatchRejected(f"{label} PR head must belong to {REPOSITORY}.")


def _validate_qa_head(pr: dict[str, Any], *, expected_sha: str) -> None:
    _pr_number(pr, "QA harness", QA_HARNESS_PR)
    head = pr["head"]
    if head.get("ref") != QA_HARNESS_BRANCH:
        raise DispatchRejected("QA harness PR branch does not match the approved branch.")
    if head.get("sha") != expected_sha:
        raise DispatchRejected("QA harness PR head is stale or does not match the approved SHA.")
    base = pr.get("base")
    if not isinstance(base, dict) or base.get("ref") != "main":
        raise DispatchRejected("QA harness PR must target main.")
    state = pr.get("state")
    merged = pr.get("merged") is True
    if not (state == "open" or (state == "closed" and merged)):
        raise DispatchRejected("QA harness PR must be open or already merged.")


def _flatten_reviews(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise DispatchRejected("PR review metadata is malformed.")
    flattened: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, list):
            flattened.extend(_flatten_reviews(item))
        elif isinstance(item, dict):
            flattened.append(item)
        else:
            raise DispatchRejected("PR review metadata contains an invalid record.")
    return flattened


def _has_current_human_approval(
    reviews_value: Any, *, author_login: str, expected_sha: str
) -> bool:
    reviews = _flatten_reviews(reviews_value)
    ordered = sorted(
        enumerate(reviews),
        key=lambda pair: (str(pair[1].get("submitted_at") or ""), pair[0]),
    )
    latest_by_reviewer: dict[str, dict[str, Any]] = {}
    for _, review in ordered:
        user = review.get("user")
        login = user.get("login") if isinstance(user, dict) else None
        if (
            not isinstance(login, str)
            or not login
            or login == author_login
            or login.endswith("[bot]")
        ):
            continue
        latest_by_reviewer[login] = review

    latest_states = [
        str(review.get("state") or "").upper()
        for review in latest_by_reviewer.values()
    ]
    if "CHANGES_REQUESTED" in latest_states:
        return False
    return any(
        str(review.get("state") or "").upper() == "APPROVED"
        and review.get("commit_id") == expected_sha
        for review in latest_by_reviewer.values()
    )


def validate_qa_pr_metadata(
    *,
    mode: str,
    qa_harness_sha: str,
    qa_pr: dict[str, Any],
    qa_reviews: Any,
) -> None:
    _require_sha(qa_harness_sha, "QA harness SHA", QA_HARNESS_SHA)
    _validate_qa_head(qa_pr, expected_sha=qa_harness_sha)
    if mode == "live":
        if qa_pr.get("draft") is True:
            raise DispatchRejected("Live mode requires the QA harness PR to be out of Draft.")
        author = str((qa_pr.get("user") or {}).get("login") or "")
        if not _has_current_human_approval(
            qa_reviews, author_login=author, expected_sha=qa_harness_sha
        ):
            raise DispatchRejected(
                "QA harness PR has no current human approval for its exact head SHA."
            )


def verify_agent_main_history(
    *,
    repo_path: str,
    agent_sha: str,
    approved_agent_sha: str,
    main_ref: str = "origin/main",
) -> str:
    _require_sha(approved_agent_sha, "Repository-approved Agent SHA")
    _require_sha(agent_sha, "Agent-under-test SHA", approved_agent_sha)
    main_result = subprocess.run(
        ["git", "-C", repo_path, "rev-parse", "--verify", f"{main_ref}^{{commit}}"],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    main_sha = main_result.stdout.strip()
    _require_sha(main_sha, "Verified main SHA")
    ancestry = subprocess.run(
        ["git", "-C", repo_path, "merge-base", "--is-ancestor", agent_sha, main_ref],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if ancestry.returncode != 0:
        raise DispatchRejected(
            f"Agent SHA {agent_sha} is not in the verified history of {main_ref}."
        )
    return main_sha


def verify_checkout_heads(
    *,
    qa_path: str,
    agent_path: str,
    qa_sha: str,
    agent_sha: str,
    approved_agent_sha: str,
    require_clean: bool = False,
) -> None:
    _require_sha(qa_sha, "QA harness SHA", QA_HARNESS_SHA)
    _require_sha(approved_agent_sha, "Repository-approved Agent SHA")
    _require_sha(agent_sha, "Agent-under-test SHA", approved_agent_sha)
    for label, path, expected in (
        ("QA harness", qa_path, qa_sha),
        ("Agent-under-test", agent_path, agent_sha),
    ):
        result = subprocess.run(
            ["git", "-C", path, "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        actual = result.stdout.strip()
        if actual != expected:
            raise DispatchRejected(
                f"{label} checkout SHA mismatch: expected {expected}, got {actual}."
            )
        if require_clean:
            status = subprocess.run(
                ["git", "-C", path, "status", "--porcelain"],
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if status.stdout.strip():
                raise DispatchRejected(
                    f"{label} checkout is not clean before harness overlay."
                )


def annotate_report(
    *,
    report_path: str,
    qa_harness_sha: str,
    agent_under_test_sha: str,
    approved_agent_sha: str,
    workflow_sha: str,
    main_sha: str,
    scenario_ids: str,
) -> None:
    _require_sha(qa_harness_sha, "QA harness SHA", QA_HARNESS_SHA)
    _require_sha(approved_agent_sha, "Repository-approved Agent SHA")
    _require_sha(agent_under_test_sha, "Agent-under-test SHA", approved_agent_sha)
    _require_sha(workflow_sha, "Workflow SHA")
    _require_sha(main_sha, "Verified main SHA")
    selected = parse_scenario_ids(scenario_ids)
    path = Path(report_path)
    report = json.loads(path.read_text(encoding="utf-8"))
    if report.get("git_sha") != agent_under_test_sha:
        raise DispatchRejected("Runner report git_sha does not match the Agent-under-test SHA.")

    metadata = {
        "dispatch_workflow_sha": workflow_sha,
        "verified_main_sha": main_sha,
        "qa_harness_pr": QA_HARNESS_PR,
        "qa_harness_sha": qa_harness_sha,
        "agent_under_test_sha": agent_under_test_sha,
        "agent_sha_approval_source": APPROVED_AGENT_SHA_VARIABLE,
        "selected_scenario_ids": selected,
        "runner_git_sha_means": "agent_under_test_sha",
    }
    report["dispatch_metadata"] = metadata
    report["qa_harness_sha"] = qa_harness_sha
    report["agent_under_test_sha"] = agent_under_test_sha
    for row in report.get("results", []):
        row["qa_harness_sha"] = qa_harness_sha
        row["agent_under_test_sha"] = agent_under_test_sha
    path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _load_json(path: str) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fail-closed guard for the trusted Agent QA dispatch entry point."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    inputs = subparsers.add_parser("validate-inputs")
    inputs.add_argument("--workflow-ref", required=True)
    inputs.add_argument("--mode", required=True)
    inputs.add_argument("--qa-harness-sha", required=True)
    inputs.add_argument("--agent-under-test-sha", required=True)
    inputs.add_argument("--approved-agent-sha", required=True)
    inputs.add_argument("--scenario-ids", required=True)
    inputs.add_argument("--confirm-live", choices=("true", "false"), required=True)
    inputs.add_argument("--github-output")

    history = subparsers.add_parser("verify-main-history")
    history.add_argument("--repo-path", required=True)
    history.add_argument("--agent-under-test-sha", required=True)
    history.add_argument("--approved-agent-sha", required=True)
    history.add_argument("--main-ref", default="origin/main")

    qa_pr = subparsers.add_parser("validate-qa-pr")
    qa_pr.add_argument("--mode", required=True)
    qa_pr.add_argument("--qa-harness-sha", required=True)
    qa_pr.add_argument("--qa-pr", required=True)
    qa_pr.add_argument("--qa-reviews", required=True)

    checkout = subparsers.add_parser("verify-checkouts")
    checkout.add_argument("--qa-path", required=True)
    checkout.add_argument("--agent-path", required=True)
    checkout.add_argument("--qa-harness-sha", required=True)
    checkout.add_argument("--agent-under-test-sha", required=True)
    checkout.add_argument("--approved-agent-sha", required=True)
    checkout.add_argument("--require-clean", action="store_true")

    annotate = subparsers.add_parser("annotate-report")
    annotate.add_argument("--report", required=True)
    annotate.add_argument("--qa-harness-sha", required=True)
    annotate.add_argument("--agent-under-test-sha", required=True)
    annotate.add_argument("--approved-agent-sha", required=True)
    annotate.add_argument("--workflow-sha", required=True)
    annotate.add_argument("--main-sha", required=True)
    annotate.add_argument("--scenario-ids", required=True)

    args = parser.parse_args(argv)
    try:
        if args.command == "validate-inputs":
            selected = validate_inputs(
                workflow_ref=args.workflow_ref,
                mode=args.mode,
                qa_harness_sha=args.qa_harness_sha,
                agent_under_test_sha=args.agent_under_test_sha,
                approved_agent_sha=args.approved_agent_sha,
                scenario_ids=args.scenario_ids,
                confirm_live=args.confirm_live == "true",
            )
            joined = ",".join(selected)
            if args.github_output:
                with Path(args.github_output).open("a", encoding="utf-8") as output:
                    output.write(f"selected_scenario_ids={joined}\n")
            print(json.dumps({"result": "PASS", "selected_scenario_ids": selected}))
        elif args.command == "verify-main-history":
            main_sha = verify_agent_main_history(
                repo_path=args.repo_path,
                agent_sha=args.agent_under_test_sha,
                approved_agent_sha=args.approved_agent_sha,
                main_ref=args.main_ref,
            )
            print(json.dumps({"result": "PASS", "agent_under_test_sha": args.agent_under_test_sha, "verified_main_sha": main_sha}))
        elif args.command == "validate-qa-pr":
            validate_qa_pr_metadata(
                mode=args.mode,
                qa_harness_sha=args.qa_harness_sha,
                qa_pr=_load_json(args.qa_pr),
                qa_reviews=_load_json(args.qa_reviews),
            )
            print("QA_PR_VALIDATION=PASS")
        elif args.command == "verify-checkouts":
            verify_checkout_heads(
                qa_path=args.qa_path,
                agent_path=args.agent_path,
                qa_sha=args.qa_harness_sha,
                agent_sha=args.agent_under_test_sha,
                approved_agent_sha=args.approved_agent_sha,
                require_clean=args.require_clean,
            )
            print("CHECKOUT_SHA_VALIDATION=PASS")
        elif args.command == "annotate-report":
            annotate_report(
                report_path=args.report,
                qa_harness_sha=args.qa_harness_sha,
                agent_under_test_sha=args.agent_under_test_sha,
                approved_agent_sha=args.approved_agent_sha,
                workflow_sha=args.workflow_sha,
                main_sha=args.main_sha,
                scenario_ids=args.scenario_ids,
            )
            print("REPORT_SHA_ANNOTATION=PASS")
        return 0
    except (
        DispatchRejected,
        OSError,
        ValueError,
        subprocess.SubprocessError,
        json.JSONDecodeError,
    ) as exc:
        print(f"DISPATCH_REJECTED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
