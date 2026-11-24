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
    op.execute("""
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
    """)
    op.execute("""
    INSERT INTO public.vault_accounts
      (id, workspace_id, branch_id, kind, currency, opened_by_user_id)
    SELECT gen_random_uuid(), b.workspace_id, b.id, 'reception', 'EGP', NULL
    FROM public.branches b JOIN public.workspaces w ON w.id=b.workspace_id
    WHERE b.is_active IS TRUE AND w.is_demo IS FALSE
    ON CONFLICT DO NOTHING
    """)


def downgrade():
    op.execute("""
    DO $$ BEGIN
      IF EXISTS (SELECT 1 FROM public.vault_accounts WHERE kind='main_bank')
        OR EXISTS (SELECT 1 FROM public.vault_transfers WHERE branch_id IS NULL) THEN
        RAISE EXCEPTION 'Bank history prevents downgrade to 0099';
      END IF;
    END $$;
    DROP TRIGGER branches_vault_auto_activate_reception ON public.branches;
    DROP FUNCTION public.vault_auto_activate_reception();
    """)
    op.alter_column("vault_transfers", "branch_id", existing_type=sa.Uuid(), nullable=False)
    op.drop_constraint("vault_account_kind", "vault_accounts", type_="check")
    op.create_check_constraint(
        "vault_account_kind", "vault_accounts",
        "kind IN ('reception','main_cash','main_visa','main_instapay')",
    )
