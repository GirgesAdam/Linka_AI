"""Fail-closed, source-only contracts for the approved 0098 workflow.

No production or staging connections. The migration upgrade itself is NOT run.
"""
from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / ".github/scripts/production_migration_0098.py"
WORKFLOW = ROOT / ".github/workflows/database-migrate-0098.yml"

spec = importlib.util.spec_from_file_location("migration_0098_guard_under_test", SCRIPT)
assert spec and spec.loader
m = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = m
spec.loader.exec_module(m)



# Use temporary files rather than PIPE handles: Windows remote terminal sessions
# do not always expose a valid inheritable stdin handle for subprocess capture.
@pytest.fixture(autouse=True)
def git_capture_via_local_files(monkeypatch, tmp_path):
    def local_git(repo, *args):
        out = tmp_path / "git-command-output.bin"
        with out.open("wb") as stdout:
            result = subprocess.run(["git", *args], cwd=repo, stdout=stdout,
                                    stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        if result.returncode != 0:
            raise m.GateFailure("GIT_SOURCE_VERIFICATION_FAILED")
        return out.read_bytes()
    monkeypatch.setattr(m, "_git", local_git)


def local_git_sha(repo: Path) -> str:
    out = repo / "fixture-head.txt"
    with out.open("wb") as stdout:
        subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                       stdout=stdout, stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    return out.read_text(encoding="ascii").strip()

@pytest.fixture
def source_git_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    file = repo / m.MIGRATION_PATH
    file.parent.mkdir(parents=True)
    file.write_bytes((ROOT / m.MIGRATION_PATH).read_bytes().replace(b"\r\n", b"\n"))
    subprocess.run(["git", "-C", str(repo), "add", m.MIGRATION_PATH], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=CI Fixture",
         "-c", "user.email=ci-fixture@example.invalid", "commit", "-qm", "approved migration"],
        check=True,
    )
    sha = local_git_sha(repo)
    return repo, sha


def args_for(repo: Path, sha: str, **overrides) -> dict:
    result = {
        "repo": repo,
        "github_ref": "refs/heads/main",
        "github_sha": sha,
        "expected_main_sha": sha,
        "expected_migration_sha256": m.APPROVED_MIGRATION_SHA256,
        "expected_current_revision": m.REVISION_BEFORE,
        "run_attempt": "1",
        "check_remote_main": False,
    }
    result.update(overrides)
    return result


def test_valid_0097_to_0098_exact_git_blob_approved(source_git_repo):
    repo, sha = source_git_repo
    assert m.validate_source(**args_for(repo, sha)) == m.APPROVED_MIGRATION_SHA256


@pytest.mark.parametrize(
    ("field", "bad"),
    [
        ("expected_current_revision", "0096_patient_customer_id"),
        ("expected_current_revision", "0098_doctor_laser_weekday_exclusions"),
        ("expected_migration_sha256", "f" * 64),
        ("expected_main_sha", "a" * 40),
        ("github_ref", "refs/heads/feature/migration"),
        ("run_attempt", "2"),
    ],
)
def test_fail_closed_on_unapproved_source_or_rerun(source_git_repo, field, bad):
    repo, sha = source_git_repo
    with pytest.raises(m.GateFailure):
        m.validate_source(**args_for(repo, sha, **{field: bad}))


def test_wrong_checkout_sha_rejected(source_git_repo):
    repo, sha = source_git_repo
    with pytest.raises(m.GateFailure, match="CHECKOUT_SHA_MISMATCH"):
        m.validate_source(**args_for(repo, sha, github_sha="0" * 40,
                                     expected_main_sha="0" * 40))


def test_missing_0098_git_blob_rejected(tmp_path):
    repo = tmp_path / "missing"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "README.md").write_text("no migration", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=CI Fixture",
                    "-c", "user.email=ci@example.invalid", "commit", "-qm", "test"],
                   check=True)
    sha = local_git_sha(repo)
    with pytest.raises(m.GateFailure, match="GIT_SOURCE_VERIFICATION_FAILED"):
        m.validate_source(**args_for(repo, sha))


