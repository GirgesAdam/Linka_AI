import argparse
import sys

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app.database.session import SessionLocal
from app.models.booking_settings import BookingSettings
from app.models.workspace import Workspace


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Provision a Linka workspace.")
    parser.add_argument("--name", required=True, help="Workspace display name, e.g. Linka")
    parser.add_argument("--slug", required=True, help="Unique lowercase slug, e.g. tia")
    parser.add_argument("--timezone", default="Africa/Cairo")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    slug = args.slug.strip().lower()

    with SessionLocal() as db:
        existing = db.scalar(select(Workspace).where(Workspace.slug == slug))
        if existing is not None:
            print(f"Workspace already exists: {existing.id}")
            return 0

        workspace = Workspace(
            name=args.name.strip(),
            slug=slug,
            timezone=args.timezone.strip(),
        )
        db.add(workspace)

        try:
            db.flush()
            db.add(BookingSettings(workspace_id=workspace.id))
            db.commit()
            db.refresh(workspace)
        except SQLAlchemyError as exc:
            db.rollback()
            print(f"Provisioning failed: {exc}", file=sys.stderr)
            return 1

        print("Workspace provisioned successfully")
        print(f"workspace_id={workspace.id}")
        print(f"name={workspace.name}")
        print(f"slug={workspace.slug}")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
