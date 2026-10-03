from dataclasses import dataclass
from functools import lru_cache
from typing import Any
from uuid import UUID

import httpx
from supabase_auth import SyncGoTrueAdminAPI

from app.core.config import settings


class SupabaseAuthError(RuntimeError):
    pass


@dataclass(frozen=True)
class VerifiedAuthIdentity:
    auth_user_id: UUID
    email: str
    full_name: str | None


@dataclass(frozen=True)
class InvitedAuthUser:
    auth_user_id: UUID
    email: str


@lru_cache
def get_admin_auth_client() -> SyncGoTrueAdminAPI:
    """Return the minimal server-side Supabase Auth admin client.

    Keep this independent from the full ``supabase.Client`` because workspace
    invitations only need the GoTrue admin API. This also avoids coupling auth
    invitations to Storage/PostgREST client option compatibility.
    """
    key = settings.supabase_secret_key
    return SyncGoTrueAdminAPI(
        url=f"{settings.supabase_url.rstrip('/')}/auth/v1",
        headers={
            "apiKey": key,
            "Authorization": f"Bearer {key}",
        },
    )


def _to_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value

    if hasattr(value, "model_dump"):
        data = value.model_dump(mode="python")
        if isinstance(data, dict):
            return data

    if hasattr(value, "dict"):
        data = value.dict()
        if isinstance(data, dict):
            return data

    raise SupabaseAuthError("Supabase returned an unsupported auth payload.")


def _parse_uuid(value: Any, *, field_name: str) -> UUID:
    if isinstance(value, UUID):
        return value

    if isinstance(value, str):
        try:
            return UUID(value)
        except ValueError as exc:
            raise SupabaseAuthError(f"Supabase returned an invalid {field_name}.") from exc

    raise SupabaseAuthError(f"Supabase did not return a valid {field_name}.")


def verify_access_token(token: str) -> VerifiedAuthIdentity:
    if not isinstance(token, str) or not token.strip():
        raise SupabaseAuthError("Access token is missing.")

    access_token = token.strip()

    try:
        response = httpx.get(
            f"{settings.supabase_url}/auth/v1/user",
            headers={
                "apikey": settings.supabase_publishable_key,
                "Authorization": f"Bearer {access_token}",
            },
            timeout=10.0,
        )
    except httpx.HTTPError as exc:
        raise SupabaseAuthError("Could not reach Supabase Auth server.") from exc

    if response.status_code != 200:
        try:
            error_payload = response.json()
        except ValueError:
            error_payload = response.text

        raise SupabaseAuthError(
            f"Supabase rejected the access token "
            f"(status={response.status_code}, response={error_payload})."
        )

    try:
        user_data = response.json()
    except ValueError as exc:
        raise SupabaseAuthError("Supabase returned a non-JSON user response.") from exc

    if not isinstance(user_data, dict):
        raise SupabaseAuthError("Supabase returned an invalid user response.")

    auth_user_id = _parse_uuid(
        user_data.get("id"),
        field_name="authenticated user id",
    )

    raw_email = user_data.get("email")
    if not isinstance(raw_email, str) or not raw_email.strip():
        raise SupabaseAuthError("Authenticated clinic users must have an email identity.")

    user_metadata = user_data.get("user_metadata")
    full_name: str | None = None

    if isinstance(user_metadata, dict):
        candidate = user_metadata.get("full_name") or user_metadata.get("name")
        if isinstance(candidate, str) and candidate.strip():
            full_name = candidate.strip()[:200]

    return VerifiedAuthIdentity(
        auth_user_id=auth_user_id,
        email=raw_email.strip().lower(),
        full_name=full_name,
    )


def invite_user_by_email(email: str) -> InvitedAuthUser:
    try:
        response = get_admin_auth_client().invite_user_by_email(email)
    except Exception as exc:
        raise SupabaseAuthError("Supabase could not send the invitation.") from exc

    raw_user = getattr(response, "user", None) or response
    user_data = _to_mapping(raw_user)

    auth_user_id = _parse_uuid(
        user_data.get("id"),
        field_name="invited user id",
    )

    response_email = user_data.get("email") or email

    return InvitedAuthUser(
        auth_user_id=auth_user_id,
        email=str(response_email).strip().lower(),
    )
