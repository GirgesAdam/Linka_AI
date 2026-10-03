from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from app.services import supabase_auth


def test_admin_invite_client_uses_minimal_gotrue_admin_api(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeAdminClient:
        def __init__(self, *, url: str, headers: dict[str, str]) -> None:
            captured["url"] = url
            captured["headers"] = headers

    supabase_auth.get_admin_auth_client.cache_clear()
    monkeypatch.setattr(supabase_auth, "SyncGoTrueAdminAPI", FakeAdminClient)
    try:
        client = supabase_auth.get_admin_auth_client()
    finally:
        supabase_auth.get_admin_auth_client.cache_clear()

    assert isinstance(client, FakeAdminClient)
    assert captured["url"] == f"{supabase_auth.settings.supabase_url.rstrip('/')}/auth/v1"
    headers = captured["headers"]
    assert isinstance(headers, dict)
    assert headers["apiKey"] == supabase_auth.settings.supabase_secret_key
    assert headers["Authorization"] == f"Bearer {supabase_auth.settings.supabase_secret_key}"


def test_invite_user_by_email_calls_admin_api_directly(monkeypatch) -> None:
    invited_id = uuid4()
    calls: list[str] = []

    class FakeAdminClient:
        def invite_user_by_email(self, email: str):
            calls.append(email)
            return SimpleNamespace(
                user=SimpleNamespace(
                    model_dump=lambda mode="python": {
                        "id": str(invited_id),
                        "email": "new.member@example.com",
                    }
                )
            )

    monkeypatch.setattr(supabase_auth, "get_admin_auth_client", lambda: FakeAdminClient())

    result = supabase_auth.invite_user_by_email("NEW.MEMBER@example.com")

    assert calls == ["NEW.MEMBER@example.com"]
    assert result.auth_user_id == invited_id
    assert result.email == "new.member@example.com"