def test_remote_main_advance_rejected(source_git_repo, monkeypatch):
    repo, sha = source_git_repo
    real = m._git

    def changed_main(path, *args):
        if args[:1] == ("ls-remote",):
            return b"0" * 40 + b"\trefs/heads/main\n"
        return real(path, *args)

    monkeypatch.setattr(m, "_git", changed_main)
    with pytest.raises(m.GateFailure, match="REMOTE_MAIN_MOVED"):
        m.validate_source(**args_for(repo, sha, check_remote_main=True))


GOOD_URL = (
    "postgresql+psycopg://postgres.ycuxjlkhnubztgqmhtom:synthetic-not-secret"
    "@aws-1-eu-west-3.pooler.supabase.com:5432/postgres"
)


def test_valid_pinned_production_session_pooler_url():
    m.assert_production_url(GOOD_URL)
    m.assert_production_url(GOOD_URL + "?sslmode=verify-full")


@pytest.mark.parametrize("bad_url", [
    GOOD_URL.replace("aws-1-eu-west-3.pooler.supabase.com", "localhost"),
    GOOD_URL.replace("ycuxjlkhnubztgqmhtom", "otherproject"),
    GOOD_URL.replace(":5432", ":6543"),
    GOOD_URL.replace("/postgres", "/other"),
    GOOD_URL.replace("postgresql+psycopg", "postgresql"),
    GOOD_URL + "?sslmode=require",
    GOOD_URL + "?hostaddr=127.0.0.1",
    GOOD_URL + "?service=other",
    "sqlite:///no-connection.db",
])
def test_unapproved_or_downgraded_target_rejected(bad_url):
    with pytest.raises(m.GateFailure, match="PRODUCTION_DATABASE_URL_IDENTITY_REJECTED"):
        m.assert_production_url(bad_url)


class FakeResult:
    def __init__(self, value=None, values=None):
        self.value = value
        self.values = list(values or [])

    def one(self):
        return self.value

    def scalar_one(self):
        return self.value

    def scalars(self):
        return self

    def all(self):
        return self.values

    def __iter__(self):
        return iter(self.values)


class PreflightConnection:
    def __init__(self, roles=None, version_num=170006, table_exists=None, parents_valid=True):
        self.roles = roles if roles is not None else ["anon", "authenticated", "supabase_admin"]
        self.version_num = version_num
        self.table_exists = table_exists
        self.parents_valid = parents_valid

    def execute(self, query, args=None):
        s = str(query)
        if "current_database()" in s:
            return FakeResult(value=("postgres", self.version_num))
        if "FROM pg_roles" in s:
            return FakeResult(values=self.roles)
        if "to_regclass(" in s:
            return FakeResult(value=self.table_exists)
        if "FROM pg_constraint" in s:
            return FakeResult(value=1 if self.parents_valid else 0)
        raise AssertionError("Unexpected SQL in isolated unit test")


@pytest.fixture
def lightweight_preflight(monkeypatch):
    monkeypatch.setattr(m, "_single_version", lambda c: m.REVISION_BEFORE)
    monkeypatch.setattr(m, "_existing_tables", lambda c: {"public.appointments"})
    monkeypatch.setattr(m, "_existing_indexes", lambda c: {"public.existing_idx"})
    monkeypatch.setattr(m, "_business_row_counts", lambda c: {"appointments": 50})


def test_correct_local_catalog_preflight_passes(lightweight_preflight):
    result = m.validate_before_locked(PreflightConnection())
    assert len(result) == 3


@pytest.mark.parametrize("conn", [
    PreflightConnection(roles=["anon", "authenticated"]),
    PreflightConnection(version_num=180004),
    PreflightConnection(table_exists="public.doctor_laser_device_weekday_exclusions"),
    PreflightConnection(parents_valid=False),
])
def test_missing_roles_wrong_pg_existing_target_or_parent_fails_closed(lightweight_preflight, conn):
    with pytest.raises(m.GateFailure):
        m.validate_before_locked(conn)


def test_already_applied_0098_prevents_second_migration(monkeypatch, lightweight_preflight):
    monkeypatch.setattr(m, "_single_version", lambda c: m.REVISION_AFTER)
    with pytest.raises(m.GateFailure, match="STARTING_REVISION_MISMATCH"):
        m.validate_before_locked(PreflightConnection())


