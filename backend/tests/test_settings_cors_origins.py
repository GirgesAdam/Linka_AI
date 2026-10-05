from app.core.config import Settings


def test_cors_origins_accepts_single_plain_origin(monkeypatch) -> None:
    monkeypatch.setenv("CORS_ORIGINS", "https://tia-demo-seven.vercel.app")

    settings = Settings()

    assert settings.cors_origins == ["https://tia-demo-seven.vercel.app"]


def test_cors_origins_accepts_comma_separated_origins(monkeypatch) -> None:
    monkeypatch.setenv(
        "CORS_ORIGINS",
        "https://tia-demo-seven.vercel.app, http://localhost:3000",
    )

    settings = Settings()

    assert settings.cors_origins == [
        "https://tia-demo-seven.vercel.app",
        "http://localhost:3000",
    ]


def test_cors_origins_keeps_json_array_compatibility(monkeypatch) -> None:
    monkeypatch.setenv(
        "CORS_ORIGINS",
        '["https://tia-demo-seven.vercel.app", "http://localhost:3000"]',
    )

    settings = Settings()

    assert settings.cors_origins == [
        "https://tia-demo-seven.vercel.app",
        "http://localhost:3000",
    ]


def test_cors_origins_supports_domain_migration_window(monkeypatch) -> None:
    monkeypatch.setenv(
        "CORS_ORIGINS",
        "https://app.linkaai.online,https://app.tiaai.online",
    )

    settings = Settings()

    assert settings.cors_origins == [
        "https://app.linkaai.online",
        "https://app.tiaai.online",
    ]
