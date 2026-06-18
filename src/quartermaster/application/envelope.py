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
    OrderNotFound,
)
from quartermaster.domain.idempotency import IdempotencyStatus
from quartermaster.domain.ids import IdempotencyKey

MAX_OCC_RETRIES = 5

# ADR-0004 classification of handler-raised domain errors.
HARD_REJECTION: tuple[type[Exception], ...] = (IllegalTransition, OrderNotFound)
TRANSIENT: tuple[type[Exception], ...] = (InsufficientStock,)


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
                    raise IdempotencyKeyReuse(command.key)
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
