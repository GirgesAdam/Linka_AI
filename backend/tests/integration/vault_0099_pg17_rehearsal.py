"""Local-only PostgreSQL 17.6 service concurrency; disposable restored replica.

Never connects to production. DB is a throwaway localhost-only container.
Results intentionally exclude patient details and credentials.
"""
from __future__ import annotations

import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from uuid import uuid4

from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.models.appointment import Appointment
from app.models.branch import Branch
from app.models.doctor import Doctor
from app.models.patient import Patient
from app.models.payment_transaction import PaymentAllocation, PaymentTransaction
from app.models.service import Service
from app.models.staff import Staff
from app.models.user import User
from app.models.vault import VaultAccount, VaultLedgerEntry, VaultPendingPayment, VaultTransfer
from app.models.workspace import Workspace
from app.models.workspace_member import WorkspaceMember
from app.services.vault import (
    VaultError,
    _balance,
    activate_account,
    record_cash_expense,
    transfer_full_balance,
)
from app.services.vault_pending import (
    post_eligible_pending_electronic,
    resolve_pending_payment,
    review_preactivation_electronic,
)

url = os.environ.get("VAULT_PG17_TEST_DATABASE_URL")
if not url:
    raise RuntimeError("Set VAULT_PG17_TEST_DATABASE_URL to a disposable localhost PG17 database.")

parsed = make_url(url)
if (
    parsed.host not in ("localhost", "127.0.0.1")
    or parsed.port != 55443
    or parsed.database != "vault_0099_financial_test"
    or parsed.username != "postgres"
):
    raise RuntimeError("Refusing any non-disposable/non-local PostgreSQL test target.")
engine = create_engine(url, pool_size=8, max_overflow=4, pool_pre_ping=True,
                       connect_args={"connect_timeout": 5})
with engine.connect() as _c:
    _version = int(_c.scalar(text("SELECT current_setting('server_version_num')")))
    _rev = _c.scalar(text("SELECT version_num FROM public.alembic_version"))
    if _version // 10000 != 17 or _rev != "0099_reception_main_vault_ledger":
        raise RuntimeError("Requires local PostgreSQL 17 at exactly Migration 0099.")


def require(condition, message):
    if not condition:
        raise AssertionError(message)


# The required CI suite MUST generate its own synthetic fixture.
# Never search a restored/production workspace, appointment or patient.
with Session(engine) as db:
    require(db.scalar(select(func.count(Workspace.id))) == 0, "NONEMPTY_DB_REFUSED")
    require(db.scalar(select(func.count(Appointment.id))) == 0, "NONEMPTY_APPOINTMENTS_REFUSED")
    require(db.scalar(select(func.count(VaultAccount.id))) == 0, "EXISTING_VAULT_REFUSED")
    seed_tag = uuid4().hex[:12]
    workspace = Workspace(name="Synthetic Vault PG17 Workspace", slug="ci-vault-" + seed_tag)
    user = User(email="vault-financial-" + seed_tag + "@example.invalid")
    db.add_all([workspace, user])
    db.flush()
    member = WorkspaceMember(
        workspace_id=workspace.id, user_id=user.id, role="admin", is_active=True,
    )
    source_branch = Branch(
        workspace_id=workspace.id, name="Synthetic Cash Reception", code="VLT-A",
    )
    source_patient = Patient(workspace_id=workspace.id, first_name="Synthetic Vault Patient")
    staff = Staff(
        workspace_id=workspace.id, first_name="Synthetic", last_name="Clinician",
    )
    service = Service(
        workspace_id=workspace.id, name="Synthetic Clinic Service",
        slug="ci-vault-service", operational_category="dermatology",
        requires_laser_device=False, duration_minutes=30, price_minor=20000,
    )
    db.add_all([member, source_branch, source_patient, staff, service])
    db.flush()
    doctor = Doctor(workspace_id=workspace.id, staff_id=staff.id)
    db.add(doctor)
    db.flush()
    start = datetime(2034, 1, 5, 12, tzinfo=UTC)
    appointment = Appointment(
        workspace_id=workspace.id, patient_id=source_patient.id,
        branch_id=source_branch.id, doctor_id=doctor.id, service_id=service.id,
        status="completed", source="staff", doctor_assignment_known=False,
        start_at=start, end_at=start + timedelta(minutes=30),
        busy_start_at=start, busy_end_at=start + timedelta(minutes=30),
        duration_minutes=30, price_minor=20000, currency="EGP",
    )
    db.add(appointment)
    db.flush()
    ws, branch, appt, patient, actor = (
        workspace.id, source_branch.id, appointment.id, source_patient.id, user.id,
    )
    db.commit()
print("SYNTHETIC_CI_FINANCIAL_FIXTURE_SEEDED")

# Deliberately record electronic receipts BEFORE any vault account exists.
# The new 0099 hook must retain their identities in Pending, not silently drop.
preactivation_electronic = {}
with Session(engine) as db:
    for method, amount in (("visa", 10_000), ("instapay", 12_000)):
        receipt = PaymentTransaction(
            id=uuid4(), workspace_id=ws, appointment_id=appt,
            origin_appointment_id=appt, patient_id=patient,
            transaction_type="payment", amount_minor=amount,
            currency="EGP", payment_method=method,
            source="staff", created_at=datetime.now(UTC),
        )
        db.add(receipt)
        db.flush()
        preactivation_electronic[method] = receipt.id
        pending = db.scalar(select(VaultPendingPayment).where(
            VaultPendingPayment.payment_transaction_id == receipt.id,
        ))
        require(pending is not None and pending.reason == "main_channel_not_activated",
                "PREACTIVATION_ELECTRONIC_NOT_TRACKED")
    db.commit()
