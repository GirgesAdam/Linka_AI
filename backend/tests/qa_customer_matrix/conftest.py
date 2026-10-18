import json
from pathlib import Path

import pytest


def pytest_addoption(parser):
    group = parser.getgroup("qa_customer_matrix")
    group.addoption("--qa-live", action="store_true", default=False,
                    help="Use the real model with the existing in-memory V2 fixture harness; no DB writes.")
    group.addoption("--qa-case", default="", help="Comma-separated scenario IDs, e.g. FAQ-001,LNG-001")
    group.addoption("--qa-evidence-dir", default="artifacts/qa_customer_matrix",
                    help="Local evidence and investigation-report directory.")


def pytest_collection_modifyitems(config, items):
    selected = {s.strip() for s in config.getoption("--qa-case").split(",") if s.strip()}
    if selected:
        suite = json.loads(Path(__file__).with_name("customer_scenarios.json").read_text())
        live_ids = {c["id"] for c in suite["cases"] if c["execution_level"] == "LIVE_FIXTURE"}
        unknown = selected - live_ids
        if unknown:
            raise pytest.UsageError(f"Not a supported LIVE_FIXTURE case: {sorted(unknown)}")
