from pathlib import Path


def _root() -> Path:
    return Path(__file__).resolve().parents[2]


def test_public_legal_brand_normalizes_legacy_tia_values_to_linka() -> None:
    root = _root()
    helper = (root / "frontend/src/lib/public-brand.ts").read_text(encoding="utf-8")
    privacy = (root / "frontend/src/app/privacy/page.tsx").read_text(encoding="utf-8")
    deletion = (root / "frontend/src/app/data-deletion/page.tsx").read_text(encoding="utf-8")

    assert 'new Set(["Tia", "Tia AI"])' in helper
    assert 'return "Linka";' in helper
    assert "resolvePublicLegalName(process.env.NEXT_PUBLIC_TIA_LEGAL_NAME)" in privacy
    assert "resolvePublicLegalName(process.env.NEXT_PUBLIC_TIA_LEGAL_NAME)" in deletion
    assert "تيا بتستخدم" not in privacy
    assert "Linka بتستخدم" in privacy
