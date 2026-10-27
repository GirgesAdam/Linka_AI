from __future__ import annotations

import asyncio
import json
from collections import deque
from typing import Any

from app.api.dependencies.security import get_workspace_admin
from app.core.historical_import_limits import MAX_HISTORICAL_IMPORT_REQUEST_BYTES
from app.middleware.historical_import_request_size import (
    HistoricalImportRequestSizeLimitMiddleware,
)

TARGET_PATH = "/api/v1/clinic/history/preview"


def _scope(*, path: str = TARGET_PATH, content_length: int | None = None) -> dict[str, Any]:
    headers: list[tuple[bytes, bytes]] = [(b"content-type", b"application/json")]
    if content_length is not None:
        headers.append((b"content-length", str(content_length).encode("ascii")))
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "headers": headers,
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
    }


def _messages(chunks: list[bytes]) -> deque[dict[str, Any]]:
    if not chunks:
        chunks = [b""]
    return deque(
        {
            "type": "http.request",
            "body": chunk,
            "more_body": index < len(chunks) - 1,
        }
        for index, chunk in enumerate(chunks)
    )


def _exercise(
    *,
    chunks: list[bytes],
    limit: int,
    path: str = TARGET_PATH,
    content_length: int | None = None,
) -> dict[str, Any]:
    incoming = _messages(chunks)
    sent: list[dict[str, Any]] = []
    receive_calls = 0
    downstream_calls = 0
    forwarded_body = b""

    async def receive() -> dict[str, Any]:
        nonlocal receive_calls
        receive_calls += 1
        if incoming:
            return incoming.popleft()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    async def downstream(scope: dict[str, Any], receive, send) -> None:
        nonlocal downstream_calls, forwarded_body
        downstream_calls += 1
        parts: list[bytes] = []
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                break
            if message["type"] != "http.request":
                continue
            parts.append(message.get("body", b""))
            if not message.get("more_body", False):
                break
        forwarded_body = b"".join(parts)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    middleware = HistoricalImportRequestSizeLimitMiddleware(
        downstream,
        path=TARGET_PATH,
        max_body_bytes=limit,
    )
    asyncio.run(middleware(_scope(path=path, content_length=content_length), receive, send))
    status = next(
        message["status"] for message in sent if message["type"] == "http.response.start"
    )
    response_body = b"".join(
        message.get("body", b"")
        for message in sent
        if message["type"] == "http.response.body"
    )
    return {
        "status": status,
        "body": response_body,
        "receive_calls": receive_calls,
        "downstream_calls": downstream_calls,
        "forwarded_body": forwarded_body,
    }


def test_sec_r1_content_length_above_cap_rejects_without_consuming_body() -> None:
    result = _exercise(chunks=[b"small"], limit=5, content_length=6)

    assert result["status"] == 413
    assert result["receive_calls"] == 0
    assert result["downstream_calls"] == 0
    assert json.loads(result["body"])["detail"] == "Historical import request is too large."


def test_sec_r2_missing_content_length_counts_chunks_and_stops_at_crossing() -> None:
    result = _exercise(chunks=[b"abc", b"def", b"remainder"], limit=5)

    assert result["status"] == 413
    assert result["receive_calls"] == 2
    assert result["downstream_calls"] == 0


def test_sec_r3_false_small_content_length_cannot_bypass_actual_byte_count() -> None:
    result = _exercise(chunks=[b"abc", b"def", b"remainder"], limit=5, content_length=3)

    assert result["status"] == 413
    assert result["receive_calls"] == 2
    assert result["downstream_calls"] == 0


def test_sec_r4_within_cap_body_is_forwarded_unchanged() -> None:
    result = _exercise(chunks=[b"abc", b"de"], limit=5, content_length=5)

    assert result["status"] == 200
    assert result["downstream_calls"] == 1
    assert result["forwarded_body"] == b"abcde"


def test_sec_r4_trailing_slash_uses_same_cap() -> None:
    result = _exercise(chunks=[b"abcdef"], limit=5, path=f"{TARGET_PATH}/")

    assert result["status"] == 413
    assert result["downstream_calls"] == 0


def test_sec_r5_unrelated_endpoint_is_not_subject_to_historical_cap() -> None:
    result = _exercise(chunks=[b"abcdef"], limit=5, path="/api/v1/health/live")

    assert result["status"] == 200
    assert result["downstream_calls"] == 1
    assert result["forwarded_body"] == b"abcdef"


def test_sec_r6_registered_guard_runs_before_workspace_admin_dependency() -> None:
    from fastapi.testclient import TestClient

    from app.main import app

    dependency_calls = 0

    def unexpected_admin_dependency() -> None:
        nonlocal dependency_calls
        dependency_calls += 1
        raise AssertionError("admin dependency must not run for oversized ingress")

    app.dependency_overrides[get_workspace_admin] = unexpected_admin_dependency
    try:
        with TestClient(app) as client:
            response = client.post(
                TARGET_PATH,
                content=b"{}",
                headers={
                    "content-type": "application/json",
                    "content-length": str(MAX_HISTORICAL_IMPORT_REQUEST_BYTES + 1),
                },
            )
    finally:
        app.dependency_overrides.pop(get_workspace_admin, None)

    assert response.status_code == 413
    assert dependency_calls == 0
