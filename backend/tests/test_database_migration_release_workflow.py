from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "database-migrate.yml"


def test_production_gate_requires_exact_main_sha_and_migration_digest() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert "EXPECTED_MAIN_SHA" in workflow
    assert 'test "${ACTUAL_MAIN_SHA}" = "${EXPECTED_MAIN_SHA}"' in workflow
    assert "EXPECTED_MIGRATION_SHA256" in workflow
    assert 'sha256sum "${MIGRATION_FILE}"' in workflow
    assert "refs/heads/main" in workflow
    assert "alembic/versions/0097_member_page_permissions.py" in workflow


def test_production_gate_pins_expected_database_revision_and_target() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert 'EXPECTED_CURRENT_REVISION}" = "0096_patient_customer_id"' in workflow
    assert "CURRENT_REVISION" in workflow
    assert "alembic upgrade 0097_member_page_permissions" in workflow


def test_staging_keeps_existing_upgrade_head_behavior() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert "Apply staging migrations" in workflow
    assert "if: inputs.environment == 'staging'" in workflow
    assert "run: alembic upgrade head" in workflow


def test_database_secrets_are_not_available_to_production_preflight() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    preflight = workflow.split("- name: Verify production release inputs", 1)[1]
    preflight = preflight.split("- name: Set up Python", 1)[0]
    assert "secrets." not in preflight
    assert "MIGRATION_DATABASE_URL" not in preflight


def test_production_uses_environment_and_non_cancelling_concurrency() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert "environment: ${{ inputs.environment }}" in workflow
    assert "cancel-in-progress: false" in workflow
    assert "permissions:" in workflow
    assert "contents: read" in workflow
