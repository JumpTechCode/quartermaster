"""Unit tests for the allocate result's JSON round-trip."""

from __future__ import annotations

from uuid import UUID

from quartermaster.application.results import AllocateResult, LineAllocation
from quartermaster.domain.ids import OrderId, ReservationId, SkuId
from quartermaster.domain.state_machines import OrderState

RESULT = AllocateResult(
    order_id=OrderId(UUID("00000000-0000-7000-8000-000000000001")),
    state=OrderState.BACKORDERED,
    lines=(LineAllocation(SkuId("SKU1"), 3), LineAllocation(SkuId("SKU2"), 0)),
    reservation_ids=(ReservationId(UUID("00000000-0000-7000-8000-0000000000aa")),),
)


def test_response_round_trips() -> None:
    assert AllocateResult.decode(RESULT.to_response()) == RESULT


def test_response_is_json_safe() -> None:
    import json

    encoded = RESULT.to_response()
    assert json.loads(json.dumps(encoded)) == encoded
    assert encoded["state"] == "backordered"


def test_create_order_result_roundtrip() -> None:
    from uuid import UUID

    from quartermaster.application.results import CreatedLine, CreateOrderResult
    from quartermaster.domain.ids import OrderId, SkuId
    from quartermaster.domain.state_machines import OrderState

    result = CreateOrderResult(
        order_id=OrderId(UUID("00000000-0000-7000-8000-000000000001")),
        state=OrderState.CREATED,
        lines=(CreatedLine(SkuId("A"), 5), CreatedLine(SkuId("B"), 2)),
    )
    assert CreateOrderResult.decode(result.to_response()) == result
