from __future__ import annotations

from app.agents.v2.semantic_context import build_semantic_context
from scripts.run_human_conversation_quality_batch1_review import _priced_env


def test_human_quality_fixture_exposes_structured_clinic_hours() -> None:
    env = _priced_env()
    context = build_semantic_context(env.catalog)
    assert context.server_metadata["clinic_operating_hours"] == [
        {"weekday": weekday, "start": "10:00", "end": "22:00"}
        for weekday in range(7)
    ]
