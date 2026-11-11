from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "database-migrate.yml"

PRE_STEP = "Verify production database revision"
FINAL_STEP = "Verify final production database revision"
PRE_REVISION = "0096_patient_customer_id"
FINAL_REVISION = "0097_member_page_permissions"


def _workflow_script(step_name: str) -> str:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    marker = f"      - name: {step_name}\n"
    step_start = workflow.index(marker) + len(marker)
    next_step = workflow.find("\n      - name:", step_start)
    step = workflow[step_start:] if next_step == -1 else workflow[step_start:next_step]

    heredoc_start = step.index("python - <<'PY'\n") + len("python - <<'PY'\n")
    heredoc_end = step.index("\n          PY", heredoc_start)
    return textwrap.dedent(step[heredoc_start:heredoc_end])


def _sqlite_database(
    tmp_path: Path,
    revisions: tuple[str, ...] = (),
    *,
    create_version_table: bool = True,
) -> str:
    database_path = tmp_path / "alembic-revisions.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    engine = create_engine(database_url)
    try:
        if create_version_table:
            with engine.begin() as connection:
                connection.exec_driver_sql(
                    "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)"
                )
                for revision in revisions:
                    connection.execute(
                        text("INSERT INTO alembic_version (version_num) VALUES (:revision)"),
                        {"revision": revision},
                    )
    finally:
        engine.dispose()
    return database_url


def _run_revision_check(
    step_name: str,
    database_url: str,
    expected_variable: str,
    expected_revision: str,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["MIGRATION_DATABASE_URL"] = database_url
    environment[expected_variable] = expected_revision
    script = _workflow_script(step_name)
    runner = textwrap.dedent(
        f"""
        import contextlib
        import sys
        import types

        app_module = types.ModuleType("app")
        app_module.__path__ = []
        database_module = types.ModuleType("app.database")
        database_module.__path__ = []
        lock_module = types.ModuleType("app.database.migration_lock")

        @contextlib.contextmanager
        def migration_session_lock(connection):
            yield

        lock_module.migration_session_lock = migration_session_lock
        sys.modules["app"] = app_module
        sys.modules["app.database"] = database_module
        sys.modules["app.database.migration_lock"] = lock_module

        exec(compile({script!r}, "<workflow-step>", "exec"), {{"__name__": "__main__"}})
        """
    )
    return subprocess.run(
        [sys.executable, "-c", runner],
        cwd=ROOT / "backend",
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(
    ("revisions", "create_table", "expected_exit", "expected_output"),
    [
        ((PRE_REVISION,), True, 0, "Verified single current Alembic revision"),
        (("0095_unexpected_revision",), True, 1, "revision mismatch"),
        ((PRE_REVISION, FINAL_REVISION), True, 1, "found 2"),
        ((PRE_REVISION, FINAL_REVISION, "0098_another_revision"), True, 1, "found 3"),
        ((), True, 1, "found 0"),
        ((), False, 1, "found 0"),
    ],
    ids=[
        "expected-single-revision",
        "unexpected-single-revision",
        "two-current-revisions",
        "three-current-revisions",
        "empty-version-table",
        "missing-version-table",
    ],
)
def test_pre_migration_revision_attestation_fails_closed(
    tmp_path: Path,
    revisions: tuple[str, ...],
    create_table: bool,
    expected_exit: int,
    expected_output: str,
) -> None:
    database_url = _sqlite_database(
        tmp_path,
        revisions,
        create_version_table=create_table,
    )
    result = _run_revision_check(
        PRE_STEP,
        database_url,
        "EXPECTED_CURRENT_REVISION",
        PRE_REVISION,
    )

    assert result.returncode == expected_exit, result.stdout + result.stderr
    assert expected_output in result.stdout + result.stderr


def test_pre_migration_revision_attestation_fails_on_database_read_error() -> None:
    result = _run_revision_check(
        PRE_STEP,
        "not-a-valid-sqlalchemy-database-url",
        "EXPECTED_CURRENT_REVISION",
        PRE_REVISION,
    )

    assert result.returncode == 1
    assert "Unable to read current Alembic revisions" in result.stderr


@pytest.mark.parametrize(
    ("revisions", "create_table", "expected_exit", "expected_output"),
    [
        ((FINAL_REVISION,), True, 0, "Verified final Alembic revision"),
        ((PRE_REVISION,), True, 1, "Final Alembic revision mismatch"),
        (("0098_unexpected_revision",), True, 1, "Final Alembic revision mismatch"),
        ((FINAL_REVISION, "0098_another_revision"), True, 1, "found 2"),
        ((FINAL_REVISION, "0098_another_revision", "0099_third_revision"), True, 1, "found 3"),
        ((), True, 1, "found 0"),
        ((), False, 1, "found 0"),
    ],
    ids=[
        "exact-final-revision",
        "still-at-0096",
        "unexpected-final-revision",
        "two-final-revisions",
        "three-final-revisions",
        "empty-version-table",
        "missing-version-table",
    ],
)
def test_final_revision_attestation_fails_closed(
    tmp_path: Path,
    revisions: tuple[str, ...],
    create_table: bool,
    expected_exit: int,
    expected_output: str,
) -> None:
    database_url = _sqlite_database(
        tmp_path,
        revisions,
        create_version_table=create_table,
    )
    result = _run_revision_check(
        FINAL_STEP,
        database_url,
        "EXPECTED_FINAL_REVISION",
        FINAL_REVISION,
    )

    assert result.returncode == expected_exit, result.stdout + result.stderr
    assert expected_output in result.stdout + result.stderr


def test_final_revision_attestation_fails_on_database_read_error() -> None:
    result = _run_revision_check(
        FINAL_STEP,
        "not-a-valid-sqlalchemy-database-url",
        "EXPECTED_FINAL_REVISION",
        FINAL_REVISION,
    )

    assert result.returncode == 1
    assert "Unable to read final Alembic revisions" in result.stderr
