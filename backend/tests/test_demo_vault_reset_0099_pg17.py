"""PG17.6 integration: demo reset must never circumvent immutable Vault history.

Only accepts explicitly named disposable localhost PG17.6 databases. Tests
create synthetic workspaces and leave them to the disposable DB teardown.
No production data, global trigger changes, or privileged deletion mechanisms.
"""
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, event, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.api.routes import vault as vault_api
from app.models.activity_event import ActivityEvent
from app.models.appointment import Appointment
from app.models.booking_settings import BookingSettings
from app.models.branch import Branch
from app.models.doctor import Doctor
from app.models.patient import Patient
from app.models.payment_transaction import PaymentAllocation, PaymentTransaction
from app.models.service import Service
from app.models.staff import Staff
from app.models.user import User
from app.models.vault import VaultAccount, VaultLedgerEntry, VaultPendingPayment
from app.models.workspace import Workspace
from app.models.workspace_member import WorkspaceMember
from app.schemas.vault import VaultActivationRequest
from app.services.demo_reset import (
    DEMO_RESET_ACTION,
    DemoResetError,
    _workspace_tables,
    capture_demo_canonical_seed,
    reset_demo_workspace,
    scheduled_demo_reset_state,
)
from app.services.vault import (
    VaultError,
    activate_account,
    record_cash_expense,
    transfer_full_balance,
)


@pytest.fixture(scope="module")
def pg17_engine():
    required = os.environ.get("VAULT_PG17_REQUIRED") == "1"
    raw = (
        os.environ.get("VAULT_PG17_TEST_DATABASE_URL")
        if required
        else os.environ.get("VAULT_DEMO_RESET_PG17_DATABASE_URL")
    )
    if not raw:
        if required:
            pytest.fail("Mandatory Demo Vault PG17 test database URL missing")
        pytest.skip("Run the independent disposable PostgreSQL 17.6 Demo Vault suite")

    parsed = make_url(raw)
    targets = {
        ("127.0.0.1", 55455, "vault_demo_0099_ci", "postgres"),
        ("127.0.0.1", 55443, "vault_0099_financial_test", "postgres"),
    }
    selected = (parsed.host, parsed.port, parsed.database, parsed.username)
    if selected not in targets or (
        required and selected != ("127.0.0.1", 55443, "vault_0099_financial_test", "postgres")
    ):
        pytest.fail("Refusing non-disposable/non-local PG17 Demo Vault test database")
    if os.environ.get("ENVIRONMENT") != "test":
        pytest.fail("ENVIRONMENT=test is required for Demo Vault integration tests")
    engine = create_engine(raw, connect_args={"connect_timeout": 5}, pool_pre_ping=True)
    try:
        with engine.connect() as db:
            assert db.scalar(text("SELECT current_setting('server_version_num')")) == "170006"
            assert db.scalar(text("SELECT version_num FROM public.alembic_version")) == (
                "0099_reception_main_vault_ledger"
            )
        yield engine
    finally:
        engine.dispose()


