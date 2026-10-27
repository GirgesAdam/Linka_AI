from __future__ import annotations

import base64
import json
import time
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from supabase_auth import SyncGoTrueClient

from app.api.dependencies.security import (
    WorkspaceAccess,
    get_current_user,
    get_verified_identity,
    get_workspace_access,
    require_workspace_roles,
)
from app.services import supabase_auth


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _jwk(private_key: ec.EllipticCurvePrivateKey, kid: str) -> dict[str, str]:
    numbers = private_key.public_key().public_numbers()
    size = 32
    return {
        "kty": "EC",
        "crv": "P-256",
        "x": _b64url(numbers.x.to_bytes(size, "big")),
        "y": _b64url(numbers.y.to_bytes(size, "big")),
        "use": "sig",
        "alg": "ES256",
        "kid": kid,
    }


def _token(
    private_key: ec.EllipticCurvePrivateKey,
    kid: str,
    *,
    issuer: str = "https://example.supabase.co/auth/v1",
    audience: str | list[str] = "authenticated",
    role: str = "authenticated",
    subject: str | None = None,
    email: str | None = "USER@EXAMPLE.COM",
    exp: int | None = None,
    metadata: dict[str, object] | None = None,
) -> str:
    header = {"alg": "ES256", "kid": kid, "typ": "JWT"}
    payload: dict[str, object] = {
        "iss": issuer,
        "aud": audience,
        "role": role,
        "exp": exp if exp is not None else int(time.time()) + 3600,
        "iat": int(time.time()),
    }
    if subject is not None:
        payload["sub"] = subject
    if email is not None:
        payload["email"] = email
    if metadata is not None:
        payload["user_metadata"] = metadata

    raw_header = _b64url(json.dumps(header, separators=(",", ":")).encode())
    raw_payload = _b64url(json.dumps(payload, separators=(",", ":")).encode())
    signing_input = f"{raw_header}.{raw_payload}".encode()
    der_signature = private_key.sign(signing_input, ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der_signature)
    raw_signature = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    return f"{raw_header}.{raw_payload}.{_b64url(raw_signature)}"


class _JwksFixture:
    def __init__(self, keys: list[dict[str, str]]) -> None:
        self.keys = keys
        self.jwks_calls = 0
        self.user_calls = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/.well-known/jwks.json"):
            self.jwks_calls += 1
            return httpx.Response(200, json={"keys": self.keys})
        if request.url.path.endswith("/user"):
            self.user_calls += 1
            return httpx.Response(500, json={"error": "unexpected user lookup"})
        return httpx.Response(404)


def _client(fixture: _JwksFixture) -> tuple[SyncGoTrueClient, httpx.Client]:
    http_client = httpx.Client(transport=httpx.MockTransport(fixture.handler))
    client = SyncGoTrueClient(
        url="https://example.supabase.co/auth/v1",
        headers={"apiKey": "sb_publishable_test_dummy"},
        auto_refresh_token=False,
        persist_session=False,
        http_client=http_client,
    )
    return client, http_client


def _install_verifier(monkeypatch, client: SyncGoTrueClient) -> None:
    monkeypatch.setattr(supabase_auth, "get_access_token_verifier", lambda: client)


def test_cold_then_warm_jwks_verification_eliminates_user_lookup(monkeypatch) -> None:
    private_key = ec.generate_private_key(ec.SECP256R1())
    kid = "kid-a"
    fixture = _JwksFixture([_jwk(private_key, kid)])
    client, http_client = _client(fixture)
    _install_verifier(monkeypatch, client)
    user_id = uuid4()
    token = _token(
        private_key,
        kid,
        subject=str(user_id),
        metadata={"full_name": " Test User "},
    )
    try:
        first = supabase_auth.verify_access_token(token)
        second = supabase_auth.verify_access_token(token)
    finally:
        http_client.close()

    assert first.auth_user_id == user_id
    assert first.email == "user@example.com"
    assert first.full_name == "Test User"
    assert second == first
    assert fixture.jwks_calls == 1
    assert fixture.user_calls == 0


def test_unknown_kid_refreshes_jwks_once_and_accepts_rotated_key(monkeypatch) -> None:
    key_a = ec.generate_private_key(ec.SECP256R1())
    key_b = ec.generate_private_key(ec.SECP256R1())
    fixture = _JwksFixture([_jwk(key_a, "kid-a")])
    client, http_client = _client(fixture)
    _install_verifier(monkeypatch, client)
    try:
        supabase_auth.verify_access_token(
            _token(key_a, "kid-a", subject=str(uuid4()))
        )
        assert fixture.jwks_calls == 1
        fixture.keys = [_jwk(key_a, "kid-a"), _jwk(key_b, "kid-b")]
        supabase_auth.verify_access_token(
            _token(key_b, "kid-b", subject=str(uuid4()))
        )
    finally:
        http_client.close()

    assert fixture.jwks_calls == 2
    assert fixture.user_calls == 0


