from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

BENCHMARK_DIR = Path(__file__).resolve().parent
if str(BENCHMARK_DIR) not in sys.path:
    sys.path.insert(0, str(BENCHMARK_DIR))

import v2_selected_agent_execution as execution


def main() -> int:
    parser = argparse.ArgumentParser(description="Prove selected-Agent QA DB identity and outer rollback isolation.")
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--expected-database-name", required=True)
    args = parser.parse_args()

    from sqlalchemy import create_engine, text

    marker = "ci-selected-agent-" + uuid.uuid4().hex
    workspace_id = str(uuid.uuid4())
    workspace_slug = "qa-selected-agent-" + uuid.uuid4().hex[:10]
    engine = create_engine(execution._normalize_sqlalchemy_url(args.database_url), pool_pre_ping=True)
    identity_engine = None
    try:
        with engine.begin() as connection:
            connection.execute(text("""
                create table if not exists public.linka_qa_environment_marker (
                    marker_key text primary key,
                    marker_value text not null
                )
            """))
            connection.execute(text("""
                create table if not exists public.linka_qa_rollback_probe (
                    probe_id text primary key,
                    created_at timestamptz not null default now()
                )
            """))
            connection.execute(text("""
                insert into public.linka_qa_environment_marker(marker_key, marker_value)
                values ('selected_agent_runner', :marker)
                on conflict (marker_key) do update set marker_value=excluded.marker_value
            """), {"marker": marker})
            connection.execute(text("""
                insert into workspaces(id, name, slug, timezone, is_active, is_demo)
                values (:id, 'Selected Agent QA CI', :slug, 'Africa/Cairo', true, true)
            """), {"id": workspace_id, "slug": workspace_slug})
            connection.execute(text("""
                insert into clinic_integrations(workspace_id, mode, adapter_key, status, config, authority_policy)
                values (:workspace_id, 'tia_native', 'tia_database', 'active', '{}'::jsonb, '{}'::jsonb)
            """), {"workspace_id": workspace_id})

        wrong_marker_rejected = False
        try:
            execution.verify_runtime_database_identity(
                args.database_url,
                expected_database_name=args.expected_database_name,
                expected_marker="definitely-wrong-marker",
                expected_workspace_id=workspace_id,
                expected_workspace_slug=workspace_slug,
            )
        except execution.LiveSafetyError:
            wrong_marker_rejected = True
        if not wrong_marker_rejected:
            raise RuntimeError("Runtime identity gate accepted an incorrect QA marker.")

        identity_engine, identity = execution.verify_runtime_database_identity(
            args.database_url,
            expected_database_name=args.expected_database_name,
            expected_marker=marker,
            expected_workspace_id=workspace_id,
            expected_workspace_slug=workspace_slug,
        )
        rollback = execution.verify_outer_transaction_rollback(identity_engine)
        if rollback["persisted_after_outer_rollback"] is not False:
            raise RuntimeError("Rollback probe persisted unexpectedly.")
        print(json.dumps({
            "database_identity_verified": True,
            "wrong_marker_rejected": wrong_marker_rejected,
            "workspace_identity_verified": identity["workspace_id"] == workspace_id,
            "workspace_is_demo": identity["workspace_is_demo"],
            "native_integration_verified": identity["clinic_integration"] == {
                "mode": "tia_native", "adapter_key": "tia_database", "status": "active"
            },
            "write_visible_during_transaction": rollback["write_visible_during_transaction"],
            "persisted_after_outer_rollback": rollback["persisted_after_outer_rollback"],
            "rollback_verified": rollback["rollback_verified"],
        }, sort_keys=True))
        return 0
    finally:
        if identity_engine is not None:
            identity_engine.dispose()
        try:
            with engine.begin() as connection:
                connection.execute(text("delete from clinic_integrations where workspace_id=:workspace_id"), {"workspace_id": workspace_id})
                connection.execute(text("delete from workspaces where id=:workspace_id"), {"workspace_id": workspace_id})
                connection.execute(text("delete from public.linka_qa_environment_marker where marker_key='selected_agent_runner'"))
                connection.execute(text("drop table if exists public.linka_qa_rollback_probe"))
                connection.execute(text("drop table if exists public.linka_qa_environment_marker"))
        finally:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
