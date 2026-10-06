from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal
from time import monotonic, sleep
from typing import Any
from uuid import UUID, uuid5
from zoneinfo import ZoneInfo

from sqlalchemy import bindparam, delete, func, select, text, update
from sqlalchemy.orm import Session

from app.database.base import Base
from app.models.activity_event import ActivityEvent
from app.models.workspace import Workspace
from app.models.workspace_member import WORKSPACE_ROLE_ADMIN, WorkspaceMember

logger = logging.getLogger("tia.demo_reset")

DEMO_SEED_VERSION = "demo-canonical-2026-09-17-v1"
DEMO_SEED_ACTION = "demo_canonical_seed"
DEMO_RESET_ACTION = "demo_daily_reset_succeeded"
DEMO_RESET_MISSED_ACTION = "demo_daily_reset_missed"
DEMO_RESET_TIMEZONE = ZoneInfo("Africa/Cairo")
DEMO_RESET_WINDOW_START_MINUTE = 4 * 60
DEMO_RESET_WINDOW_END_MINUTE = 4 * 60 + 10
DEMO_UUID_NAMESPACE = UUID("8f005f0d-e6b7-4bb0-b3d3-cb45c03af365")

PRESERVE_TABLES = frozenset({
    "analytics_saved_views",
    "automation_workers",
    "channel_connections",
    "channel_provider_credentials",
    "clinic_integration_sync_schedules",
    "clinic_integrations",
    "workspace_members",
})

RESET_RESEED_TABLES = frozenset({
    "appointment_additional_services",
    "availability_block_services",
    "availability_blocks",
    "appointment_product_lines",
    "appointment_status_history",
    "appointments",
    "automation_rules",
    "booking_settings",
    "branch_working_hours",
    "branches",
    "clinic_knowledge_entries",
    "clinic_laser_devices",
    "clinic_products",
    "doctor_availability_windows",
    "doctor_branches",
    "doctor_service_categories",
    "doctor_services",
    "doctor_working_hours",
    "doctors",
    "expenses",
    "inventory_items",
    "inventory_usages",
    "leads",
    "package_usages",
    "patient_notes",
    "patient_packages",
    "patient_pulse_packs",
    "patient_tag_assignments",
    "patient_tags",
    "patients",
    "payment_allocations",
    "payment_transactions",
    "pulse_billing_settings",
    "pulse_pack_offers",
    "pulse_usages",
    "appointment_pulse_settlements",
    "service_device_prices",
    "service_package_offers",
    "services",
    "staff",
})

CLEAR_TABLES = frozenset({
    "activity_events",
    "agent_actions",
    "automation_jobs",
    "channel_delivery_events",
    "channel_identities",
    "channel_inbound_events",
    "clinic_data_issues",
    "clinic_historical_import_batches",
    "clinic_historical_import_links",
    "clinic_historical_import_rows",
    "clinic_integration_entity_links",
    "clinic_integration_sync_checkpoints",
    "clinic_integration_sync_failures",
    "clinic_integration_sync_runs",
    "conversation_flow_events",
    "conversation_flow_states",
    "conversations",
    "crm_campaign_conversions",
    "crm_campaign_recipients",
    "crm_campaigns",
    "crm_cohort_members",
    "crm_cohorts",
    "crm_tasks",
    "handoff_events",
    "handoff_requests",
    "message_dispatches",
    "messages",
    "onboarding_ai_events",
    "onboarding_ai_sessions",
})

CYCLE_NULL_COLUMNS = {
    "appointments": ("patient_package_id",),
    "patient_packages": ("purchase_transaction_id",),
    "payment_transactions": (
        "appointment_id",
        "origin_appointment_id",
        "patient_package_id",
        "patient_pulse_pack_id",
    ),
    "patient_pulse_packs": (
        "origin_appointment_id",
        "purchase_transaction_id",
    ),
}

_USER_REFERENCE_COLUMNS = frozenset({
    "actor_user_id",
    "assigned_user_id",
    "author_user_id",
    "created_by_user_id",
    "sent_by_user_id",
    "user_id",
})


class DemoResetError(RuntimeError):
    pass


class DemoResetInProgress(DemoResetError):
    pass


@dataclass(frozen=True)
class DemoResetResult:
    workspace_id: UUID
    seed_version: str
    reset_date: date
    reseeded_rows: int
    cleared_rows: int


def _workspace_tables() -> dict[str, Any]:
    import app.models  # noqa: F401

    return {
        table.name: table
        for table in Base.metadata.tables.values()
        if "workspace_id" in table.c
    }


