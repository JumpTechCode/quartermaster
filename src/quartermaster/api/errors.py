"""Centralized exception -> HTTP mapping.

Every error response shares one shape: ``{"error": <code>, "detail": <message>}``
— the same shape the envelope persists for cached rejections. Routes never
translate errors themselves; they raise, and these handlers map.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from quartermaster.application.errors import RetryExhausted
from quartermaster.domain.errors import (
    IdempotencyKeyReuse,
    IllegalTransition,
    InsufficientStock,
    OrderNotFound,
    UnknownSku,
)


class MissingIdempotencyKey(Exception):
    """A write request lacked the required Idempotency-Key header."""


# (exception type, HTTP status, error code)
_STATUS_MAP: tuple[tuple[type[Exception], int, str], ...] = (
    (MissingIdempotencyKey, 400, "missing_idempotency_key"),
    (UnknownSku, 422, "unknown_sku"),
    (OrderNotFound, 404, "order_not_found"),
    (IllegalTransition, 409, "illegal_transition"),
    (IdempotencyKeyReuse, 409, "idempotency_key_reuse"),
    (InsufficientStock, 409, "insufficient_stock"),
    (RetryExhausted, 503, "retry_exhausted"),
)

_Handler = Callable[[Request, Exception], Awaitable[JSONResponse]]


def _error_body(code: str, detail: str) -> dict[str, str]:
    return {"error": code, "detail": detail}


def _make_handler(status: int, code: str) -> _Handler:
    async def handler(request: Request, exc: Exception) -> JSONResponse:
        headers = {"Retry-After": "0"} if status == 503 else None
        return JSONResponse(
            status_code=status, content=_error_body(code, str(exc)), headers=headers
        )

    return handler


async def _validation_handler(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(status_code=422, content=_error_body("validation_error", str(exc)))


async def _internal_error_handler(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=500,
        content=_error_body("internal_error", "an unexpected error occurred"),
    )


def register_error_handlers(app: FastAPI) -> None:
    """Attach the domain-error and validation-error handlers to ``app``."""
    for exc_type, status, code in _STATUS_MAP:
        app.add_exception_handler(exc_type, _make_handler(status, code))
    app.add_exception_handler(RequestValidationError, _validation_handler)
    # Catch-all: unmapped exceptions become a uniform shaped 500.
    # Starlette routes Exception-keyed handlers to ServerErrorMiddleware (the outermost
    # layer), so specific domain handlers in ExceptionMiddleware still win for their types.
    app.add_exception_handler(Exception, _internal_error_handler)
