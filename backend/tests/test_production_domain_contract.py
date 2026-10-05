from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OLD_DOMAIN = "tiaai.online"
ALLOWED_RUNTIME_COMPATIBILITY = {
    ROOT / "frontend/src/lib/production-domain.ts",
}


def test_runtime_has_no_uncontrolled_old_domain_references() -> None:
    matches: list[str] = []
    for base in (ROOT / "frontend/src", ROOT / "backend/app"):
        for path in base.rglob("*"):
            if not path.is_file() or path.suffix not in {".py", ".ts", ".tsx", ".js", ".jsx"}:
                continue
            text = path.read_text(encoding="utf-8")
            if OLD_DOMAIN in text and path not in ALLOWED_RUNTIME_COMPATIBILITY:
                matches.append(str(path.relative_to(ROOT)))
    assert matches == []


def test_domain_migration_runtime_contract_is_explicit() -> None:
    production_domain = (ROOT / "frontend/src/lib/production-domain.ts").read_text(
        encoding="utf-8"
    )
    signup = (ROOT / "frontend/src/app/signup/actions.ts").read_text(encoding="utf-8")
    recovery = (ROOT / "frontend/src/app/forgot-password/actions.ts").read_text(
        encoding="utf-8"
    )
    proxy = (ROOT / "frontend/src/lib/supabase/proxy.ts").read_text(encoding="utf-8")

    assert 'CANONICAL_APP_URL = "https://app.linkaai.online"' in production_domain
    assert 'CANONICAL_ROOT_URL = "https://linkaai.online"' in production_domain
    assert 'LEGACY_APP_HOST = "app.tiaai.online"' in production_domain
    assert 'authCallbackUrl(origin, "/onboarding")' in signup
    assert 'authCallbackUrl(origin, "/reset-password")' in recovery
    assert 'LINKA_LEGACY_DOMAIN_REDIRECT_ENABLED === "true"' in proxy
    assert 'current.pathname === "/auth/callback"' in production_domain
