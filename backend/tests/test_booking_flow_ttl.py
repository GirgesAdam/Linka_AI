from __future__ import annotations

from app.core.config import settings
from app.services.conversation_flows import flow_ttl_hours


def test_booking_flow_gets_longer_resume_window(monkeypatch) -> None:
    monkeypatch.setattr(settings, "agent_flow_ttl_hours", 24)
    monkeypatch.setattr(settings, "agent_booking_flow_ttl_hours", 168)

    assert flow_ttl_hours("booking") == 168
    assert flow_ttl_hours("appointment_reschedule") == 24