def test_warm_verifier_100_requests_make_no_auth_user_or_jwks_calls(monkeypatch) -> None:
    private_key = ec.generate_private_key(ec.SECP256R1())
    kid = "kid-perf"
    fixture = _JwksFixture([_jwk(private_key, kid)])
    client, http_client = _client(fixture)
    _install_verifier(monkeypatch, client)
    token = _token(private_key, kid, subject=str(uuid4()))
    try:
        supabase_auth.verify_access_token(token)
        assert fixture.jwks_calls == 1
        fixture.jwks_calls = 0
        for _ in range(100):
            supabase_auth.verify_access_token(token)
    finally:
        http_client.close()

    assert fixture.user_calls == 0
    assert fixture.jwks_calls == 0


@pytest.mark.parametrize(
    ("case", "mutate"),
    [
        ("wrong_issuer", {"issuer": "https://other.supabase.co/auth/v1"}),
        ("wrong_audience", {"audience": "anon"}),
        ("wrong_role", {"role": "service_role"}),
        ("missing_sub", {"subject": None}),
        ("invalid_sub", {"subject": "not-a-uuid"}),
        ("missing_email", {"email": None}),
    ],
)
def test_verified_claim_contract_rejects_invalid_user_tokens(
    monkeypatch, case: str, mutate: dict[str, object]
) -> None:
    del case
    private_key = ec.generate_private_key(ec.SECP256R1())
    kid = "kid-claims"
    fixture = _JwksFixture([_jwk(private_key, kid)])
    client, http_client = _client(fixture)
    _install_verifier(monkeypatch, client)
    kwargs: dict[str, object] = {"subject": str(uuid4())}
    kwargs.update(mutate)
    token = _token(private_key, kid, **kwargs)
    try:
        with pytest.raises(supabase_auth.SupabaseAuthError):
            supabase_auth.verify_access_token(token)
    finally:
        http_client.close()
    assert fixture.user_calls == 0


def test_malformed_bad_signature_expired_and_cross_project_tokens_are_rejected(monkeypatch) -> None:
    trusted_key = ec.generate_private_key(ec.SECP256R1())
    other_key = ec.generate_private_key(ec.SECP256R1())
    fixture = _JwksFixture([_jwk(trusted_key, "trusted-kid")])
    client, http_client = _client(fixture)
    _install_verifier(monkeypatch, client)
    valid = _token(trusted_key, "trusted-kid", subject=str(uuid4()))
    header_part, payload_part, signature_part = valid.split(".")
    signature = bytearray(base64.urlsafe_b64decode(signature_part + "=="))
    signature[0] ^= 0x01
    bad_signature = f"{header_part}.{payload_part}.{_b64url(bytes(signature))}"
    expired = _token(
        trusted_key,
        "trusted-kid",
        subject=str(uuid4()),
        exp=int(time.time()) - 60,
    )
    cross_project = _token(other_key, "other-kid", subject=str(uuid4()))
    try:
        for token in ("not-a-jwt", bad_signature, expired, cross_project):
            with pytest.raises(supabase_auth.SupabaseAuthError):
                supabase_auth.verify_access_token(token)
    finally:
        http_client.close()
    assert fixture.user_calls == 0


def test_legacy_symmetric_or_missing_kid_tokens_do_not_fallback_to_user_lookup(monkeypatch) -> None:
    fixture = _JwksFixture([])
    client, http_client = _client(fixture)
    _install_verifier(monkeypatch, client)
    payload = _b64url(json.dumps({"exp": int(time.time()) + 3600}).encode())
    hs256_header = _b64url(json.dumps({"alg": "HS256", "kid": "legacy"}).encode())
    no_kid_header = _b64url(json.dumps({"alg": "ES256"}).encode())
    try:
        for token in (
            f"{hs256_header}.{payload}.fake",
            f"{no_kid_header}.{payload}.fake",
        ):
            with pytest.raises(supabase_auth.SupabaseAuthError):
                supabase_auth.verify_access_token(token)
    finally:
        http_client.close()
    assert fixture.user_calls == 0
    assert fixture.jwks_calls == 0


