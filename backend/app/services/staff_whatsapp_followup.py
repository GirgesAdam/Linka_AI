from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.core.meta_whatsapp_config import meta_whatsapp_settings
from app.models.channel_connection import ChannelConnection
from app.models.channel_provider_credential import ChannelProviderCredential
from app.services.provider_credentials import (
    ProviderCredentialError,
    decrypt_provider_access_token,
)

STAFF_FOLLOWUP_TEMPLATE_NAME = "tia_staff_followup_01"
STAFF_FOLLOWUP_TEMPLATE_LANGUAGE = "ar_EG"
STAFF_FOLLOWUP_TEMPLATE_CATEGORY = "UTILITY"
STAFF_FOLLOWUP_TEMPLATE_BODY = (
    "أهلًا {{1}} 👋 بنتابع معاك بخصوص طلبك السابق مع {{2}}. "
    "لو حابب/حابة نكمل المتابعة، رد علينا هنا وإحنا موجودين."
)
STAFF_FOLLOWUP_TEMPLATE_EXAMPLE = ("مريم", "Linka Clinic")


class StaffWhatsAppFollowupError(RuntimeError):
    pass


def _graph_url(path: str) -> str:
    version = str(meta_whatsapp_settings.meta_graph_api_version or "").strip()
    if not version:
        raise StaffWhatsAppFollowupError("Meta Graph API version is not configured.")
    normalized = version if version.startswith("v") else f"v{version}"
    return f"https://graph.facebook.com/{normalized}/{path.lstrip('/')}"


def _provider_error(response: httpx.Response) -> str:
    fallback = f"Meta request failed with HTTP {response.status_code}."
    try:
        payload = response.json()
    except ValueError:
        return fallback
    if not isinstance(payload, dict):
        return fallback
    raw = payload.get("error")
    if not isinstance(raw, dict):
        return fallback
    message = str(raw.get("message") or fallback).strip()
    return message[:2000]


def _template_statuses(connection: ChannelConnection) -> dict[str, str]:
    raw = (connection.config_json or {}).get("template_statuses")
    if not isinstance(raw, dict):
        return {}
    return {str(key): str(value).strip().lower() for key, value in raw.items()}


def staff_followup_template_status(connection: ChannelConnection) -> str:
    return _template_statuses(connection).get(STAFF_FOLLOWUP_TEMPLATE_NAME, "missing")


def render_staff_followup_message(*, patient_first_name: str, clinic_name: str) -> str:
    return (
        f"أهلًا {patient_first_name} 👋 بنتابع معاك بخصوص طلبك السابق مع {clinic_name}. "
        "لو حابب/حابة نكمل المتابعة، رد علينا هنا وإحنا موجودين."
    )


def _store_status(
    connection: ChannelConnection,
    *,
    status: str,
    error: str | None = None,
) -> None:
    config: dict[str, Any] = dict(connection.config_json or {})
    statuses = _template_statuses(connection)
    statuses[STAFF_FOLLOWUP_TEMPLATE_NAME] = status
    config["template_statuses"] = statuses
    config["staff_followup_template"] = {
        "name": STAFF_FOLLOWUP_TEMPLATE_NAME,
        "language_code": STAFF_FOLLOWUP_TEMPLATE_LANGUAGE,
        "status": status,
        "last_checked_at": datetime.now(UTC).isoformat(),
    }
    if error:
        config["staff_followup_template_error"] = error[:2000]
    else:
        config.pop("staff_followup_template_error", None)
    connection.config_json = config


def ensure_staff_followup_template(
    db: Session,
    *,
    connection: ChannelConnection,
) -> tuple[str, str | None]:
    """Ensure the optional single-customer follow-up template has been requested.

    This template is intentionally outside the standard automation template set so
    its Meta review never blocks clinic onboarding or automation readiness.
    """
    if connection.channel != "whatsapp" or connection.provider != "meta_cloud":
        return "unsupported", "This WhatsApp connection is not using Meta Cloud API."

    current_status = staff_followup_template_status(connection)
    if current_status in {"approved", "pending", "rejected", "disabled"}:
        return current_status, None

    credential = db.get(ChannelProviderCredential, connection.id)
    if credential is None or credential.workspace_id != connection.workspace_id:
        return "unavailable", "WhatsApp provider credentials are missing."
    if credential.expires_at is not None and credential.expires_at <= datetime.now(UTC):
        return "unavailable", "Meta access token has expired."

    try:
        token = decrypt_provider_access_token(credential.access_token_ciphertext)
    except ProviderCredentialError as exc:
        return "unavailable", str(exc)

    waba_id = str((connection.config_json or {}).get("waba_id") or "").strip()
    if not waba_id:
        return "unavailable", "WhatsApp Business Account ID is missing."

    payload = {
        "name": STAFF_FOLLOWUP_TEMPLATE_NAME,
        "language": STAFF_FOLLOWUP_TEMPLATE_LANGUAGE,
        "category": STAFF_FOLLOWUP_TEMPLATE_CATEGORY,
        "components": [
            {
                "type": "BODY",
                "text": STAFF_FOLLOWUP_TEMPLATE_BODY,
                "example": {"body_text": [list(STAFF_FOLLOWUP_TEMPLATE_EXAMPLE)]},
            }
        ],
    }
    try:
        response = httpx.post(
            _graph_url(f"{waba_id}/message_templates"),
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
            timeout=20.0,
        )
    except httpx.HTTPError as exc:
        error = str(exc)[:2000]
        _store_status(connection, status="error", error=error)
        db.flush()
        return "error", error

    if response.status_code >= 400:
        error = _provider_error(response)
        _store_status(connection, status="error", error=error)
        db.flush()
        return "error", error

    body = response.json()
    raw_status = body.get("status") if isinstance(body, dict) else None
    status = str(raw_status or "pending").strip().lower()
    _store_status(connection, status=status)
    db.flush()
    return status, None
