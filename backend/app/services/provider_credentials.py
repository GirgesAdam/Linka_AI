from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken

from app.core.meta_whatsapp_config import meta_whatsapp_settings


class ProviderCredentialError(RuntimeError):
    pass


def provider_credential_encryption_ready() -> bool:
    value = meta_whatsapp_settings.channel_credential_encryption_key
    return bool(value and value.strip())


def _fernet() -> Fernet:
    raw = meta_whatsapp_settings.channel_credential_encryption_key
    if not raw or not raw.strip():
        raise ProviderCredentialError(
            "Provider credential encryption is not configured on the Linka platform."
        )
    try:
        return Fernet(raw.strip().encode("utf-8"))
    except (ValueError, TypeError) as exc:
        raise ProviderCredentialError(
            "Provider credential encryption key is invalid."
        ) from exc


def encrypt_provider_secret(value: str) -> str:
    clean = value.strip()
    if not clean:
        raise ProviderCredentialError("Provider secret cannot be empty.")
    return _fernet().encrypt(clean.encode("utf-8")).decode("utf-8")


def decrypt_provider_secret(ciphertext: str) -> str:
    try:
        return _fernet().decrypt(ciphertext.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        raise ProviderCredentialError(
            "Stored provider credential cannot be decrypted with the configured key."
        ) from exc


def encrypt_provider_access_token(token: str) -> str:
    return encrypt_provider_secret(token)


def decrypt_provider_access_token(ciphertext: str) -> str:
    return decrypt_provider_secret(ciphertext)