def _synthetic_workspace(engine, *, demo=True) -> SimpleNamespace:
    key = uuid4().hex[:12]
    with Session(engine) as db:
        workspace = Workspace(
            name="Synthetic Vault Reset Clinic", slug="demo-vault-atomic-" + key,
            timezone="Africa/Cairo", is_demo=demo, is_active=True,
        )
        user = User(email="demo-vault-" + key + "@example.invalid", auth_user_id=uuid4())
        db.add_all([workspace, user])
        db.flush()
        member = WorkspaceMember(
            workspace_id=workspace.id, user_id=user.id, role="admin", is_active=True,
        )
        branch = Branch(workspace_id=workspace.id, name="Synthetic Demo", code="VLT-DEM")
        staff = Staff(workspace_id=workspace.id, first_name="Synthetic", last_name="Doctor")
        service = Service(
            workspace_id=workspace.id, name="Synthetic Demo Service",
            slug="demo-vault-service", category="facial", duration_minutes=30,
            price_minor=10000, currency="EGP",
        )
        booking = BookingSettings(
            workspace_id=workspace.id, slot_interval_minutes=30,
            minimum_notice_minutes=0, booking_horizon_days=90,
            cancellation_notice_minutes=60, allow_same_day_booking=True,
            require_confirmation=False,
        )
        patient = Patient(workspace_id=workspace.id, first_name="Synthetic Demo Patient")
        db.add_all([member, branch, staff, service, booking, patient])
        db.flush()
        doctor = Doctor(workspace_id=workspace.id, staff_id=staff.id)
        db.add(doctor)
        db.flush()
        start = datetime(2034, 4, 5, 11, tzinfo=UTC)
        appointment = Appointment(
            workspace_id=workspace.id, branch_id=branch.id,
            patient_id=patient.id, doctor_id=doctor.id, service_id=service.id,
            status="completed", source="staff",
            start_at=start, end_at=start + timedelta(minutes=30),
            busy_start_at=start, busy_end_at=start + timedelta(minutes=30),
            duration_minutes=30, price_minor=10000, currency="EGP",
        )
        db.add(appointment)
        workspace.primary_branch_id = branch.id
        db.flush()
        result = SimpleNamespace(
            workspace=workspace.id, user=user.id, branch=branch.id,
            patient=patient.id, appointment=appointment.id,
        )
        db.commit()
        if demo:
            capture_demo_canonical_seed(db, workspace_id=result.workspace)
        return result


def _post_financial_history(engine, identity) -> SimpleNamespace:
    wid, actor, branch_id, appt = (
        identity.workspace, identity.user, identity.branch, identity.appointment,
    )
    with Session(engine) as db:
        if db.get(Workspace, wid).is_demo:
            # Legacy synthetic fixture only: these records model a Demo that
            # already had a posted ledger before the new activation policy.
            # Do not call the now-prohibited activation service for a Demo.
            main = VaultAccount(
                workspace_id=wid, kind="main_cash", currency="EGP",
                branch_id=None, opened_by_user_id=actor,
            )
            reception = VaultAccount(
                workspace_id=wid, kind="reception", currency="EGP",
                branch_id=branch_id, opened_by_user_id=actor,
            )
            db.add_all([main, reception])
            db.flush()
            db.add(VaultLedgerEntry(
                workspace_id=wid, account_id=reception.id, kind="opening",
                amount_minor=10000, actor_user_id=actor,
                note="Synthetic legacy Demo record predating activation policy",
            ))
        else:
            activate_account(
                db, workspace_id=wid, actor_user_id=actor, kind="main_cash",
                branch_id=None, opening_balance_minor=0, currency="EGP",
            )
            reception = activate_account(
                db, workspace_id=wid, actor_user_id=actor, kind="reception",
                branch_id=branch_id, opening_balance_minor=10000, currency="EGP",
            )
        reception_id = reception.id
        db.commit()

    with Session(engine) as db:
        cash = PaymentTransaction(
            workspace_id=wid, patient_id=identity.patient,
            appointment_id=appt, origin_appointment_id=appt,
            created_by_user_id=actor, transaction_type="payment",
            amount_minor=5000, currency="EGP", payment_method="cash",
            source="staff", created_at=datetime.now(UTC),
        )
        db.add(cash)
        db.flush()
        payment_id = cash.id
        allocation = PaymentAllocation(
            workspace_id=wid, transaction_id=payment_id,
            appointment_id=appt, amount_minor=5000,
        )
        db.add(allocation)
        db.flush()
        allocation_id = allocation.id
        entry = db.scalar(select(VaultLedgerEntry).where(
            VaultLedgerEntry.workspace_id == wid,
            VaultLedgerEntry.payment_transaction_id == payment_id,
        ))
        assert entry is not None and entry.amount_minor == 5000
        db.commit()

    with Session(engine) as db:
        expense = record_cash_expense(
            db, workspace_id=wid, branch=db.get(Branch, branch_id),
            actor_user_id=actor, amount_minor=2000,
            description="Synthetic demo financial expense",
            idempotency_key="demo-atomic-expense-" + uuid4().hex,
            workspace_timezone="Africa/Cairo",
        )
        expense_id = expense.id
        transfer = transfer_full_balance(
            db, workspace_id=wid, branch_id=branch_id,
            actor_user_id=actor, idempotency_key="demo-atomic-transfer-" + uuid4().hex,
        )
        assert transfer.amount_minor == 13000
        db.commit()

    with Session(engine) as db:
        entries = list(db.scalars(select(VaultLedgerEntry).where(
            VaultLedgerEntry.workspace_id == wid,
        )))
        assert len(entries) == 5
        assert sorted((e.kind, e.amount_minor) for e in entries) == sorted([
            ("opening", 10000), ("payment", 5000), ("expense", -2000),
            ("transfer_out", -13000), ("transfer_in", 13000),
        ])
        reception_balance = db.scalar(select(func.sum(VaultLedgerEntry.amount_minor)).where(
            VaultLedgerEntry.workspace_id == wid, VaultLedgerEntry.account_id == reception_id,
        ))
        assert reception_balance == 0
    return SimpleNamespace(
        payment=payment_id, allocation=allocation_id, expense=expense_id,
        reception=reception_id, ledger_entry=entries[0].id,
    )


