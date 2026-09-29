from __future__ import annotations

import argparse
import sys
from pathlib import Path
from uuid import UUID

from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker


# Ensure `backend/` is importable even when this file is executed directly:
#   python scripts/bootstrap_admin.py ...
BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.models.user import User
from app.models.workspace import Workspace
from app.models.workspace_member import WORKSPACE_ROLE_ADMIN, WorkspaceMember


class BootstrapSettings(BaseSettings):
    database_url: str

    model_config = SettingsConfigDict(
        env_file=BACKEND_DIR / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Attach an existing Supabase Auth user as an admin "
            "of a Linka workspace."
        )
    )
    parser.add_argument(
        "--workspace-slug",
        required=True,
        help="Workspace slug, for example: tia",
    )
    parser.add_argument(
        "--auth-user-id",
        required=True,
        help="UUID from Supabase Authentication > Users",
    )
    parser.add_argument(
        "--email",
        required=True,
        help="Email of the same Supabase Auth user",
    )
    parser.add_argument(
        "--full-name",
        default=None,
        help="Optional display name",
    )
    return parser.parse_args()


def build_session_factory() -> sessionmaker[Session]:
    settings = BootstrapSettings()

    if not settings.database_url.startswith("postgresql+psycopg://"):
        raise RuntimeError(
            "DATABASE_URL must start with postgresql+psycopg://"
        )

    engine = create_engine(
        settings.database_url,
        pool_pre_ping=True,
    )

    return sessionmaker(
        bind=engine,
        class_=Session,
        autoflush=False,
        expire_on_commit=False,
    )


def main() -> int:
    args = parse_args()

    try:
        auth_user_id = UUID(args.auth_user_id)
    except ValueError:
        print("Invalid --auth-user-id UUID.", file=sys.stderr)
        return 2

    email = args.email.strip().lower()
    workspace_slug = args.workspace_slug.strip().lower()
    full_name = args.full_name.strip() if args.full_name else None

    if not email:
        print("--email cannot be empty.", file=sys.stderr)
        return 2

    try:
        session_factory = build_session_factory()
    except Exception as exc:
        print(
            f"Could not load DATABASE_URL from backend/.env: {exc}",
            file=sys.stderr,
        )
        return 1

    with session_factory() as db:
        workspace = db.scalar(
            select(Workspace).where(
                Workspace.slug == workspace_slug,
            )
        )

        if workspace is None:
            print(
                f"Workspace not found: {workspace_slug}",
                file=sys.stderr,
            )
            return 1

        user = db.scalar(
            select(User).where(
                User.auth_user_id == auth_user_id,
            )
        )

        if user is None:
            user = db.scalar(
                select(User).where(
                    User.email == email,
                )
            )

        if user is None:
            user = User(
                auth_user_id=auth_user_id,
                email=email,
                full_name=full_name,
            )
            db.add(user)
            db.flush()
        else:
            if user.auth_user_id not in (None, auth_user_id):
                print(
                    "This email is already linked to a different "
                    "Supabase Auth user.",
                    file=sys.stderr,
                )
                return 1

            user.auth_user_id = auth_user_id
            user.email = email

            if full_name:
                user.full_name = full_name

        membership = db.scalar(
            select(WorkspaceMember).where(
                WorkspaceMember.workspace_id == workspace.id,
                WorkspaceMember.user_id == user.id,
            )
        )

        if membership is None:
            membership = WorkspaceMember(
                workspace_id=workspace.id,
                user_id=user.id,
                role=WORKSPACE_ROLE_ADMIN,
                is_active=True,
            )
            db.add(membership)
        else:
            membership.role = WORKSPACE_ROLE_ADMIN
            membership.is_active = True

        try:
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            print(
                f"Bootstrap failed because of a database constraint: {exc}",
                file=sys.stderr,
            )
            return 1

        print("Workspace admin bootstrapped successfully")
        print(f"workspace_id={workspace.id}")
        print(f"user_id={user.id}")
        print(f"auth_user_id={auth_user_id}")
        print(f"email={email}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())