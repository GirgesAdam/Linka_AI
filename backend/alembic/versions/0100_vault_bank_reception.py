"""Auto-provision reception accounts and support bank deposits.

Revision ID: 0100_vault_bank_reception
Revises: 0099_reception_main_vault_ledger
"""
import sqlalchemy as sa

from alembic import op

revision = "0100_vault_bank_reception"
down_revision = "0099_reception_main_vault_ledger"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint("vault_account_kind", "vault_accounts", type_="check")
    op.create_check_constraint(
        "vault_account_kind", "vault_accounts",
        "kind IN ('reception','main_cash','main_visa','main_instapay','main_bank')",
    )
    # Branchless transfer headers are bank deposits; the existing immutable
    # ledger rows identify both accounts and retain the unique transfer sides.
    op.alter_column("vault_transfers", "branch_id", existing_type=sa.Uuid(), nullable=True)
    op.execute(sa.text("""
    CREATE FUNCTION public.vault_auto_activate_reception()
    RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER
    SET search_path = public, pg_catalog AS $$
    BEGIN
      IF NEW.is_active IS TRUE AND EXISTS (
          SELECT 1 FROM public.workspaces w
          WHERE w.id=NEW.workspace_id AND w.is_demo IS FALSE
      ) THEN
        INSERT INTO public.vault_accounts
          (id, workspace_id, branch_id, kind, currency, opened_by_user_id)
        VALUES
          (gen_random_uuid(), NEW.workspace_id, NEW.id, 'reception', 'EGP', NULL)
        ON CONFLICT DO NOTHING;
      END IF;
      RETURN NEW;
    END;
    $$;
    CREATE TRIGGER branches_vault_auto_activate_reception
    AFTER INSERT OR UPDATE OF is_active ON public.branches
    FOR EACH ROW EXECUTE FUNCTION public.vault_auto_activate_reception();
    REVOKE EXECUTE ON FUNCTION public.vault_auto_activate_reception()
      FROM PUBLIC, anon, authenticated;
    """))
    op.execute(sa.text("""
    INSERT INTO public.vault_accounts
      (id, workspace_id, branch_id, kind, currency, opened_by_user_id)
    SELECT gen_random_uuid(), b.workspace_id, b.id, 'reception', 'EGP', NULL
    FROM public.branches b JOIN public.workspaces w ON w.id=b.workspace_id
    WHERE b.is_active IS TRUE AND w.is_demo IS FALSE
    ON CONFLICT DO NOTHING
    """))

    # A branchless transfer is valid only with exactly its two matching,
    # workspace-scoped ledger sides. The check runs at commit, after the
    # service has inserted both rows. The existing ledger remains immutable.
    op.execute(sa.text("""
    CREATE FUNCTION public.vault_check_bank_transfer()
    RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER
    SET search_path = public, pg_catalog AS $$
    DECLARE
      tid uuid;
      wid uuid;
      header public.vault_transfers%ROWTYPE;
      side_count integer;
      valid_sides integer;
    BEGIN
      IF TG_TABLE_NAME = 'vault_transfers' THEN
        IF TG_OP = 'UPDATE' THEN
          IF (OLD.branch_id IS NULL) <> (NEW.branch_id IS NULL) THEN
            RAISE EXCEPTION 'VAULT_TRANSFER_SCOPE_IMMUTABLE';
          END IF;
        END IF;
        tid := NEW.id;
        wid := NEW.workspace_id;
      ELSE
        tid := NEW.transfer_id;
        wid := NEW.workspace_id;
        IF tid IS NULL THEN RETURN NEW; END IF;
      END IF;
      SELECT * INTO header FROM public.vault_transfers
        WHERE id=tid AND workspace_id=wid;
      IF NOT FOUND OR header.branch_id IS NOT NULL THEN RETURN NEW; END IF;
      SELECT count(*), count(*) FILTER (
        WHERE l.payment_transaction_id IS NULL AND l.expense_id IS NULL
          AND ((l.kind='transfer_out' AND a.kind='main_cash'
                 AND l.amount_minor=-header.amount_minor)
           OR (l.kind='transfer_in' AND a.kind='main_bank'
                 AND l.amount_minor=header.amount_minor))
      ) INTO side_count, valid_sides
      FROM public.vault_ledger_entries l
      JOIN public.vault_accounts a
        ON a.id=l.account_id AND a.workspace_id=l.workspace_id
      WHERE l.workspace_id=wid AND l.transfer_id=tid;
      IF side_count <> 2 OR valid_sides <> 2 THEN
        RAISE EXCEPTION 'VAULT_BANK_TRANSFER_REQUIRES_MATCHING_LEDGER_SIDES';
      END IF;
      RETURN NEW;
    END;
    $$;
    CREATE CONSTRAINT TRIGGER vault_bank_transfer_header_guard
    AFTER INSERT OR UPDATE ON public.vault_transfers
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
    EXECUTE FUNCTION public.vault_check_bank_transfer();
    CREATE CONSTRAINT TRIGGER vault_bank_transfer_ledger_guard
    AFTER INSERT ON public.vault_ledger_entries
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
    EXECUTE FUNCTION public.vault_check_bank_transfer();
    REVOKE EXECUTE ON FUNCTION public.vault_check_bank_transfer()
      FROM PUBLIC, anon, authenticated;
    """))


def downgrade():
    op.execute("""
    DO $$ BEGIN
      IF EXISTS (SELECT 1 FROM public.vault_accounts WHERE kind='main_bank')
        OR EXISTS (SELECT 1 FROM public.vault_transfers WHERE branch_id IS NULL) THEN
        RAISE EXCEPTION 'Bank history prevents downgrade to 0099';
      END IF;
    END $$;
    DROP TRIGGER vault_bank_transfer_ledger_guard ON public.vault_ledger_entries;
    DROP TRIGGER vault_bank_transfer_header_guard ON public.vault_transfers;
    DROP FUNCTION public.vault_check_bank_transfer();
    DROP TRIGGER branches_vault_auto_activate_reception ON public.branches;
    DROP FUNCTION public.vault_auto_activate_reception();
    """)
    op.alter_column("vault_transfers", "branch_id", existing_type=sa.Uuid(), nullable=False)
    op.drop_constraint("vault_account_kind", "vault_accounts", type_="check")
    op.create_check_constraint(
        "vault_account_kind", "vault_accounts",
        "kind IN ('reception','main_cash','main_visa','main_instapay')",
    )
