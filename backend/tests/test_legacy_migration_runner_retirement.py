from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LEGACY_WORKFLOW = ROOT / ".github" / "workflows" / "apply-0068-production.yml"
CANONICAL_WORKFLOW = ROOT / ".github" / "workflows" / "database-migrate.yml"


def test_legacy_production_migration_workflow_is_retired() -> None:
    assert not LEGACY_WORKFLOW.exists()


def test_canonical_migration_workflow_remains_present() -> None:
    assert CANONICAL_WORKFLOW.is_file()