def validate_demo_reset_manifest() -> None:
    discovered = set(_workspace_tables())
    classified = set(PRESERVE_TABLES | RESET_RESEED_TABLES | CLEAR_TABLES)
    missing = sorted(discovered - classified)
    stale = sorted(classified - discovered)
    overlaps = {
        "preserve_reset": sorted(PRESERVE_TABLES & RESET_RESEED_TABLES),
        "preserve_clear": sorted(PRESERVE_TABLES & CLEAR_TABLES),
        "reset_clear": sorted(RESET_RESEED_TABLES & CLEAR_TABLES),
    }
    overlaps = {key: value for key, value in overlaps.items() if value}
    if missing or stale or overlaps:
        raise DemoResetError(
            f"Demo reset classification mismatch missing={missing} stale={stale} overlaps={overlaps}"
        )


def _lock_key(workspace_id: UUID) -> int:
    raw = hashlib.sha256(f"tia-demo-reset:{workspace_id}".encode()).digest()[:8]
    return int.from_bytes(raw, "big", signed=True)


def acquire_demo_request_lock(db: Session, workspace: Workspace) -> None:
    if not workspace.is_demo:
        return
    deadline = monotonic() + 3.0
    while True:
        acquired = bool(
            db.scalar(
                text("SELECT pg_try_advisory_xact_lock_shared(:key)"),
                {"key": _lock_key(workspace.id)},
            )
        )
        if acquired:
            return
        if monotonic() >= deadline:
            raise DemoResetInProgress("Demo data is being restored.")
        sleep(0.05)


def _acquire_reset_lock(db: Session, workspace_id: UUID) -> None:
    db.execute(
        text("SELECT pg_advisory_xact_lock(:key)"),
        {"key": _lock_key(workspace_id)},
    )


def _stable_token(table_name: str, value: UUID) -> str:
    digest = hashlib.sha256(str(value).encode()).hexdigest()[:32]
    return f"uuid:{table_name}:{digest}"


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Decimal):
        return {"$decimal": str(value)}
    if isinstance(value, datetime):
        return {"$datetime": value.isoformat()}
    if isinstance(value, date):
        return {"$date": value.isoformat()}
    if isinstance(value, time):
        return {"$time": value.isoformat()}
    if isinstance(value, (dict, list)):
        return value
    return str(value)


def _uuid_identity_table(column: Any, seen: set[tuple[str, str]] | None = None) -> str | None:
    key = (column.table.name, column.name)
    visited = set(seen or ())
    if key in visited:
        raise DemoResetError(f"UUID identity cycle at {column.table.name}.{column.name}")
    visited.add(key)

    identities: set[str] = set()
    for fk in column.foreign_keys:
        target = fk.column
        if target.table.name == "workspaces":
            continue
        if target.primary_key or target.name == "id":
            identities.add(target.table.name)
            continue
        identity = _uuid_identity_table(target, visited)
        if identity is not None:
            identities.add(identity)

    if len(identities) > 1:
        raise DemoResetError(
            f"Ambiguous UUID identity for {column.table.name}.{column.name}: {sorted(identities)}"
        )
    return next(iter(identities), None)


def _encode_row(table: Any, row: dict[str, Any]) -> dict[str, Any]:
    encoded: dict[str, Any] = {}
    for column in table.c:
        name = column.name
        if name == "workspace_id":
            continue
        value = row[name]
        if name in _USER_REFERENCE_COLUMNS:
            encoded[name] = None
            continue
        if isinstance(value, UUID):
            parent = _uuid_identity_table(column)
            if parent == "users":
                encoded[name] = None
                continue
            token_table = parent or table.name
            encoded[name] = {"$uuid": _stable_token(token_table, value)}
            continue
        encoded[name] = _json_value(value)
    return encoded


