"""Isolated execution of revision 0098 on PostgreSQL 17.6 synthetic fixtures."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import make_url

from app import models  # noqa: F401
from app.database.base import Base


def test_revision_0098_up_and_down_on_disposable_pg17():
    raw = os.environ.get("DOCTOR_WEEKDAY_MIGRATION_TEST_DATABASE_URL")
    if not raw:
        pytest.skip("Requires the dedicated disposable localhost migration test database.")
    url = make_url(raw)
    assert url.host in ("localhost", "127.0.0.1")
    assert url.port == 55432
    assert url.username == "package_ci_user"
    assert url.database == "doctor_weekday_revision_ci"

    # Create only the prerequisite pre-0098 tables and their referenced parents.
    names = {"doctor_branches", "clinic_laser_devices"}
    while True:
        before = len(names)
        for name in tuple(names):
            names.update(fk.column.table.name for fk in Base.metadata.tables[name].foreign_keys)
        if len(names) == before:
            break
    target_name = "doctor_laser_device_weekday_exclusions"
    assert target_name not in names
    engine = create_engine(raw)
    try:
        Base.metadata.create_all(engine, tables=[Base.metadata.tables[name] for name in names])
        path = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "0098_doctor_laser_weekday_exclusions.py"
        spec = importlib.util.spec_from_file_location("doctor_weekday_revision_under_test", path)
        assert spec and spec.loader
        revision = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(revision)

        with engine.begin() as connection:
            revision.op = Operations(MigrationContext.configure(connection))
            # Idempotent test setup if a previous assertion failed after upgrade.
            if target_name in inspect(connection).get_table_names():
                revision.downgrade()
            revision.upgrade()

        schema = inspect(engine)
        assert target_name in schema.get_table_names()
        columns = {col["name"] for col in schema.get_columns(target_name)}
        assert columns == {
            "id", "workspace_id", "doctor_id", "branch_id", "weekday",
            "device_key", "created_at", "updated_at",
        }
        assert len(schema.get_foreign_keys(target_name)) == 2
        assert len(schema.get_unique_constraints(target_name)) == 1
        assert len(schema.get_check_constraints(target_name)) == 1
        assert len([index for index in schema.get_indexes(target_name)
                    if not index.get("duplicates_constraint")]) == 3

        with engine.begin() as connection:
            revision.op = Operations(MigrationContext.configure(connection))
            revision.downgrade()
        assert target_name not in inspect(engine).get_table_names()
    finally:
        engine.dispose()
