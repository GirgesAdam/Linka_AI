"""Reception cash vault and workspace main treasury, 0098-dependent.

Revision ID: 0099_reception_main_vault_ledger
Revises: 0098_doctor_laser_weekday_exclusions
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0099_reception_main_vault_ledger"
down_revision: str | Sequence[str] | None = "0098_doctor_laser_weekday_exclusions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # An Expense is shared with the existing finance/profitability subsystem.
    # The composite reference enforces the original expense's workspace.
    op.create_unique_constraint(
        "uq_expenses_workspace_id_id", "expenses", ["workspace_id", "id"]
    )
    op.create_table(
        "vault_accounts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("branch_id", sa.Uuid(), nullable=True),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("opened_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id", "branch_id"], ["branches.workspace_id", "branches.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["opened_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("workspace_id", "id", name="uq_vault_accounts_workspace_id_id"),
        sa.CheckConstraint(
            "kind IN ('reception','main_cash','main_visa','main_instapay')",
            name="vault_account_kind",
        ),
        sa.CheckConstraint(
            "(kind = 'reception' AND branch_id IS NOT NULL) OR "
            "(kind <> 'reception' AND branch_id IS NULL)",
            name="vault_account_branch",
        ),
        sa.CheckConstraint("currency = 'EGP'", name="vault_account_currency"),
    )
    op.create_index("ix_vault_accounts_workspace_id", "vault_accounts", ["workspace_id"])
    op.create_index(
        "uq_vault_accounts_workspace_reception", "vault_accounts",
        ["workspace_id", "branch_id", "currency"], unique=True,
        postgresql_where=sa.text("kind = 'reception'"),
    )
    op.create_index(
        "uq_vault_accounts_workspace_main_kind", "vault_accounts",
        ["workspace_id", "kind", "currency"], unique=True,
        postgresql_where=sa.text("kind <> 'reception'"),
    )
    op.create_table(
        "vault_transfers",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("branch_id", sa.Uuid(), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("actor_user_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id", "branch_id"], ["branches.workspace_id", "branches.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("workspace_id", "id", name="uq_vault_transfers_workspace_id_id"),
        sa.UniqueConstraint("workspace_id", "idempotency_key", name="uq_vault_transfer_retry"),
        sa.CheckConstraint("amount_minor > 0", name="vault_transfer_positive"),
        sa.CheckConstraint("currency = 'EGP'", name="vault_transfer_currency"),
    )
    op.create_index("ix_vault_transfers_workspace_id", "vault_transfers", ["workspace_id"])
    op.create_table(
        "vault_ledger_entries",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("payment_transaction_id", sa.Uuid(), nullable=True),
        sa.Column("expense_id", sa.Uuid(), nullable=True),
        sa.Column("transfer_id", sa.Uuid(), nullable=True),
        sa.Column("idempotency_key", sa.String(128), nullable=True),
        sa.Column("actor_user_id", sa.Uuid(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["workspace_id", "account_id"], ["vault_accounts.workspace_id", "vault_accounts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["workspace_id", "transfer_id"], ["vault_transfers.workspace_id", "vault_transfers.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["workspace_id", "payment_transaction_id"], ["payment_transactions.workspace_id", "payment_transactions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["workspace_id", "expense_id"], ["expenses.workspace_id", "expenses.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.CheckConstraint("amount_minor <> 0", name="vault_entry_nonzero"),
        sa.CheckConstraint(
            "kind IN ('opening','payment','refund','expense','transfer_out','transfer_in','correction')",
            name="vault_entry_kind",
        ),
        sa.UniqueConstraint("workspace_id", "payment_transaction_id", name="uq_vault_entry_payment"),
        sa.UniqueConstraint("workspace_id", "expense_id", name="uq_vault_entry_expense"),
        sa.UniqueConstraint("workspace_id", "idempotency_key", name="uq_vault_entry_idempotency"),
        sa.UniqueConstraint("workspace_id", "transfer_id", "kind", name="uq_vault_entry_transfer_side"),
    )
    op.create_index("ix_vault_ledger_entries_workspace_id", "vault_ledger_entries", ["workspace_id"])
    op.create_index("ix_vault_ledger_entries_account_id", "vault_ledger_entries", ["account_id"])
    op.create_index(
        "ix_vault_entries_workspace_account_created", "vault_ledger_entries",
        ["workspace_id", "account_id", "created_at"],
    )
    op.create_index(
        "uq_vault_opening_per_account", "vault_ledger_entries",
        ["workspace_id", "account_id"], unique=True,
        postgresql_where=sa.text("kind = 'opening'"),
    )
    op.create_table(
        "vault_pending_payments",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("payment_transaction_id", sa.Uuid(), nullable=False),
        sa.Column("reason", sa.String(64), nullable=False),
        sa.Column("review_outcome", sa.String(32), nullable=True),
        sa.Column("review_note", sa.Text(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewed_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("reviewed_account_id", sa.Uuid(), nullable=True),
        # First observation by PostgreSQL, not the client application's clock.
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("clock_timestamp()")),
        sa.CheckConstraint(
            "review_outcome IS NULL OR review_outcome IN ('included_in_opening', 'requires_accounting_decision')",
            name="vault_pending_review_outcome_valid",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "payment_transaction_id"],
            ["payment_transactions.workspace_id", "payment_transactions.id"], ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "reviewed_account_id"],
            ["vault_accounts.workspace_id", "vault_accounts.id"], ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["reviewed_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("workspace_id", "payment_transaction_id", name="uq_vault_pending_payment"),
    )
    op.create_index("ix_vault_pending_payments_workspace_id", "vault_pending_payments", ["workspace_id"])
    op.create_table(
        "vault_branch_grants",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("branch_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("granted_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["workspace_id", "branch_id"], ["branches.workspace_id", "branches.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id", "user_id"], ["workspace_members.workspace_id", "workspace_members.user_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["granted_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("workspace_id", "branch_id", "user_id", name="uq_vault_branch_grant"),
    )
    op.create_index("ix_vault_branch_grants_workspace_id", "vault_branch_grants", ["workspace_id"])

    for table in (
        "vault_accounts", "vault_transfers", "vault_ledger_entries",
        "vault_pending_payments", "vault_branch_grants",
    ):
        # These tables are backend-only. Never expose raw accounting tables to browser roles.
        op.execute(sa.text(f'ALTER TABLE public."{table}" ENABLE ROW LEVEL SECURITY'))
        op.execute(sa.text(
            f'REVOKE ALL ON TABLE public."{table}" FROM PUBLIC, anon, authenticated'
        ))

    op.execute(sa.text("""
    CREATE FUNCTION public.vault_reject_posted_expense_change()
    RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER SET search_path = public, pg_catalog AS $$
    BEGIN
      IF EXISTS (
        SELECT 1 FROM public.vault_ledger_entries
        WHERE workspace_id = OLD.workspace_id AND expense_id = OLD.id
      ) THEN
        RAISE EXCEPTION 'VAULT_POSTED_EXPENSE_IMMUTABLE';
      END IF;
      IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
      RETURN NEW;
    END; $$;
    CREATE TRIGGER vault_protect_expense
    BEFORE UPDATE OR DELETE ON public.expenses
    FOR EACH ROW EXECUTE FUNCTION public.vault_reject_posted_expense_change();
    REVOKE EXECUTE ON FUNCTION public.vault_reject_posted_expense_change() FROM PUBLIC, anon, authenticated;

    CREATE FUNCTION public.vault_reject_entry_mutation()
    RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER SET search_path = public, pg_catalog AS $$
    BEGIN
      RAISE EXCEPTION 'VAULT_LEDGER_IMMUTABLE';
    END; $$;
    CREATE TRIGGER vault_ledger_immutable
    BEFORE UPDATE OR DELETE ON public.vault_ledger_entries
    FOR EACH ROW EXECUTE FUNCTION public.vault_reject_entry_mutation();
    REVOKE EXECUTE ON FUNCTION public.vault_reject_entry_mutation() FROM PUBLIC, anon, authenticated;
    """))

    # Every payment source inserts the same PaymentTransaction fact. Posting is
    # triggered within that transaction, so failed checkouts leave no vault entries.
    # Cash must have a complete allocation to a *single* branch. Multi-branch and
    # unallocated payments are recorded as pending, NEVER guessed.
    op.execute(sa.text("""
    CREATE FUNCTION public.vault_try_post_payment(p_transaction_id uuid)
    RETURNS void LANGUAGE plpgsql SECURITY INVOKER SET search_path = public, pg_catalog AS $$
    DECLARE
      p public.payment_transactions%ROWTYPE;
      acct public.vault_accounts%ROWTYPE;
      origin_entry public.vault_ledger_entries%ROWTYPE;
      source_payment public.payment_transactions%ROWTYPE;
      total_allocated bigint;
      branch_count integer;
      patient_mismatch_count integer;
      attributed_branch uuid;
      signed_amount bigint;
      current_balance bigint;
      pending_reason text;
      first_seen_at timestamptz;
    BEGIN
      SELECT * INTO p FROM public.payment_transactions WHERE id = p_transaction_id;
      IF NOT FOUND THEN RETURN; END IF;
      IF EXISTS (SELECT 1 FROM public.vault_ledger_entries
                 WHERE workspace_id=p.workspace_id AND payment_transaction_id=p.id) THEN
        RETURN;
      END IF;
      IF p.source = 'legacy_backfill'
         OR p.payment_method NOT IN ('cash','visa','instapay')
         OR p.currency <> 'EGP' THEN RETURN; END IF;
      SELECT v.created_at INTO first_seen_at
      FROM public.vault_pending_payments v
      WHERE v.workspace_id=p.workspace_id AND v.payment_transaction_id=p.id;

      IF p.transaction_type = 'refund' THEN
        -- Pay a refund from the same holding account as its original payment.
        SELECT * INTO origin_entry FROM public.vault_ledger_entries
        WHERE workspace_id=p.workspace_id AND payment_transaction_id=p.reference_transaction_id
          AND kind='payment';
        IF NOT FOUND THEN
          pending_reason := 'unposted_original_payment';
        ELSE
          SELECT * INTO source_payment FROM public.payment_transactions
          WHERE id=p.reference_transaction_id AND workspace_id=p.workspace_id;
          IF NOT FOUND OR source_payment.payment_method <> p.payment_method
             OR source_payment.currency <> p.currency THEN
            RAISE EXCEPTION 'VAULT_REFUND_SOURCE_MISMATCH';
          END IF;
          SELECT * INTO acct FROM public.vault_accounts
          WHERE id=origin_entry.account_id AND workspace_id=p.workspace_id FOR UPDATE;
          IF NOT FOUND THEN RAISE EXCEPTION 'VAULT_ORIGINAL_ACCOUNT_MISSING'; END IF;
          signed_amount := -p.amount_minor;
        END IF;
      ELSIF p.payment_method IN ('visa','instapay') THEN
        SELECT * INTO acct FROM public.vault_accounts
        WHERE workspace_id=p.workspace_id
          AND kind=CASE WHEN p.payment_method='visa' THEN 'main_visa' ELSE 'main_instapay' END
          AND currency=p.currency FOR UPDATE;
        IF NOT FOUND THEN pending_reason := 'main_channel_not_activated'; END IF;
        signed_amount := p.amount_minor;
      ELSIF p.payment_method = 'cash' THEN
        -- A payment's nullable appointment hints alone do not prove attribution.
        -- Lock the source appointments while attributing custody, so an
        -- appointment branch/patient update cannot race with first posting.
        PERFORM 1 FROM public.appointments ap
        WHERE ap.workspace_id=p.workspace_id AND ap.id IN (
          SELECT a.appointment_id FROM public.payment_allocations a
          WHERE a.workspace_id=p.workspace_id AND a.transaction_id=p.id
        ) ORDER BY ap.id FOR SHARE;
        -- Its explicit allocations must add up and all point to the same branch.
        SELECT COALESCE(sum(a.amount_minor), 0), count(DISTINCT ap.branch_id),
               count(*) FILTER (WHERE ap.patient_id IS DISTINCT FROM p.patient_id),
               (array_agg(ap.branch_id ORDER BY ap.branch_id))[1]
          INTO total_allocated, branch_count, patient_mismatch_count, attributed_branch
        FROM public.payment_allocations a
        JOIN public.appointments ap
          ON ap.workspace_id=a.workspace_id AND ap.id=a.appointment_id
        WHERE a.workspace_id=p.workspace_id AND a.transaction_id=p.id;
        IF patient_mismatch_count > 0 THEN
          pending_reason := 'allocation_patient_mismatch';
        ELSIF total_allocated <> p.amount_minor THEN
          pending_reason := 'incomplete_payment_allocations';
        ELSIF branch_count <> 1 THEN
          pending_reason := 'ambiguous_branch';
        ELSE
          SELECT * INTO acct FROM public.vault_accounts
          WHERE workspace_id=p.workspace_id AND kind='reception'
            AND branch_id=attributed_branch AND currency=p.currency FOR UPDATE;
          IF NOT FOUND THEN pending_reason := 'reception_not_activated'; END IF;
        END IF;
        signed_amount := p.amount_minor;
      END IF;

      IF pending_reason IS NOT NULL THEN
        -- Track post-0099 payment facts even when no vault has been opened.
        -- This trigger only fires on new writes; it never backfills historic rows.
        -- Manual reconciliation decides whether a pre-activation receipt is
        -- already part of a declared opening balance; never auto-add it.
        INSERT INTO public.vault_pending_payments
            (id,workspace_id,payment_transaction_id,reason)
        VALUES (gen_random_uuid(),p.workspace_id,p.id,pending_reason)
        ON CONFLICT (workspace_id,payment_transaction_id)
        DO UPDATE SET reason=EXCLUDED.reason;
        RETURN;
      END IF;
      IF acct.id IS NULL THEN RETURN; END IF;
      -- A backend-generated staff receipt can have application-clock skew.
      -- First observation is recorded by the same PostgreSQL clock that owns
      -- the account. For non-staff/backdated receipt sources, a historical
      -- business timestamp still requires explicit reconciliation.
      -- Only tolerate bounded clock skew for freshly inserted staff facts.
      -- An older historical/backdated staff receipt must still remain pending.
      IF (first_seen_at IS NOT NULL AND first_seen_at < acct.opened_at)
         OR (first_seen_at IS NULL AND p.created_at < acct.opened_at)
         OR (p.created_at < acct.opened_at AND
            (p.source <> 'staff'
             OR first_seen_at - p.created_at > interval '5 seconds')) THEN
        INSERT INTO public.vault_pending_payments
            (id,workspace_id,payment_transaction_id,reason)
        VALUES (gen_random_uuid(),p.workspace_id,p.id,
          CASE WHEN p.created_at < acct.opened_at
            AND first_seen_at >= acct.opened_at
            AND (p.source <> 'staff' OR first_seen_at - p.created_at > interval '5 seconds')
            THEN 'backdated_receipt_requires_review'
          WHEN p.payment_method IN ('visa','instapay')
            THEN 'electronic_payment_predates_activation'
            ELSE 'payment_predates_vault_activation' END)
        ON CONFLICT (workspace_id,payment_transaction_id)
        DO UPDATE SET reason=EXCLUDED.reason;
        RETURN;
      END IF;
      IF signed_amount < 0 THEN
        -- A refund after a full cash handover MUST NOT secretly fund itself from
        -- another vault. The owner must authorize a replenishment/refund policy.
        SELECT COALESCE(sum(amount_minor),0) INTO current_balance
        FROM public.vault_ledger_entries
        WHERE workspace_id=p.workspace_id AND account_id=acct.id;
        IF current_balance + signed_amount < 0 THEN
          RAISE EXCEPTION 'VAULT_REFUND_REQUIRES_APPROVED_FUNDING';
        END IF;
      END IF;
      INSERT INTO public.vault_ledger_entries
          (id,workspace_id,account_id,kind,amount_minor,payment_transaction_id,actor_user_id)
      VALUES (gen_random_uuid(),p.workspace_id,acct.id,
              CASE WHEN p.transaction_type='refund' THEN 'refund' ELSE 'payment' END,
              signed_amount,p.id,p.created_by_user_id)
      ON CONFLICT (workspace_id,payment_transaction_id) DO NOTHING;
      DELETE FROM public.vault_pending_payments
      WHERE workspace_id=p.workspace_id AND payment_transaction_id=p.id;
    END;
    $$;
    REVOKE EXECUTE ON FUNCTION public.vault_try_post_payment(uuid)
       FROM PUBLIC, anon, authenticated;

    CREATE FUNCTION public.vault_payment_insert_hook()
    RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER SET search_path = public, pg_catalog AS $$
    BEGIN
      -- Capture the fact's first DB-observed time before attempting to post.
      -- This is only for NEW supported facts, never an existing history sweep.
      IF NEW.source <> 'legacy_backfill'
        AND NEW.payment_method IN ('cash','visa','instapay')
        AND NEW.currency='EGP' THEN
        INSERT INTO public.vault_pending_payments
            (id,workspace_id,payment_transaction_id,reason)
        VALUES (gen_random_uuid(),NEW.workspace_id,NEW.id,'awaiting_posting')
        ON CONFLICT (workspace_id,payment_transaction_id) DO NOTHING;
      END IF;
      PERFORM public.vault_try_post_payment(NEW.id);
      RETURN NEW;
    END;
    $$;
    CREATE TRIGGER vault_payment_insert
    AFTER INSERT ON public.payment_transactions
    FOR EACH ROW EXECUTE FUNCTION public.vault_payment_insert_hook();
    REVOKE EXECUTE ON FUNCTION public.vault_payment_insert_hook() FROM PUBLIC, anon, authenticated;

    CREATE FUNCTION public.vault_allocation_insert_hook()
    RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER SET search_path = public, pg_catalog AS $$
    DECLARE
      total_allocated bigint;
      source_amount bigint;
    BEGIN
      -- A posted receipt may be linked to new visits only while its total
      -- allocations remain within the immutable payment amount.
      IF EXISTS (SELECT 1 FROM public.vault_ledger_entries
                 WHERE workspace_id=NEW.workspace_id AND payment_transaction_id=NEW.transaction_id) THEN
        SELECT amount_minor INTO source_amount FROM public.payment_transactions
        WHERE workspace_id=NEW.workspace_id AND id=NEW.transaction_id;
        SELECT COALESCE(sum(amount_minor),0) INTO total_allocated
        FROM public.payment_allocations
        WHERE workspace_id=NEW.workspace_id AND transaction_id=NEW.transaction_id;
        IF total_allocated > source_amount THEN
          RAISE EXCEPTION 'VAULT_ALLOCATION_EXCEEDS_POSTED_PAYMENT';
        END IF;
      END IF;
      PERFORM public.vault_try_post_payment(NEW.transaction_id);
      RETURN NEW;
    END;
    $$;
    CREATE TRIGGER vault_payment_allocation_insert
    AFTER INSERT ON public.payment_allocations
    FOR EACH ROW EXECUTE FUNCTION public.vault_allocation_insert_hook();
    REVOKE EXECUTE ON FUNCTION public.vault_allocation_insert_hook() FROM PUBLIC, anon, authenticated;

    CREATE FUNCTION public.vault_guard_posted_allocation_change()
    RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER SET search_path = public, pg_catalog AS $$
    BEGIN
      IF EXISTS (SELECT 1 FROM public.vault_ledger_entries
          WHERE workspace_id=OLD.workspace_id AND payment_transaction_id=OLD.transaction_id) THEN
        IF TG_OP='DELETE' OR NEW.amount_minor <> OLD.amount_minor
           OR NEW.transaction_id <> OLD.transaction_id
           OR NEW.workspace_id <> OLD.workspace_id
           OR NEW.appointment_id <> OLD.appointment_id THEN
          RAISE EXCEPTION 'VAULT_POSTED_ALLOCATION_IMMUTABLE';
        END IF;
      END IF;
      IF TG_OP='DELETE' THEN RETURN OLD; END IF;
      RETURN NEW;
    END; $$;
    CREATE TRIGGER vault_posted_allocation_guard
    BEFORE UPDATE OR DELETE ON public.payment_allocations
    FOR EACH ROW EXECUTE FUNCTION public.vault_guard_posted_allocation_change();
    REVOKE EXECUTE ON FUNCTION public.vault_guard_posted_allocation_change() FROM PUBLIC, anon, authenticated;

    -- Pre-posting allocation corrections must also re-evaluate eligible pending
    -- payments; the BEFORE guard above rejects mutations of posted evidence.
    CREATE FUNCTION public.vault_allocation_change_hook()
    RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER SET search_path = public, pg_catalog AS $$
    BEGIN
      IF TG_OP='DELETE' THEN
        PERFORM public.vault_try_post_payment(OLD.transaction_id);
        RETURN OLD;
      END IF;
      IF NEW.transaction_id IS DISTINCT FROM OLD.transaction_id THEN
        PERFORM public.vault_try_post_payment(OLD.transaction_id);
      END IF;
      PERFORM public.vault_try_post_payment(NEW.transaction_id);
      RETURN NEW;
    END; $$;
    CREATE TRIGGER vault_payment_allocation_change
    AFTER UPDATE OR DELETE ON public.payment_allocations
    FOR EACH ROW EXECUTE FUNCTION public.vault_allocation_change_hook();
    REVOKE EXECUTE ON FUNCTION public.vault_allocation_change_hook()
      FROM PUBLIC, anon, authenticated;

    -- Only an already-posted CASH payment freezes its appointment's
    -- custody-defining patient/branch facts; status/time changes are unaffected.
    CREATE FUNCTION public.vault_guard_posted_cash_appointment_change()
    RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER SET search_path = public, pg_catalog AS $$
    BEGIN
      IF (NEW.branch_id IS DISTINCT FROM OLD.branch_id
          OR NEW.patient_id IS DISTINCT FROM OLD.patient_id)
         AND EXISTS (
           SELECT 1
           FROM public.payment_allocations a
           JOIN public.payment_transactions p
             ON p.workspace_id=a.workspace_id AND p.id=a.transaction_id
           JOIN public.vault_ledger_entries e
             ON e.workspace_id=p.workspace_id AND e.payment_transaction_id=p.id
           JOIN public.vault_accounts acc
             ON acc.workspace_id=e.workspace_id AND acc.id=e.account_id
           WHERE a.workspace_id=OLD.workspace_id AND a.appointment_id=OLD.id
             AND p.payment_method='cash' AND p.transaction_type='payment'
             AND e.kind='payment' AND acc.kind='reception'
         ) THEN
        RAISE EXCEPTION 'VAULT_POSTED_CASH_APPOINTMENT_ATTRIBUTION_IMMUTABLE';
      END IF;
      RETURN NEW;
    END; $$;
    CREATE TRIGGER vault_posted_cash_appointment_guard
    BEFORE UPDATE OF branch_id, patient_id ON public.appointments
    FOR EACH ROW EXECUTE FUNCTION public.vault_guard_posted_cash_appointment_change();
    REVOKE EXECUTE ON FUNCTION public.vault_guard_posted_cash_appointment_change()
      FROM PUBLIC, anon, authenticated;
    """))


def downgrade() -> None:
    # No automatic Production downgrade is authorized. Test-only downgrades
    # destroy financial history; a separate operator decision is mandatory.
    op.execute(sa.text("DROP TRIGGER IF EXISTS vault_posted_cash_appointment_guard ON public.appointments"))
    op.execute(sa.text("DROP TRIGGER IF EXISTS vault_payment_allocation_change ON public.payment_allocations"))
    op.execute(sa.text("DROP TRIGGER IF EXISTS vault_posted_allocation_guard ON public.payment_allocations"))
    op.execute(sa.text("DROP TRIGGER IF EXISTS vault_payment_allocation_insert ON public.payment_allocations"))
    op.execute(sa.text("DROP TRIGGER IF EXISTS vault_payment_insert ON public.payment_transactions"))
    op.execute(sa.text("DROP TRIGGER IF EXISTS vault_ledger_immutable ON public.vault_ledger_entries"))
    op.execute(sa.text("DROP TRIGGER IF EXISTS vault_protect_expense ON public.expenses"))
    for function in (
        "vault_guard_posted_cash_appointment_change()", "vault_allocation_change_hook()", "vault_guard_posted_allocation_change()", "vault_allocation_insert_hook()", "vault_payment_insert_hook()",
        "vault_try_post_payment(uuid)", "vault_reject_entry_mutation()",
        "vault_reject_posted_expense_change()",
    ):
        op.execute(sa.text(f"DROP FUNCTION IF EXISTS public.{function}"))
    op.drop_table("vault_branch_grants")
    op.drop_table("vault_pending_payments")
    op.drop_table("vault_ledger_entries")
    op.drop_table("vault_transfers")
    op.drop_table("vault_accounts")
    op.drop_constraint("uq_expenses_workspace_id_id", "expenses", type_="unique")
