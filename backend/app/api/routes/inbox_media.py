from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies.page_permissions import (
    require_page_permission,
)
from app.api.dependencies.security import WorkspaceAccess
from app.database.session import get_db
from app.models.channel_connection import ChannelConnection
from app.models.channel_provider_credential import ChannelProviderCredential
from app.models.message import Message
from app.services.meta_whatsapp_media import (
    MetaWhatsAppMediaError,
    open_meta_media_stream,
)
from app.services.provider_credentials import (
    ProviderCredentialError,
    decrypt_provider_access_token,
)

router = APIRouter()
_MEDIA_TYPES = frozenset({"image", "audio", "video", "document", "sticker"})


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _safe_filename(message: Message) -> str:
    metadata = message.metadata_json if isinstance(message.metadata_json, dict) else {}
    raw_media = metadata.get("media")
    media = raw_media if isinstance(raw_media, dict) else {}
    raw = str(media.get("filename") or "").strip()
    if not raw:
        extension = {
            "image": "jpg",
            "audio": "audio",
            "video": "mp4",
            "document": "file",
            "sticker": "webp",
        }.get(message.message_type, "file")
        raw = f"tia-{message.message_type}-{message.id}.{extension}"
    sanitized = raw.replace("\r", " ").replace("\n", " ").replace("/", "-").replace("\\", "-")
    return sanitized[:180] or f"tia-media-{message.id}"


@router.get("/messages/{message_id}/media", include_in_schema=False)
def stream_inbox_message_media(
    message_id: UUID,
    access: Annotated[WorkspaceAccess, Depends(require_page_permission("inbox.detail", legacy_access="reader"))],
    db: Annotated[Session, Depends(get_db)],
    range_header: Annotated[str | None, Header(alias="Range")] = None,
) -> StreamingResponse:
    message = db.scalar(
        select(Message).where(
            Message.workspace_id == access.workspace.id,
            Message.id == message_id,
        )
    )
    if (
        message is None
        or message.direction != "inbound"
        or message.message_type not in _MEDIA_TYPES
        or message.channel_connection_id is None
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Media message not found.")

    raw_metadata = message.metadata_json if isinstance(message.metadata_json, dict) else {}
    raw_media = raw_metadata.get("media")
    media = raw_media if isinstance(raw_media, dict) else {}
    media_id = str(media.get("id") or "").strip()
    if not media_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Media reference is missing.")

    connection = db.scalar(
        select(ChannelConnection).where(
            ChannelConnection.workspace_id == access.workspace.id,
            ChannelConnection.id == message.channel_connection_id,
            ChannelConnection.channel == "whatsapp",
            ChannelConnection.provider == "meta_cloud",
        )
    )
    if connection is None or not str(connection.external_account_id or "").strip():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="WhatsApp media connection is unavailable.",
        )

    credential = db.scalar(
        select(ChannelProviderCredential).where(
            ChannelProviderCredential.workspace_id == access.workspace.id,
            ChannelProviderCredential.channel_connection_id == connection.id,
            ChannelProviderCredential.provider == "meta_cloud",
        )
    )
    if credential is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="WhatsApp media credentials are unavailable.",
        )
    if credential.expires_at is not None and _as_utc(credential.expires_at) <= datetime.now(UTC):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="WhatsApp media credentials have expired.",
        )

    try:
        token = decrypt_provider_access_token(credential.access_token_ciphertext)
        stream = open_meta_media_stream(
            access_token=token,
            media_id=media_id,
            phone_number_id=str(connection.external_account_id),
            expected_mime_type=str(media.get("mime_type") or "").strip() or None,
            range_header=range_header,
        )
    except ProviderCredentialError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Stored WhatsApp media credential cannot be used.",
        ) from exc
    except MetaWhatsAppMediaError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not retrieve this WhatsApp media item from Meta.",
        ) from exc

    filename = _safe_filename(message)
    disposition = "attachment" if message.message_type == "document" else "inline"
    headers = {
        "Cache-Control": "private, no-store",
        "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(filename)}",
        "X-Content-Type-Options": "nosniff",
    }
    for name, value in (
        ("Content-Length", stream.content_length),
        ("Content-Range", stream.content_range),
        ("Accept-Ranges", stream.accept_ranges),
    ):
        if value:
            headers[name] = value

    return StreamingResponse(
        stream.iter_bytes(),
        status_code=stream.status_code,
        media_type=stream.content_type,
        headers=headers,
    )
