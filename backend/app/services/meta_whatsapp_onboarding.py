from __future__ import annotations

import hmac
import secrets
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.channel_adapter import generate_adapter_token
from app.core.meta_whatsapp_config import meta_whatsapp_settings as settings
from app.core.meta_whatsapp_templates import (
    STANDARD_TEMPLATE_BY_RULE_KEY,
    STANDARD_WHATSAPP_TEMPLATES,
)
from app.models.channel_connection import ChannelConnection
from app.models.channel_provider_credential import ChannelProviderCredential
from app.models.workspace import Workspace
from app.schemas.whatsapp_setup import WhatsAppSetupState, WhatsAppTemplateSetupStatus
from app.services.provider_credentials import (
    ProviderCredentialError,
    decrypt_provider_access_token,
    encrypt_provider_access_token,
    encrypt_provider_secret,
    provider_credential_encryption_ready,
)
from app.services.workspace_runtime_policy import (
    demo_whatsapp_reply_test_enabled,
    workspace_runtime_policy,
)


class MetaWhatsAppSetupError(RuntimeError):
    pass


class MetaWhatsAppConfigurationError(MetaWhatsAppSetupError):
    pass


class MetaWhatsAppConflictError(MetaWhatsAppSetupError):
    pass


class MetaWhatsAppProviderError(MetaWhatsAppSetupError):
    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


def _clean_optional(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def direct_setup_available() -> bool:
    return all(
        (
            _clean_optional(settings.meta_graph_api_version),
            provider_credential_encryption_ready(),
        )
    )


def _graph_url(path: str) -> str:
    version = _clean_optional(settings.meta_graph_api_version)
    if not version:
        raise MetaWhatsAppConfigurationError(
            "Meta Graph API version is not configured on the Tia platform."
        )
    normalized = version if version.startswith("v") else f"v{version}"
    return f"https://graph.facebook.com/{normalized}/{path.lstrip('/')}"


def _provider_error(response: httpx.Response, fallback: str) -> MetaWhatsAppProviderError:
    code: str | None = None
    message = fallback
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        raw_error = payload.get("error")
        if isinstance(raw_error, dict):
            if raw_error.get("message"):
                message = str(raw_error["message"])
            if raw_error.get("code") is not None:
                code = str(raw_error["code"])
    return MetaWhatsAppProviderError(message, code=code)


def _setup_provider_health(
    provider_error: MetaWhatsAppProviderError | None,
    *,
    now: datetime,
) -> dict[str, Any]:
    code = provider_error.code if provider_error else None
    state = (
        "disabled"
        if code == "131031"
        else "degraded"
        if provider_error is not None
        else "setup_pending"
    )
    health: dict[str, Any] = {
        "state": state,
        "current_error_code": code,
        "current_error": str(provider_error) if provider_error else None,
        "last_checked_at": now.isoformat(),
    }
    if code == "190":
        health["action_required"] = "reconnect_meta"
    elif code == "131031":
        health["action_required"] = "meta_account_review"
    return health


def _validate_token_and_app(*, app_id: str, app_secret: str, token: str) -> None:
    try:
        response = httpx.get(
            _graph_url("debug_token"),
            params={
                "input_token": token,
                "access_token": f"{app_id}|{app_secret}",
            },
            timeout=20.0,
        )
    except httpx.HTTPError as exc:
        raise MetaWhatsAppProviderError(
            "Could not reach Meta while validating the app and access token."
        ) from exc
    if response.status_code >= 400:
        raise _provider_error(
            response,
            "Meta could not validate the App ID, App Secret, and System User token.",
        )
    payload = response.json()
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict) or not bool(data.get("is_valid")):
        raise MetaWhatsAppProviderError(
            "Meta says this System User access token is not valid. Generate a new token and try again."
        )
    returned_app_id = str(data.get("app_id") or "").strip()
    if returned_app_id and returned_app_id != app_id:
        raise MetaWhatsAppProviderError(
            "The System User token belongs to a different Meta App than the App ID entered in Tia."
        )
    raw_scopes = data.get("scopes")
    scopes = {str(value) for value in raw_scopes} if isinstance(raw_scopes, list) else set()
    required = {"whatsapp_business_management", "whatsapp_business_messaging"}
    missing = sorted(required - scopes)
    if missing:
        raise MetaWhatsAppProviderError(
            "The System User token is missing required WhatsApp permissions: "
            + ", ".join(missing)
        )


