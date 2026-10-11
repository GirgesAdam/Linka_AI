from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from uuid import UUID

from fastapi import FastAPI, Header, HTTPException, Request

app = FastAPI()
USER_ID = UUID("d6a87151-d0c5-4be0-93d8-8a3f0194b239")
EMAIL = "audit.reception@linka.local"
PASSWORD = "AuditPass123!"
JWT_SECRET = os.environ.get("AUDIT_JWT_SECRET", "audit-only-secret-not-for-production")


def b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def make_token() -> str:
    now = int(time.time())
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "sub": str(USER_ID), "email": EMAIL, "aud": "authenticated", "role": "authenticated",
        "iat": now, "exp": now + 3600, "user_metadata": {"full_name": "Audit Reception"},
    }
    unsigned = f"{b64(json.dumps(header, separators=(',', ':')).encode())}.{b64(json.dumps(payload, separators=(',', ':')).encode())}"
    signature = hmac.new(JWT_SECRET.encode(), unsigned.encode(), hashlib.sha256).digest()
    return f"{unsigned}.{b64(signature)}"


def user_payload() -> dict:
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return {
        "id": str(USER_ID), "aud": "authenticated", "role": "authenticated", "email": EMAIL,
        "email_confirmed_at": now, "phone": "", "confirmed_at": now,
        "last_sign_in_at": now, "app_metadata": {"provider": "email", "providers": ["email"]},
        "user_metadata": {"full_name": "Audit Reception"}, "identities": [],
        "created_at": now, "updated_at": now, "is_anonymous": False,
    }


@app.post("/auth/v1/token")
async def token(request: Request, grant_type: str = "password"):
    body = await request.json()
    if grant_type == "password":
        if body.get("email") != EMAIL or body.get("password") != PASSWORD:
            raise HTTPException(400, "Invalid login credentials")
    elif grant_type == "refresh_token":
        if not body.get("refresh_token"):
            raise HTTPException(400, "Missing refresh token")
    else:
        raise HTTPException(400, "Unsupported grant_type")
    access = make_token()
    return {
        "access_token": access, "token_type": "bearer", "expires_in": 3600,
        "expires_at": int(time.time()) + 3600, "refresh_token": "audit-refresh-token",
        "user": user_payload(),
    }


@app.get("/auth/v1/user")
async def user(authorization: str | None = Header(default=None)):
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Missing token")
    return user_payload()


@app.get("/auth/v1/.well-known/jwks.json")
@app.get("/.well-known/jwks.json")
async def jwks():
    return {"keys": []}


@app.post("/auth/v1/logout")
async def logout():
    return {}


@app.get("/health")
async def health():
    return {"ok": True}