def _snapshot(engine, workspace_id) -> dict:
    """Fingerprint all modeled workspace-owned facts, not only Ledger balances."""
    result = {}
    tables = _workspace_tables()
    with Session(engine) as db:
        for name, table in sorted(tables.items()):
            rows = db.execute(select(table).where(table.c.workspace_id == workspace_id)).mappings()
            result[name] = sorted(
                tuple((column, repr(value)) for column, value in row.items())
                for row in rows
            )
        workspace = db.get(Workspace, workspace_id)
        result["workspace_state"] = (
            workspace.id, workspace.primary_branch_id, workspace.is_demo, workspace.is_active,
        )
    return result


def _assert_no_reset_marker(engine, workspace_id):
    with Session(engine) as db:
        assert db.scalar(select(func.count(ActivityEvent.id)).where(
            ActivityEvent.workspace_id == workspace_id,
            ActivityEvent.action == DEMO_RESET_ACTION,
        )) == 0


def test_demo_vault_activation_is_denied_without_any_opening_ledger(pg17_engine):
    fx = _synthetic_workspace(pg17_engine)
    before = _snapshot(pg17_engine, fx.workspace)
    for kind, branch_id, opening in (
        ("main_cash", None, 7000),
        ("main_visa", None, 0),
        ("reception", fx.branch, 4500),
    ):
        with Session(pg17_engine) as db:
            with pytest.raises(VaultError, match="unavailable in demo mode"):
                activate_account(
                    db, workspace_id=fx.workspace, actor_user_id=fx.user,
                    kind=kind, branch_id=branch_id,
                    opening_balance_minor=opening, currency="EGP",
                )
            db.rollback()
    assert _snapshot(pg17_engine, fx.workspace) == before


def test_demo_activation_direct_api_handler_returns_business_conflict(pg17_engine):
    fx = _synthetic_workspace(pg17_engine)
    with Session(pg17_engine) as db:
        access = SimpleNamespace(
            workspace=db.get(Workspace, fx.workspace),
            user=db.get(User, fx.user),
        )
        payload = VaultActivationRequest(
            kind="main_cash", branch_id=None,
            opening_balance_minor=10000, currency="EGP",
        )
        with pytest.raises(HTTPException) as error:
            vault_api.activate(payload=payload, access=access, db=db)
        assert error.value.status_code == 409
        assert "unavailable in demo mode" in error.value.detail
        assert db.scalar(select(func.count(VaultAccount.id)).where(
            VaultAccount.workspace_id == fx.workspace,
        )) == 0
        assert db.scalar(select(func.count(VaultLedgerEntry.id)).where(
            VaultLedgerEntry.workspace_id == fx.workspace,
        )) == 0


