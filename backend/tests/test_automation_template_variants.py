from types import SimpleNamespace
from uuid import UUID

from app.schemas.automation import AutomationRuleUpdate
from app.services.automations import (
    _appointment_template_body_parameters,
    _rule_template_candidates,
    _select_rule_template,
)


def _rule(*, key: str = "appointment_reminder_6h") -> SimpleNamespace:
    return SimpleNamespace(
        key=key,
        template_name="tia_appointment_confirmation_01",
        template_language="ar",
        config_json={
            "template_variants": [
                {"name": "tia_reminder_02", "language_code": "ar"},
                {"name": "tia_reminder_03", "language_code": "ar"},
                {"name": "tia_reminder_02", "language_code": "ar"},
            ]
        },
    )


def test_template_pool_deduplicates_primary_and_variants() -> None:
    assert _rule_template_candidates(_rule()) == [
        ("tia_appointment_confirmation_01", "ar"),
    ]


def test_template_selection_is_stable_for_same_appointment() -> None:
    rule = _rule()
    appointment_id = UUID("11111111-1111-1111-1111-111111111111")
    first = _select_rule_template(rule, appointment_id)
    second = _select_rule_template(rule, appointment_id)
    assert first == second
    assert first[2] == 1
    assert first[:2] in _rule_template_candidates(rule)


def test_template_variants_keep_same_variable_contract() -> None:
    reminder = _appointment_template_body_parameters(
        "appointment_reminder_6h",
        {
            "patient_name": "سارة",
            "service_name": "ليزر",
            "time": "18:00",
            "branch_name": "التجمع",
            "date": "25/08/2026",
        },
    )
    post_visit = _appointment_template_body_parameters(
        "post_visit_followup",
        {
            "patient_name": "سارة",
            "service_name": "ليزر",
            "time": "18:00",
            "branch_name": "التجمع",
            "date": "25/08/2026",
        },
    )
    assert reminder == ["سارة", "ليزر", "25/08/2026", "18:00"]
    assert post_visit == ["سارة", "ليزر", "25/08/2026"]


def test_rule_update_accepts_multiple_template_names_with_shared_language() -> None:
    payload = AutomationRuleUpdate(
        template_name="tia_reminder_01",
        template_language="ar",
        template_variants=[
            {"name": " tia_reminder_02 ", "language_code": " ar "},
            {"name": "tia_reminder_03", "language_code": "ar"},
        ],
    )
    assert [item.name for item in payload.template_variants or []] == [
        "tia_reminder_02",
        "tia_reminder_03",
    ]
    assert all(item.language_code == "ar" for item in payload.template_variants or [])
