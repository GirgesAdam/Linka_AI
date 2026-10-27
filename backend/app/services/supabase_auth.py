import base64
import json
from dataclasses import dataclass
from functools import lru_cache
from typing import Any
from uuid import UUID

import httpx
from supabase_auth import SyncGoTrueAdminAPI, SyncGoTrueClient
from supabase_auth.errors import AuthError

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
def get_access_token_verifier() -> SyncGoTrueClient:
    """Return the process-level Supabase JWT verifier.

    ``get_claims`` verifies asymmetric JWT signatures against Supabase JWKS and
    keeps its public-key cache on this client instance. Authorization decisions
    remain in the database-backed FastAPI dependencies and are never cached here.
    """
    return SyncGoTrueClient(
        url=f"{settings.supabase_url.rstrip('/')}/auth/v1",
        headers={"apiKey": settings.supabase_publishable_key},
        auto_refresh_token=False,
        persist_session=False,
    )


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


def _parse_unverified_header(token: str) -> dict[str, Any]:
    """Parse JWT header only to choose the verification path; never authenticate from it."""
    try:
        raw_header = token.split(".", 1)[0]
        padded = raw_header + ("=" * (-len(raw_header) % 4))
        decoded = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise SupabaseAuthError("Access token is malformed.") from exc
    if not isinstance(decoded, dict):
        raise SupabaseAuthError("Access token header is invalid.")
    return decoded


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
    header = _parse_unverified_header(access_token)
    if header.get("alg") not in {"ES256", "RS256"} or not isinstance(header.get("kid"), str):
        raise SupabaseAuthError("Access token does not use a supported asymmetric signing key.")

    try:
        response = get_access_token_verifier().get_claims(access_token)
    except (AuthError, httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
        raise SupabaseAuthError("Supabase rejected the access token.") from exc

    if not response:
        raise SupabaseAuthError("Supabase did not return verified JWT claims.")

    claims = response.get("claims")
    if not isinstance(claims, dict):
        raise SupabaseAuthError("Supabase returned invalid verified JWT claims.")

    if claims.get("iss") != settings.supabase_auth_issuer:
        raise SupabaseAuthError("Access token issuer is invalid.")

    audience = claims.get("aud")
    if isinstance(audience, str):
        audience_ok = audience == "authenticated"
    elif isinstance(audience, list):
        audience_ok = "authenticated" in audience
    else:
        audience_ok = False
    if not audience_ok or claims.get("role") != "authenticated":
        raise SupabaseAuthError("Access token is not an authenticated user token.")

    auth_user_id = _parse_uuid(
        claims.get("sub"),
        field_name="authenticated user id",
    )

    raw_email = claims.get("email")
    if not isinstance(raw_email, str) or not raw_email.strip():
        raise SupabaseAuthError("Authenticated clinic users must have an email identity.")

    user_metadata = claims.get("user_metadata")
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
