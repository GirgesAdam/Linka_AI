from __future__ import annotations

import argparse
import sys
from pathlib import Path
from uuid import UUID

from sqlalchemy import delete, func, select, update

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

# Clinic/setup/configuration that must survive a runtime-data wipe.
PROTECTED_TABLES = {
    "workspaces",
    "users",
    "workspace_members",
    "branches",
    "services",
    "staff",
    "doctors",
    "doctor_services",
    "doctor_branches",
    "branch_working_hours",
    "doctor_working_hours",
    "doctor_availability_windows",
    "booking_settings",
    "clinic_integrations",
    "channel_connections",
    "automation_rules",
    "automation_workers",
    "analytics_saved_views",
}


def _workspace(db, *, workspace_id: str | None, workspace_slug: str | None):
    from app.models.historical_import import ClinicHistoricalImportBatch
    from app.models.workspace import Workspace

    if workspace_id:
        return db.get(Workspace, UUID(workspace_id))
    if workspace_slug:
        return db.scalar(select(Workspace).where(Workspace.slug == workspace_slug))

    ws = db.scalar(
        select(Workspace)
        .join(ClinicHistoricalImportBatch, ClinicHistoricalImportBatch.workspace_id == Workspace.id)
        .order_by(ClinicHistoricalImportBatch.created_at.desc())
        .limit(1)
    )
    if ws is not None:
        return ws
    return db.scalar(select(Workspace).order_by(Workspace.created_at.desc()).limit(1))


def _runtime_tables():
    # Import all models so Base.metadata is complete. Do not use
    # Base.metadata.sorted_tables here: the runtime schema intentionally has a
    # small FK cycle between appointments, patient_packages and
    # payment_transactions.
    import app.models  # noqa: F401
    from app.database.base import Base

    return {
        table.name: table
        for table in Base.metadata.tables.values()
        if "workspace_id" in table.c and table.name not in PROTECTED_TABLES
    }


# The only strongly-connected runtime FK component in the current schema.
# We break it explicitly before computing the normal child-first delete order.
# Never generalize this to nullable FKs across every table: some tables (for
# example automation_jobs) have check constraints that require a valid target.
CYCLE_NULL_COLUMNS = {
    "appointments": ("patient_package_id",),
    "patient_packages": ("purchase_transaction_id",),
    "payment_transactions": ("appointment_id", "origin_appointment_id", "patient_package_id"),
}


def _runtime_dependencies(tables: dict[str, object]) -> dict[str, set[str]]:
    names = set(tables)
    deps: dict[str, set[str]] = {name: set() for name in names}
    for name, table in tables.items():
        for fk in table.foreign_key_constraints:
            parent = fk.referred_table.name
            if parent in names and parent != name:
                deps[name].add(parent)
    return deps


def _delete_order(tables: dict[str, object]) -> list[str]:
    # child -> parent graph using *all* runtime FK edges, nullable or not.
    # Nullable does not mean it is safe to null generically: check constraints
    # may make a nullable target semantically required.
    deps = _runtime_dependencies(tables)

    # The explicit cycle is broken before deletion, so remove just those edges
    # from the ordering graph. All other FK edges remain and determine order.
    cycle_names = set(CYCLE_NULL_COLUMNS)
    for child in cycle_names:
        deps.get(child, set()).difference_update(cycle_names)

    order: list[str] = []
    remaining = set(tables)
    while remaining:
        # A child is ready when it has no remaining children pointing to it.
        # Equivalently, choose nodes that are not parents of any remaining
        # dependency, producing child-first deletion directly.
        parents_in_use = {parent for child in remaining for parent in (deps[child] & remaining)}
        ready = sorted(remaining - parents_in_use)
        if not ready:
            unresolved = {name: sorted(deps[name] & remaining) for name in sorted(remaining)}
            raise RuntimeError(f"unresolved runtime FK cycle(s): {unresolved}")
        order.extend(ready)
        remaining.difference_update(ready)
    return order


def _break_known_cycles(db, tables: dict[str, object], workspace_id) -> None:
    for table_name, column_names in CYCLE_NULL_COLUMNS.items():
        table = tables.get(table_name)
        if table is None:
            continue
        values = {name: None for name in column_names if name in table.c and table.c[name].nullable}
        if values:
            db.execute(update(table).where(table.c.workspace_id == workspace_id).values(**values))

def _count(db, table, wid) -> int:
    return int(db.scalar(select(func.count()).select_from(table).where(table.c.workspace_id == wid)) or 0)


def main() -> int:
    parser = argparse.ArgumentParser(description="Wipe runtime/test data for one Linka workspace while preserving clinic setup/configuration.")
    parser.add_argument("--workspace-id")
    parser.add_argument("--workspace-slug")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()

    from app.database.session import SessionLocal

    with SessionLocal() as db:
        ws = _workspace(db, workspace_id=args.workspace_id, workspace_slug=args.workspace_slug)
        if ws is None:
            print("ERROR: workspace not found")
            return 2

        tables = _runtime_tables()
        counts = {name: _count(db, table, ws.id) for name, table in sorted(tables.items())}
        total = sum(counts.values())

        print(f"Workspace: {ws.name} ({ws.id})")
        print("\nProtected clinic/config tables:")
        print("  " + ", ".join(sorted(PROTECTED_TABLES)))
        print("\nRuntime rows scheduled for deletion:")
        for name, count in counts.items():
            if count:
                print(f"  {name}: {count}")
        print(f"\nTOTAL runtime rows: {total}")

        if not args.apply:
            print("\nDRY RUN ONLY. Nothing was changed.")
            print("To execute: python scripts\\wipe_workspace_runtime_data.py --apply --confirm WIPE")
            return 0
        if args.confirm != "WIPE":
            print("\nREFUSED: --apply requires --confirm WIPE")
            return 3

        # Break only the known three-table FK cycle. Every other dependency is
        # handled by the complete child-first delete order below.
        _break_known_cycles(db, tables, ws.id)
        order = _delete_order(tables)
        deleted = 0
        for name in order:
            table = tables[name]
            result = db.execute(delete(table).where(table.c.workspace_id == ws.id))
            deleted += int(result.rowcount or 0)

        # Verify before committing.
        remaining = {name: _count(db, table, ws.id) for name, table in tables.items()}
        remaining = {name: count for name, count in remaining.items() if count}
        if remaining:
            db.rollback()
            print(f"\nERROR: wipe verification failed; transaction rolled back: {remaining}")
            return 4

        db.commit()
        print(f"\nWIPE COMPLETE. Deleted {deleted} runtime rows.")
        print("Clinic setup/configuration was preserved.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
