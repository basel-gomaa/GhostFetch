"""Request-level validation the routes cannot do for themselves.

Three things live here because they apply to EVERY route, before any handler or
dependency runs:

* BodySizeLimit -- FastAPI reads and parses the whole JSON body BEFORE the
  auth dependency is solved, so on its own any client that can reach the port,
  signed in or not, could make the API hold a body of any size in memory. The
  largest legitimate body today is a run input of a few KB; the default limit
  (1 MiB, ACTOR_PLATFORM_MAX_BODY_BYTES) is generous on purpose.

* safe_validation_errors -- FastAPI's default 422 under Pydantic v2 copies the
  offending INPUT into the response: a signup missing one field answered with
  the whole body, plaintext password included. The handler keeps FastAPI's
  shape and status ({"detail": [{type, loc, msg}]}) and drops only `input`,
  `ctx` and `url`, so a client that read the old body still can.

* SafeText -- a str that refuses NUL and unpaired surrogates. Both are valid
  JSON and neither can be stored: Postgres text and jsonb reject NUL, and a
  lone surrogate is not UTF-8. Reaching the database with one was a 500.
"""

from __future__ import annotations

import json
import os
from typing import Annotated, Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import AfterValidator

MAX_BODY_ENV = "ACTOR_PLATFORM_MAX_BODY_BYTES"
DEFAULT_MAX_BODY_BYTES = 1024 * 1024


def max_body_bytes(environ: dict[str, str] | None = None) -> int:
    """The configured body limit. A bad value falls back to the default."""
    raw = (environ if environ is not None else os.environ).get(MAX_BODY_ENV, "").strip()
    try:
        value = int(raw) if raw else DEFAULT_MAX_BODY_BYTES
    except ValueError:
        return DEFAULT_MAX_BODY_BYTES
    return value if value > 0 else DEFAULT_MAX_BODY_BYTES


def _too_large_body(limit: int) -> bytes:
    return json.dumps({
        "error": "request too large",
        "detail": f"the request body is larger than {limit} bytes",
    }).encode()


class BodySizeLimit:
    """Pure ASGI middleware: refuse an HTTP request body larger than `limit`.

    A declared Content-Length over the limit is refused before a byte is read.
    Otherwise the body is read HERE, counting, and only handed on once it is
    known to fit -- which is what makes a chunked body (no Content-Length) as
    bounded as a declared one. Buffering is capped at `limit`, so this never
    holds more than the app would have. WebSockets and lifespan pass through.
    """

    def __init__(self, app, limit: int | None = None) -> None:
        self.app = app
        self.limit = limit if limit is not None else max_body_bytes()

    async def _refuse(self, send) -> None:
        body = _too_large_body(self.limit)
        await send({"type": "http.response.start", "status": 413, "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
            (b"connection", b"close"),
        ]})
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        for name, value in scope.get("headers") or ():
            if name == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    declared = None  # the server rejects a malformed one itself
                if declared is not None and declared > self.limit:
                    await self._refuse(send)
                    return

        chunks: list[bytes] = []
        size = 0
        while True:
            message = await receive()
            if message["type"] != "http.request":
                # A disconnect before the body arrived: nothing to serve.
                return
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > self.limit:
                await self._refuse(send)
                return
            chunks.append(chunk)
            if not message.get("more_body", False):
                break

        delivered = False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": b"".join(chunks), "more_body": False}
            # After the body, the original channel: that is where a client
            # disconnect arrives, which streaming responses listen for.
            return await receive()

        await self.app(scope, replay, send)


def install(app: FastAPI) -> None:
    """Attach the body limit and the redacted validation-error handler."""

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        detail = [
            {"type": error.get("type"), "loc": list(error.get("loc", ())), "msg": error.get("msg")}
            for error in exc.errors()
        ]
        return JSONResponse(status_code=422, content={"detail": jsonable_encoder(detail)})

    # Read NOW, when the app is built: Starlette constructs middleware lazily,
    # on the first request, and the limit must not depend on when that is.
    app.add_middleware(BodySizeLimit, limit=max_body_bytes())


def _storable(value: str) -> str:
    if "\x00" in value:
        raise ValueError("must not contain NUL characters")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("must not contain unpaired surrogate characters") from None
    return value


SafeText = Annotated[str, AfterValidator(_storable)]


def storable(value: Any) -> bool:
    """True for a str a database can hold. For values outside a model."""
    if not isinstance(value, str):
        return False
    try:
        _storable(value)
    except ValueError:
        return False
    return True
