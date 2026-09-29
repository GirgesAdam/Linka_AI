from __future__ import annotations

import os
import re
from collections.abc import Callable


class SecretResolutionError(RuntimeError):
    """Raised when a configured secret reference cannot be resolved safely."""


SecretResolver = Callable[[str], str]

_ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]{1,127}$", re.IGNORECASE)
_RESOLVERS: dict[str, SecretResolver] = {}


def register_secret_resolver(scheme: str, resolver: SecretResolver) -> None:
    normalized = str(scheme).strip().lower()
    if not normalized or ":" in normalized or "/" in normalized:
        raise ValueError("Secret resolver scheme must be a simple non-empty name.")
    _RESOLVERS[normalized] = resolver


def _resolve_env(reference: str) -> str:
    name = reference.strip().lstrip("/")
    if not _ENV_NAME.fullmatch(name):
        raise SecretResolutionError("Environment secret reference is invalid.")
    value = os.getenv(name)
    if value is None or not value.strip():
        raise SecretResolutionError(
            f"Environment secret reference {name!r} is not configured on the Linka server."
        )
    return value.strip()


def resolve_secret_ref(secret_ref: str | None) -> str:
    """Resolve server-side secret material without persisting it in Linka's database.

    References use ``scheme:value`` or ``scheme://value``. Only the reference is
    persisted. Secret values stay in the resolver backend and are never included
    in errors returned by this module.
    """

    reference = str(secret_ref or "").strip()
    if not reference:
        raise SecretResolutionError("Connector requires a server-side secret_ref.")
    if ":" not in reference:
        raise SecretResolutionError("Secret reference must include a resolver scheme.")
    scheme, value = reference.split(":", 1)
    resolver = _RESOLVERS.get(scheme.strip().lower())
    if resolver is None:
        raise SecretResolutionError(
            f"Secret resolver scheme {scheme.strip().lower()!r} is not installed on this Linka server."
        )
    try:
        resolved = resolver(value)
    except SecretResolutionError:
        raise
    except Exception as exc:  # pragma: no cover - defensive resolver boundary
        raise SecretResolutionError("Secret resolver failed.") from exc
    if not isinstance(resolved, str) or not resolved.strip():
        raise SecretResolutionError("Secret resolver returned an empty value.")
    return resolved.strip()


register_secret_resolver("env", _resolve_env)
