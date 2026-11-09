from pathlib import Path

from app.services.operational_readiness import (
    EXPECTED_MIGRATION_HEAD,
    STALE_LOCK_MINUTES,
)


def test_release_gate_targets_current_schema_head() -> None:
    assert EXPECTED_MIGRATION_HEAD == "0072_workspace_demo_policy"


def test_stale_lock_threshold_matches_existing_worker_reclaim_window_or_later() -> None:
    # Channel/automation workers already reclaim around 10 minutes. The readiness
    # gate waits longer so it reports genuinely stuck work, not normal reclaim.
    assert STALE_LOCK_MINUTES >= 15


def test_operations_endpoint_is_admin_only() -> None:
    backend = Path(__file__).resolve().parent.parent
    source = (backend / "app/api/routes/operations.py").read_text(encoding="utf-8")

    assert 'require_page_permission("activity", legacy_access="admin")' in source
    assert 'router.get("/readiness"' in source


def test_readiness_gate_is_read_only() -> None:
    backend = Path(__file__).resolve().parent.parent
    service = (backend / "app/services/operational_readiness.py").read_text(encoding="utf-8")

    forbidden = (
        "db.commit(",
        "db.delete(",
        "db.add(",
        "delete(",
        "update(",
        "insert(",
    )
    for token in forbidden:
        assert token not in service


def test_provider_readiness_does_not_expose_api_key() -> None:
    backend = Path(__file__).resolve().parent.parent
    service = (backend / "app/services/operational_readiness.py").read_text(encoding="utf-8")

    assert '"gemini_api_key"' not in service
    assert '"openai_api_key"' not in service
    assert '"onboarding_primary_model"' in service
    assert '"onboarding_fallback_model"' in service


def test_readiness_checks_workspace_clinic_integration() -> None:
    backend = Path(__file__).resolve().parent.parent
    service = (backend / "app/services/operational_readiness.py").read_text(encoding="utf-8")

    assert 'key="clinic_integration"' in service
    assert "registered_clinic_adapter_keys()" in service
    assert 'clinic_integration.status == "active"' in service


def test_provider_readiness_is_provider_aware() -> None:
    backend = Path(__file__).resolve().parent.parent
    service = (backend / "app/services/operational_readiness.py").read_text(encoding="utf-8")

    assert 'key="llm_provider_configuration"' in service
    assert 'provider_name == "openai"' in service
    assert "settings.openai_api_key" in service
    assert 'runtime_strategy = "single_model"' in service
    assert '"configured": provider_configured' in service