print("ELECTRONIC_PREACTIVATION_BEFORE_ANY_ACCOUNT_PENDING_PASS")

with Session(engine) as db:
    for kind, source_branch, opening in (
        ("reception", branch, 0), ("main_cash", None, 0),
        ("main_visa", None, 20_000), ("main_instapay", None, 15_000),
    ):
        activate_account(
            db, workspace_id=ws, actor_user_id=actor,
            kind=kind, branch_id=source_branch,
            opening_balance_minor=opening, currency="EGP",
        )
    db.commit()

with Session(engine) as db:
    reception = db.scalar(select(VaultAccount).where(
        VaultAccount.workspace_id == ws, VaultAccount.kind == "reception",
        VaultAccount.branch_id == branch,
    ))
    main = db.scalar(select(VaultAccount).where(
        VaultAccount.workspace_id == ws, VaultAccount.kind == "main_cash",
    ))
    require(reception is not None and main is not None, "VAULT_NOT_ACTIVATED")
    cash_id, main_id = reception.id, main.id

# Independent synthetic fixtures: one patient across TWO actual branches,
# a second same-branch appointment, and a different-patient appointment.
with Session(engine) as db:
    source_appt = db.get(Appointment, appt)
    alt_branch = Branch(
        workspace_id=ws,
        name="Synthetic Vault Branch " + uuid4().hex[:5],
        code="VB" + uuid4().hex[:8],
        is_active=True,
    )
    alt_patient = Patient(workspace_id=ws, first_name="Synthetic Vault Patient")
    db.add_all([alt_branch, alt_patient])
    db.flush()
    branch2 = alt_branch.id
    other_patient_id = alt_patient.id

    def clone_visit(target_branch, target_patient, offset):
        start = datetime(2034, 2, 1, 8, tzinfo=UTC) + timedelta(minutes=offset)
        item = Appointment(
            workspace_id=ws, branch_id=target_branch,
            patient_id=target_patient, doctor_id=source_appt.doctor_id,
            service_id=source_appt.service_id,
            created_by_user_id=actor, doctor_assignment_known=False,
            status="completed", source="staff",
            start_at=start, end_at=start + timedelta(minutes=30),
            busy_start_at=start, busy_end_at=start + timedelta(minutes=30),
            duration_minutes=30, price_minor=20_000, currency="EGP",
        )
        db.add(item)
        db.flush()
        return item.id

    same_branch_appt = clone_visit(branch, patient, 0)
    different_branch_appt = clone_visit(branch2, patient, 60)
    different_patient_appt = clone_visit(branch, other_patient_id, 120)
    db.commit()
print("SYNTHETIC_TWO_BRANCH_SAME_PATIENT_PG17_FIXTURE_READY")


def balance(account_id):
    with Session(engine) as db:
        return _balance(db, workspace_id=ws, account_id=account_id)


def add_payment(amount):
    """Actual PaymentTransaction + PaymentAllocation flush triggers on PostgreSQL."""
    transaction_id = uuid4()
    with Session(engine) as db:
        p = PaymentTransaction(
            id=transaction_id, workspace_id=ws,
            appointment_id=appt, origin_appointment_id=appt,
            patient_id=patient, created_by_user_id=actor,
            transaction_type="payment", amount_minor=amount,
            currency="EGP", payment_method="cash",
            source="staff", created_at=datetime.now(UTC),
        )
        db.add(p)
        db.flush()
        pending = db.scalar(select(VaultPendingPayment).where(
            VaultPendingPayment.workspace_id == ws,
            VaultPendingPayment.payment_transaction_id == p.id,
        ))
        require(pending is not None, "CASH_UNALLOCATED_NOT_PENDING")
        db.add(PaymentAllocation(
            workspace_id=ws, transaction_id=p.id, appointment_id=appt,
            amount_minor=amount,
        ))
        db.commit()
    with Session(engine) as db:
        posted = db.scalars(select(VaultLedgerEntry).where(
            VaultLedgerEntry.workspace_id == ws,
            VaultLedgerEntry.payment_transaction_id == transaction_id,
        )).all()
        require(len(posted) == 1 and posted[0].amount_minor == amount,
                "CASH_NOT_POSTED_EXACTLY_ONCE")
    return transaction_id


def run_pair(task1, task2):
    barrier = Barrier(2)
    def wrap(task):
        barrier.wait(timeout=10)
        try:
            return task()
        except (VaultError, DBAPIError) as exc:
            return ("REJECTED", type(exc).__name__, str(exc).split("\n", 1)[0][:100])
    with ThreadPoolExecutor(max_workers=2) as ex:
        futures = [ex.submit(wrap, task1), ex.submit(wrap, task2)]
        return [f.result(timeout=20) for f in futures]


def do_transfer(key):
    with Session(engine) as db:
        try:
            result = transfer_full_balance(
                db, workspace_id=ws, branch_id=branch,
                actor_user_id=actor, idempotency_key=key,
            )
            transfer_id = result.id
            amount = result.amount_minor
            db.commit()
            return "OK", transfer_id, amount
        except BaseException:
            db.rollback()
            raise


