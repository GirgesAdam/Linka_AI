from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
import sys

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only production validation gate for Linka Analytics."
    )
    parser.add_argument("--workspace-slug", required=True)
    parser.add_argument(
        "--explain",
        action="store_true",
        help="Run PostgreSQL EXPLAIN ANALYZE on representative heavy analytics access patterns.",
    )
    parser.add_argument(
        "--max-query-ms",
        type=float,
        default=1500.0,
        help="Fail when a representative EXPLAIN ANALYZE execution exceeds this threshold.",
    )
    args = parser.parse_args()

    # Keep database/driver imports lazy so `--help` and static release tooling do
    # not require the PostgreSQL runtime packages. A real gate run still uses the
    # same bounded Analytics pool as the product.
    from sqlalchemy import select

    from app.database.session import AnalyticsSessionLocal
    from app.models.workspace import Workspace
    from app.services.analytics_integrity import run_analytics_integrity_gate

    with AnalyticsSessionLocal() as db:
        workspace = db.scalar(select(Workspace).where(Workspace.slug == args.workspace_slug))
        if workspace is None:
            print(json.dumps({"passed": False, "error": "workspace_not_found"}, ensure_ascii=False))
            return 2
        try:
            report = run_analytics_integrity_gate(
                db,
                workspace_id=workspace.id,
                now=datetime.now(UTC),
                include_postgres_explain=args.explain,
            )
        except RuntimeError as exc:
            print(json.dumps({"passed": False, "error": str(exc)}, ensure_ascii=False))
            return 3

    payload = report.as_dict()
    slow = [
        item
        for item in payload["plan_audits"]
        if item.get("execution_ms") is not None and item["execution_ms"] > args.max_query_ms
    ]
    if slow:
        payload["passed"] = False
        payload["slow_query_plans"] = slow
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0 if payload["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