def _read_phone_number(token: str, phone_number_id: str) -> dict[str, Any]:
    try:
        response = httpx.get(
            _graph_url(phone_number_id),
            params={"fields": "id,display_phone_number,verified_name,quality_rating"},
            headers={"Authorization": f"Bearer {token}"},
            timeout=20.0,
        )
    except httpx.HTTPError as exc:
        raise MetaWhatsAppProviderError(
            "Could not verify the WhatsApp phone number with Meta."
        ) from exc
    if response.status_code >= 400:
        raise _provider_error(response, "Meta could not verify this WhatsApp phone number.")
    payload = response.json()
    return payload if isinstance(payload, dict) else {}


def _verify_waba_contains_phone(token: str, waba_id: str, phone_number_id: str) -> None:
    try:
        response = httpx.get(
            _graph_url(f"{waba_id}/phone_numbers"),
            params={"fields": "id", "limit": 100},
            headers={"Authorization": f"Bearer {token}"},
            timeout=20.0,
        )
    except httpx.HTTPError as exc:
        raise MetaWhatsAppProviderError(
            "Could not verify the WhatsApp Business Account with Meta."
        ) from exc
    if response.status_code >= 400:
        raise _provider_error(
            response,
            "Meta could not verify this WhatsApp Business Account.",
        )
    payload = response.json()
    data = payload.get("data") if isinstance(payload, dict) else None
    phone_ids = {
        str(item.get("id"))
        for item in data if isinstance(data, list)
        if isinstance(item, dict) and item.get("id") is not None
    }
    if phone_number_id not in phone_ids:
        raise MetaWhatsAppProviderError(
            "The Phone Number ID does not belong to the WhatsApp Business Account ID entered in Tia."
        )


