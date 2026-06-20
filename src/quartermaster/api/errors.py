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
    InvalidReceiptLine,
    OrderNotFound,
    ReceiptNotFound,
    UnknownLocation,
    UnknownSku,
)


class MissingIdempotencyKey(Exception):
    """A write request lacked the required Idempotency-Key header."""


# (exception type, HTTP status, error code)
_STATUS_MAP: tuple[tuple[type[Exception], int, str], ...] = (
    (MissingIdempotencyKey, 400, "missing_idempotency_key"),
    (UnknownSku, 422, "unknown_sku"),
    (UnknownLocation, 422, "unknown_location"),
    (InvalidReceiptLine, 422, "invalid_receipt_line"),
    (OrderNotFound, 404, "order_not_found"),
    (ReceiptNotFound, 404, "receipt_not_found"),
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


def _shape_validation_errors(exc: RequestValidationError) -> str:
    """Reduce ``exc.errors()`` into a concise ``field: message`` summary.

    The default ``str(RequestValidationError)`` dump includes the full Pydantic
    error structure (loc, msg, type, input, url) which is verbose and leaks
    internal structure. This keeps only the field path and message.
    """
    parts: list[str] = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", ()) if p != "body")
        msg = err.get("msg", "invalid")
        parts.append(f"{loc}: {msg}" if loc else msg)
    return "; ".join(parts) if parts else "validation failed"


async def _validation_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    return JSONResponse(
        status_code=422,
        content=_error_body("validation_error", _shape_validation_errors(exc)),
    )


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
