from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CALLBACK = (ROOT / ".." / "frontend" / "src" / "app" / "auth" / "callback" / "route.ts").resolve().read_text(encoding="utf-8")
TEMPLATE = (ROOT / ".." / "frontend" / "supabase-email-templates" / "confirm-signup.html").resolve().read_text(encoding="utf-8")


def test_auth_callback_supports_token_hash_and_preserves_code_path():
    assert 'params.get("token_hash")' in CALLBACK
    assert 'params.get("type")' in CALLBACK
    assert "supabase.auth.verifyOtp({ token_hash: tokenHash, type })" in CALLBACK
    assert 'params.get("code")' in CALLBACK
    assert "supabase.auth.exchangeCodeForSession(code)" in CALLBACK


def test_auth_callback_safe_next_blocks_protocol_relative_redirects():
    assert 'value.startsWith("/")' in CALLBACK
    assert '!value.startsWith("//")' in CALLBACK


def test_signup_confirmation_template_is_token_hash_link_first():
    assert "{{ .SiteURL }}/auth/callback?next=/onboarding" in TEMPLATE
    assert "{{ .TokenHash }}" in TEMPLATE
    assert "type=email" in TEMPLATE
    assert "{{ .Token }}" not in TEMPLATE
    assert "{{ .ConfirmationURL }}" not in TEMPLATE
