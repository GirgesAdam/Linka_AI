from __future__ import annotations

import json
from typing import Any

AVAILABILITY_PRESENTATION_SCOPE_KEY = "availability_presentation_scope_key"


def availability_scope_key(parameters: dict[str, object]) -> str:
    """Stable identity for the canonical verified availability read scope."""

    return json.dumps(
        _canonical_scope_value(parameters),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def availability_scope_matches(
    previous_read_context: dict[str, Any] | None,
    *,
    current_scope_key: str | None,
) -> bool:
    if not current_scope_key or not isinstance(previous_read_context, dict):
        return False
    previous = previous_read_context.get(AVAILABILITY_PRESENTATION_SCOPE_KEY)
    return isinstance(previous, str) and previous == current_scope_key


def availability_scope_key_from_reads(reads: list[object]) -> str | None:
    for read in reads:
        if getattr(read, "kind", None) != "availability":
            continue
        parameters = getattr(read, "parameters", None)
        if isinstance(parameters, dict):
            return availability_scope_key(parameters)
    return None


def _canonical_scope_value(value: object) -> object:
    if isinstance(value, dict):
        return {
            str(key): _canonical_scope_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_scope_value(item) for item in value]
    if isinstance(value, set | frozenset):
        normalized = [_canonical_scope_value(item) for item in value]
        return sorted(normalized, key=lambda item: json.dumps(item, sort_keys=True, default=str))
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)
