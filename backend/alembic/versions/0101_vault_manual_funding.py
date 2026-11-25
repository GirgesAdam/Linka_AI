"""Automatic treasury provisioning and non-revenue manual funding."""

import sqlalchemy as sa

from alembic import op

revision = "0101_vault_manual_funding"
down_revision = "0100_vault_bank_reception"
branch_labels = None
depends_on = None

_OLD_KINDS = "kind IN ('opening','payment','refund','expense','transfer_out','transfer_in','correction')"
_NEW_KINDS = "kind IN ('opening','payment','refund','expense','transfer_out','transfer_in','correction','manual_funding')"


def upgrade():
    op.drop_constraint("vault_entry_kind", "vault_ledger_entries", type_="check")
    op.create_check_constraint("vault_entry_kind", "vault_ledger_entries", _NEW_KINDS)
    op.create_check_constraint(
        "vault_manual_funding_shape", "vault_ledger_entries",
        "kind <> 'manual_funding' OR (amount_minor > 0 AND payment_transaction_id IS NULL "
        "AND expense_id IS NULL AND transfer_id IS NULL AND idempotency_key IS NOT NULL)",
    )
    # NOT VALID preserves historical opening rows while blocking new ones.
    # PostgreSQL still enforces the check on every new insert.
    op.execute(sa.text("""
    ALTER TABLE public.vault_ledger_entries
      ADD CONSTRAINT ck_vault_ledger_entries_no_new_opening
      CHECK (kind <> 'opening') NOT VALID;
    """))
    op.execute(sa.text("""
    CREATE FUNCTION public.vault_auto_activate_treasury()
    RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER
    SET search_path = public, pg_catalog AS $$
    BEGIN
      IF NEW.is_active IS TRUE AND NEW.is_demo IS FALSE THEN
        INSERT INTO public.vault_accounts
          (id, workspace_id, branch_id, kind, currency, opened_by_user_id)
        SELECT gen_random_uuid(), NEW.id, NULL, k.kind, 'EGP', NULL
        FROM (VALUES ('main_cash'), ('main_bank'), ('main_visa'), ('main_instapay')) AS k(kind)
        ON CONFLICT DO NOTHING;
      END IF;
      RETURN NEW;
    END;
    $$;
    CREATE TRIGGER workspaces_vault_auto_activate_treasury
    AFTER INSERT OR UPDATE OF is_active, is_demo ON public.workspaces
    FOR EACH ROW EXECUTE FUNCTION public.vault_auto_activate_treasury();
    REVOKE EXECUTE ON FUNCTION public.vault_auto_activate_treasury()
      FROM PUBLIC, anon, authenticated;
    INSERT INTO public.vault_accounts
      (id, workspace_id, branch_id, kind, currency, opened_by_user_id)
    SELECT gen_random_uuid(), w.id, NULL, k.kind, 'EGP', NULL
    FROM public.workspaces w
    CROSS JOIN (VALUES ('main_cash'), ('main_bank'), ('main_visa'), ('main_instapay')) AS k(kind)
    WHERE w.is_active IS TRUE AND w.is_demo IS FALSE
    ON CONFLICT DO NOTHING;
    """))


def downgrade():
    # Never discard financial history to return to the old application.
    op.execute(sa.text("""
    DO $$ BEGIN
      IF EXISTS (SELECT 1 FROM public.vault_ledger_entries WHERE kind='manual_funding') THEN
        RAISE EXCEPTION 'Manual funding history prevents downgrade';
      END IF;
    END $$;
    DROP TRIGGER workspaces_vault_auto_activate_treasury ON public.workspaces;
    DROP FUNCTION public.vault_auto_activate_treasury();
    """))
    op.execute("ALTER TABLE public.vault_ledger_entries DROP CONSTRAINT ck_vault_ledger_entries_no_new_opening")
    op.drop_constraint("vault_manual_funding_shape", "vault_ledger_entries", type_="check")
    op.drop_constraint("vault_entry_kind", "vault_ledger_entries", type_="check")
    op.create_check_constraint("vault_entry_kind", "vault_ledger_entries", _OLD_KINDS)
