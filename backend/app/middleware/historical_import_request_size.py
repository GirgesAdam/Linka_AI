from __future__ import annotations

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.historical_import_limits import MAX_HISTORICAL_IMPORT_REQUEST_BYTES


class HistoricalImportRequestSizeLimitMiddleware:
    """Bound the historical preview body before FastAPI reads or parses it."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        path: str,
        max_body_bytes: int = MAX_HISTORICAL_IMPORT_REQUEST_BYTES,
    ) -> None:
        if max_body_bytes <= 0:
            raise ValueError("max_body_bytes must be positive")
        self.app = app
        self.path = path.rstrip("/") or "/"
        self.max_body_bytes = max_body_bytes

    def _is_target(self, scope: Scope) -> bool:
        if scope.get("type") != "http" or scope.get("method", "").upper() != "POST":
            return False
        path = str(scope.get("path", ""))
        return path == self.path or path == f"{self.path}/"

    @staticmethod
    def _content_length(scope: Scope) -> int | None:
        values = [
            value
            for name, value in scope.get("headers", [])
            if name.lower() == b"content-length"
        ]
        if len(values) != 1:
            return None
        try:
            parsed = int(values[0])
        except (TypeError, ValueError):
            return None
        return parsed if parsed >= 0 else None

    @staticmethod
    async def _reject(scope: Scope, receive: Receive, send: Send) -> None:
        response = JSONResponse(
            {"detail": "Historical import request is too large."},
            status_code=413,
        )
        await response(scope, receive, send)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not self._is_target(scope):
            await self.app(scope, receive, send)
            return

        declared_length = self._content_length(scope)
        if declared_length is not None and declared_length > self.max_body_bytes:
            await self._reject(scope, receive, send)
            return

        buffered_body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.request":
                body = message.get("body", b"")
                if len(buffered_body) + len(body) > self.max_body_bytes:
                    await self._reject(scope, receive, send)
                    return
                buffered_body.extend(body)
                if not message.get("more_body", False):
                    break
            elif message["type"] == "http.disconnect":
                await self.app(scope, receive, send)
                return

        replay_body = bytes(buffered_body)
        replayed = False

        async def replay_receive() -> Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": replay_body, "more_body": False}
            return await receive()

        await self.app(scope, replay_receive, send)
