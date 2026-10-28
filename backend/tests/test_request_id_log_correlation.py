from __future__ import annotations

import asyncio
import io
import logging
from contextlib import contextmanager
from uuid import UUID

import httpx
from fastapi import FastAPI, HTTPException, status
from fastapi.testclient import TestClient
from pydantic import BaseModel

from app.core.logging import REQUEST_LOG_FORMAT, RequestIdFilter
from app.core.request_context import MAX_REQUEST_ID_LENGTH
from app.main import request_id_middleware
from app.middleware.historical_import_request_size import (
    HistoricalImportRequestSizeLimitMiddleware,
)

TARGET_PATH = "/api/v1/clinic/history/preview"
probe_logger = logging.getLogger("tests.request_id_probe")


class _Payload(BaseModel):
    count: int


@contextmanager
def _captured_application_logs():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter(REQUEST_LOG_FORMAT))
    handler.addFilter(RequestIdFilter())
    root = logging.getLogger()
    original_root_level = root.level
    correlated_loggers = [logging.getLogger("tia.http"), probe_logger]
    original_logger_state = [
        (logger, logger.disabled, logger.level, logger.propagate)
        for logger in correlated_loggers
    ]
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    for logger in correlated_loggers:
        # Alembic migration tests call logging.fileConfig() in-process, which
        # disables existing application loggers. Keep this capture helper
        # hermetic so full-suite order cannot suppress the records under test.
        logger.disabled = False
        logger.setLevel(logging.NOTSET)
        logger.propagate = True
    try:
        yield stream
    finally:
        for logger, disabled, level, propagate in original_logger_state:
            logger.disabled = disabled
            logger.setLevel(level)
            logger.propagate = propagate
        root.setLevel(original_root_level)
        root.removeHandler(handler)
        handler.close()


def _build_probe_app() -> FastAPI:
    probe = FastAPI()
    probe.add_middleware(
        HistoricalImportRequestSizeLimitMiddleware,
        path=TARGET_PATH,
        max_body_bytes=5,
    )
    probe.middleware("http")(request_id_middleware)

    @probe.get("/ok")
    def ok() -> dict[str, bool]:
        return {"ok": True}

    @probe.get("/unauthorized")
    def unauthorized() -> None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required.")

    @probe.get("/forbidden")
    def forbidden() -> None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden.")

    @probe.post("/validate")
    def validate(payload: _Payload) -> dict[str, int]:
        return {"count": payload.count}

    @probe.get("/error")
    def error() -> None:
        raise RuntimeError("controlled-observability-test")

    def nested_helper(label: str) -> None:
        probe_logger.info("nested_helper label=%s", label)

    @probe.get("/nested/{label}")
    async def nested(label: str) -> dict[str, str]:
        await asyncio.sleep(0)
        probe_logger.info("route_log label=%s", label)
        nested_helper(label)
        return {"label": label}

    @probe.post(TARGET_PATH)
    def historical_preview() -> dict[str, bool]:
        return {"ok": True}

    return probe


def _request_id(response) -> str:
    return response.headers["x-request-id"]


def test_obs_r1_valid_client_request_id_correlates_2xx_response_and_completion_log() -> None:
    probe = _build_probe_app()
    with _captured_application_logs() as logs, TestClient(probe) as client:
        response = client.get("/ok", headers={"x-request-id": "client-abc.123"})

    assert response.status_code == 200
    assert _request_id(response) == "client-abc.123"
    output = logs.getvalue()
    assert "request_id=client-abc.123" in output
    assert "http_request method=GET path=/ok status=200" in output


def test_obs_r2_missing_client_request_id_generates_one_id_for_response_and_log() -> None:
    probe = _build_probe_app()
    with _captured_application_logs() as logs, TestClient(probe) as client:
        response = client.get("/ok")

    generated = _request_id(response)
    UUID(generated)
    assert f"request_id={generated}" in logs.getvalue()