def test_postflight_rejects_mismatched_columns(monkeypatch):
    monkeypatch.setattr(m, "_single_version", lambda c: m.REVISION_AFTER)

    class MissingColumns:
        def execute(self, stmt, args=None):
            return FakeResult(values=["id"])

    with pytest.raises(m.GateFailure, match="POST_MIGRATION_COLUMNS_INVALID"):
        m.validate_after_locked(MissingColumns(), set(), set(), {})


def _assert_0098_security_contract(workflow: str, runner: str) -> None:
    """Preserve the Runner contract while permitting the existing GitHub secret."""
    assert re.search(r"(?m)^on:\s*\n  workflow_dispatch:", workflow)
    events = workflow.split("on:", 1)[1].split("permissions:", 1)[0]
    for trigger in ("push:", "pull_request:", "pull_request_target:",
                    "schedule:", "workflow_call:"):
        assert trigger not in events
    assert "permissions:\n  contents: read" in workflow
    assert "group: database-migration-global" in workflow
    assert "cancel-in-progress: false" in workflow
    assert workflow.count("github.run_attempt == '1'") >= 2
    assert workflow.count("github.ref == 'refs/heads/main'") >= 2
    assert workflow.count("persist-credentials: false") >= 2
    assert "secrets.PRODUCTION_MIGRATION_0098_DATABASE_URL" not in workflow

    # GitHub Secret source is shared, but the Runner's INTERNAL variable is unchanged.
    assert re.search(
        r"(?m)^          PRODUCTION_MIGRATION_0098_DATABASE_URL:\s*"
        r"\$\{\{\s*secrets\.PRODUCTION_MIGRATION_DATABASE_URL\s*\}\}\s*$",
        workflow,
    )
    assert re.search(
        r"(?m)^          PRODUCTION_MIGRATION_DB_CA_CERT:\s*"
        r"\$\{\{\s*secrets\.PRODUCTION_MIGRATION_DB_CA_CERT\s*\}\}\s*$",
        workflow,
    )
    preflight = workflow.split("  source-preflight:\n", 1)[1].split(
        "  migrate-production-0098:\n", 1
    )[0]
    protected = workflow.split("  migrate-production-0098:\n", 1)[1]
    assert "secrets." not in preflight
    assert "PRODUCTION_MIGRATION_0098_DATABASE_URL" not in preflight
    assert "environment:" not in preflight
    assert "environment:\n      name: production" in protected
    assert "needs: source-preflight" in protected
    assert "needs.source-preflight.result == 'success'" in protected
    assert "github.ref == 'refs/heads/main'" in protected
    assert "github.run_attempt == '1'" in protected
    assert "EXPECTED_MAIN_SHA: ${{ inputs.expected_main_sha }}" in preflight
    assert "EXPECTED_MIGRATION_SHA256: ${{ inputs.expected_migration_sha256 }}" in preflight
    assert "EXPECTED_CURRENT_REVISION: ${{ inputs.expected_current_revision }}" in preflight
    assert "python .github/scripts/production_migration_0098.py source" in preflight
    assert "python .github/scripts/production_migration_0098.py execute" in protected
    assert "alembic upgrade head" not in workflow
    assert "alembic downgrade" not in workflow

    # Also pin the existing Runner guards, not only the YAML variable spelling.
    for guard in (
        "APPROVED_MIGRATION_SHA256 = ",
        "if github_sha != expected_main_sha:",
        "if expected_current_revision != REVISION_BEFORE:",
        "if expected_migration_sha256.lower() != APPROVED_MIGRATION_SHA256:",
        "assert_production_url(raw_url)",
        'query.get("sslmode", "verify-full") == "verify-full"',
        'connect_args={"sslmode": "verify-full"',
        "sslrootcert",
        "_write_ca(ca_pem",
        "with migration_session_lock(connection):",
        "with connection.begin():",
        "validate_before_locked(connection)",
        "validate_after_locked(connection",
        "module.upgrade()",
        "ALEMBIC_VERSION_UPDATE_RACE",
    ):
        assert guard in runner, guard


def test_workflow_only_manual_dispatch_and_protected_environment():
    _assert_0098_security_contract(
        WORKFLOW.read_text(encoding="utf-8"),
        SCRIPT.read_text(encoding="utf-8"),
    )


