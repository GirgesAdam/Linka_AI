from __future__ import annotations

import hashlib
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / ".github" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from production_migration_credential_guard import (  # noqa: E402
    credential_is_configured,
)
from production_migration_credential_guard import (  # noqa: E402
    main as credential_guard_main,
)
from production_migration_preflight import (  # noqa: E402
    EXPECTED_PREVIOUS_REVISION,
    PreflightError,
    validate_preflight,
)
from production_migration_preflight import (  # noqa: E402
    main as preflight_main,
)

WORKFLOW = ROOT / ".github" / "workflows" / "database-migrate.yml"
MIGRATION_PATH = "backend/alembic/versions/0097_member_page_permissions.py"


def _git(workspace: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _git_repo(tmp_path: Path, *, include_migration: bool = True) -> tuple[Path, str, str]:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    _git(workspace, "init")
    _git(workspace, "config", "user.name", "Preflight Test")
    _git(workspace, "config", "user.email", "preflight@example.invalid")

    if include_migration:
        migration = workspace / MIGRATION_PATH
        migration.parent.mkdir(parents=True)
        migration.write_bytes(b"revision = '0097_member_page_permissions'\n")
    else:
        (workspace / "README.md").write_text("fixture\n", encoding="utf-8")

    _git(workspace, "add", ".")
    _git(workspace, "commit", "-m", "fixture source")
    source_sha = _git(workspace, "rev-parse", "HEAD")

    if include_migration:
        blob = subprocess.run(
            ["git", "show", f"{source_sha}:{MIGRATION_PATH}"],
            cwd=workspace,
            check=True,
            capture_output=True,
        ).stdout
    else:
        blob = b""

    return workspace, source_sha, hashlib.sha256(blob).hexdigest()


def _validate(
    workspace: Path,
    source_sha: str,
    migration_sha: str,
    **overrides: object,
) -> dict[str, str]:
    values: dict[str, object] = {
        "selected_environment": "production",
        "github_ref": "refs/heads/main",
        "run_attempt": "1",
        "github_sha": source_sha,
        "expected_main_sha": source_sha,
        "expected_migration_sha256": migration_sha,
        "expected_current_revision": EXPECTED_PREVIOUS_REVISION,
        "workspace": workspace,
    }
    values.update(overrides)
    return validate_preflight(**values)  # type: ignore[arg-type]


def test_production_preflight_passes_for_exact_main_commit(tmp_path: Path) -> None:
    workspace, source_sha, migration_sha = _git_repo(tmp_path)

    result = _validate(workspace, source_sha, migration_sha)

    assert result["selected_environment"] == "production"
    assert result["source_sha"] == source_sha
    assert result["approved_main_sha"] == source_sha
    assert result["migration_sha256"] == migration_sha
    assert result["expected_previous_revision"] == EXPECTED_PREVIOUS_REVISION
    assert result["migration_target"] == "alembic upgrade 0097_member_page_permissions"


def test_production_preflight_rejects_feature_branch(tmp_path: Path) -> None:
    workspace, source_sha, migration_sha = _git_repo(tmp_path)

    with pytest.raises(PreflightError, match="only be dispatched from main"):
        _validate(workspace, source_sha, migration_sha, github_ref="refs/heads/feature/test")


@pytest.mark.parametrize("attempt", ["2", "3"])
def test_production_preflight_rejects_reruns(tmp_path: Path, attempt: str) -> None:
    workspace, source_sha, migration_sha = _git_repo(tmp_path)

    with pytest.raises(PreflightError, match="reruns are blocked"):
        _validate(workspace, source_sha, migration_sha, run_attempt=attempt)


def test_production_preflight_rejects_unexpected_approved_source_sha(tmp_path: Path) -> None:
    workspace, source_sha, migration_sha = _git_repo(tmp_path)

    with pytest.raises(PreflightError, match="does not match the workflow source SHA"):
        _validate(
            workspace,
            source_sha,
            migration_sha,
            expected_main_sha="0" * 40,
        )


def test_production_preflight_rejects_checkout_sha_mismatch(tmp_path: Path) -> None:
    workspace, source_sha, migration_sha = _git_repo(tmp_path)
    different_sha = "1" * 40

    with pytest.raises(PreflightError, match="checked-out source SHA"):
        _validate(
            workspace,
            source_sha,
            migration_sha,
            github_sha=different_sha,
            expected_main_sha=different_sha,
        )


def test_production_preflight_rejects_incorrect_migration_sha(tmp_path: Path) -> None:
    workspace, source_sha, migration_sha = _git_repo(tmp_path)

    with pytest.raises(PreflightError, match="does not match the approved value"):
        _validate(workspace, source_sha, migration_sha, expected_migration_sha256="f" * 64)


def test_production_preflight_rejects_missing_migration_file(tmp_path: Path) -> None:
    workspace, source_sha, _ = _git_repo(tmp_path, include_migration=False)

    with pytest.raises(PreflightError, match="absent from the approved source commit"):
        _validate(workspace, source_sha, "0" * 64)


def test_production_preflight_rejects_wrong_previous_revision(tmp_path: Path) -> None:
    workspace, source_sha, migration_sha = _git_repo(tmp_path)

    with pytest.raises(PreflightError, match="previous Alembic revision is not approved"):
        _validate(
            workspace,
            source_sha,
            migration_sha,
            expected_current_revision="0095_unexpected_revision",
        )


@pytest.mark.parametrize("environment", ["staging", "production-like", ""])
def test_production_preflight_rejects_non_production_environment(
    tmp_path: Path,
    environment: str,
) -> None:
    workspace, source_sha, migration_sha = _git_repo(tmp_path)

    with pytest.raises(PreflightError, match="Only Production migrations are allowed"):
        _validate(
            workspace,
            source_sha,
            migration_sha,
            selected_environment=environment,
        )



def test_production_credential_guard_rejects_missing_and_whitespace(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    for value in (None, "", "   "):
        if value is None:
            monkeypatch.delenv("PRODUCTION_MIGRATION_DATABASE_URL", raising=False)
        else:
            monkeypatch.setenv("PRODUCTION_MIGRATION_DATABASE_URL", value)
        assert not credential_is_configured(value)
        assert credential_guard_main() == 1
        assert "is missing" in capsys.readouterr().err


def test_production_credential_guard_never_logs_the_configured_value(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sentinel = "postgresql://do-not-log-this-secret"
    monkeypatch.setenv("PRODUCTION_MIGRATION_DATABASE_URL", sentinel)

    assert credential_guard_main() == 0
    captured = capsys.readouterr()
    assert "configured" in captured.out
    assert sentinel not in captured.out
    assert sentinel not in captured.err


def _job_block(workflow: str, job_name: str) -> str:
    marker = f"  {job_name}:\n"
    start = workflow.index(marker)
    remaining = workflow[start + len(marker) :]
    next_job = re.search(r"(?m)^  [a-z0-9_-]+:\s*$", remaining)
    end = start + len(marker) + next_job.start() if next_job else len(workflow)
    return workflow[start:end]


def test_preflight_is_before_and_outside_production_environment() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    preflight = _job_block(workflow, "preflight")
    production = _job_block(workflow, "migrate-production")

    assert not re.search(r"(?m)^    environment:", preflight)
    assert "secrets." not in preflight
    assert "MIGRATION_DATABASE_URL" not in preflight
    assert "PRODUCTION_MIGRATION_DATABASE_URL" not in preflight
    assert "needs: preflight" in production
    assert "needs.preflight.result == 'success'" in production
    assert "github.ref == 'refs/heads/main'" in production
    assert "github.run_attempt == '1'" in production


def test_production_secret_is_environment_job_scoped_and_staging_job_is_absent() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    production = _job_block(workflow, "migrate-production")
    jobs = dict(_all_workflow_job_blocks(workflow))

    assert "name: production" in production
    assert "secrets.PRODUCTION_MIGRATION_DATABASE_URL" in production
    assert "secrets.MIGRATION_DATABASE_URL" not in production
    assert "migrate-staging" not in jobs
    assert "secrets.MIGRATION_DATABASE_URL" not in workflow
    assert "PRODUCTION_MIGRATION_DATABASE_URL" not in jobs["preflight"]


def test_production_secret_name_is_not_referenced_by_other_workflows() -> None:
    for path in (ROOT / ".github" / "workflows").glob("*.yml"):
        if path == WORKFLOW:
            continue
        assert "secrets.PRODUCTION_MIGRATION_DATABASE_URL" not in path.read_text(encoding="utf-8")


def test_staging_database_secret_users_remain_staging_scoped() -> None:
    for path in (ROOT / ".github" / "workflows").glob("*.yml"):
        if path == WORKFLOW:
            continue
        workflow = path.read_text(encoding="utf-8")
        if "secrets.MIGRATION_DATABASE_URL" in workflow:
            assert re.search(r"(?m)^\s+environment:\s*staging\s*$", workflow), path


def test_workflow_has_only_manual_trigger_and_non_cancelling_concurrency() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    trigger = workflow.split("permissions:", 1)[0]

    assert "workflow_dispatch:" in trigger
    assert "pull_request:" not in trigger
    assert "pull_request_target:" not in trigger
    assert "push:" not in trigger
    assert "cancel-in-progress: false" in workflow
    assert "contents: read" in workflow


def test_exact_production_target_is_preserved_and_staging_upgrade_is_absent() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    production = _job_block(workflow, "migrate-production")

    assert "run: alembic upgrade 0097_member_page_permissions" in production
    assert "alembic upgrade head" not in production
    assert "alembic upgrade head" not in workflow
    assert "name: production" in production
    assert "migrate-staging:" not in workflow


def test_dispatch_inputs_are_production_preflight_requirements_only() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    trigger = workflow.split("permissions:", 1)[0]
    assert "workflow_dispatch:" in trigger
    assert "inputs.environment" not in workflow
    assert not re.search(r"(?m)^      environment:\s*$", trigger)

    input_block = trigger.split("inputs:", 1)[1]
    input_names = re.findall(r"(?m)^      ([a-z0-9_]+):\s*$", input_block)
    assert input_names == [
        "expected_main_sha",
        "expected_migration_sha256",
        "expected_current_revision",
    ]
    assert len(re.findall(r"(?m)^        required: true$", input_block)) == 3


def test_canonical_workflow_has_exactly_one_external_production_job() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    jobs = dict(_all_workflow_job_blocks(workflow))

    assert set(jobs) == {"preflight", "migrate-production"}
    assert "migrate-staging" not in jobs
    assert "secrets.MIGRATION_DATABASE_URL" not in workflow
    assert "secrets.PRODUCTION_MIGRATION_DATABASE_URL" in jobs["migrate-production"]
    assert "alembic upgrade head" not in workflow
    assert workflow.count("alembic upgrade 0097_member_page_permissions") == 1


def test_production_audit_evidence_covers_run_source_revisions_and_result() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    production = _job_block(workflow, "migrate-production")

    for field in (
        "GITHUB_ACTOR",
        "GITHUB_RUN_ID",
        "GITHUB_RUN_ATTEMPT",
        "WORKFLOW_SHA",
        "GITHUB_REF_NAME",
        "GITHUB_SHA",
        "APPROVED_MAIN_SHA",
        "MIGRATION_SHA256",
        "DATABASE_PREVIOUS_REVISION",
        "DATABASE_FINAL_REVISION",
        "MIGRATION_TARGET",
        "JOB_RESULT",
        "GITHUB_STEP_SUMMARY",
    ):
        assert field in production


def test_existing_production_revision_attestation_and_lock_guards_remain() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    production = _job_block(workflow, "migrate-production")

    assert production.count("MigrationContext.configure(") == 2
    assert production.count(".get_current_heads()") == 2
    assert production.count("if len(current_revisions) != 1:") == 2
    assert production.count("with migration_session_lock(connection):") == 2
    assert "EXPECTED_FINAL_REVISION: 0097_member_page_permissions" in production
    assert "tail -n 1" not in production


def test_legacy_migration_runner_remains_absent() -> None:
    assert not (ROOT / ".github" / "workflows" / "apply-0068-production.yml").exists()


def test_ci_lints_and_compiles_production_migration_guard_scripts() -> None:
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert ".github/scripts/production_migration_preflight.py" in ci
    assert ".github/scripts/production_migration_credential_guard.py" in ci
    assert "python -m compileall -q .github/scripts/production_migration_preflight.py" in ci


def test_preflight_main_exports_exact_approval_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace, source_sha, migration_sha = _git_repo(tmp_path)
    output_path = tmp_path / "github_output"
    monkeypatch.setenv("GITHUB_REF", "refs/heads/main")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    monkeypatch.setenv("GITHUB_SHA", source_sha)
    monkeypatch.setenv("EXPECTED_MAIN_SHA", source_sha)
    monkeypatch.setenv("EXPECTED_MIGRATION_SHA256", migration_sha)
    monkeypatch.setenv("EXPECTED_CURRENT_REVISION", EXPECTED_PREVIOUS_REVISION)
    monkeypatch.setenv("GITHUB_WORKSPACE", str(workspace))
    monkeypatch.setenv("GITHUB_OUTPUT", str(output_path))

    assert preflight_main() == 0
    output = output_path.read_text(encoding="utf-8")
    assert "selected_environment=production" in output
    assert f"approved_main_sha={source_sha}" in output
    assert f"migration_sha256={migration_sha}" in output
    assert "migration_target=alembic upgrade 0097_member_page_permissions" in output
    assert "passed for production" in capsys.readouterr().out


EXPECTED_MIGRATION_SHA256 = "cc0b6c30bda89ecaae1cf73455c6a65f80129812e7194d809d0f0da139f2beda"


def _all_workflow_job_blocks(workflow: str) -> list[tuple[str, str]]:
    jobs_match = re.search(r"(?m)^jobs:\s*$", workflow)
    assert jobs_match is not None
    jobs_start = jobs_match.end()
    remaining = workflow[jobs_start:]
    headers = list(re.finditer(r"(?m)^  ([A-Za-z0-9_-]+):\s*$", remaining))
    blocks: list[tuple[str, str]] = []
    for index, header in enumerate(headers):
        end = headers[index + 1].start() if index + 1 < len(headers) else len(remaining)
        blocks.append((header.group(1), remaining[header.start() : end]))
    return blocks


def test_production_only_workflow_retains_global_concurrency_group() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    concurrency_matches = list(re.finditer(r"(?m)^concurrency:\s*$", workflow))

    assert len(concurrency_matches) == 1
    start = concurrency_matches[0].start()
    next_top_level = re.search(
        r"(?m)^[A-Za-z_][A-Za-z0-9_-]*:\s*$",
        workflow[concurrency_matches[0].end() :],
    )
    end = concurrency_matches[0].end() + next_top_level.start() if next_top_level else len(workflow)
    concurrency = workflow[start:end]

    assert re.search(r"(?m)^  group: database-migration-global$", concurrency)
    assert re.search(r"(?m)^  queue: max$", concurrency)
    assert re.search(r"(?m)^  cancel-in-progress: false$", concurrency)
    assert "inputs.environment" not in concurrency
    assert "$" + "{{" not in concurrency

    jobs = dict(_all_workflow_job_blocks(workflow))
    assert set(jobs) == {"preflight", "migrate-production"}
    production = jobs["migrate-production"]
    assert not re.search(r"(?m)^    concurrency:\s*$", production)
    assert production.count("alembic upgrade 0097_member_page_permissions") == 1
    assert "alembic upgrade head" not in workflow


def test_every_non_release_alembic_mutation_targets_an_isolated_local_postgres() -> None:
    external_migration_jobs: list[tuple[str, str]] = []
    isolated_test_migration_jobs: list[tuple[str, str]] = []
    migration_command = re.compile(r"\balembic\s+(?:upgrade|downgrade|stamp)\b")

    for path in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
        workflow = path.read_text(encoding="utf-8")
        for job_name, job in _all_workflow_job_blocks(workflow):
            if not migration_command.search(job):
                continue

            if path == WORKFLOW:
                external_migration_jobs.append((path.name, job_name))
                continue

            isolated_test_migration_jobs.append((path.name, job_name))
            assert not re.search(
                r"(?m)^\s*(?:DATABASE_URL|MIGRATION_DATABASE_URL):\s*\$\{\{\s*secrets\.",
                job,
            ), (path, job_name)
            assert re.search(r"(?m)^    services:\s*$", job), (path, job_name)
            assert re.search(r"(?m)^      postgres:\s*$", job), (path, job_name)
            for variable in ("DATABASE_URL", "MIGRATION_DATABASE_URL"):
                assert re.search(
                    rf"(?m)^\s*{variable}:\s*postgresql[^\n]*@(localhost|127\.0\.0\.1):",
                    job,
                ), (path, job_name, variable)

    assert set(external_migration_jobs) == {
        ("database-migrate.yml", "migrate-production"),
    }
    assert isolated_test_migration_jobs


def test_no_non_test_runtime_script_exposes_an_alternative_alembic_upgrade_entrypoint() -> None:
    runtime_roots = (
        ROOT / "backend" / "app",
        ROOT / "backend" / "scripts",
        ROOT / ".github" / "scripts",
    )
    upgrade_entrypoint = re.compile(r"(?:alembic\.)?command\.(?:upgrade|downgrade|stamp)\s*\(")

    for runtime_root in runtime_roots:
        for path in runtime_root.rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            assert not upgrade_entrypoint.search(source), path


def test_migration_0097_approved_sha256_matches_raw_git_blob_and_preflight_path() -> None:
    preflight_source = (SCRIPTS / "production_migration_preflight.py").read_text(encoding="utf-8")
    path_match = re.search(
        r'(?m)^MIGRATION_RELATIVE_PATH = "([^"]+)"$',
        preflight_source,
    )

    assert path_match is not None
    assert path_match.group(1) == MIGRATION_PATH

    result = subprocess.run(
        ["git", "show", f"HEAD:{MIGRATION_PATH}"],
        cwd=ROOT,
        capture_output=True,
        check=True,
    )
    actual_sha256 = hashlib.sha256(result.stdout).hexdigest()
    assert actual_sha256 == EXPECTED_MIGRATION_SHA256


def test_postgresql_advisory_lock_still_wraps_the_actual_alembic_migration() -> None:
    alembic_env = (ROOT / "backend" / "alembic" / "env.py").read_text(encoding="utf-8")
    lock_source = (ROOT / "backend" / "app" / "database" / "migration_lock.py").read_text(
        encoding="utf-8"
    )
    online_migrations = alembic_env.split("def run_migrations_online", 1)[1]

    lock_position = online_migrations.index("with migration_session_lock(connection):")
    migration_position = online_migrations.index("context.run_migrations()")

    assert lock_position < migration_position
    assert "pg_try_advisory_lock" in lock_source
    assert "pg_advisory_unlock" in lock_source