def _decode_value(workspace_id: UUID, value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    if "$uuid" in value:
        token = str(value["$uuid"])
        return uuid5(DEMO_UUID_NAMESPACE, f"{workspace_id}:{token}")
    if "$datetime" in value:
        return datetime.fromisoformat(str(value["$datetime"]))
    if "$date" in value:
        return date.fromisoformat(str(value["$date"]))
    if "$time" in value:
        return time.fromisoformat(str(value["$time"]))
    if "$decimal" in value:
        return Decimal(str(value["$decimal"]))
    return value


def _decode_row(table: Any, workspace_id: UUID, row: dict[str, Any]) -> dict[str, Any]:
    values = {"workspace_id": workspace_id}
    for column in table.c:
        if column.name == "workspace_id":
            continue
        if column.name in row:
            values[column.name] = _decode_value(workspace_id, row[column.name])
    return values


def _table_rows(db: Session, table: Any, workspace_id: UUID) -> list[dict[str, Any]]:
    stmt = select(table).where(table.c.workspace_id == workspace_id)
    if "id" in table.c:
        stmt = stmt.order_by(table.c.id)
    elif table.primary_key.columns:
        stmt = stmt.order_by(*table.primary_key.columns)
    rows = db.execute(stmt).mappings()
    return [_encode_row(table, dict(row)) for row in rows]


def _seed_event(db: Session, workspace_id: UUID) -> ActivityEvent | None:
    return db.scalar(
        select(ActivityEvent)
        .where(
            ActivityEvent.workspace_id == workspace_id,
            ActivityEvent.action == DEMO_SEED_ACTION,
        )
        .order_by(ActivityEvent.created_at.desc())
        .limit(1)
    )


def capture_demo_canonical_seed(
    db: Session,
    *,
    workspace_id: UUID,
    replace: bool = False,
) -> ActivityEvent:
    validate_demo_reset_manifest()
    workspace = db.get(Workspace, workspace_id)
    if workspace is None:
        raise DemoResetError("Workspace not found.")
    if not workspace.is_demo:
        raise DemoResetError("Refusing to capture a canonical seed for a real workspace.")
    if not workspace.is_active:
        raise DemoResetError("Refusing to capture an inactive demo workspace.")

    _acquire_reset_lock(db, workspace.id)
    existing = _seed_event(db, workspace.id)
    if existing is not None and not replace:
        raise DemoResetError("Canonical demo seed already exists; explicit replace is required.")

    tables = _workspace_tables()
    rows = {
        name: _table_rows(db, tables[name], workspace.id)
        for name in sorted(RESET_RESEED_TABLES)
    }
    primary_branch = (
        {"$uuid": _stable_token("branches", workspace.primary_branch_id)}
        if workspace.primary_branch_id is not None
        else None
    )
    payload = {
        "seed_version": DEMO_SEED_VERSION,
        "captured_at": datetime.now(UTC).isoformat(),
        "workspace_timezone": workspace.timezone,
        "primary_branch_id": primary_branch,
        "row_counts": {name: len(value) for name, value in rows.items()},
        "tables": rows,
    }
    if existing is not None:
        db.delete(existing)
        db.flush()
    event = ActivityEvent(
        workspace_id=workspace.id,
        actor_type="system",
        actor_user_id=None,
        action=DEMO_SEED_ACTION,
        entity_type="workspace",
        entity_id=workspace.id,
        summary=f"Canonical demo dataset {DEMO_SEED_VERSION}",
        metadata_json=payload,
    )
    db.add(event)
    db.commit()
    db.refresh(event)
    return event


def _dependencies(tables: dict[str, Any]) -> dict[str, set[str]]:
    names = set(tables)
    deps = {name: set() for name in names}
    for name, table in tables.items():
        for fk in table.foreign_key_constraints:
            parent = fk.referred_table.name
            if parent in names and parent != name:
                deps[name].add(parent)
    return deps


def _delete_order(tables: dict[str, Any]) -> list[str]:
    deps = _dependencies(tables)
    cycle_names = set(CYCLE_NULL_COLUMNS)
    for child in cycle_names:
        deps.get(child, set()).difference_update(cycle_names)
    remaining = set(tables)
    order: list[str] = []
    while remaining:
        parents_in_use = {
            parent
            for child in remaining
            for parent in (deps[child] & remaining)
        }
        ready = sorted(remaining - parents_in_use)
        if not ready:
            unresolved = {
                name: sorted(deps[name] & remaining)
                for name in sorted(remaining)
            }
            raise DemoResetError(f"Unresolved demo reset FK cycle(s): {unresolved}")
        order.extend(ready)
        remaining.difference_update(ready)
    return order


def _insert_order(tables: dict[str, Any]) -> list[str]:
    return list(reversed(_delete_order(tables)))


def _break_known_cycles(db: Session, tables: dict[str, Any], workspace_id: UUID) -> None:
    for table_name, column_names in CYCLE_NULL_COLUMNS.items():
        table = tables.get(table_name)
        if table is None:
            continue
        values = {
            name: None
            for name in column_names
            if name in table.c and table.c[name].nullable
        }
        if values:
            db.execute(
                update(table)
                .where(table.c.workspace_id == workspace_id)
                .values(**values)
            )


def _deferred_fk_columns(table: Any) -> set[str]:
    deferred = set(CYCLE_NULL_COLUMNS.get(table.name, ()))
    for column in table.c:
        if not column.nullable:
            continue
        for fk in column.foreign_keys:
            if fk.column.table.name == table.name:
                deferred.add(column.name)
    return deferred


def _restore_deferred_links(
    db: Session,
    tables: dict[str, Any],
    workspace_id: UUID,
    seed_tables: dict[str, list[dict[str, Any]]],
) -> None:
    for table_name, table in tables.items():
        column_names = sorted(_deferred_fk_columns(table))
        if not column_names:
            continue
        if "id" not in table.c:
            raise DemoResetError(
                f"Deferred-FK table {table_name} has no id column for deterministic restore."
            )
        params = []
        for encoded in seed_tables.get(table_name, []):
            params.append(
                {
                    "_demo_row_id": _decode_value(workspace_id, encoded["id"]),
                    **{
                        f"_demo_{name}": _decode_value(workspace_id, encoded.get(name))
                        for name in column_names
                    },
                }
            )
        if not params:
            continue
        stmt = (
            update(table)
            .where(table.c.id == bindparam("_demo_row_id"))
            .values(**{name: bindparam(f"_demo_{name}") for name in column_names})
        )
        db.execute(stmt, params)


def _verify_seed_invariants(
    db: Session,
    workspace: Workspace,
    row_counts: dict[str, int],
) -> None:
    db.refresh(workspace)
    if not workspace.is_active or not workspace.is_demo:
        raise DemoResetError("Demo workspace invariant failed after reseed.")
    admins = db.scalar(
        select(func.count())
        .select_from(WorkspaceMember)
        .where(
            WorkspaceMember.workspace_id == workspace.id,
            WorkspaceMember.role == WORKSPACE_ROLE_ADMIN,
            WorkspaceMember.is_active.is_(True),
        )
    )
    if not admins:
        raise DemoResetError("Demo admin membership invariant failed after reseed.")
    tables = _workspace_tables()
    for required in ("branches", "services", "doctors", "booking_settings"):
        expected = int(row_counts.get(required, 0))
        actual = int(
            db.scalar(
                select(func.count())
                .select_from(tables[required])
                .where(tables[required].c.workspace_id == workspace.id)
            )
            or 0
        )
        if expected <= 0 or actual != expected:
            raise DemoResetError(
                f"Canonical invariant failed for {required}: expected={expected} actual={actual}"
            )


def _dated_activity_exists(
    db: Session,
    *,
    workspace_id: UUID,
    action: str,
    event_date: date,
) -> bool:
    events = db.scalars(
        select(ActivityEvent).where(
            ActivityEvent.workspace_id == workspace_id,
            ActivityEvent.action == action,
        )
    )
    return any(
        str((event.metadata_json or {}).get("reset_date")) == event_date.isoformat()
        for event in events
    )


def _successful_reset_exists(db: Session, workspace_id: UUID, reset_date: date) -> bool:
    return _dated_activity_exists(
        db,
        workspace_id=workspace_id,
        action=DEMO_RESET_ACTION,
        event_date=reset_date,
    )


def _reset_demo_workspace(
    db: Session,
    *,
    workspace_id: UUID,
    reset_date: date | None = None,
    injected_failure_after_clear: bool = False,
) -> DemoResetResult:
    validate_demo_reset_manifest()
    workspace = db.get(Workspace, workspace_id)
    if workspace is None:
        raise DemoResetError("Workspace not found.")
    if not workspace.is_demo:
        raise DemoResetError("Refusing to reset a real workspace.")
    if not workspace.is_active:
        raise DemoResetError("Refusing to reset an inactive demo workspace.")

    day = reset_date or datetime.now(DEMO_RESET_TIMEZONE).date()
    _acquire_reset_lock(db, workspace.id)
    seed_event = _seed_event(db, workspace.id)
    if seed_event is None:
        raise DemoResetError("Demo canonical seed has not been captured.")
    seed = dict(seed_event.metadata_json or {})
    if seed.get("seed_version") != DEMO_SEED_VERSION:
        raise DemoResetError(
            f"Unexpected demo seed version: {seed.get('seed_version')!r}"
        )
    seed_tables = seed.get("tables")
    row_counts = seed.get("row_counts")
    if not isinstance(seed_tables, dict) or not isinstance(row_counts, dict):
        raise DemoResetError("Demo canonical seed is malformed.")

    tables = _workspace_tables()
    mutable_names = set(RESET_RESEED_TABLES | CLEAR_TABLES)
    mutable = {name: tables[name] for name in mutable_names}
    reset_tables = {name: tables[name] for name in RESET_RESEED_TABLES}

    workspace.primary_branch_id = None
    db.flush()
    _break_known_cycles(db, mutable, workspace.id)
    deleted = 0
    for name in _delete_order(mutable):
        result = db.execute(
            delete(mutable[name]).where(mutable[name].c.workspace_id == workspace.id)
        )
        deleted += int(result.rowcount or 0)

    if injected_failure_after_clear:
        raise DemoResetError("Injected demo reset failure after clear.")

    inserted = 0
    for name in _insert_order(reset_tables):
        table = reset_tables[name]
        deferred_columns = _deferred_fk_columns(table)
        values_batch = []
        for encoded in seed_tables.get(name, []):
            values = _decode_row(table, workspace.id, encoded)
            for deferred_column in deferred_columns:
                if deferred_column in values:
                    values[deferred_column] = None
            values_batch.append(values)
        if values_batch:
            db.execute(table.insert(), values_batch)
            inserted += len(values_batch)

    _restore_deferred_links(db, reset_tables, workspace.id, seed_tables)
    primary_branch = seed.get("primary_branch_id")
    workspace.primary_branch_id = (
        _decode_value(workspace.id, primary_branch)
        if primary_branch is not None
        else None
    )
    _verify_seed_invariants(db, workspace, row_counts)

    seed_copy = ActivityEvent(
        workspace_id=workspace.id,
        actor_type="system",
        actor_user_id=None,
        action=DEMO_SEED_ACTION,
        entity_type="workspace",
        entity_id=workspace.id,
        summary=f"Canonical demo dataset {DEMO_SEED_VERSION}",
        metadata_json=seed,
    )
    marker = ActivityEvent(
        workspace_id=workspace.id,
        actor_type="system",
        actor_user_id=None,
        action=DEMO_RESET_ACTION,
        entity_type="workspace",
        entity_id=workspace.id,
        summary=f"Demo dataset restored for {day.isoformat()}",
        metadata_json={
            "reset_date": day.isoformat(),
            "seed_version": DEMO_SEED_VERSION,
            "reseeded_rows": inserted,
            "cleared_rows": deleted,
        },
    )
    db.add_all([seed_copy, marker])
    db.commit()
    return DemoResetResult(
        workspace_id=workspace.id,
        seed_version=DEMO_SEED_VERSION,
        reset_date=day,
        reseeded_rows=inserted,
        cleared_rows=deleted,
    )


def reset_demo_workspace(
    db: Session,
    *,
    workspace_id: UUID,
    reset_date: date | None = None,
    injected_failure_after_clear: bool = False,
) -> DemoResetResult:
    try:
        return _reset_demo_workspace(
            db,
            workspace_id=workspace_id,
            reset_date=reset_date,
            injected_failure_after_clear=injected_failure_after_clear,
        )
    except Exception:
        db.rollback()
        raise


def scheduled_demo_reset_state(
    db: Session,
    *,
    workspace: Workspace,
    now: datetime,
) -> str:
    if not workspace.is_demo or not workspace.is_active:
        return "not_demo"
    local_now = now.astimezone(DEMO_RESET_TIMEZONE)
    minute = local_now.hour * 60 + local_now.minute
    if _successful_reset_exists(db, workspace.id, local_now.date()):
        return "already_reset"
    if DEMO_RESET_WINDOW_START_MINUTE <= minute <= DEMO_RESET_WINDOW_END_MINUTE:
        reset_demo_workspace(db, workspace_id=workspace.id, reset_date=local_now.date())
        return "reset"
    if minute > DEMO_RESET_WINDOW_END_MINUTE:
        if _dated_activity_exists(
            db,
            workspace_id=workspace.id,
            action=DEMO_RESET_MISSED_ACTION,
            event_date=local_now.date(),
        ):
            return "missed_window_recorded"
        db.add(
            ActivityEvent(
                workspace_id=workspace.id,
                actor_type="system",
                actor_user_id=None,
                action=DEMO_RESET_MISSED_ACTION,
                entity_type="workspace",
                entity_id=workspace.id,
                summary=f"Demo reset window missed for {local_now.date().isoformat()}",
                metadata_json={
                    "reset_date": local_now.date().isoformat(),
                    "seed_version": DEMO_SEED_VERSION,
                },
            )
        )
        db.commit()
        return "missed_window"
    return "before_window"
