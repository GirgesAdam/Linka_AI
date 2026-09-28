from __future__ import annotations

import hashlib
import hmac
import json
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from sqlalchemy.orm import Session

from app.api.dependencies.security import WorkspaceAccess, get_workspace_admin
from app.core.meta_whatsapp_config import meta_whatsapp_settings
from app.database.session import get_db
from app.models.channel_connection import ChannelConnection
from app.models.channel_provider_credential import ChannelProviderCredential
from app.models.workspace import Workspace
from app.schemas.whatsapp_setup import (
    WhatsAppDirectConnect,
    WhatsAppDirectConnectResult,
    WhatsAppSetupState,
)
from app.services.meta_whatsapp_media import ingest_meta_media_webhook
from app.services.meta_whatsapp_onboarding import (
    MetaWhatsAppConfigurationError,
    MetaWhatsAppConflictError,
    MetaWhatsAppProviderError,
    build_whatsapp_setup_state,
    connect_direct_meta,
    finish_direct_meta_setup,
    verify_direct_webhook_challenge,
)
from app.services.meta_whatsapp_transport import ingest_meta_webhook, run_meta_transport_tick
from app.services.provider_credentials import ProviderCredentialError, decrypt_provider_secret
from app.services.workspace_runtime_policy import (
    demo_whatsapp_reply_test_enabled,
    workspace_runtime_policy,
)

router = APIRouter()


def _verify_scoped_signature(body: bytes, signature_header: str | None, app_secret: str) -> bool:
    signature = (signature_header or "").strip()
    if not signature.startswith("sha256="):
        return False
    supplied = signature.removeprefix("sha256=").strip().lower()
    if not supplied:
        return False
    expected = hmac.new(app_secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, supplied)


def _provider_setup_detail(exc: MetaWhatsAppProviderError) -> str:
    message = str(exc).strip() or "Meta rejected the WhatsApp setup request."
    return f"{message} (Meta code {exc.code})" if exc.code else message


def _public_callback_base_url(request: Request) -> str:
    base_url = str(request.base_url).rstrip("/")
    forwarded_proto = (
        (request.headers.get("x-forwarded-proto") or "")
        .split(",", 1)[0]
        .strip()
        .lower()
    )
    if forwarded_proto == "https" and base_url.startswith("http://"):
        return "https://" + base_url.removeprefix("http://")
    return base_url


@router.get("/webhook/{connection_id}", include_in_schema=False)
def whatsapp_meta_webhook_verify(
    connection_id: UUID,
    db: Annotated[Session, Depends(get_db)],
    mode: Annotated[str | None, Query(alias="hub.mode")] = None,
    verify_token: Annotated[str | None, Query(alias="hub.verify_token")] = None,
    challenge: Annotated[str | None, Query(alias="hub.challenge")] = None,
) -> Response:
    if challenge is None or not verify_direct_webhook_challenge(
        db,
        connection_id=connection_id,
        mode=mode,
        verify_token=verify_token,
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid Meta webhook verification.",
        )
    return Response(content=challenge, media_type="text/plain")


@router.post("/webhook/{connection_id}", include_in_schema=False)
async def whatsapp_meta_webhook_receive(
    connection_id: UUID,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    x_hub_signature_256: Annotated[
        str | None, Header(alias="X-Hub-Signature-256")
    ] = None,
) -> dict[str, int | bool]:
    connection = db.get(ChannelConnection, connection_id)
    if (
        connection is None
        or connection.channel != "whatsapp"
        or connection.provider != "meta_cloud"
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="WhatsApp connection not found.")
    credential = db.get(ChannelProviderCredential, connection_id)
    if credential is None or not credential.app_secret_ciphertext:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Meta App Secret is not configured for this connection.",
        )
    try:
        app_secret = decrypt_provider_secret(credential.app_secret_ciphertext)
    except ProviderCredentialError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Stored Meta App Secret cannot be decrypted.",
        ) from exc

    body = await request.body()
    if not _verify_scoped_signature(body, x_hub_signature_256, app_secret):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Meta webhook signature.",
        )
    try:
        payload = json.loads(body)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid Meta webhook JSON.") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid Meta webhook payload.")
    workspace = db.get(Workspace, connection.workspace_id)
    if workspace is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workspace not found.")
    policy = workspace_runtime_policy(workspace)
    if not policy.allow_external_ingress and not demo_whatsapp_reply_test_enabled(
        workspace, connection
    ):
        return {"received": True, "ignored": True}
    media_result = ingest_meta_media_webhook(db, connection=connection, payload=payload)
    result = ingest_meta_webhook(db, payload)
    return {"received": True, **result, **media_result}


def _require_transport_worker(
    x_tia_transport_token: Annotated[
        str | None, Header(alias="X-Tia-Transport-Token")
    ] = None,
) -> None:
    expected = meta_whatsapp_settings.channel_transport_worker_token
    if not expected or not expected.strip():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Tia WhatsApp transport worker is not configured.",
        )
    supplied = (x_tia_transport_token or "").strip()
    if not supplied or not hmac.compare_digest(supplied, expected.strip()):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid transport worker token.",
        )


@router.post("/transport/tick")
def whatsapp_transport_tick(
    _worker: Annotated[None, Depends(_require_transport_worker)],
    db: Annotated[Session, Depends(get_db)],
    limit_per_connection: Annotated[int, Query(ge=1, le=50)] = 10,
    max_connections: Annotated[int, Query(ge=1, le=100)] = 25,
) -> dict[str, int]:
    return run_meta_transport_tick(
        db,
        limit_per_connection=limit_per_connection,
        max_connections=max_connections,
    )


@router.get("/setup", response_model=WhatsAppSetupState)
def whatsapp_setup_state(
    access: Annotated[WorkspaceAccess, Depends(get_workspace_admin)],
    db: Annotated[Session, Depends(get_db)],
) -> WhatsAppSetupState:
    return build_whatsapp_setup_state(db, workspace_id=access.workspace.id)


@router.post("/setup/direct", response_model=WhatsAppDirectConnectResult)
def whatsapp_direct_connect(
    payload: WhatsAppDirectConnect,
    request: Request,
    access: Annotated[WorkspaceAccess, Depends(get_workspace_admin)],
    db: Annotated[Session, Depends(get_db)],
) -> WhatsAppDirectConnectResult:
    try:
        state = connect_direct_meta(
            db,
            workspace_id=access.workspace.id,
            created_by_user_id=access.user.id,
            app_id=payload.app_id,
            app_secret=payload.app_secret,
            waba_id=payload.waba_id,
            phone_number_id=payload.phone_number_id,
            access_token=payload.access_token,
            callback_base_url=_public_callback_base_url(request),
        )
    except MetaWhatsAppConfigurationError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    except MetaWhatsAppConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except MetaWhatsAppProviderError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=_provider_setup_detail(exc),
        ) from exc
    return WhatsAppDirectConnectResult(state=state)


@router.post("/setup/direct/finish", response_model=WhatsAppDirectConnectResult)
def whatsapp_direct_finish(
    access: Annotated[WorkspaceAccess, Depends(get_workspace_admin)],
    db: Annotated[Session, Depends(get_db)],
) -> WhatsAppDirectConnectResult:
    try:
        state = finish_direct_meta_setup(db, workspace_id=access.workspace.id)
    except MetaWhatsAppConfigurationError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    except MetaWhatsAppConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except MetaWhatsAppProviderError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=_provider_setup_detail(exc),
        ) from exc
    return WhatsAppDirectConnectResult(state=state)
