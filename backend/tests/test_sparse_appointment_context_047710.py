from pathlib import Path

from app.services.operational_readiness import EXPECTED_MIGRATION_HEAD


def _backend() -> Path:
    return Path(__file__).resolve().parent.parent


def test_sparse_appointment_migration_is_current_head_and_excludes_unknown_doctor_resources() -> None:
    backend = _backend()
    migration = (backend / "alembic/versions/0046_sparse_appointment_context.py").read_text(
        encoding="utf-8"
    )
    model = (backend / "app/models/appointment.py").read_text(encoding="utf-8")

    assert EXPECTED_MIGRATION_HEAD == "0072_workspace_demo_policy"
    assert 'revision: str = "0046_sparse_appointment_context"' in migration
    assert 'down_revision: str | None = "0045_public_table_rls_hardening"' in migration
    assert '"doctor_assignment_known"' in migration
    assert "doctor_assignment_known AND status IN" in migration
    assert "doctor_assignment_known: Mapped[bool]" in model
    assert "doctor_assignment_known AND NOT is_quick_booking AND status IN" in model


def test_history_contract_does_not_require_branch_or_doctor_for_appointments() -> None:
    root = _backend().parent
    history = (root / "backend/app/services/historical_import.py").read_text(encoding="utf-8")
    frontend = (root / "frontend/src/app/(dashboard)/setup/integration/history-uploader.tsx").read_text(encoding="utf-8")

    appointment_block = history[
        history.index("def _normalize_appointment") : history.index("def _normalize_payment")
    ]
    assert '"appointments": "appointment"' in history
    assert '"patient_phone"' in appointment_block
    assert '"patient_name"' in appointment_block
    assert '"service_id"' in appointment_block
    assert '"service_name"' in appointment_block
    assert '"doctor_id"' in appointment_block
    assert '"doctor_name"' in appointment_block
    assert '"branch_id"' not in appointment_block
    assert "ملفات Linka التاريخية القديمة متعددة الشيتات ما زالت مدعومة" in frontend


def test_sparse_appointment_migration_uses_postgresql_ddl_for_exclusion_constraint() -> None:
    import importlib.util
    from types import SimpleNamespace

    migration_path = _backend() / "alembic/versions/0046_sparse_appointment_context.py"
    spec = importlib.util.spec_from_file_location("migration_0046_sparse_context", migration_path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    calls: list[tuple[str, str]] = []

    def _execute(statement: object) -> None:
        calls.append(("execute", str(statement)))

    migration.op = SimpleNamespace(
        add_column=lambda table, column: calls.append(("add_column", table)),
        execute=_execute,
        drop_column=lambda table, column: calls.append(("drop_column", f"{table}.{column}")),
    )

    migration.upgrade()
    assert calls[0] == ("add_column", "appointments")
    assert calls[1] == (
        "execute",
        "ALTER TABLE appointments DROP CONSTRAINT excl_appointments_doctor_busy_time",
    )
    assert "EXCLUDE USING gist" in calls[2][1]
    assert "doctor_assignment_known AND status IN" in calls[2][1]

    calls.clear()
    migration.downgrade()
    assert calls[0] == (
        "execute",
        "ALTER TABLE appointments DROP CONSTRAINT excl_appointments_doctor_busy_time",
    )
    assert "EXCLUDE USING gist" in calls[1][1]
    assert "doctor_assignment_known AND status IN" not in calls[1][1]
    assert calls[2] == ("drop_column", "appointments.doctor_assignment_known")

    source = migration_path.read_text(encoding="utf-8")
    assert 'type_="exclude"' not in source