def test_real_workspace_vault_activation_and_opening_still_work(pg17_engine):
    fx = _synthetic_workspace(pg17_engine, demo=False)
    with Session(pg17_engine) as db:
        main = activate_account(
            db, workspace_id=fx.workspace, actor_user_id=fx.user,
            kind="main_cash", branch_id=None,
            opening_balance_minor=8500, currency="EGP",
        )
        db.commit()
        ledger = db.scalar(select(VaultLedgerEntry).where(
            VaultLedgerEntry.workspace_id == fx.workspace,
            VaultLedgerEntry.account_id == main.id,
            VaultLedgerEntry.kind == "opening",
        ))
        assert ledger is not None and ledger.amount_minor == 8500


def test_demo_payment_and_allocation_stay_supported_before_daily_reset(pg17_engine):
    fx = _synthetic_workspace(pg17_engine)
    with Session(pg17_engine) as db:
        for method, amount in (("cash", 1700), ("visa", 2300), ("instapay", 1300)):
            payment = PaymentTransaction(
                workspace_id=fx.workspace, appointment_id=fx.appointment,
                origin_appointment_id=fx.appointment, patient_id=fx.patient,
                created_by_user_id=fx.user, transaction_type="payment",
                amount_minor=amount, payment_method=method,
                currency="EGP", source="staff",
                created_at=datetime.now(UTC),
            )
            db.add(payment)
            db.flush()
            if method == "cash":
                db.add(PaymentAllocation(
                    workspace_id=fx.workspace, transaction_id=payment.id,
                    appointment_id=fx.appointment, amount_minor=amount,
                ))
                db.flush()
        db.commit()
        assert db.scalar(select(func.count(PaymentTransaction.id)).where(
            PaymentTransaction.workspace_id == fx.workspace,
        )) == 3
        assert db.scalar(select(func.count(VaultPendingPayment.id)).where(
            VaultPendingPayment.workspace_id == fx.workspace,
        )) == 3
        assert db.scalar(select(func.count(VaultAccount.id)).where(
            VaultAccount.workspace_id == fx.workspace,
        )) == 0
        assert db.scalar(select(func.count(VaultLedgerEntry.id)).where(
            VaultLedgerEntry.workspace_id == fx.workspace,
        )) == 0
        assert scheduled_demo_reset_state(
            db, workspace=db.get(Workspace, fx.workspace),
            now=datetime(2026, 11, 14, 2, 3, tzinfo=UTC),
        ) == "reset"
        assert db.scalar(select(func.count(PaymentTransaction.id)).where(
            PaymentTransaction.workspace_id == fx.workspace,
        )) == 0
        assert db.scalar(select(func.count(VaultPendingPayment.id)).where(
            VaultPendingPayment.workspace_id == fx.workspace,
        )) == 0


def test_demo_without_vault_postings_resets_and_clears_pending(pg17_engine):
    fx = _synthetic_workspace(pg17_engine)
    with Session(pg17_engine) as db:
        db.add(PaymentTransaction(
            workspace_id=fx.workspace, appointment_id=fx.appointment,
            origin_appointment_id=fx.appointment, patient_id=fx.patient,
            transaction_type="payment", amount_minor=500,
            currency="EGP", payment_method="cash", source="staff",
        ))
        db.commit()
        assert db.scalar(select(func.count(VaultPendingPayment.id)).where(
            VaultPendingPayment.workspace_id == fx.workspace,
        )) == 1
        result = reset_demo_workspace(db, workspace_id=fx.workspace)
        assert result.reseeded_rows > 0
        assert db.scalar(select(func.count(VaultPendingPayment.id)).where(
            VaultPendingPayment.workspace_id == fx.workspace,
        )) == 0