@pytest.mark.parametrize("violation", [
    "old_secret", "runner_variable_changed", "ca_binding_removed",
    "main_guard_removed", "source_sha_untrusted", "revision_untrusted",
    "auto_dispatch_added", "tls_downgraded", "ca_check_removed",
    "migration_lock_removed", "postflight_removed",
])
def test_0098_protected_workflow_rejects_synthetic_regression(violation: str):
    workflow = WORKFLOW.read_text(encoding="utf-8")
    runner = SCRIPT.read_text(encoding="utf-8")
    source_is_runner = violation in {
        "tls_downgraded", "ca_check_removed",
        "migration_lock_removed", "postflight_removed",
    }
    original = runner if source_is_runner else workflow
    mutations = {
        "old_secret": (
            "secrets.PRODUCTION_MIGRATION_DATABASE_URL",
            "secrets.PRODUCTION_MIGRATION_0098_DATABASE_URL",
        ),
        "runner_variable_changed": (
            "          PRODUCTION_MIGRATION_0098_DATABASE_URL:",
            "          DATABASE_URL:",
        ),
        "ca_binding_removed": (
            "secrets.PRODUCTION_MIGRATION_DB_CA_CERT",
            "secrets.UNTRUSTED_CA",
        ),
        "main_guard_removed": ("github.ref == 'refs/heads/main' && ", ""),
        "source_sha_untrusted": (
            "EXPECTED_MAIN_SHA: ${{ inputs.expected_main_sha }}",
            "EXPECTED_MAIN_SHA: invalid",
        ),
        "revision_untrusted": (
            "EXPECTED_CURRENT_REVISION: ${{ inputs.expected_current_revision }}",
            "EXPECTED_CURRENT_REVISION: invalid",
        ),
        "auto_dispatch_added": (
            "  workflow_dispatch:\n",
            "  workflow_dispatch:\n  schedule:\n    - cron: '0 * * * *'\n",
        ),
        "tls_downgraded": (
            'connect_args={"sslmode": "verify-full"',
            'connect_args={"sslmode": "require"',
        ),
        "ca_check_removed": ("_write_ca(ca_pem", "_untrusted_ca(ca_pem"),
        "migration_lock_removed": (
            "with migration_session_lock(connection):",
            "if True:  # missing lock",
        ),
        "postflight_removed": (
            "validate_after_locked(connection", "skip_after_locked(connection",
        ),
    }
    old, new = mutations[violation]
    assert old in original, violation
    mutated = original.replace(old, new)
    if source_is_runner:
        runner = mutated
    else:
        workflow = mutated
    with pytest.raises(AssertionError):
        _assert_0098_security_contract(workflow, runner)



def test_lock_timeout_and_rollback_contract_statically_attested():
    text = SCRIPT.read_text(encoding="utf-8")
    assert "with migration_session_lock(connection)" in text
    assert "with migration_timeout_guard(connection)" in text
    assert "with connection.begin()" in text
    assert text.index("validate_before_locked(connection)") < text.index("module.upgrade()")
    assert text.index("module.upgrade()") < text.rindex("validate_after_locked(connection")
    assert "module.downgrade()" not in text
    assert 'connect_args={"sslmode": "verify-full"' in text
    assert "NullPool" in text
    assert "ALEMBIC_VERSION_UPDATE_RACE" in text


def test_error_output_redacts_credentials_and_patient_text(monkeypatch, capsys):
    fake_secret = "SYNTHETIC_CANARY_NOT_TO_LOG"
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("PRODUCTION_MIGRATION_0098_DATABASE_URL", GOOD_URL)
    monkeypatch.setenv("PRODUCTION_MIGRATION_DB_CA_CERT", fake_secret)
    monkeypatch.setattr(m, "validate_source", lambda **kwargs: m.APPROVED_MIGRATION_SHA256)
    monkeypatch.setattr(m, "execute_production", lambda *a: (_ for _ in ()).throw(RuntimeError(fake_secret)))
    monkeypatch.setattr(sys, "argv", ["runner", "execute"])
    assert m.main() == 1
    output = capsys.readouterr()
    assert fake_secret not in output.out + output.err
    assert "0098_EXECUTION_FAILED_REVIEW_REQUIRED" in output.err


def test_ca_secret_required_before_any_production_connection(tmp_path):
    with pytest.raises(m.GateFailure, match="DATABASE_CA_CERTIFICATE_NOT_CONFIGURED"):
        m._write_ca("not-a-valid-certificate", tmp_path)
    assert list(tmp_path.iterdir()) == []
