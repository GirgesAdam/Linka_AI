from __future__ import annotations

import argparse
import sys
from pathlib import Path
from uuid import UUID

from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.models.automation_worker import AutomationWorker
from app.models.workspace import Workspace
from app.services.automations import generate_worker_token


class ProvisionSettings(BaseSettings):
    database_url: str
    environment: str = "staging"

    model_config = SettingsConfigDict(
        env_file=BACKEND_DIR / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create/rotate the real n8n automation scheduler worker."
    )
    parser.add_argument("--workspace-id", required=True)
    parser.add_argument("--name", default="Linka n8n Runtime")
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
            "Refusing to provision/rotate a production automation worker token "
            "without --allow-production.",
            file=sys.stderr,
        )
        return 2
    try:
        workspace_id = UUID(args.workspace_id)
    except ValueError:
        print("Invalid --workspace-id UUID.", file=sys.stderr)
        return 2
    name = args.name.strip()
    if not name or name.startswith(("Regression ", "Final Gate ")):
        print("Runtime worker name cannot be empty or use a test prefix.", file=sys.stderr)
        return 2

    raw, token_hash = generate_worker_token()
    factory = build_session_factory(settings)
    with factory() as db:
        if db.get(Workspace, workspace_id) is None:
            print("Workspace not found.", file=sys.stderr)
            return 1
        worker = db.scalar(
            select(AutomationWorker).where(
                AutomationWorker.workspace_id == workspace_id,
                AutomationWorker.name == name,
            )
        )
        created = worker is None
        if worker is None:
            worker = AutomationWorker(
                workspace_id=workspace_id,
                name=name,
                token_hash=token_hash,
                status="active",
                created_by_user_id=None,
            )
            db.add(worker)
        else:
            worker.token_hash = token_hash
            worker.status = "active"
        db.commit()
        db.refresh(worker)
        print("n8n automation worker provisioned successfully")
        print(f"action={'created' if created else 'rotated'}")
        print(f"workspace_id={workspace_id}")
        print(f"worker_id={worker.id}")
        print(f"worker_name={worker.name}")
        print(f"worker_token={raw}")
        print(
            "Store worker_token in n8n Header Auth as X-Automation-Token. "
            "Linka stores only its hash."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
