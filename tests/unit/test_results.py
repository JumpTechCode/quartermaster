"""Unit tests for the allocate result's JSON round-trip."""

from __future__ import annotations

from uuid import UUID

from quartermaster.application.results import (
    AllocateResult,
    ArriveResult,
    CreatedReceiptLine,
    CreateReceiptResult,
    LineAllocation,
    ReceivedLine,
    ReceiveResult,
)
from quartermaster.domain.ids import OrderId, ReceiptId, ReservationId, SkuId
from quartermaster.domain.receipts import ReceiptKind
from quartermaster.domain.state_machines import OrderState, ReceiptState

RESULT = AllocateResult(
    order_id=OrderId(UUID("00000000-0000-7000-8000-000000000001")),
    state=OrderState.BACKORDERED,
    lines=(LineAllocation(SkuId("SKU1"), 3), LineAllocation(SkuId("SKU2"), 0)),
    reservation_ids=(ReservationId(UUID("00000000-0000-7000-8000-0000000000aa")),),
)

_RID = ReceiptId(UUID("00000000-0000-7000-8000-000000000005"))


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


def test_create_order_result_response_is_json_safe() -> None:
    import json
    from uuid import UUID

    from quartermaster.application.results import CreatedLine, CreateOrderResult
    from quartermaster.domain.ids import OrderId, SkuId
    from quartermaster.domain.state_machines import OrderState

    result = CreateOrderResult(
        order_id=OrderId(UUID("00000000-0000-7000-8000-000000000001")),
        state=OrderState.CREATED,
        lines=(CreatedLine(SkuId("A"), 5), CreatedLine(SkuId("B"), 2)),
    )
    encoded = result.to_response()
    assert json.loads(json.dumps(encoded)) == encoded
    assert encoded["state"] == "created"


def test_pick_result_roundtrip() -> None:
    from uuid import UUID

    from quartermaster.application.results import PickedLine, PickResult
    from quartermaster.domain.ids import OrderId, SkuId
    from quartermaster.domain.state_machines import OrderState

    result = PickResult(
        order_id=OrderId(UUID("00000000-0000-7000-8000-000000000001")),
        state=OrderState.PICKED,
        lines=(PickedLine(SkuId("A"), 5), PickedLine(SkuId("B"), 2)),
    )
    assert PickResult.decode(result.to_response()) == result


def test_pick_result_response_is_json_safe() -> None:
    import json
    from uuid import UUID

    from quartermaster.application.results import PickedLine, PickResult
    from quartermaster.domain.ids import OrderId, SkuId
    from quartermaster.domain.state_machines import OrderState

    result = PickResult(
        order_id=OrderId(UUID("00000000-0000-7000-8000-000000000001")),
        state=OrderState.PICKED,
        lines=(PickedLine(SkuId("A"), 5),),
    )
    encoded = result.to_response()
    assert json.loads(json.dumps(encoded)) == encoded
    assert encoded["state"] == "picked"


def test_pack_result_roundtrip() -> None:
    from uuid import UUID

    from quartermaster.application.results import PackResult
    from quartermaster.domain.ids import OrderId
    from quartermaster.domain.state_machines import OrderState

    result = PackResult(
        order_id=OrderId(UUID("00000000-0000-7000-8000-000000000001")),
        state=OrderState.PACKED,
    )
    assert PackResult.decode(result.to_response()) == result
    assert result.to_response()["state"] == "packed"


def test_ship_result_roundtrip() -> None:
    from uuid import UUID

    from quartermaster.application.results import ShippedLine, ShipResult
    from quartermaster.domain.ids import OrderId, SkuId
    from quartermaster.domain.state_machines import OrderState

    result = ShipResult(
        order_id=OrderId(UUID("00000000-0000-7000-8000-000000000001")),
        state=OrderState.SHIPPED,
        lines=(ShippedLine(SkuId("A"), 5),),
    )
    assert ShipResult.decode(result.to_response()) == result
    assert result.to_response()["state"] == "shipped"


def test_cancel_result_roundtrip() -> None:
    from uuid import UUID

    from quartermaster.application.results import CancelResult
    from quartermaster.domain.ids import OrderId, ReservationId
    from quartermaster.domain.state_machines import OrderState

    result = CancelResult(
        order_id=OrderId(UUID("00000000-0000-7000-8000-000000000001")),
        state=OrderState.CANCELLED,
        released_reservation_ids=(ReservationId(UUID("00000000-0000-7000-8000-0000000000aa")),),
    )
    assert CancelResult.decode(result.to_response()) == result
    assert result.to_response()["state"] == "cancelled"


def test_create_receipt_result_round_trips() -> None:
    r = CreateReceiptResult(
        _RID,
        ReceiptKind.SUPPLIER_RECEIPT,
        ReceiptState.EXPECTED,
        (CreatedReceiptLine(SkuId("A"), 5),),
    )
    assert CreateReceiptResult.decode(r.to_response()) == r


def test_arrive_result_round_trips() -> None:
    r = ArriveResult(_RID, ReceiptState.ARRIVED)
    assert ArriveResult.decode(r.to_response()) == r


def test_receive_result_round_trips() -> None:
    r = ReceiveResult(_RID, ReceiptState.RECEIVED, (ReceivedLine(SkuId("A"), 3),))
    assert ReceiveResult.decode(r.to_response()) == r
