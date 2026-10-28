from __future__ import annotations

from contextvars import ContextVar, Token
from uuid import uuid4

MAX_REQUEST_ID_LENGTH = 128
REQUEST_ID_PLACEHOLDER = "-"

_current_request_id: ContextVar[str | None] = ContextVar(
    "linka_request_id",
    default=None,
)


def _is_safe_client_request_id(value: str) -> bool:
    return (
        0 < len(value) <= MAX_REQUEST_ID_LENGTH
        and all(0x21 <= ord(char) <= 0x7E for char in value)
    )


def resolve_request_id(supplied: str | None) -> str:
    if supplied is not None and _is_safe_client_request_id(supplied):
        return supplied
    return str(uuid4())


def set_request_id(request_id: str) -> Token:
    return _current_request_id.set(request_id)


def reset_request_id(token: Token) -> None:
    _current_request_id.reset(token)


def get_request_id() -> str | None:
    return _current_request_id.get()
