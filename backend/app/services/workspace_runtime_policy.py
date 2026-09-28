from __future__ import annotations

from dataclasses import dataclass

from app.core.config import settings
from app.models.channel_connection import ChannelConnection
from app.models.workspace import Workspace


@dataclass(frozen=True)
class WorkspaceRuntimePolicy:
    is_demo: bool
    allow_external_dispatch: bool
    allow_external_configuration: bool
    allow_external_ingress: bool
    allow_external_sync: bool
    agent_hourly_turn_limit: int | None


DEMO_WHATSAPP_REPLY_TEST_FLAG = "demo_whatsapp_reply_test_enabled"


def demo_whatsapp_reply_test_enabled(
    workspace: Workspace,
    connection: ChannelConnection,
) -> bool:
    """Allow reactive Meta test-number traffic without opening demo side effects broadly."""
    if not workspace.is_demo:
        return False
    if connection.channel != "whatsapp" or connection.provider != "meta_cloud":
        return False
    return (connection.config_json or {}).get(DEMO_WHATSAPP_REPLY_TEST_FLAG) is True


def workspace_runtime_policy(workspace: Workspace) -> WorkspaceRuntimePolicy:
    """Return tenant-scoped runtime capabilities.

    The workspace row is the only business source of truth. Legacy deployment
    DEMO_MODE flags are deliberately ignored so one demo tenant can safely share
    a production runtime with real clinics.
    """
    is_demo = bool(workspace.is_demo)
    return WorkspaceRuntimePolicy(
        is_demo=is_demo,
        allow_external_dispatch=not is_demo,
        allow_external_configuration=not is_demo,
        allow_external_ingress=not is_demo,
        allow_external_sync=not is_demo,
        agent_hourly_turn_limit=(
            settings.demo_agent_hourly_turn_limit if is_demo else None
        ),
    )