def do_expense(key, amount):
    with Session(engine) as db:
        try:
            expense = record_cash_expense(
                db, workspace_id=ws, branch=db.get(__import__(
                    "app.models.branch", fromlist=["Branch"]).Branch, branch),
                actor_user_id=actor, amount_minor=amount,
                description="Synthetic cash expense " + key,
                idempotency_key=key, workspace_timezone="Africa/Cairo",
            )
            expense_id = expense.id
            db.commit()
            return "OK", expense_id, amount
        except BaseException:
            db.rollback()
            raise


def do_refund(payment_id, amount):
    refund_id = uuid4()
    with Session(engine) as db:
        try:
            db.add(PaymentTransaction(
                id=refund_id, workspace_id=ws, appointment_id=appt,
                origin_appointment_id=appt, patient_id=patient,
                created_by_user_id=actor, reference_transaction_id=payment_id,
                transaction_type="refund", amount_minor=amount,
                currency="EGP", payment_method="cash",
                source="staff", created_at=datetime.now(UTC),
            ))
            db.flush()
            db.commit()
            return "OK", refund_id, amount
        except BaseException:
            db.rollback()
            return "REJECTED", refund_id, amount


def test_transfer_race():
    add_payment(100_000)
    start = balance(cash_id)
    main_start = balance(main_id)
    results = run_pair(
        lambda: do_transfer("two-transfer-A-" + suffix),
        lambda: do_transfer("two-transfer-B-" + suffix),
    )
    require(sum(r[0] == "OK" for r in results) == 1, "TWO_FULL_TRANSFERS_COMMITTED")
    require(balance(cash_id) == 0, "CASH_NOT_ZERO_AFTER_FULL_HANDOVER")
    require(balance(main_id) - main_start == start, "MAIN_TRANSFER_NOT_EXACTLY_RECEIVED")
    print("TWO_CONCURRENT_FULL_TRANSFERS_PASS")


def test_expenses_race():
    add_payment(100_000)
    results = run_pair(
        lambda: do_expense("expense-A-" + suffix, 70_000),
        lambda: do_expense("expense-B-" + suffix, 70_000),
    )
    require(sum(r[0] == "OK" for r in results) == 1, "BOTH_OVERDRAW_EXPENSES_COMMITTED")
    require(balance(cash_id) == 30_000, "EXPENSE_ACCOUNT_BALANCE_MISMATCH")
    print("TWO_CONCURRENT_EXPENSES_PASS")
    do_transfer("reset-expenses-" + suffix)


def test_expense_transfer_race():
    add_payment(60_000)
    main_start = balance(main_id)
    results = run_pair(
        lambda: do_expense("expense-transfer-" + suffix, 20_000),
        lambda: do_transfer("transfer-expense-" + suffix),
    )
    require(sum(r[0] == "OK" for r in results) in (1, 2), "NO_WINNER_IN_RACE")
    require(balance(cash_id) == 0, "RACE_LEFT_INCORRECT_CASH")
    expense_paid = 20_000 if results[0][0] == "OK" else 0
    require(balance(main_id) - main_start == 60_000 - expense_paid,
            "EXPENSE_VS_TRANSFER_NOT_RECONCILED")
    print("EXPENSE_VS_TRANSFER_RACE_PASS")


def test_refund_transfer_race():
    original = add_payment(100_000)
    main_start = balance(main_id)
    results = run_pair(
        lambda: do_refund(original, 60_000),
        lambda: do_transfer("refund-transfer-" + suffix),
    )
    # Either refund first (40k transfer) or transfer first (100k, refund rejected).
    require(results[1][0] == "OK", "TRANSFER_NOT_COMPLETED")
    refunded = results[0][0] == "OK"
    expected_cash_transfer = 40_000 if refunded else 100_000
    require(balance(cash_id) == 0, "REFUND_RACE_NEGATIVE_CASH")
    require(balance(main_id) - main_start == expected_cash_transfer,
            "REFUND_RACE_MAIN_BALANCE_MISMATCH")
    if not refunded:
        with Session(engine) as db:
            require(db.scalar(select(PaymentTransaction.id).where(
                PaymentTransaction.id == results[0][1])) is None,
                "FAILED_REFUND_HAS_COMMITTED_PAYMENT_TRANSACTION")
    print("CASH_REFUND_VS_TRANSFER_RACE_PASS", "REFUND_POSTED" if refunded else "REFUND_ROLLED_BACK")


def test_pending_resolution_race():
    # Fault injection in disposable DB only: mimic a missed allocation trigger.
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE public.payment_allocations DISABLE TRIGGER vault_payment_allocation_insert"))
    payment_id, pending_id = uuid4(), uuid4()
    try:
        with Session(engine) as db:
            db.add(PaymentTransaction(
                id=payment_id, workspace_id=ws, appointment_id=appt,
                origin_appointment_id=appt, patient_id=patient,
                created_by_user_id=actor, transaction_type="payment",
                amount_minor=9000, currency="EGP", payment_method="cash",
                source="staff", created_at=datetime.now(UTC),
            ))
            db.flush()
            existing = db.scalar(select(VaultPendingPayment).where(
                VaultPendingPayment.payment_transaction_id == payment_id))
            require(existing is not None, "EXPECTED_PENDING_NOT_FOUND")
            pending_id = existing.id
            db.add(PaymentAllocation(
                workspace_id=ws, transaction_id=payment_id,
                appointment_id=appt, amount_minor=9000,
            ))
            db.commit()
    finally:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE public.payment_allocations ENABLE TRIGGER vault_payment_allocation_insert"))
    require(balance(cash_id) == 0, "FAULT_INJECTION_POSTED_EARLY")
    def resolve():
        with Session(engine) as db:
            try:
                row = resolve_pending_payment(
                    db, workspace_id=ws, pending_id=pending_id,
                    branch_id=branch, actor_user_id=actor,
                    explanation="Verified allocation from local synthetic fault injection",
                )
                db.commit()
                return "OK", row.id, row.amount_minor
            except BaseException:
                db.rollback()
                raise
    results = run_pair(resolve, resolve)
    require(sum(x[0] == "OK" for x in results) == 1,
            "PENDING_RESOLUTION_DOUBLE_COMMIT")
    with Session(engine) as db:
        rows = db.scalars(select(VaultLedgerEntry).where(
            VaultLedgerEntry.workspace_id == ws,
            VaultLedgerEntry.payment_transaction_id == payment_id,
        )).all()
        require(len(rows) == 1, "PENDING_SAME_SOURCE_DOUBLE_POST")
        require(db.scalar(select(VaultPendingPayment.id).where(
            VaultPendingPayment.payment_transaction_id == payment_id,
        )) is None, "PENDING_NOT_CLEARED")
    require(balance(cash_id) == 9000, "MANUAL_RESOLUTION_WRONG_BALANCE")
    print("PENDING_RESOLUTION_CONCURRENCY_PASS")
    do_transfer("reset-pending-" + suffix)


