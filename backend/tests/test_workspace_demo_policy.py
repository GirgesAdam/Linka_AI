from pathlib import Path

from app.models.workspace import Workspace
from app.services.workspace_runtime_policy import workspace_runtime_policy


def test_demo_capability_migration_is_durable_and_defaults_false():
    backend = Path(__file__).resolve().parent.parent
    source = (backend / "alembic/versions/0072_workspace_demo_policy.py").read_text(encoding="utf-8")
    assert 'revision: str = "0072_workspace_demo_policy"' in source
    assert '0071_conversation_agent_ordering' in source
    assert 'server_default=sa.false()' in source
    assert 'WHERE slug = :slug' in source
    assert '.bindparams(slug="tia")' in source


def test_runtime_demo_policy_uses_only_durable_workspace_flag():
    demo = Workspace(name="Anything", slug="anything", is_demo=True)
    real = Workspace(name="Linka", slug="tia", is_demo=False)
    assert workspace_runtime_policy(demo).allow_external_dispatch is False
    assert workspace_runtime_policy(real).allow_external_dispatch is True