def test_obs_r3_401_response_and_completion_log_share_request_id() -> None:
    probe = _build_probe_app()
    with _captured_application_logs() as logs, TestClient(probe) as client:
        response = client.get("/unauthorized", headers={"x-request-id": "obs-401"})

    assert response.status_code == 401
    assert _request_id(response) == "obs-401"
    assert "request_id=obs-401 http_request method=GET path=/unauthorized status=401" in logs.getvalue()


def test_handled_403_response_and_completion_log_share_request_id() -> None:
    probe = _build_probe_app()
    with _captured_application_logs() as logs, TestClient(probe) as client:
        response = client.get("/forbidden", headers={"x-request-id": "obs-403"})

    assert response.status_code == 403
    assert _request_id(response) == "obs-403"
    assert "request_id=obs-403 http_request method=GET path=/forbidden status=403" in logs.getvalue()


def test_obs_r4_404_response_and_completion_log_share_request_id() -> None:
    probe = _build_probe_app()
    with _captured_application_logs() as logs, TestClient(probe) as client:
        response = client.get("/missing", headers={"x-request-id": "obs-404"})

    assert response.status_code == 404
    assert _request_id(response) == "obs-404"
    assert "request_id=obs-404 http_request method=GET path=/missing status=404" in logs.getvalue()


def test_obs_r5_422_response_and_completion_log_share_request_id() -> None:
    probe = _build_probe_app()
    with _captured_application_logs() as logs, TestClient(probe) as client:
        response = client.post(
            "/validate",
            json={"count": "not-an-int"},
            headers={"x-request-id": "obs-422"},
        )

    assert response.status_code == 422
    assert _request_id(response) == "obs-422"
    assert "request_id=obs-422 http_request method=POST path=/validate status=422" in logs.getvalue()


def test_obs_r6_historical_import_413_preserves_security_behavior_and_correlates_log() -> None:
    probe = _build_probe_app()
    with _captured_application_logs() as logs, TestClient(probe) as client:
        response = client.post(
            TARGET_PATH,
            content=b"abcdef",
            headers={"x-request-id": "obs-413"},
        )

    assert response.status_code == 413
    assert response.json()["detail"] == "Historical import request is too large."
    assert _request_id(response) == "obs-413"
    assert (
        f"request_id=obs-413 http_request method=POST path={TARGET_PATH} status=413"
        in logs.getvalue()
    )


def test_obs_r7_unhandled_500_returns_same_id_and_logs_correlated_traceback() -> None:
    probe = _build_probe_app()
    with _captured_application_logs() as logs, TestClient(probe) as client:
        response = client.get("/error", headers={"x-request-id": "obs-500"})

    assert response.status_code == 500
    assert response.text == "Internal Server Error"
    assert "controlled-observability-test" not in response.text
    assert _request_id(response) == "obs-500"
    output = logs.getvalue()
    assert "request_id=obs-500 http_request method=GET path=/error status=500" in output
    assert "Traceback (most recent call last)" in output
    assert "RuntimeError: controlled-observability-test" in output


def test_obs_r8_nested_service_logs_inherit_request_id_without_manual_parameter() -> None:
    probe = _build_probe_app()
    with _captured_application_logs() as logs, TestClient(probe) as client:
        response = client.get("/nested/nested", headers={"x-request-id": "obs-nested"})

    assert response.status_code == 200
    output = logs.getvalue()
    assert "request_id=obs-nested route_log label=nested" in output
    assert "request_id=obs-nested nested_helper label=nested" in output


def test_obs_r9_outside_request_logging_uses_safe_placeholder() -> None:
    with _captured_application_logs() as logs:
        probe_logger.info("outside_request")

    assert "request_id=- outside_request" in logs.getvalue()


def test_obs_r10_unsafe_oversized_client_id_is_not_logged_or_echoed() -> None:
    probe = _build_probe_app()
    unsafe = "x" * (MAX_REQUEST_ID_LENGTH + 1)
    with _captured_application_logs() as logs, TestClient(probe) as client:
        response = client.get("/ok", headers={"x-request-id": unsafe})

    generated = _request_id(response)
    UUID(generated)
    assert generated != unsafe
    output = logs.getvalue()
    assert unsafe not in output
    assert f"request_id={generated}" in output