def _subscribe_app(token: str, waba_id: str) -> None:
    try:
        response = httpx.post(
            _graph_url(f"{waba_id}/subscribed_apps"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=20.0,
        )
    except httpx.HTTPError as exc:
        raise MetaWhatsAppProviderError(
            "Could not subscribe this WhatsApp account to its Meta webhook."
        ) from exc
    if response.status_code >= 400:
        raise _provider_error(
            response,
            "Meta rejected the WhatsApp webhook subscription for this business account.",
        )


def _template_statuses(connection: ChannelConnection | None) -> dict[str, str]:
    if connection is None:
        return {}
    raw = (connection.config_json or {}).get("template_statuses")
    if not isinstance(raw, dict):
        return {}
    return {str(key): str(value) for key, value in raw.items()}


def _connection_for_workspace(db: Session, workspace_id: UUID) -> ChannelConnection | None:
    return db.scalar(
        select(ChannelConnection)
        .where(
            ChannelConnection.workspace_id == workspace_id,
            ChannelConnection.channel == "whatsapp",
            ChannelConnection.status != "disconnected",
        )
        .order_by(ChannelConnection.created_at.desc())
        .limit(1)
    )


def build_whatsapp_setup_state(
    db: Session,
    *,
    workspace_id: UUID,
) -> WhatsAppSetupState:
    connection = _connection_for_workspace(db, workspace_id)
    config = dict(connection.config_json or {}) if connection is not None else {}
    health_raw = config.get("provider_health")
    health = health_raw if isinstance(health_raw, dict) else {}
    credential = (
        db.get(ChannelProviderCredential, connection.id) if connection is not None else None
    )
    statuses = _template_statuses(connection)
    raw_template_errors = config.get("template_provisioning_errors")
    template_errors = raw_template_errors if isinstance(raw_template_errors, dict) else {}
    template_states = [
        WhatsAppTemplateSetupStatus(
            rule_key=template.rule_key,
            label=template.label_ar,
            name=template.name,
            language=template.language,
            category=template.category,
            status=str(statuses.get(template.name) or "missing").lower(),
            error=(
                str(template_errors.get(template.name))
                if template_errors.get(template.name)
                else None
            ),
        )
        for template in STANDARD_WHATSAPP_TEMPLATES
    ]
    templates_ready = all(item.status == "approved" for item in template_states)
    provider_health_state = (
        str(health.get("state")) if health.get("state") is not None else None
    )
    provider_error_code = (
        str(health.get("current_error_code"))
        if health.get("current_error_code") is not None
        else None
    )
    provider_error = (
        str(health.get("current_error")) if health.get("current_error") else None
    )
    credentials_ready = bool(
        credential is not None
        and credential.access_token_ciphertext
        and credential.app_secret_ciphertext
    )
    transport_ready = bool(config.get("transport_ready"))
    webhook_verified = bool(config.get("webhook_verified_at"))
    connected = connection is not None
    ready = bool(
        connection is not None
        and connection.status == "active"
        and credentials_ready
        and webhook_verified
        and transport_ready
        and templates_ready
        and provider_health_state not in {"disabled", "degraded"}
    )

    admin_action: str = "none"
    admin_message = None
    system_message = None
    health_action = str(health.get("action_required") or "")
    if connection is None or not credentials_ready:
        admin_action = "connect_meta_direct"
        admin_message = (
            "اربط Meta مرة واحدة من الخطوات الموجودة هنا. Tia ستتحقق من البيانات وتخزن الأسرار مشفرة."
        )
    elif health_action == "reconnect_meta":
        admin_action = "connect_meta_direct"
        admin_message = (
            "System User token لم يعد صالحًا. أنشئ Token جديد من Meta والصقه في Tia لإعادة الربط."
        )
    elif provider_health_state == "disabled" or provider_error_code == "131031":
        admin_action = "resolve_meta_restriction"
        admin_message = (
            "Meta أوقفت أو قيّدت حساب واتساب. افتح Business Support Home ونفّذ المراجعة المطلوبة."
        )
    elif not webhook_verified:
        admin_action = "configure_webhook"
        admin_message = (
            "بيانات Meta صحيحة. انسخ Callback URL وVerify Token إلى صفحة WhatsApp Configuration ثم اضغط تحقق وكمل."
        )
    elif not transport_ready:
        system_message = "Tia بتفحص الرقم والقوالب ومسار الإرسال المباشر مع Meta."
    elif not templates_ready:
        rejected_templates = [
            item.name for item in template_states if item.status == "rejected"
        ]
        if rejected_templates:
            admin_action = "wait_for_template_review"
            admin_message = (
                "Meta رفضت قالب رسالة مطلوب للـAutomation. عدّل القالب أو اطلب مراجعته من WhatsApp Manager."
            )
        else:
            system_message = "Tia أنشأت القوالب القياسية تلقائيًا وبتتابع اعتمادها من Meta."

    return WhatsAppSetupState(
        connection_id=connection.id if connection else None,
        connection_status=connection.status if connection else None,
        connected=connected,
        display_name=connection.display_name if connection else None,
        display_phone_number=(
            str(config.get("display_phone_number"))
            if config.get("display_phone_number")
            else None
        ),
        verified_name=(
            str(config.get("verified_name")) if config.get("verified_name") else None
        ),
        provider_health_state=provider_health_state,
        provider_error_code=provider_error_code,
        provider_error=provider_error,
        direct_setup_available=direct_setup_available(),
        provider_credentials_ready=credentials_ready,
        transport_ready=transport_ready,
        templates_ready=templates_ready,
        ready_for_automations=ready,
        meta_app_id=str(config.get("meta_app_id")) if config.get("meta_app_id") else None,
        webhook_callback_url=(
            str(config.get("webhook_callback_url"))
            if config.get("webhook_callback_url")
            else None
        ),
        webhook_verify_token=(
            str(config.get("webhook_verify_token"))
            if config.get("webhook_verify_token")
            else None
        ),
        webhook_verified=webhook_verified,
        templates=template_states,
        admin_action=admin_action,  # type: ignore[arg-type]
        admin_message=admin_message,
        system_message=system_message,
    )


def connect_direct_meta(
    db: Session,
    *,
    workspace_id: UUID,
    created_by_user_id: UUID,
    app_id: str,
    app_secret: str,
    waba_id: str,
    phone_number_id: str,
    access_token: str,
    callback_base_url: str,
) -> WhatsAppSetupState:
    workspace = db.get(Workspace, workspace_id)
    if workspace is None:
        raise MetaWhatsAppConflictError("Workspace not found.")
    if not workspace_runtime_policy(workspace).allow_external_configuration:
        raise MetaWhatsAppConflictError(
            "External provider configuration is disabled for demo workspaces."
        )
    if not direct_setup_available():
        raise MetaWhatsAppConfigurationError(
            "Direct WhatsApp setup is not configured on the Tia platform."
        )

    _validate_token_and_app(app_id=app_id, app_secret=app_secret, token=access_token)
    _verify_waba_contains_phone(access_token, waba_id, phone_number_id)
    phone_info = _read_phone_number(access_token, phone_number_id)

    try:
        token_ciphertext = encrypt_provider_access_token(access_token)
        app_secret_ciphertext = encrypt_provider_secret(app_secret)
    except ProviderCredentialError as exc:
        raise MetaWhatsAppConfigurationError(str(exc)) from exc

    cross_workspace_connection = db.scalar(
        select(ChannelConnection.id).where(
            ChannelConnection.workspace_id != workspace_id,
            ChannelConnection.channel == "whatsapp",
            ChannelConnection.provider == "meta_cloud",
            ChannelConnection.external_account_id == phone_number_id,
            ChannelConnection.status.in_(("active", "paused")),
        )
    )
    if cross_workspace_connection is not None:
        raise MetaWhatsAppConflictError(
            "This WhatsApp number is already connected to another clinic."
        )

    same_connection = db.scalar(
        select(ChannelConnection).where(
            ChannelConnection.workspace_id == workspace_id,
            ChannelConnection.channel == "whatsapp",
            ChannelConnection.provider == "meta_cloud",
            ChannelConnection.external_account_id == phone_number_id,
        )
    )
    other_connection = db.scalar(
        select(ChannelConnection).where(
            ChannelConnection.workspace_id == workspace_id,
            ChannelConnection.channel == "whatsapp",
            ChannelConnection.status != "disconnected",
            ChannelConnection.external_account_id != phone_number_id,
        )
    )
    if same_connection is None and other_connection is not None:
        raise MetaWhatsAppConflictError(
            "A different WhatsApp number is already connected to this clinic. Disconnect it before replacing the number."
        )

    now = datetime.now(UTC)
    display_phone = str(phone_info.get("display_phone_number") or "").strip() or None
    verified_name = str(phone_info.get("verified_name") or "").strip() or None
    quality_rating = str(phone_info.get("quality_rating") or "").strip() or None

    if same_connection is None:
        _, adapter_token_hash = generate_adapter_token()
        connection = ChannelConnection(
            workspace_id=workspace_id,
            channel="whatsapp",
            provider="meta_cloud",
            display_name=verified_name or display_phone or "WhatsApp",
            status="paused",
            external_account_id=phone_number_id,
            adapter_token_hash=adapter_token_hash,
            created_by_user_id=created_by_user_id,
            config_json={},
        )
        db.add(connection)
        db.flush()
    else:
        connection = same_connection
        connection.display_name = verified_name or display_phone or connection.display_name
        connection.status = "paused"

    current_config = dict(connection.config_json or {})
    previous_app_id = str(current_config.get("meta_app_id") or "")
    verify_token = str(current_config.get("webhook_verify_token") or "").strip()
    if not verify_token:
        verify_token = secrets.token_urlsafe(32)
    callback_url = (
        callback_base_url.rstrip("/")
        + f"/api/v1/channels/whatsapp/webhook/{connection.id}"
    )
    if previous_app_id and previous_app_id != app_id:
        current_config.pop("webhook_verified_at", None)

    current_config.update(
        {
            "meta_app_id": app_id,
            "waba_id": waba_id,
            "phone_number_id": phone_number_id,
            "display_phone_number": display_phone,
            "verified_name": verified_name,
            "quality_rating": quality_rating,
            "direct_setup_connected_at": now.isoformat(),
            "webhook_callback_url": callback_url,
            "webhook_verify_token": verify_token,
            "transport_ready": False,
            "runtime_kind": "real",
            "transport": "tia_native_meta_cloud",
            "provider_health": _setup_provider_health(None, now=now),
        }
    )
    connection.config_json = current_config

    credential = db.get(ChannelProviderCredential, connection.id)
    if credential is None:
        credential = ChannelProviderCredential(
            channel_connection_id=connection.id,
            workspace_id=workspace_id,
            provider="meta_cloud",
            access_token_ciphertext=token_ciphertext,
            app_secret_ciphertext=app_secret_ciphertext,
            token_type="bearer",
            expires_at=None,
        )
        db.add(credential)
    else:
        credential.provider = "meta_cloud"
        credential.access_token_ciphertext = token_ciphertext
        credential.app_secret_ciphertext = app_secret_ciphertext
        credential.token_type = "bearer"
        credential.expires_at = None

    db.commit()
    db.refresh(connection)

    from app.services.meta_whatsapp_transport import provision_standard_whatsapp_templates

    template_statuses, template_errors = provision_standard_whatsapp_templates(
        access_token,
        waba_id,
    )
    current_config = dict(connection.config_json or {})
    current_config["template_statuses"] = template_statuses
    current_config["template_provisioning_errors"] = template_errors
    lead_template = STANDARD_TEMPLATE_BY_RULE_KEY["lead_not_booked_followup"]
    current_config["ai_followup_templates"] = [
        {"name": lead_template.name, "language_code": lead_template.language}
    ]
    current_config["ai_followup_template"] = {
        "name": lead_template.name,
        "language_code": lead_template.language,
    }
    connection.config_json = current_config
    db.commit()
    db.refresh(connection)
    return build_whatsapp_setup_state(db, workspace_id=workspace_id)


def verify_direct_webhook_challenge(
    db: Session,
    *,
    connection_id: UUID,
    mode: str | None,
    verify_token: str | None,
) -> bool:
    connection = db.get(ChannelConnection, connection_id)
    if (
        connection is None
        or connection.channel != "whatsapp"
        or connection.provider != "meta_cloud"
    ):
        return False
    workspace = db.get(Workspace, connection.workspace_id)
    if workspace is None:
        return False
    if (
        not workspace_runtime_policy(workspace).allow_external_configuration
        and not demo_whatsapp_reply_test_enabled(workspace, connection)
    ):
        return False
    expected = str((connection.config_json or {}).get("webhook_verify_token") or "").strip()
    supplied = (verify_token or "").strip()
    if mode != "subscribe" or not expected or not supplied:
        return False
    if not hmac.compare_digest(expected, supplied):
        return False
    config = dict(connection.config_json or {})
    config["webhook_verified_at"] = datetime.now(UTC).isoformat()
    connection.config_json = config
    db.commit()
    return True


def finish_direct_meta_setup(
    db: Session,
    *,
    workspace_id: UUID,
) -> WhatsAppSetupState:
    workspace = db.get(Workspace, workspace_id)
    if workspace is None:
        raise MetaWhatsAppConflictError("Workspace not found.")
    if not workspace_runtime_policy(workspace).allow_external_configuration:
        raise MetaWhatsAppConflictError(
            "External provider configuration is disabled for demo workspaces."
        )
    connection = _connection_for_workspace(db, workspace_id)
    if connection is None:
        raise MetaWhatsAppConflictError("Connect the clinic WhatsApp account first.")
    config = dict(connection.config_json or {})
    if not config.get("webhook_verified_at"):
        raise MetaWhatsAppConflictError(
            "Meta has not verified the Callback URL yet. Open WhatsApp Configuration, click Verify and Save, then try again."
        )
    waba_id = str(config.get("waba_id") or "").strip()
    credential = db.get(ChannelProviderCredential, connection.id)
    if credential is None:
        raise MetaWhatsAppConfigurationError("The stored Meta credential is missing.")
    try:
        token = decrypt_provider_access_token(credential.access_token_ciphertext)
    except ProviderCredentialError as exc:
        raise MetaWhatsAppConfigurationError(str(exc)) from exc
    if not waba_id:
        raise MetaWhatsAppConfigurationError("WhatsApp Business Account ID is missing.")

    try:
        _subscribe_app(token, waba_id)
    except MetaWhatsAppProviderError as exc:
        config = dict(connection.config_json or {})
        config["transport_ready"] = False
        config["provider_health"] = _setup_provider_health(exc, now=datetime.now(UTC))
        connection.config_json = config
        connection.status = "paused"
        db.commit()
        raise

    from app.services.meta_whatsapp_transport import refresh_meta_connection_readiness

    refresh_meta_connection_readiness(db, connection)
    return build_whatsapp_setup_state(db, workspace_id=workspace_id)
