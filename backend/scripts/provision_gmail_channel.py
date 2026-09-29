from __future__ import annotations

import argparse
import sys
from pathlib import Path
from uuid import UUID

from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core.channel_adapter import generate_adapter_token
from app.models.channel_connection import ChannelConnection
from app.models.workspace import Workspace


class ProvisionSettings(BaseSettings):
    database_url: str
    environment: str = "staging"

    model_config = SettingsConfigDict(
        env_file=BACKEND_DIR / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


PROVIDER = "n8n_gmail"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create/rotate a real Gmail channel adapter for n8n."
    )
    parser.add_argument("--workspace-id", required=True)
    parser.add_argument(
        "--sender-email",
        required=True,
        help="Gmail/Google Workspace account connected to the n8n credential.",
    )
    parser.add_argument("--display-name", default="Linka Gmail")
    parser.add_argument(
        "--not-default",
        action="store_true",
        help="Do not make this the default outbound Gmail account.",
    )
    parser.add_argument("--allow-production", action="store_true")
    return parser.parse_args()


def build_session_factory(settings: ProvisionSettings) -> sessionmaker[Session]:
    if not settings.database_url.startswith("postgresql+psycopg://"):
        raise RuntimeError("DATABASE_URL must start with postgresql+psycopg://")
    engine = create_engine(settings.database_url, pool_pre_ping=True)
    return sessionmaker(
        bind=engine,
        class_=Session,
        autoflush=False,
        expire_on_commit=False,
    )


def main() -> int:
    args = parse_args()
    settings = ProvisionSettings()
    if settings.environment.lower() == "production" and not args.allow_production:
        print(
            "Refusing to provision/rotate a production Gmail adapter token "
            "without --allow-production.",
            file=sys.stderr,
        )
        return 2

    try:
        workspace_id = UUID(args.workspace_id)
    except ValueError:
        print("Invalid --workspace-id UUID.", file=sys.stderr)
        return 2

    sender_email = args.sender_email.strip().lower()
    display_name = args.display_name.strip()
    if "@" not in sender_email or not display_name:
        print("A valid --sender-email and display name are required.", file=sys.stderr)
        return 2

    adapter_token, adapter_token_hash = generate_adapter_token()
    session_factory = build_session_factory(settings)

    with session_factory() as db:
        workspace = db.scalar(select(Workspace).where(Workspace.id == workspace_id))
        if workspace is None:
            print("Workspace not found.", file=sys.stderr)
            return 1

        connection = db.scalar(
            select(ChannelConnection).where(
                ChannelConnection.workspace_id == workspace_id,
                ChannelConnection.channel == "email",
                ChannelConnection.provider == PROVIDER,
                ChannelConnection.external_account_id == sender_email,
            )
        )
        created = connection is None

        if not args.not_default:
            existing_gmail = list(
                db.scalars(
                    select(ChannelConnection).where(
                        ChannelConnection.workspace_id == workspace_id,
                        ChannelConnection.channel == "email",
                        ChannelConnection.provider == PROVIDER,
                    )
                )
            )
            for row in existing_gmail:
                row.config_json = {**(row.config_json or {}), "default": False}

        config = {
            "transport": "n8n",
            "provider": "gmail",
            "runtime_kind": "real",
            "sender_email": sender_email,
            "default": not args.not_default,
        }

        if connection is None:
            connection = ChannelConnection(
                workspace_id=workspace_id,
                channel="email",
                provider=PROVIDER,
                display_name=display_name,
                status="active",
                external_account_id=sender_email,
                adapter_token_hash=adapter_token_hash,
                created_by_user_id=None,
                config_json=config,
            )
            db.add(connection)
        else:
            connection.display_name = display_name
            connection.status = "active"
            connection.adapter_token_hash = adapter_token_hash
            connection.config_json = config

        try:
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            print(f"Could not provision Gmail channel: {exc}", file=sys.stderr)
            return 1

        db.refresh(connection)
        print("Gmail channel provisioned successfully")
        print(f"action={'created' if created else 'rotated'}")
        print(f"workspace_id={workspace_id}")
        print(f"connection_id={connection.id}")
        print(f"sender_email={sender_email}")
        print(f"provider={PROVIDER}")
        print(f"adapter_token={adapter_token}")
        print(
            "Store adapter_token in an n8n HTTP Header Auth credential as "
            "X-Channel-Token. Linka stores only its hash."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