def test_duplicate_key_and_interrupted_retry():
    add_payment(33_000)
    key = "duplicate-transfer-" + suffix
    results = run_pair(lambda: do_transfer(key), lambda: do_transfer(key))
    require(all(x[0] == "OK" for x in results), "DUPLICATE_KEY_RETRY_REJECTED")
    require(results[0][1] == results[1][1], "DUPLICATE_KEY_DIFFERENT_TRANSFER")
    with Session(engine) as db:
        records = db.scalars(select(VaultTransfer).where(
            VaultTransfer.workspace_id == ws, VaultTransfer.idempotency_key == key,
        )).all()
        require(len(records) == 1, "DUPLICATE_TRANSFER_CREATED")
        entries = db.scalars(select(VaultLedgerEntry).where(
            VaultLedgerEntry.transfer_id == records[0].id,
        )).all()
        require(len(entries) == 2 and sum(e.amount_minor for e in entries) == 0,
                "DUPLICATE_REQUEST_UNBALANCED")
    retry = do_transfer(key)  # simulation: server committed but response was lost
    require(retry[1] == results[0][1], "POST_TIMEOUT_RETRY_NOT_IDEMPOTENT")
    print("DUPLICATE_KEY_AND_LOST_RESPONSE_RETRY_PASS")


def test_failed_refund_atomic_rollback():
    payment_id = add_payment(15_000)
    do_transfer("full-refund-preflight-" + suffix)
    before_cash, before_main = balance(cash_id), balance(main_id)
    failed = do_refund(payment_id, 10_000)
    require(failed[0] == "REJECTED", "UNFUNDED_REFUND_COMMITTED")
    with Session(engine) as db:
        require(db.scalar(select(PaymentTransaction.id).where(
            PaymentTransaction.id == failed[1])) is None, "REFUND_FACT_SURVIVED_ROLLBACK")
        require(db.scalar(select(VaultLedgerEntry.id).where(
            VaultLedgerEntry.payment_transaction_id == failed[1],
        )) is None, "REFUND_VAULT_ROW_SURVIVED_ROLLBACK")
    require(balance(cash_id) == before_cash and balance(main_id) == before_main,
            "REFUND_FAILURE_CHANGED_VAULT_BALANCES")
    print("FAILED_REFUND_FULL_ROLLBACK_PASS")


def test_posted_allocation_and_appointment_attribution_guards():
    transaction_id = add_payment(8_000)
    before_cash = balance(cash_id)
    for label, target in (
        ("same_branch", same_branch_appt),
        ("different_branch", different_branch_appt),
        ("different_patient", different_patient_appt),
    ):
        with Session(engine) as db:
            a = db.scalar(select(PaymentAllocation).where(
                PaymentAllocation.transaction_id == transaction_id,
            ))
            a.appointment_id = target
            try:
                db.flush()
                raise AssertionError("POSTED_APPOINTMENT_REASSIGNED_" + label)
            except DBAPIError as exc:
                require("VAULT_POSTED_ALLOCATION_IMMUTABLE" in str(exc.orig),
                        "APPOINTMENT_REASSIGNMENT_WRONG_REJECTION_" + label)
                db.rollback()
    for field, new_value in (
        ("branch_id", branch2),
        ("patient_id", other_patient_id),
    ):
        with Session(engine) as db:
            visit = db.get(Appointment, appt)
            setattr(visit, field, new_value)
            try:
                db.flush()
                raise AssertionError("POSTED_APPOINTMENT_CHANGED_" + field)
            except DBAPIError as exc:
                require("VAULT_POSTED_CASH_APPOINTMENT_ATTRIBUTION_IMMUTABLE" in str(exc.orig),
                        "UNDERLYING_APPOINTMENT_GUARD_WRONG_" + field)
                db.rollback()
    # Non-custody fields must continue to be editable (e.g. internal note).
    with Session(engine) as db:
        visit = db.get(Appointment, appt)
        visit.customer_note = "Synthetic, non-financial note allowed"
        db.flush()
        db.rollback()
    require(balance(cash_id) == before_cash,
            "REJECTED_APPOINTMENT_MUTATION_AFFECTED_LEDGER")

    # Legitimate PRE-posting appointment/amount correction is allowed and
    # the AFTER UPDATE hook must now complete the pending receipt exactly once.
    source_id = uuid4()
    with Session(engine) as db:
        db.add(PaymentTransaction(
            id=source_id, workspace_id=ws, patient_id=patient,
            appointment_id=appt, origin_appointment_id=appt,
            transaction_type="payment", amount_minor=20_000,
            currency="EGP", payment_method="cash", source="staff",
            created_at=datetime.now(UTC),
        ))
        db.flush()
        allocation = PaymentAllocation(
            workspace_id=ws, transaction_id=source_id,
            appointment_id=appt, amount_minor=5_000,
        )
        db.add(allocation)
        db.flush()
        require(db.scalar(select(VaultLedgerEntry.id).where(
            VaultLedgerEntry.payment_transaction_id == source_id,
        )) is None, "PARTIAL_ALLOCATION_POSTED_PREMATURELY")
        allocation.appointment_id = same_branch_appt
        allocation.amount_minor = 20_000
        db.flush()
        db.commit()
    with Session(engine) as db:
        posted = db.scalars(select(VaultLedgerEntry).where(
            VaultLedgerEntry.payment_transaction_id == source_id,
        )).all()
        require(len(posted) == 1 and posted[0].account_id == cash_id
                and posted[0].amount_minor == 20_000,
                "PREPOSTING_ALLOCATION_CORRECTION_NOT_POSTED_ONCE")
    print("POSTED_APPOINTMENT_IMMUTABLE_PREPOSTING_CORRECTIONS_PASS")
    do_transfer("immutable-guard-reset-" + suffix)


