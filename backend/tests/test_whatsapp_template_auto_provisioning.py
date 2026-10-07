from pathlib import Path
from types import SimpleNamespace

from app.core.meta_whatsapp_config import meta_whatsapp_settings
from app.core.meta_whatsapp_templates import (
    STANDARD_TEMPLATE_BY_RULE_KEY,
    STANDARD_WHATSAPP_TEMPLATES,
    template_create_payload,
)
from app.services import meta_whatsapp_transport as transport


def test_every_product_whatsapp_template_has_a_canonical_meta_contract() -> None:
    rule_keys = {template.rule_key for template in STANDARD_WHATSAPP_TEMPLATES}
    assert {
        "appointment_reminder_6h",
        "post_visit_followup",
        "cancellation_recovery",
        "lead_not_booked_followup",
    }.issubset(rule_keys)
    assert "appointment_reminder_legacy" in rule_keys
    assert "booking_confirmation" not in STANDARD_TEMPLATE_BY_RULE_KEY
    assert len({template.name for template in STANDARD_WHATSAPP_TEMPLATES}) == len(
        STANDARD_WHATSAPP_TEMPLATES
    )
    assert all(template.language == "ar_EG" for template in STANDARD_WHATSAPP_TEMPLATES)
    assert STANDARD_TEMPLATE_BY_RULE_KEY["lead_not_booked_followup"].category == "MARKETING"
    assert all(
        len(template.example_body_parameters) == template.body_text.count("{{")
        for template in STANDARD_WHATSAPP_TEMPLATES
    )
    for template in STANDARD_WHATSAPP_TEMPLATES:
        payload = template_create_payload(template)
        assert payload["name"] == template.name
        assert payload["language"] == "ar_EG"
        assert payload["components"]


def test_provisioning_creates_all_missing_templates(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(meta_whatsapp_settings, "meta_graph_api_version", "v26.0")

    def fake_post(url, *, json, headers, timeout):
        calls.append((url, json, headers, timeout))
        return SimpleNamespace(status_code=200, json=lambda: {"status": "PENDING"})

    monkeypatch.setattr(transport.httpx, "post", fake_post)
    statuses, errors = transport.provision_standard_whatsapp_templates(
        "token",
        "123",
        current_statuses={},
    )
    assert not errors
    assert len(calls) == len(STANDARD_WHATSAPP_TEMPLATES)
    assert set(statuses) == {template.name for template in STANDARD_WHATSAPP_TEMPLATES}
    assert set(statuses.values()) == {"pending"}


def test_provisioning_does_not_recreate_existing_templates(monkeypatch) -> None:
    approved = {template.name: "approved" for template in STANDARD_WHATSAPP_TEMPLATES}
    monkeypatch.setattr(meta_whatsapp_settings, "meta_graph_api_version", "v26.0")

    def unexpected_post(*args, **kwargs):
        raise AssertionError("existing standard templates must not be recreated")

    monkeypatch.setattr(transport.httpx, "post", unexpected_post)
    statuses, errors = transport.provision_standard_whatsapp_templates(
        "token",
        "123",
        current_statuses=approved,
    )
    assert statuses == approved
    assert not errors


def test_automation_ui_has_no_manual_template_names_or_seven_day_limit() -> None:
    backend = Path(__file__).resolve().parent.parent
    repo = backend.parent
    page = (repo / "frontend/src/app/(dashboard)/automations/page.tsx").read_text(encoding="utf-8")
    onboarding = (
        repo / "frontend/src/app/(dashboard)/automations/whatsapp-direct-onboarding.tsx"
    ).read_text(encoding="utf-8")
    timing = (repo / "frontend/src/components/automation-timing-form.tsx").read_text(encoding="utf-8")
    actions = (repo / "frontend/src/app/(dashboard)/automations/actions.ts").read_text(encoding="utf-8")
    schema = (backend / "app/schemas/automation.py").read_text(encoding="utf-8")

    assert "إعدادات قوالب واتساب المعتمدة" not in page
    assert "saveAiFollowupTemplates" not in page
    assert "قوالب الرسائل" in onboarding
    assert "10080" not in timing
    assert "cannot exceed 7 days" not in actions
    assert "le=10080" not in schema
