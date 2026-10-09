from __future__ import annotations

import re

_DOCTOR_PREFIX = re.compile(
    r"^(?:د\.\s*|دكتور(?:ة)?(?:\s+|$)|(?:dr|doctor)\.?(?:\s+|$))",
    re.IGNORECASE,
)


def format_doctor_name(value: object, *, arabic: bool) -> str:
    """Return one customer-facing doctor name with at most one title prefix."""
    text = " ".join(str(value or "").split())
    if not text:
        return ""
    if _DOCTOR_PREFIX.match(text):
        return text
    return f"د. {text}" if arabic else f"Dr. {text}"