def test_channel_posting_and_source_variants():
    entries = []
    for method, source in (("visa", "integration"), ("instapay", "system")):
        transaction_id = uuid4()
        with Session(engine) as db:
            db.add(PaymentTransaction(
                id=transaction_id, workspace_id=ws, appointment_id=appt,
                origin_appointment_id=appt, patient_id=patient,
                transaction_type="payment", amount_minor=12_345,
                currency="EGP", payment_method=method, source=source,
                created_at=datetime.now(UTC),
            ))
            db.commit()
        with Session(engine) as db:
            matching = db.scalars(select(VaultLedgerEntry).join(
                VaultAccount, VaultAccount.id == VaultLedgerEntry.account_id,
            ).where(
                VaultLedgerEntry.payment_transaction_id == transaction_id,
                VaultAccount.kind == "main_" + method,
                VaultLedgerEntry.amount_minor == 12_345,
            )).all()
            require(len(matching) == 1, "ELECTRONIC_POST_NOT_EXACTLY_ONCE")
            entries.append(transaction_id)
    print("ELECTRONIC_CHANNELS_POST_WITHOUT_BRANCH_PASS")


def test_historical_payment_stays_outside_new_vault():
    from datetime import timedelta
    transaction_id = uuid4()
    with Session(engine) as db:
        db.add(PaymentTransaction(
            id=transaction_id, workspace_id=ws, appointment_id=appt,
            origin_appointment_id=appt, patient_id=patient,
            transaction_type="payment", amount_minor=3_000,
            currency="EGP", payment_method="cash", source="staff",
            created_at=datetime.now(UTC) - timedelta(days=10),
        ))
        db.flush()
        db.add(PaymentAllocation(
            workspace_id=ws, transaction_id=transaction_id,
            appointment_id=appt, amount_minor=3_000,
        ))
        db.commit()
    with Session(engine) as db:
        require(db.scalar(select(VaultLedgerEntry.id).where(
            VaultLedgerEntry.payment_transaction_id == transaction_id,
        )) is None, "PREACTIVATION_PAYMENT_GOT_BACKFILLED")
    print("PRE_ACTIVATION_NO_UNAPPROVED_CASH_BACKFILL_PASS")


def test_source_allocation_guards():
    source_id = add_payment(6_000)
    with Session(engine) as db:
        row = db.scalar(select(PaymentAllocation).where(
            PaymentAllocation.transaction_id == source_id,
        ))
        require(row is not None, "MISSING_SOURCE_ALLOCATION")
        row.amount_minor = 1
        try:
            db.flush()
            raise AssertionError("POSTED_ALLOCATION_AMOUNT_MUTATED")
        except DBAPIError as exc:
            require("VAULT_POSTED_ALLOCATION_IMMUTABLE" in str(exc.orig),
                    "INCORRECT_UPDATE_GUARD")
            db.rollback()
    with Session(engine) as db:
        row = db.scalar(select(PaymentAllocation).where(
            PaymentAllocation.transaction_id == source_id,
        ))
        db.delete(row)
        try:
            db.flush()
            raise AssertionError("POSTED_ALLOCATION_DELETED")
        except DBAPIError as exc:
            require("VAULT_POSTED_ALLOCATION_IMMUTABLE" in str(exc.orig),
                    "INCORRECT_DELETE_GUARD")
            db.rollback()
    with Session(engine) as db:
        alternate = db.scalar(select(Appointment.id).where(
            Appointment.workspace_id == ws, Appointment.id != appt,
        ))
    if alternate is not None:
        with Session(engine) as db:
            db.add(PaymentAllocation(
                workspace_id=ws, transaction_id=source_id,
                appointment_id=alternate, amount_minor=1,
            ))
            try:
                db.flush()
                raise AssertionError("POSTED_PAYMENT_OVERALLOCATED")
            except DBAPIError as exc:
                require("VAULT_ALLOCATION_EXCEEDS_POSTED_PAYMENT" in str(exc.orig),
                        "INCORRECT_OVERALLOCATION_GUARD")
                db.rollback()
        print("POSTED_ALLOCATION_EXCESS_INSERT_GUARD_PASS")
    else:
        print("POSTED_ALLOCATION_EXCESS_INSERT_NOT_TESTED_NO_SECOND_APPOINTMENT")
    with Session(engine) as db:
        linked = db.scalars(select(VaultLedgerEntry).where(
            VaultLedgerEntry.payment_transaction_id == source_id,
        )).all()
        require(len(linked) == 1 and linked[0].amount_minor == 6000,
                "ALLOCATION_REJECTION_CHANGED_VAULT")
    print("POSTED_ALLOCATION_UPDATE_DELETE_GUARDS_PASS")
    do_transfer("guard-cleanup-" + suffix)