def test_publishable_secret_and_service_role_style_credentials_cannot_authenticate(monkeypatch) -> None:
    private_key = ec.generate_private_key(ec.SECP256R1())
    kid = "kid-user"
    fixture = _JwksFixture([_jwk(private_key, kid)])
    client, http_client = _client(fixture)
    _install_verifier(monkeypatch, client)
    service_role_style = _token(
        private_key,
        kid,
        subject=str(uuid4()),
        email="service@example.com",
        role="service_role",
    )
    try:
        for credential in (
            "sb_publishable_test_dummy",
            "sb_secret_test_dummy",
            service_role_style,
        ):
            with pytest.raises(supabase_auth.SupabaseAuthError):
                supabase_auth.verify_access_token(credential)
    finally:
        http_client.close()
    assert fixture.user_calls == 0


def test_process_level_verifier_is_reused(monkeypatch) -> None:
    created: list[dict[str, object]] = []

    class FakeClient:
        def __init__(self, **kwargs) -> None:
            created.append(kwargs)

    supabase_auth.get_access_token_verifier.cache_clear()
    monkeypatch.setattr(supabase_auth, "SyncGoTrueClient", FakeClient)
    try:
        first = supabase_auth.get_access_token_verifier()
        second = supabase_auth.get_access_token_verifier()
    finally:
        supabase_auth.get_access_token_verifier.cache_clear()

    assert first is second
    assert len(created) == 1
    assert created[0]["persist_session"] is False
    assert created[0]["auto_refresh_token"] is False


def test_local_inactive_user_still_returns_403() -> None:
    identity = supabase_auth.VerifiedAuthIdentity(uuid4(), "user@example.com", None)
    user = SimpleNamespace(
        auth_user_id=identity.auth_user_id,
        email=identity.email,
        full_name=None,
        is_active=False,
    )
    db = SimpleNamespace(scalar=lambda _stmt: user)
    with pytest.raises(HTTPException) as exc:
        get_current_user(identity=identity, db=db)
    assert exc.value.status_code == 403


def test_missing_or_inactive_workspace_membership_still_returns_403() -> None:
    db = SimpleNamespace(scalar=lambda _stmt: None)
    with pytest.raises(HTTPException) as exc:
        get_workspace_access(
            x_workspace_id=uuid4(),
            user=SimpleNamespace(id=uuid4()),
            db=db,
        )
    assert exc.value.status_code == 403


def test_inactive_workspace_behavior_stays_404() -> None:
    membership = SimpleNamespace(role="member")
    values = iter([membership, None])
    db = SimpleNamespace(scalar=lambda _stmt: next(values))
    with pytest.raises(HTTPException) as exc:
        get_workspace_access(
            x_workspace_id=uuid4(),
            user=SimpleNamespace(id=uuid4()),
            db=db,
        )
    assert exc.value.status_code == 404


def test_member_role_still_cannot_cross_admin_boundary() -> None:
    require_admin = require_workspace_roles("admin")
    access = WorkspaceAccess(
        user=SimpleNamespace(id=uuid4()),
        workspace=SimpleNamespace(id=uuid4()),
        membership=SimpleNamespace(role="member"),
    )
    with pytest.raises(HTTPException) as exc:
        require_admin(access=access)
    assert exc.value.status_code == 403


def test_invalid_token_keeps_existing_external_401_contract(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.api.dependencies.security.verify_access_token",
        lambda _token: (_ for _ in ()).throw(supabase_auth.SupabaseAuthError("internal detail")),
    )
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="bad-token")
    with pytest.raises(HTTPException) as exc:
        get_verified_identity(credentials=credentials)
    assert exc.value.status_code == 401
    assert exc.value.detail == "Invalid or expired access token."


def test_jwks_unavailable_without_cached_key_fails_closed(monkeypatch) -> None:
    private_key = ec.generate_private_key(ec.SECP256R1())
    kid = "kid-outage"

    def unavailable(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("JWKS unavailable")

    http_client = httpx.Client(transport=httpx.MockTransport(unavailable))
    client = SyncGoTrueClient(
        url="https://example.supabase.co/auth/v1",
        headers={"apiKey": "sb_publishable_test_dummy"},
        auto_refresh_token=False,
        persist_session=False,
        http_client=http_client,
    )
    _install_verifier(monkeypatch, client)
    token = _token(private_key, kid, subject=str(uuid4()))
    try:
        with pytest.raises(supabase_auth.SupabaseAuthError):
            supabase_auth.verify_access_token(token)
    finally:
        http_client.close()