def test_daily_demo_without_vault_resets_on_schedule_once(pg17_engine):
    fx = _synthetic_workspace(pg17_engine)
    scheduled_time = datetime(2026, 11, 14, 2, 3, tzinfo=UTC)
    with Session(pg17_engine) as db:
        workspace = db.get(Workspace, fx.workspace)
        assert scheduled_demo_reset_state(
            db, workspace=workspace, now=scheduled_time,
        ) == "reset"
        assert scheduled_demo_reset_state(
            db, workspace=workspace, now=scheduled_time,
        ) == "already_reset"
        assert db.scalar(select(func.count(ActivityEvent.id)).where(
            ActivityEvent.workspace_id == fx.workspace,
            ActivityEvent.action == DEMO_RESET_ACTION,
        )) == 1


def test_zero_opening_vault_without_ledger_can_reset(pg17_engine):
    fx = _synthetic_workspace(pg17_engine)
    with Session(pg17_engine) as db:
        # Pre-policy synthetic accounts with zero opening ledger: the existing
        # reset must still remove them without an immutability exception.
        db.add_all([
            VaultAccount(workspace_id=fx.workspace, opened_by_user_id=fx.user,
                         kind="main_cash", branch_id=None, currency="EGP"),
            VaultAccount(workspace_id=fx.workspace, opened_by_user_id=fx.user,
                         kind="reception", branch_id=fx.branch, currency="EGP"),
        ])
        db.commit()
        assert db.scalar(select(func.count(VaultLedgerEntry.id)).where(
            VaultLedgerEntry.workspace_id == fx.workspace,
        )) == 0
        reset_demo_workspace(db, workspace_id=fx.workspace)
        assert db.scalar(select(func.count(VaultAccount.id)).where(
            VaultAccount.workspace_id == fx.workspace,
        )) == 0


def test_demo_with_posted_financial_history_rejects_before_any_delete(pg17_engine):
    fx = _synthetic_workspace(pg17_engine)
    _post_financial_history(pg17_engine, fx)
    before = _snapshot(pg17_engine, fx.workspace)
    deletes = []

    def observe(_conn, _cursor, statement, _parameters, _context, _executemany):
        if statement.lstrip().upper().startswith("DELETE"):
            deletes.append(statement)

    event.listen(pg17_engine, "before_cursor_execute", observe)
    try:
        with Session(pg17_engine) as db:
            with pytest.raises(DemoResetError, match="immutable Vault financial history"):
                reset_demo_workspace(db, workspace_id=fx.workspace)
    finally:
        event.remove(pg17_engine, "before_cursor_execute", observe)
    assert not deletes, deletes
    assert _snapshot(pg17_engine, fx.workspace) == before
    _assert_no_reset_marker(pg17_engine, fx.workspace)


def test_daily_demo_reset_with_posted_history_fails_closed_atomically(pg17_engine):
    fx = _synthetic_workspace(pg17_engine)
    _post_financial_history(pg17_engine, fx)
    before = _snapshot(pg17_engine, fx.workspace)
    with Session(pg17_engine) as db:
        with pytest.raises(DemoResetError, match="immutable Vault financial history"):
            scheduled_demo_reset_state(
                db, workspace=db.get(Workspace, fx.workspace),
                now=datetime(2026, 11, 14, 2, 3, tzinfo=UTC),
            )
    assert _snapshot(pg17_engine, fx.workspace) == before
    _assert_no_reset_marker(pg17_engine, fx.workspace)