def test_cross_patient_payment_never_hits_vault():
    with Session(engine) as db:
        other = db.scalar(select(Appointment.id).where(
            Appointment.workspace_id == ws,
            Appointment.patient_id != patient,
            Appointment.branch_id.is_not(None),
        ).limit(1))
    if other is None:
        print("CROSS_PATIENT_CASE_NOT_TESTED_NO_FIXTURE")
        return
    txn = uuid4()
    before = balance(cash_id)
    with Session(engine) as db:
        db.add(PaymentTransaction(
            id=txn, workspace_id=ws, patient_id=patient,
            appointment_id=appt, origin_appointment_id=appt,
            transaction_type="payment", amount_minor=11_000,
            currency="EGP", payment_method="cash", source="staff",
            created_at=datetime.now(UTC),
        ))
        db.flush()
        db.add(PaymentAllocation(
            workspace_id=ws, transaction_id=txn,
            appointment_id=other, amount_minor=11_000,
        ))
        db.commit()
    with Session(engine) as db:
        require(db.scalar(select(VaultPendingPayment.id).where(
            VaultPendingPayment.payment_transaction_id == txn,
            VaultPendingPayment.reason == "allocation_patient_mismatch",
        )) is not None, "CROSS_PATIENT_PENDING_REASON_MISSING")
        require(db.scalar(select(VaultLedgerEntry.id).where(
            VaultLedgerEntry.payment_transaction_id == txn,
        )) is None, "CROSS_PATIENT_WRONGLY_POSTED")
    require(balance(cash_id) == before, "CROSS_PATIENT_CREDITED_CASH")
    print("CROSS_PATIENT_ALLOCATION_FAIL_CLOSED_PASS")


def test_multibranch_pending_remains_unposted():
    with Session(engine) as db:
        other = db.execute(select(Appointment.id, Appointment.branch_id).where(
            Appointment.workspace_id == ws, Appointment.branch_id != branch,
            Appointment.patient_id == patient,
            Appointment.branch_id.is_not(None),
        ).limit(1)).first()
    if other is None:
        print("MULTI_BRANCH_CASE_NOT_TESTED_SINGLE_BRANCH_FIXTURE")
        return
    other_appt, other_branch = other
    txn = uuid4()
    before = balance(cash_id)
    with Session(engine) as db:
        db.add(PaymentTransaction(
            id=txn, workspace_id=ws, patient_id=patient,
            appointment_id=appt, origin_appointment_id=appt,
            transaction_type="payment", amount_minor=20_000,
            currency="EGP", payment_method="cash", source="integration",
            created_at=datetime.now(UTC),
        ))
        db.flush()
        db.add(PaymentAllocation(
            workspace_id=ws, transaction_id=txn,
            appointment_id=appt, amount_minor=10_000,
        ))
        db.flush()
        db.add(PaymentAllocation(
            workspace_id=ws, transaction_id=txn,
            appointment_id=other_appt, amount_minor=10_000,
        ))
        db.commit()
    with Session(engine) as db:
        require(db.scalar(select(VaultPendingPayment.id).where(
            VaultPendingPayment.payment_transaction_id == txn,
            VaultPendingPayment.reason == "ambiguous_branch",
        )) is not None, "AMBIGUOUS_PAYMENT_NOT_MARKED_PENDING")
        require(db.scalar(select(VaultLedgerEntry.id).where(
            VaultLedgerEntry.payment_transaction_id == txn,
        )) is None, "AMBIGUOUS_PAYMENT_WRONGLY_POSTED")
    require(balance(cash_id) == before, "AMBIGUOUS_PAYMENT_CHANGED_CASH")
    print("MULTIBRANCH_PENDING_FAIL_CLOSED_PASS")


