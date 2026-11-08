from __future__ import annotations

import re
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_DIRECTORY = REPOSITORY_ROOT / ".github" / "workflows"
SECRET_REFERENCE = re.compile(
    r"\$\{\{\s*secrets\.[A-Za-z0-9_]+\s*\}\}"
)
PULL_REQUEST_TRIGGER = re.compile(r"(?m)^  pull_request(?:_target)?:\s*$")
JOB_HEADER = re.compile(r"(?m)^  ([A-Za-z0-9_-]+):\s*$")
MAIN_BRANCH_GUARD = re.compile(
    r"(?m)^    if: github\.ref == 'refs/heads/main'\s*$"
)


def _secret_bearing_jobs(workflow: str) -> dict[str, str]:
    jobs_section = workflow.split("\njobs:\n", maxsplit=1)[1]
    headers = list(JOB_HEADER.finditer(jobs_section))
    secret_jobs: dict[str, str] = {}
    for index, header in enumerate(headers):
        end = (
            headers[index + 1].start()
            if index + 1 < len(headers)
            else len(jobs_section)
        )
        block = jobs_section[header.start():end]
        if SECRET_REFERENCE.search(block):
            secret_jobs[header.group(1)] = block
    return secret_jobs


def test_secret_bearing_workflows_never_run_on_pull_request_events() -> None:
    for workflow_path in WORKFLOW_DIRECTORY.glob("*.yml"):
        workflow = workflow_path.read_text(encoding="utf-8-sig")
        if SECRET_REFERENCE.search(workflow):
            trigger_section = workflow.split("\njobs:\n", maxsplit=1)[0]
            assert not PULL_REQUEST_TRIGGER.search(trigger_section), (
                f"{workflow_path.name} exposes a secret-bearing job to pull_request"
            )


def test_human_quality_live_review_is_main_only() -> None:
    workflow_path = (
        WORKFLOW_DIRECTORY / "human-conversation-quality-batch1-live-review.yml"
    )
    workflow = workflow_path.read_text(encoding="utf-8-sig")
    assert "  workflow_dispatch:\n" in workflow
    secret_jobs = _secret_bearing_jobs(workflow)
    assert set(secret_jobs) == {"human-conversation-quality"}
    assert all(MAIN_BRANCH_GUARD.search(block) for block in secret_jobs.values())


def test_grouped_live_review_secret_jobs_are_main_only() -> None:
    workflow_path = (
        WORKFLOW_DIRECTORY / "grouped-multiservice-intent-live-review.yml"
    )
    workflow = workflow_path.read_text(encoding="utf-8-sig")
    assert "  workflow_dispatch:\n" in workflow
    secret_jobs = _secret_bearing_jobs(workflow)
    assert set(secret_jobs) == {"standalone", "batch1-compatibility"}
    assert all(MAIN_BRANCH_GUARD.search(block) for block in secret_jobs.values())
