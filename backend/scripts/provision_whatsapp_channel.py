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


PROVIDER = "n8n_whatsapp_cloud"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Create or rotate a Linka WhatsApp channel connection for the "
            "n8n + WhatsApp Business Cloud bridge."
        )
    )
    parser.add_argument("--workspace-id", required=True)
    parser.add_argument(
        "--phone-number-id",
        required=True,
        help="Meta WhatsApp Phone Number ID (not the visible phone number).",
    )
    parser.add_argument(
        "--display-name",
        default="Linka WhatsApp",
        help="Friendly connection name shown inside Linka.",
    )
    parser.add_argument(
        "--waba-id",
        default=None,
        help="Optional WhatsApp Business Account ID (non-secret metadata).",
    )
    parser.add_argument(
        "--business-phone",
        default=None,
        help="Optional visible WhatsApp business phone number.",
    )
    parser.add_argument(
        "--allow-production",
        action="store_true",
        help="Required when ENVIRONMENT=production.",
    )
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
            "Refusing to rotate/create a production WhatsApp adapter token without "
            "--allow-production.",
            file=sys.stderr,
        )
        return 2

    try:
        workspace_id = UUID(args.workspace_id)
    except ValueError:
        print("Invalid --workspace-id UUID.", file=sys.stderr)
        return 2

    phone_number_id = args.phone_number_id.strip()
    display_name = args.display_name.strip()
    if not phone_number_id or not display_name:
        print("--phone-number-id and --display-name cannot be empty.", file=sys.stderr)
        return 2

    config: dict[str, str | bool] = {
        "phone_number_id": phone_number_id,
        "transport": "n8n",
        "provider": "meta_cloud",
        "runtime_kind": "real",
        "do_not_send": False,
    }
    if args.waba_id and args.waba_id.strip():
        config["waba_id"] = args.waba_id.strip()
    if args.business_phone and args.business_phone.strip():
        config["business_phone"] = args.business_phone.strip()

    session_factory = build_session_factory(settings)
    adapter_token, adapter_token_hash = generate_adapter_token()

    with session_factory() as db:
        workspace = db.scalar(
            select(Workspace).where(Workspace.id == workspace_id)
        )
        if workspace is None:
            print("Workspace not found.", file=sys.stderr)
            return 1

        connection = db.scalar(
            select(ChannelConnection).where(
                ChannelConnection.workspace_id == workspace_id,
                ChannelConnection.channel == "whatsapp",
                ChannelConnection.provider == PROVIDER,
                ChannelConnection.external_account_id == phone_number_id,
            )
        )

        created = connection is None
        if connection is None:
            connection = ChannelConnection(
                workspace_id=workspace_id,
                channel="whatsapp",
                provider=PROVIDER,
                display_name=display_name,
                status="active",
                external_account_id=phone_number_id,
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
            print(f"Could not provision WhatsApp channel: {exc}", file=sys.stderr)
            return 1

        db.refresh(connection)

        print("WhatsApp channel provisioned successfully")
        print(f"action={'created' if created else 'rotated'}")
        print(f"workspace_id={workspace_id}")
        print(f"connection_id={connection.id}")
        print(f"phone_number_id={phone_number_id}")
        print(f"provider={PROVIDER}")
        print(f"adapter_token={adapter_token}")
        print("Store adapter_token in n8n Header Auth credentials. It will not be shown again.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