def test_preactivation_electronic_admin_attestation():
    for method in ("visa", "instapay"):
        pid = preactivation_electronic[method]
        with Session(engine) as db:
            payment = db.get(PaymentTransaction, pid)
            account = db.scalar(select(VaultAccount).where(
                VaultAccount.workspace_id == ws,
                VaultAccount.kind == "main_" + method,
            ))
            pending = db.scalar(select(VaultPendingPayment).where(
                VaultPendingPayment.payment_transaction_id == pid,
            ))
            require(account is not None and pending is not None,
                    "PREACTIVATION_ELECTRONIC_PENDING_WAS_LOST")
            require(payment.created_at < account.opened_at,
                    "PAYMENT_NOT_BEFORE_CHANNEL_ACTIVATION")
            require(db.scalar(select(VaultLedgerEntry.id).where(
                VaultLedgerEntry.payment_transaction_id == pid,
            )) is None, "PREACTIVATION_RECEIPT_AUTOMATICALLY_DOUBLE_COUNTED")
            pending_id = pending.id

        outcome = "included_in_opening" if method == "visa" else "requires_accounting_decision"
        with Session(engine) as db:
            reviewed = review_preactivation_electronic(
                db, workspace_id=ws, pending_id=pending_id,
                actor_user_id=actor, outcome=outcome,
                explanation="Synthetic bank statement and opening balance review",
            )
            require(reviewed.review_outcome == outcome, "REVIEW_RESULT_NOT_RECORDED")
            db.commit()
        with Session(engine) as db:
            record = db.scalar(select(VaultPendingPayment).where(
                VaultPendingPayment.id == pending_id,
            ))
            require(record is not None and record.reviewed_by_user_id == actor
                    and record.review_note and record.reviewed_at,
                    "ELECTRONIC_RECONCILIATION_AUDIT_FIELDS_MISSING")
            require(db.scalar(select(VaultLedgerEntry.id).where(
                VaultLedgerEntry.payment_transaction_id == pid,
            )) is None, "ELECTRONIC_REVIEW_ADDED_DUPLICATE_LEDGER")
        with Session(engine) as db:
            try:
                review_preactivation_electronic(
                    db, workspace_id=ws, pending_id=pending_id,
                    actor_user_id=actor, outcome=outcome,
                    explanation="Repeated review must be rejected",
                )
                raise AssertionError("ELECTRONIC_REVIEW_DOUBLE_ATTESTED")
            except VaultError as exc:
                require("already" in str(exc).lower(), "INCORRECT_REVIEW_RETRY_ERROR")
                db.rollback()
    print("ELECTRONIC_PREACTIVATION_AUDITED_NO_DOUBLE_CREDIT_PASS")


def test_opening_balance_cannot_overattest_earlier_receipts():
    with Session(engine) as db:
        account = db.scalar(select(VaultAccount).where(
            VaultAccount.workspace_id == ws, VaultAccount.kind == "main_visa",
        ))
        previous_payment_time = account.opened_at - timedelta(minutes=10)
    payment_id = uuid4()
    with Session(engine) as db:
        db.add(PaymentTransaction(
            id=payment_id, workspace_id=ws, appointment_id=appt,
            origin_appointment_id=appt, patient_id=patient,
            transaction_type="payment", amount_minor=15_000,
            currency="EGP", payment_method="visa", source="staff",
            created_at=previous_payment_time,
        ))
        db.commit()
    with Session(engine) as db:
        pending = db.scalar(select(VaultPendingPayment).where(
            VaultPendingPayment.payment_transaction_id == payment_id,
        ))
        require(pending is not None, "BACKDATED_ELECTRONIC_PAYMENT_DISAPPEARED")
        try:
            review_preactivation_electronic(
                db, workspace_id=ws, pending_id=pending.id,
                actor_user_id=actor, outcome="included_in_opening",
                explanation="Would exceed attested opening balance",
            )
            raise AssertionError("OPENING_BALANCE_ATTESTATION_OVERFLOW_ALLOWED")
        except VaultError as exc:
            require("does not cover" in str(exc), "WRONG_OPENING_OVERFLOW_ERROR")
            db.rollback()
    print("OPENING_BALANCE_ATTESTATION_CAP_GUARD_PASS")


def test_postactivation_electronic_retry_is_exactly_once():
    # Fault injection is restricted to this disposable 0099 database.
    with engine.begin() as connection:
        connection.execute(text(
            "ALTER TABLE public.payment_transactions DISABLE TRIGGER vault_payment_insert"
        ))
    receipt_id, pending_id = uuid4(), uuid4()
    try:
        with Session(engine) as db:
            db.add(PaymentTransaction(
                id=receipt_id, workspace_id=ws, patient_id=patient,
                appointment_id=appt, origin_appointment_id=appt,
                transaction_type="payment", amount_minor=17_000,
                currency="EGP", payment_method="instapay",
                source="integration", created_at=datetime.now(UTC),
            ))
            db.flush()
            pending = VaultPendingPayment(
                id=pending_id, workspace_id=ws,
                payment_transaction_id=receipt_id,
                reason="main_channel_not_activated",
            )
            db.add(pending)
            db.commit()
    finally:
        with engine.begin() as connection:
            connection.execute(text(
                "ALTER TABLE public.payment_transactions ENABLE TRIGGER vault_payment_insert"
            ))

    def do_review():
        with Session(engine) as db:
            try:
                row = post_eligible_pending_electronic(
                    db, workspace_id=ws, pending_id=pending_id,
                    actor_user_id=actor,
                    explanation="Confirmed receipt after channel activation",
                )
                db.commit()
                return "OK", row.id
            except BaseException:
                db.rollback()
                raise

    results = run_pair(do_review, do_review)
    require(sum(x[0] == "OK" for x in results) == 1,
            "ELECTRONIC_RETRY_CONCURRENT_DOUBLE_POST")
    with Session(engine) as db:
        rows = db.scalars(select(VaultLedgerEntry).where(
            VaultLedgerEntry.payment_transaction_id == receipt_id,
        )).all()
        require(len(rows) == 1 and rows[0].amount_minor == 17_000,
                "ELECTRONIC_RETRY_LEDGER_POST_COUNT_WRONG")
        require(db.scalar(select(VaultPendingPayment.id).where(
            VaultPendingPayment.payment_transaction_id == receipt_id,
        )) is None, "RETRIED_ELECTRONIC_RECEIPT_STILL_PENDING")
    print("ELECTRONIC_POSTACTIVATION_CONCURRENT_RETRY_PASS")


