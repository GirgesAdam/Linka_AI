from __future__ import annotations

import json

MIB = 1024 * 1024

MAX_DOCUMENTS = 10
MAX_DOCUMENT_NAME_CHARS = 255
MAX_DOCUMENT_BYTES = 25 * MIB
MAX_UPLOAD_BYTES = 60 * MIB
MAX_IMPORT_ROWS = 250_000


def base64_encoded_length(decoded_bytes: int) -> int:
    """Return the standard padded base64 length for decoded_bytes."""
    if decoded_bytes < 0:
        raise ValueError("decoded_bytes must be non-negative")
    return 4 * ((decoded_bytes + 2) // 3)


def max_base64_chars_for_independent_documents(total_decoded_bytes: int, max_documents: int) -> int:
    """Exact maximum encoded length when up to max_documents are encoded independently."""
    if total_decoded_bytes < 0:
        raise ValueError("total_decoded_bytes must be non-negative")
    if max_documents < 1:
        raise ValueError("max_documents must be positive")

    best = 0
    for remainder_one_count in range(max_documents + 1):
        for remainder_two_count in range(max_documents - remainder_one_count + 1):
            remainder_bytes = remainder_one_count + (2 * remainder_two_count)
            if remainder_bytes > total_decoded_bytes:
                continue
            quotient_bytes = total_decoded_bytes - remainder_bytes
            if quotient_bytes % 3:
                continue
            encoded_chars = 4 * (
                (quotient_bytes // 3) + remainder_one_count + remainder_two_count
            )
            best = max(best, encoded_chars)
    return best


MAX_DOCUMENT_BASE64_CHARS = base64_encoded_length(MAX_DOCUMENT_BYTES)
MAX_UPLOAD_BASE64_CHARS = max_base64_chars_for_independent_documents(
    MAX_UPLOAD_BYTES,
    MAX_DOCUMENTS,
)


def _max_historical_import_json_envelope_bytes() -> int:
    # content_base64 is intentionally empty here: its quotes/key are envelope bytes and the
    # exact base64 character budget is added separately below. Standard base64 is one-byte
    # ASCII and needs no escaping in the compact JSON contract used by Linka. For bounded
    # metadata, ensure_ascii=True is conservative: a non-BMP filename code point expands to
    # a 12-byte surrogate-pair escape, which is larger than its UTF-8 representation.
    worst_name = "\U0010ffff" * MAX_DOCUMENT_NAME_CHARS
    worst_document = {
        "name": worst_name,
        "format": "xlsx",
        "content_base64": "",
    }
    payload = {
        "documents": [dict(worst_document) for _ in range(MAX_DOCUMENTS)],
        "mode": "replace_previous_imports",
    }
    encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    return len(encoded)


MAX_HISTORICAL_IMPORT_JSON_ENVELOPE_BYTES = _max_historical_import_json_envelope_bytes()
MAX_HISTORICAL_IMPORT_REQUEST_BYTES = (
    MAX_UPLOAD_BASE64_CHARS + MAX_HISTORICAL_IMPORT_JSON_ENVELOPE_BYTES
)
