from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def test_failed_scenario_preserves_turn_evidence_in_artifact(tmp_path: Path) -> None:
    backend_dir = Path(__file__).resolve().parents[1]
    report_path = tmp_path / "explicit-daypart-live-review.json"
    child = r'''
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import scripts.run_explicit_daypart_live_review as review


@contextmanager
def fake_rollback_session():
    yield object()


def fake_setup(_db, scenario, *, hole=False):
    assert scenario == "RA1"
    assert hole is True
    return SimpleNamespace(
        workspace=SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000001")),
        turns=[],
    )


def failing_validator(ctx):
    ctx.turns.append(
        {
            "customer": "RA1 customer",
            "agent": "16:15 is unavailable; 16:00 or 16:30 are available.",
            "raw_structured": [
                {
                    "operations": [
                        {
                            "time_period_provenance": "explicit_pm",
                            "entities": {
                                "date": {"value": "2026-10-15"},
                                "time": {"start_time": "16:15"},
                            },
                        }
                    ]
                }
            ],
            "active_task": {
                "constraints": {
                    "date": {"value": "2026-10-15"},
                    "time": {"start_time": "16:15"},
                }
            },
            "availability_requests": [
                {"date": "2026-10-15", "start_time": "16:15"}
            ],
            "read_batches": [
                {
                    "requests": [
                        {
                            "kind": "availability",
                            "parameters": {
                                "date": "2026-10-15",
                                "start_time": "16:15",
                            },
                        }
                    ],
                    "results": [
                        {
                            "kind": "availability",
                            "ok": True,
                            "payload": {
                                "nearest_alternative_slots": [
                                    {"start_local": "2026-10-15T16:00:00+03:00"},
                                    {"start_local": "2026-10-15T16:30:00+03:00"},
                                ]
                            },
                        }
                    ],
                }
            ],
            "availability_reference_context": {
                "availability_reference_options": []
            },
            "db_effect": {
                "appointments_before": [],
                "appointments_after": [],
                "appointment_delta": 0,
            },
        }
    )
    raise AssertionError("forced RA1 observability failure")


review.SCENARIOS = [("RA1", True, failing_validator)]
review.rollback_session = fake_rollback_session
review._setup = fake_setup
review._persisted_appointments = lambda _workspace_id: 0
review.REPORT_PATH = Path(sys.argv[1])
raise SystemExit(review.main())
'''
    env = os.environ.copy()
    env.setdefault("OPENAI_API_KEY", "test-observability-no-call")
    completed = subprocess.run(
        [sys.executable, "-c", child, str(report_path)],
        cwd=backend_dir,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 1, completed.stdout + completed.stderr
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    result = payload["results"][0]
    turn = result["turns"][0]

    assert payload["status"] == "FAIL"
    assert result["status"] == "FAIL"
    assert result["error"] == "AssertionError: forced RA1 observability failure"
    assert len(result["turns"]) == 1
    assert turn["read_batches"][0]["results"][0]["payload"]["nearest_alternative_slots"]
    assert turn["agent"] == "16:15 is unavailable; 16:00 or 16:30 are available."
    assert turn["db_effect"]["appointment_delta"] == 0