def test_single_opening_balance_db_guard():
    with Session(engine) as db:
        visa = db.scalar(select(VaultAccount).where(
            VaultAccount.workspace_id == ws, VaultAccount.kind == "main_visa",
        ))
        require(visa is not None, "NO_VISA_ACCOUNT")
        visa_id = visa.id
        # Activation already created the one allowed opening record. A second
        # must fail even when the actor posts directly instead of using the API.
        db.add(VaultLedgerEntry(
            workspace_id=ws, account_id=visa_id, kind="opening",
            amount_minor=1_000, actor_user_id=actor,
        ))
        try:
            db.flush()
            raise AssertionError("DUPLICATE_OPENING_BALANCE_POSTED")
        except DBAPIError as exc:
            require("uq_vault_opening_per_account" in str(exc.orig),
                    "WRONG_OPENING_BALANCE_GUARD")
            db.rollback()
    with Session(engine) as db:
        opening_count = db.scalar(select(func.count(VaultLedgerEntry.id)).where(
            VaultLedgerEntry.workspace_id == ws,
            VaultLedgerEntry.account_id == visa_id,
            VaultLedgerEntry.kind == "opening",
        ))
        require(opening_count == 1, "OPENING_BALANCE_COUNT_WRONG")
    print("SINGLE_OPENING_BALANCE_DB_CONSTRAINT_PASS")


def test_bounded_application_clock_skew_cannot_hide_fresh_staff_cash():
    # Create a NEW reception vault immediately before posting, then
    # intentionally stamp staff payment 250 ms before its activation time.
    with Session(engine) as db:
        activate_account(
            db, workspace_id=ws, actor_user_id=actor,
            kind="reception", branch_id=branch2,
            opening_balance_minor=0, currency="EGP",
        )
        db.commit()
        second_account = db.scalar(select(VaultAccount).where(
            VaultAccount.workspace_id == ws, VaultAccount.branch_id == branch2,
            VaultAccount.kind == "reception",
        ))
        second_account_id = second_account.id
        just_before_activation = second_account.opened_at - timedelta(milliseconds=250)
    for source in ("staff", "integration"):
        pid = uuid4()
        with Session(engine) as db:
            db.add(PaymentTransaction(
                id=pid, workspace_id=ws, appointment_id=different_branch_appt,
                origin_appointment_id=different_branch_appt, patient_id=patient,
                transaction_type="payment", amount_minor=4_500,
                currency="EGP", payment_method="cash", source=source,
                created_at=just_before_activation,
            ))
            db.flush()
            db.add(PaymentAllocation(
                workspace_id=ws, transaction_id=pid,
                appointment_id=different_branch_appt, amount_minor=4_500,
            ))
            db.commit()
        with Session(engine) as db:
            entries = db.scalars(select(VaultLedgerEntry).where(
                VaultLedgerEntry.payment_transaction_id == pid,
            )).all()
            pending = db.scalar(select(VaultPendingPayment).where(
                VaultPendingPayment.payment_transaction_id == pid,
            ))
            if source == "staff":
                require(len(entries) == 1 and entries[0].account_id == second_account_id,
                        "DB_CLOCK_SKEW_HID_REAL_STAFF_CHECKOUT")
                require(pending is None, "POSTED_STAFF_PAYMENT_LEFT_PENDING")
            else:
                require(not entries and pending is not None,
                        "BACKDATED_INTEGRATION_WAS_AUTOMATICALLY_POSTED")
    print("BOUNDED_DB_FIRST_SEEN_CLOCK_SKEW_GUARD_PASS")


def test_legacy_source_not_automatically_backfilled():
    source_id = uuid4()
    with Session(engine) as db:
        db.add(PaymentTransaction(
            id=source_id, workspace_id=ws, patient_id=patient,
            appointment_id=appt, origin_appointment_id=appt,
            transaction_type="payment", amount_minor=2_345,
            currency="EGP", payment_method="cash",
            source="legacy_backfill", created_at=datetime.now(UTC),
        ))
        db.flush()
        db.add(PaymentAllocation(
            workspace_id=ws, transaction_id=source_id,
            appointment_id=appt, amount_minor=2_345,
        ))
        db.commit()
    with Session(engine) as db:
        require(db.scalar(select(VaultLedgerEntry.id).where(
            VaultLedgerEntry.payment_transaction_id == source_id,
        )) is None, "LEGACY_SOURCE_CREDITED_TO_VAULT")
    print("LEGACY_SOURCE_EXCLUSION_PASS")


suffix = uuid4().hex[:8]
for test in (
    test_single_opening_balance_db_guard,
    test_preactivation_electronic_admin_attestation,
    test_opening_balance_cannot_overattest_earlier_receipts,
    test_postactivation_electronic_retry_is_exactly_once,
    test_posted_allocation_and_appointment_attribution_guards,
    test_bounded_application_clock_skew_cannot_hide_fresh_staff_cash,
    test_legacy_source_not_automatically_backfilled,
    test_channel_posting_and_source_variants,
    test_historical_payment_stays_outside_new_vault,
    test_source_allocation_guards,
    test_cross_patient_payment_never_hits_vault,
    test_multibranch_pending_remains_unposted,
    test_transfer_race, test_expenses_race, test_expense_transfer_race,
    test_refund_transfer_race, test_pending_resolution_race,
    test_duplicate_key_and_interrupted_retry, test_failed_refund_atomic_rollback,
):
    test()

engine.dispose()
print("LOCAL_PG17_SERVICE_CONCURRENCY_ALL_PASS")
