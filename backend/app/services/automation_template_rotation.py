from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.meta_whatsapp_templates import (
    STANDARD_TEMPLATE_BY_RULE_KEY,
    STANDARD_TEMPLATES_BY_RULE_KEY,
    approved_standard_templates,
    approved_template_refs_for_rule,
)
from app.models.automation_rule import AutomationRule
from app.models.channel_connection import ChannelConnection


def _template_statuses(connection: ChannelConnection) -> dict[str, str]:
    raw = (connection.config_json or {}).get("template_statuses")
    if not isinstance(raw, dict):
        return {}
    return {str(name): str(status).lower() for name, status in raw.items()}


def sync_approved_automation_template_rotation(
    db: Session,
    *,
    workspace_id: UUID,
) -> int:
    """Keep automatic rotation limited to Meta-approved Linka templates.

    The patient-facing wording is selected by Linka, not by the clinic admin. Rules
    keep their normal ON/OFF and timing controls; this function only maintains the
    approved message pool used by the existing deterministic template selectors.
    """
    connection = db.scalar(
        select(ChannelConnection)
        .where(
            ChannelConnection.workspace_id == workspace_id,
            ChannelConnection.channel == "whatsapp",
            ChannelConnection.provider == "meta_cloud",
            ChannelConnection.status != "disconnected",
        )
        .order_by(ChannelConnection.created_at.desc())
        .limit(1)
    )
    if connection is None:
        return 0

    statuses = _template_statuses(connection)
    if not statuses:
        return 0

    rule_keys = tuple(STANDARD_TEMPLATES_BY_RULE_KEY)
    rules = list(
        db.scalars(
            select(AutomationRule).where(
                AutomationRule.workspace_id == workspace_id,
                AutomationRule.key.in_(rule_keys),
            )
        )
    )

    changed = 0
    for rule in rules:
        approved = approved_standard_templates(rule.key, statuses)
        approved_refs = approved_template_refs_for_rule(rule.key, statuses)
        config = dict(rule.config_json or {})
        next_config = {
            **config,
            "template_variants": approved_refs,
            "template_rotation": "automatic",
        }
        if next_config != config:
            rule.config_json = next_config
            changed += 1

        if approved:
            canonical = STANDARD_TEMPLATE_BY_RULE_KEY[rule.key]
            primary = next(
                (template for template in approved if template.name == canonical.name),
                approved[0],
            )
            if rule.template_name != primary.name or rule.template_language != primary.language:
                rule.template_name = primary.name
                rule.template_language = primary.language
                changed += 1

    lead_refs = approved_template_refs_for_rule("lead_not_booked_followup", statuses)
    connection_config = dict(connection.config_json or {})
    lead_primary = lead_refs[0] if lead_refs else {}
    next_connection_config = {
        **connection_config,
        "ai_followup_templates": lead_refs,
        "ai_followup_template": lead_primary,
    }
    if next_connection_config != connection_config:
        connection.config_json = next_connection_config
        changed += 1

    if changed:
        db.commit()
    return changed
