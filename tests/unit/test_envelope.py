"""Unit tests for the transaction envelope's orchestration and ADR-0004 policy."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from quartermaster.application.envelope import HARD_REJECTION, _rejection_error, execute
from quartermaster.application.errors import OccConflict, RetryExhausted
from quartermaster.application.ports import ClaimOutcome, StoredResponse, UnitOfWork
from quartermaster.domain.errors import (
    IdempotencyKeyReuse,
    IllegalTransition,
    InsufficientStock,
    InvalidReceiptLine,
    InvariantViolation,
    LocationKindMismatch,
    ReceiptNotFound,
    StockConflict,
    UnknownLocation,
)
from quartermaster.domain.idempotency import IdempotencyStatus
from quartermaster.domain.ids import IdempotencyKey
from tests.unit.fakes import FakeIdempotencyRepo, FakeUnitOfWork, fake_factory


@dataclass(frozen=True)
class FakeResult:
    value: int

    def to_response(self) -> dict[str, Any]:
        return {"value": self.value}


def decode_fake(data: dict[str, Any]) -> FakeResult:
    return FakeResult(value=int(data["value"]))


@dataclass(frozen=True)
class FakeCommand:
    key: IdempotencyKey = field(default_factory=lambda: IdempotencyKey("k"))

    def fingerprint(self) -> str:
        return "fp"


async def test_success_finalizes_succeeded_and_commits() -> None:
    idempotency = FakeIdempotencyRepo()
    uow = FakeUnitOfWork(idempotency=idempotency)

    async def handler(u: UnitOfWork, c: FakeCommand) -> FakeResult:
        return FakeResult(7)

    result = await execute(fake_factory(uow), FakeCommand(), handler, decode_fake)

    assert result == FakeResult(7)
    assert uow.commits == 1 and uow.rollbacks == 0
    ((_key, status, response),) = idempotency.finalize_calls
    assert status is IdempotencyStatus.SUCCEEDED
    assert response == {"value": 7}


async def test_hard_rejection_finalizes_rejected_commits_and_raises() -> None:
    idempotency = FakeIdempotencyRepo()
    uow = FakeUnitOfWork(idempotency=idempotency)

    async def handler(u: UnitOfWork, c: FakeCommand) -> FakeResult:
        raise IllegalTransition("order: illegal transition shipped -> allocated")

    with pytest.raises(IllegalTransition):
        await execute(fake_factory(uow), FakeCommand(), handler, decode_fake)

    assert uow.commits == 1 and uow.rollbacks == 0
    ((_key, status, _response),) = idempotency.finalize_calls
    assert status is IdempotencyStatus.REJECTED


async def test_transient_failure_rolls_back_and_does_not_persist() -> None:
    idempotency = FakeIdempotencyRepo()
    uow = FakeUnitOfWork(idempotency=idempotency)

    async def handler(u: UnitOfWork, c: FakeCommand) -> FakeResult:
        raise InsufficientStock("nope")

    with pytest.raises(InsufficientStock):
        await execute(fake_factory(uow), FakeCommand(), handler, decode_fake)

    assert uow.commits == 0 and uow.rollbacks == 1
    assert idempotency.finalize_calls == []


async def test_stock_conflict_rolls_back_and_does_not_persist() -> None:
    # StockConflict is transient like InsufficientStock: a foreseeable shortfall
    # on otherwise-valid input that a retry may clear (issue #32). Rolled back so
    # any partial line work is discarded; never finalized, so it is not cached.
    idempotency = FakeIdempotencyRepo()
    uow = FakeUnitOfWork(idempotency=idempotency)

    async def handler(u: UnitOfWork, c: FakeCommand) -> FakeResult:
        raise StockConflict("from_location lacks the stock")

    with pytest.raises(StockConflict):
        await execute(fake_factory(uow), FakeCommand(), handler, decode_fake)

    assert uow.commits == 0 and uow.rollbacks == 1
    assert idempotency.finalize_calls == []


async def test_invariant_violation_rolls_back_without_finalizing() -> None:
    # A genuine consistency breach is a server-side alarm, not a business
    # rejection: it rolls back (no partial state commits) and is never cached, so
    # it surfaces as a classified 500 rather than a replayed rejection (issue #32).
    idempotency = FakeIdempotencyRepo()
    uow = FakeUnitOfWork(idempotency=idempotency)

    async def handler(u: UnitOfWork, c: FakeCommand) -> FakeResult:
        raise InvariantViolation("reservation held but its stock is missing")

    with pytest.raises(InvariantViolation):
        await execute(fake_factory(uow), FakeCommand(), handler, decode_fake)

    assert uow.commits == 0 and uow.rollbacks == 1
    assert idempotency.finalize_calls == []


async def test_existing_key_replays_the_stored_response() -> None:
    stored = StoredResponse("fp", IdempotencyStatus.SUCCEEDED, {"value": 42})
    idempotency = FakeIdempotencyRepo(ClaimOutcome.EXISTS, stored)
    uow = FakeUnitOfWork(idempotency=idempotency)

    async def handler(u: UnitOfWork, c: FakeCommand) -> FakeResult:
        raise AssertionError("handler must not run on replay")

    result = await execute(fake_factory(uow), FakeCommand(), handler, decode_fake)

    assert result == FakeResult(42)
    assert idempotency.finalize_calls == []


async def test_fingerprint_mismatch_raises_key_reuse() -> None:
    stored = StoredResponse("DIFFERENT", IdempotencyStatus.SUCCEEDED, {"value": 1})
    uow = FakeUnitOfWork(idempotency=FakeIdempotencyRepo(ClaimOutcome.EXISTS, stored))

    async def handler(u: UnitOfWork, c: FakeCommand) -> FakeResult:
        return FakeResult(0)

    with pytest.raises(IdempotencyKeyReuse):
        await execute(fake_factory(uow), FakeCommand(), handler, decode_fake)


async def test_occ_conflict_retries_then_succeeds() -> None:
    idempotency = FakeIdempotencyRepo()
    uow = FakeUnitOfWork(idempotency=idempotency)
    attempts = {"n": 0}

    async def handler(u: UnitOfWork, c: FakeCommand) -> FakeResult:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise OccConflict("cas miss")
        return FakeResult(5)

    result = await execute(fake_factory(uow), FakeCommand(), handler, decode_fake)

    assert result == FakeResult(5)
    assert attempts["n"] == 2
    assert uow.rollbacks == 1 and uow.commits == 1


async def test_occ_conflict_exhausts_retries() -> None:
    uow = FakeUnitOfWork()

    async def handler(u: UnitOfWork, c: FakeCommand) -> FakeResult:
        raise OccConflict("always")

    with pytest.raises(RetryExhausted):
        await execute(fake_factory(uow), FakeCommand(), handler, decode_fake)

    assert uow.commits == 0


async def test_replay_of_cached_rejection_reraises() -> None:
    stored = StoredResponse(
        "fp",
        IdempotencyStatus.REJECTED,
        {"error": "IllegalTransition", "detail": "shipped -> allocated"},
    )
    uow = FakeUnitOfWork(idempotency=FakeIdempotencyRepo(ClaimOutcome.EXISTS, stored))

    async def handler(u: UnitOfWork, c: FakeCommand) -> FakeResult:
        raise AssertionError("handler must not run on replay")

    with pytest.raises(IllegalTransition):
        await execute(fake_factory(uow), FakeCommand(), handler, decode_fake)


async def test_unknown_sku_is_a_cached_hard_rejection() -> None:
    from quartermaster.domain.errors import UnknownSku

    idempotency = FakeIdempotencyRepo()
    uow = FakeUnitOfWork(idempotency=idempotency)

    async def handler(u: UnitOfWork, c: FakeCommand) -> FakeResult:
        raise UnknownSku("sku WIDGET-9 does not exist")

    with pytest.raises(UnknownSku):
        await execute(fake_factory(uow), FakeCommand(), handler, decode_fake)

    assert uow.commits == 1 and uow.rollbacks == 0
    ((_key, status, response),) = idempotency.finalize_calls
    assert status is IdempotencyStatus.REJECTED
    assert response == {"error": "UnknownSku", "detail": "sku WIDGET-9 does not exist"}


async def test_unknown_sku_replay_reraises() -> None:
    from quartermaster.domain.errors import UnknownSku

    stored = StoredResponse(
        command_fingerprint="fp",
        status=IdempotencyStatus.REJECTED,
        response={"error": "UnknownSku", "detail": "sku WIDGET-9 does not exist"},
    )
    idempotency = FakeIdempotencyRepo(claim_outcome=ClaimOutcome.EXISTS, stored=stored)
    uow = FakeUnitOfWork(idempotency=idempotency)

    async def handler(u: UnitOfWork, c: FakeCommand) -> FakeResult:
        raise AssertionError("handler must not run on replay")

    with pytest.raises(UnknownSku):
        await execute(fake_factory(uow), FakeCommand(), handler, decode_fake)


def test_inbound_errors_are_hard_rejections() -> None:
    for exc_type in (ReceiptNotFound, UnknownLocation, InvalidReceiptLine, LocationKindMismatch):
        assert exc_type in HARD_REJECTION


def test_return_not_allowed_is_a_hard_rejection() -> None:
    from quartermaster.domain.errors import ReturnNotAllowed

    assert ReturnNotAllowed in HARD_REJECTION
    assert isinstance(
        _rejection_error({"error": "ReturnNotAllowed", "detail": "x"}), ReturnNotAllowed
    )


def test_rejection_error_maps_inbound_codes() -> None:
    assert isinstance(
        _rejection_error({"error": "ReceiptNotFound", "detail": "x"}), ReceiptNotFound
    )
    assert isinstance(
        _rejection_error({"error": "UnknownLocation", "detail": "x"}), UnknownLocation
    )
    assert isinstance(
        _rejection_error({"error": "InvalidReceiptLine", "detail": "x"}), InvalidReceiptLine
    )
    assert isinstance(
        _rejection_error({"error": "LocationKindMismatch", "detail": "x"}), LocationKindMismatch
    )