def test_real_workspace_cannot_reset_even_with_posted_vault(pg17_engine):
    fx = _synthetic_workspace(pg17_engine, demo=False)
    _post_financial_history(pg17_engine, fx)
    before = _snapshot(pg17_engine, fx.workspace)
    with Session(pg17_engine) as db:
        with pytest.raises(DemoResetError, match="Refusing to reset a real workspace"):
            reset_demo_workspace(db, workspace_id=fx.workspace)
    assert _snapshot(pg17_engine, fx.workspace) == before


def test_real_and_demo_ledger_and_posted_evidence_remain_immutable(pg17_engine):
    for is_demo in (True, False):
        fx = _synthetic_workspace(pg17_engine, demo=is_demo)
        posted = _post_financial_history(pg17_engine, fx)
        with Session(pg17_engine) as db:
            entry_id = db.scalar(select(VaultLedgerEntry.id).where(
                VaultLedgerEntry.workspace_id == fx.workspace,
                VaultLedgerEntry.kind == "opening",
            ))
            checks = [
                ("UPDATE vault_ledger_entries SET amount_minor=amount_minor+1 WHERE id=:id",
                 entry_id, "VAULT_LEDGER_IMMUTABLE"),
                ("DELETE FROM vault_ledger_entries WHERE id=:id",
                 entry_id, "VAULT_LEDGER_IMMUTABLE"),
                ("DELETE FROM payment_allocations WHERE id=:id",
                 posted.allocation, "VAULT_POSTED_ALLOCATION_IMMUTABLE"),
                ("UPDATE expenses SET title='CHANGED' WHERE id=:id",
                 posted.expense, "VAULT_POSTED_EXPENSE_IMMUTABLE"),
            ]
            for statement, pk, error in checks:
                with pytest.raises(DBAPIError, match=error):
                    with db.begin_nested():
                        db.execute(text(statement), {"id": pk})
            db.rollback()
        with pg17_engine.connect() as db:
            trigger = db.execute(text(
                "SELECT t.tgenabled, p.prosrc FROM pg_trigger t "
                "JOIN pg_proc p ON p.oid=t.tgfoid "
                "WHERE t.tgname='vault_ledger_immutable' "
                "AND t.tgrelid='public.vault_ledger_entries'::regclass"
            )).one()
            assert trigger[0] == "O" and "VAULT_LEDGER_IMMUTABLE" in trigger[1]


def test_other_demo_reset_does_not_touch_posted_financial_workspace(pg17_engine):
    posted_fx = _synthetic_workspace(pg17_engine)
    _post_financial_history(pg17_engine, posted_fx)
    before = _snapshot(pg17_engine, posted_fx.workspace)
    clean_fx = _synthetic_workspace(pg17_engine)
    with Session(pg17_engine) as db:
        result = reset_demo_workspace(db, workspace_id=clean_fx.workspace)
        assert result.cleared_rows >= 0
    assert _snapshot(pg17_engine, posted_fx.workspace) == before
    with Session(pg17_engine) as db:
        assert db.scalar(select(func.count(VaultLedgerEntry.id)).where(
            VaultLedgerEntry.workspace_id == posted_fx.workspace,
        )) == 5


def test_injected_reset_failure_rolls_back_unposted_financial_data(pg17_engine):
    fx = _synthetic_workspace(pg17_engine)
    with Session(pg17_engine) as db:
        db.add(PaymentTransaction(
            workspace_id=fx.workspace, appointment_id=fx.appointment,
            origin_appointment_id=fx.appointment, patient_id=fx.patient,
            transaction_type="payment", amount_minor=450,
            currency="EGP", payment_method="cash", source="staff",
        ))
        db.commit()
    before = _snapshot(pg17_engine, fx.workspace)
    with Session(pg17_engine) as db:
        with pytest.raises(DemoResetError, match="Injected"):
            reset_demo_workspace(
                db, workspace_id=fx.workspace, injected_failure_after_clear=True,
            )
    assert _snapshot(pg17_engine, fx.workspace) == before
    _assert_no_reset_marker(pg17_engine, fx.workspace)
