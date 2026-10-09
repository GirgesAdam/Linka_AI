from __future__ import annotations

import re

_DIGIT_TRANSLATION = str.maketrans({
    "\u0660": "0", "\u0661": "1", "\u0662": "2", "\u0663": "3", "\u0664": "4",
    "\u0665": "5", "\u0666": "6", "\u0667": "7", "\u0668": "8", "\u0669": "9",
    "\u06f0": "0", "\u06f1": "1", "\u06f2": "2", "\u06f3": "3", "\u06f4": "4",
    "\u06f5": "5", "\u06f6": "6", "\u06f7": "7", "\u06f8": "8", "\u06f9": "9",
})
_EXPLICIT_CLOCK_RE = re.compile(r"(?<![\d:])([0-2]?\d):([0-5]\d)(?![\d:])")


def normalize_decimal_digits(text: str) -> str:
    return text.translate(_DIGIT_TRANSLATION)


def extract_explicit_hhmm_values(text: str) -> list[str]:
    """Return valid colon-formatted clock values without AM/PM reinterpretation."""
    normalized = normalize_decimal_digits(text)
    values: list[str] = []
    for match in _EXPLICIT_CLOCK_RE.finditer(normalized):
        hour = int(match.group(1))
        minute = int(match.group(2))
        if hour > 23:
            continue
        value = f"{hour:02d}:{minute:02d}"
        if value not in values:
            values.append(value)
    return values


def extract_single_explicit_hhmm(text: str) -> str | None:
    values = extract_explicit_hhmm_values(text)
    return values[0] if len(values) == 1 else None