def test_unsafe_whitespace_client_id_is_replaced_without_rejecting_request() -> None:
    probe = _build_probe_app()
    unsafe = "request with space"
    with _captured_application_logs() as logs, TestClient(probe) as client:
        response = client.get("/ok", headers={"x-request-id": unsafe})

    generated = _request_id(response)
    UUID(generated)
    assert response.status_code == 200
    assert generated != unsafe
    assert unsafe not in logs.getvalue()


def test_obs_r11_concurrent_client_ids_do_not_cross_contaminate_nested_logs() -> None:
    probe = _build_probe_app()

    async def run() -> list[tuple[int, str]]:
        transport = httpx.ASGITransport(app=probe)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            responses = await asyncio.gather(
                *(
                    client.get(
                        f"/nested/{index}",
                        headers={"x-request-id": f"concurrent-{index}"},
                    )
                    for index in range(8)
                )
            )
        return [(response.status_code, _request_id(response)) for response in responses]

    with _captured_application_logs() as logs:
        results = asyncio.run(run())

    assert results == [(200, f"concurrent-{index}") for index in range(8)]
    lines = logs.getvalue().splitlines()
    for index in range(8):
        matching = [line for line in lines if f"route_log label={index}" in line]
        assert len(matching) == 1
        assert f"request_id=concurrent-{index}" in matching[0]


def test_generated_ids_are_unique_and_correlate_under_concurrency() -> None:
    probe = _build_probe_app()

    async def run() -> list[tuple[int, str]]:
        transport = httpx.ASGITransport(app=probe)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            responses = await asyncio.gather(
                *(client.get(f"/nested/generated-{index}") for index in range(6))
            )
        return [(response.status_code, _request_id(response)) for response in responses]

    with _captured_application_logs() as logs:
        results = asyncio.run(run())

    ids = [request_id for status_code, request_id in results if status_code == 200]
    assert len(ids) == 6
    assert len(set(ids)) == 6
    lines = logs.getvalue().splitlines()
    for index, request_id in enumerate(ids):
        UUID(request_id)
        matching = [line for line in lines if f"route_log label=generated-{index}" in line]
        assert len(matching) == 1
        assert f"request_id={request_id}" in matching[0]


def test_obs_r12_request_context_is_reset_after_request() -> None:
    probe = _build_probe_app()

    async def run():
        transport = httpx.ASGITransport(app=probe)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/ok", headers={"x-request-id": "cleanup-id"})
        probe_logger.info("after_request")
        return response

    with _captured_application_logs() as logs:
        response = asyncio.run(run())

    assert response.status_code == 200
    output = logs.getvalue()
    assert "request_id=cleanup-id http_request method=GET path=/ok status=200" in output
    assert "request_id=- after_request" in output


def test_request_context_is_reset_after_unhandled_500() -> None:
    probe = _build_probe_app()

    async def run():
        transport = httpx.ASGITransport(app=probe)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/error", headers={"x-request-id": "cleanup-500"})
        probe_logger.info("after_500")
        return response

    with _captured_application_logs() as logs:
        response = asyncio.run(run())

    assert response.status_code == 500
    output = logs.getvalue()
    assert "request_id=cleanup-500 http_request method=GET path=/error status=500" in output
    assert "request_id=- after_500" in output


def test_capture_harness_is_independent_of_alembic_logging_side_effects() -> None:
    root = logging.getLogger()
    http_logger = logging.getLogger("tia.http")
    original_root_level = root.level
    original_http_disabled = http_logger.disabled
    original_probe_disabled = probe_logger.disabled
    root.setLevel(logging.WARNING)
    http_logger.disabled = True
    probe_logger.disabled = True
    try:
        with _captured_application_logs() as logs:
            http_logger.info("completion_probe")
            probe_logger.info("nested_probe")
    finally:
        root.setLevel(original_root_level)
        http_logger.disabled = original_http_disabled
        probe_logger.disabled = original_probe_disabled

    output = logs.getvalue()
    assert "request_id=- completion_probe" in output
    assert "request_id=- nested_probe" in output
