from __future__ import annotations

import re
import unicodedata

# Doctor titles are presentation, not identity. Linka stores staff names without them
# and adds the appropriate title in the UI/copy layer when needed.
_DOCTOR_TITLE_RE = re.compile(
    r"^(?:(?:(?:أ|ا)\s*\.\s*د\s*\.?|د\.|dr\.|prof\.)\s*|(?:د|دكتور|دكتورة|dr|doctor|prof|professor)\s+)+",
    flags=re.IGNORECASE,
)


def _clean_text(value: str | None) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    return re.sub(r"\s+", " ", text).strip()


def normalize_doctor_display_name(value: str) -> str:
    """Return the canonical stored display name, without a doctor honorific.

    This deliberately does not transliterate or fuzzy-match names. For example,
    Arabic and English spellings stay distinct unless an external stable identity
    already maps them to the same canonical doctor.
    """
    text = _clean_text(value)
    previous = None
    while text and text != previous:
        previous = text
        text = _DOCTOR_TITLE_RE.sub("", text).strip()
    return _clean_text(text)


def split_person_name(value: str) -> tuple[str, str]:
    text = _clean_text(value)
    if not text:
        return "", ""
    parts = text.split(" ")
    if len(parts) == 1:
        return parts[0], ""
    return " ".join(parts[:-1]), parts[-1]


def split_doctor_name(value: str) -> tuple[str, str]:
    return split_person_name(normalize_doctor_display_name(value))


def normalize_doctor_name_parts(first_name: str, last_name: str) -> tuple[str, str]:
    combined = _clean_text(" ".join(part for part in (first_name, last_name) if part))
    normalized = normalize_doctor_display_name(combined)
    first, last = split_person_name(normalized)
    # The database column is non-null but permits an empty string. Keeping a
    # single-token real name is safer than inventing an extra surname.
    return first, last
