"""The transaction envelope: one transaction per command, bounded OCC retry.

Encodes the design §5 pipeline and the ADR-0004 idempotency policy: successes
and hard validation rejections commit (and are replayed on retry); transient
business failures roll back so the key is not persisted and a later retry may
succeed. The idempotency key claim serializes concurrent duplicates; the OCC
retry loop handles internal document-CAS conflicts. The two retries are never
conflated (design §5.4).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from quartermaster.application.errors import OccConflict, RetryExhausted
from quartermaster.application.ports import ClaimOutcome, UnitOfWork, UnitOfWorkFactory
from quartermaster.domain.errors import (
    IdempotencyKeyReuse,
    IllegalTransition,
    InsufficientStock,
    InvalidReceiptLine,
    InvariantViolation,
    LocationKindMismatch,
    OrderNotFound,
    ReceiptNotFound,
    ReturnNotAllowed,
    StockConflict,
    UnknownLocation,
    UnknownSku,
)
from quartermaster.domain.idempotency import IdempotencyStatus
from quartermaster.domain.ids import IdempotencyKey

MAX_OCC_RETRIES = 5

_REJECTION_TYPES: dict[str, type[Exception]] = {
    "IllegalTransition": IllegalTransition,
    "OrderNotFound": OrderNotFound,
    "UnknownSku": UnknownSku,
    "ReceiptNotFound": ReceiptNotFound,
    "UnknownLocation": UnknownLocation,
    "InvalidReceiptLine": InvalidReceiptLine,
    "LocationKindMismatch": LocationKindMismatch,
    "ReturnNotAllowed": ReturnNotAllowed,
}


def _rejection_error(response: dict[str, Any] | None) -> Exception:
    payload = response or {}
    error_type = _REJECTION_TYPES.get(str(payload.get("error")), IllegalTransition)
    return error_type(str(payload.get("detail", "rejected")))


# ADR-0004 classification of handler-raised domain errors.
HARD_REJECTION: tuple[type[Exception], ...] = (
    IllegalTransition,
    OrderNotFound,
    UnknownSku,
    ReceiptNotFound,
    UnknownLocation,
    InvalidReceiptLine,
    LocationKindMismatch,
    ReturnNotAllowed,
)
# Business shortfalls: rolled back so the key is not persisted and a later retry
# may succeed. StockConflict (putaway from a cell lacking the unreserved stock)
# joins InsufficientStock here -- both are "not enough stock right now" outcomes
# on otherwise-valid input, not consistency breaches (ADR-0024).
TRANSIENT: tuple[type[Exception], ...] = (InsufficientStock, StockConflict)


class Command(Protocol):
    @property
    def key(self) -> IdempotencyKey: ...

    def fingerprint(self) -> str: ...


class Response(Protocol):
    def to_response(self) -> dict[str, Any]: ...


async def execute[C: Command, R: Response](
    uow_factory: UnitOfWorkFactory,
    command: C,
    handler: Callable[[UnitOfWork, C], Awaitable[R]],
    decode: Callable[[dict[str, Any]], R],
) -> R:
    """Run ``command`` through the envelope and return its (fresh or replayed) result."""
    fingerprint = command.fingerprint()
    for _attempt in range(MAX_OCC_RETRIES):
        async with uow_factory() as uow:
            if await uow.idempotency.claim(command.key, fingerprint) is ClaimOutcome.EXISTS:
                stored = await uow.idempotency.load(command.key)
                assert stored is not None  # claim said EXISTS, so the row is there
                if stored.command_fingerprint != fingerprint:
                    raise IdempotencyKeyReuse(
                        f"idempotency key {command.key!r} reused with a different command"
                    )
                if stored.status is IdempotencyStatus.REJECTED:
                    raise _rejection_error(stored.response)
                assert stored.response is not None
                return decode(stored.response)
            try:
                result = await handler(uow, command)
            except OccConflict:
                await uow.rollback()
                continue
            except TRANSIENT:
                await uow.rollback()
                raise
            except InvariantViolation:
                # A genuine consistency breach (a reservation whose backing stock
                # is gone): a server-side alarm, not a business rejection. Roll
                # back so no partial state commits and never finalize -- caching
                # would both mislabel an alarm as a rejection and (mid-loop) risk
                # committing partial work. It surfaces as a classified 500
                # (ADR-0024), distinct from the opaque internal_error catch-all.
                await uow.rollback()
                raise
            except HARD_REJECTION as exc:
                await uow.idempotency.finalize(
                    command.key,
                    IdempotencyStatus.REJECTED,
                    {"error": type(exc).__name__, "detail": str(exc)},
                )
                await uow.commit()
                raise
            await uow.idempotency.finalize(
                command.key, IdempotencyStatus.SUCCEEDED, result.to_response()
            )
            await uow.commit()
            return result
    raise RetryExhausted(command.key)
