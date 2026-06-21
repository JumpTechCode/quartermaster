"""ReceiptLine tracks expected vs. received (partial receipts); Receipt is the
inbound document. An RMA is a receipt whose kind is CUSTOMER_RMA referencing the
order it returns (design spec §3, §4).
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from quartermaster.domain.errors import InvariantViolation
from quartermaster.domain.ids import OrderId, ReceiptId, SkuId
from quartermaster.domain.quantities import MAX_QTY as COLUMN_MAX_QTY
from quartermaster.domain.receipts import Receipt, ReceiptKind, ReceiptLine
from quartermaster.domain.state_machines import ReceiptState

RECEIPT_ID = ReceiptId(uuid4())
ORDER_ID = OrderId(uuid4())
SKU_ID = SkuId("WIDGET-1")


def rline(expected: int, received: int) -> ReceiptLine:
    return ReceiptLine(receipt_id=RECEIPT_ID, sku_id=SKU_ID, expected=expected, received=received)


# --- ReceiptLine ------------------------------------------------------------


def test_line_holds_its_quantities() -> None:
    ln = rline(expected=10, received=6)
    assert (ln.expected, ln.received) == (10, 6)


@pytest.mark.parametrize(("expected", "received"), [(10, 11), (10, -1), (-1, 0)])
def test_line_rejects_out_of_range(expected: int, received: int) -> None:
    with pytest.raises(InvariantViolation):
        rline(expected, received)


def test_line_rejects_expected_above_column_max() -> None:
    with pytest.raises(InvariantViolation):
        rline(expected=COLUMN_MAX_QTY + 1, received=0)


def test_line_accepts_expected_at_column_max() -> None:
    assert rline(expected=COLUMN_MAX_QTY, received=0).expected == COLUMN_MAX_QTY


def test_shortfall_and_completeness() -> None:
    assert rline(10, 4).shortfall == 6
    assert rline(10, 4).is_complete is False
    assert rline(10, 10).is_complete is True


def test_receive_advances_received() -> None:
    assert rline(10, 4).receive(3) == rline(10, 7)


def test_receive_beyond_expected_raises() -> None:
    with pytest.raises(InvariantViolation):
        rline(10, 8).receive(3)


def test_receive_rejects_negative() -> None:
    with pytest.raises(ValueError):
        rline(10, 4).receive(-1)


# --- Receipt document -------------------------------------------------------


def test_supplier_receipt_has_no_origin() -> None:
    now = datetime.now(UTC)
    receipt = Receipt(
        receipt_id=RECEIPT_ID,
        kind=ReceiptKind.SUPPLIER_RECEIPT,
        state=ReceiptState.EXPECTED,
        version=1,
        created_at=now,
        origin_order_id=None,
    )
    assert receipt.kind is ReceiptKind.SUPPLIER_RECEIPT
    assert receipt.origin_order_id is None


def test_rma_references_its_order() -> None:
    receipt = Receipt(
        receipt_id=RECEIPT_ID,
        kind=ReceiptKind.CUSTOMER_RMA,
        state=ReceiptState.EXPECTED,
        version=1,
        created_at=datetime.now(UTC),
        origin_order_id=ORDER_ID,
    )
    assert receipt.origin_order_id == ORDER_ID


def test_supplier_receipt_with_origin_is_rejected() -> None:
    with pytest.raises(InvariantViolation):
        Receipt(
            receipt_id=RECEIPT_ID,
            kind=ReceiptKind.SUPPLIER_RECEIPT,
            state=ReceiptState.EXPECTED,
            version=1,
            created_at=datetime.now(UTC),
            origin_order_id=ORDER_ID,
        )


def test_rma_without_origin_is_rejected() -> None:
    with pytest.raises(InvariantViolation):
        Receipt(
            receipt_id=RECEIPT_ID,
            kind=ReceiptKind.CUSTOMER_RMA,
            state=ReceiptState.EXPECTED,
            version=1,
            created_at=datetime.now(UTC),
            origin_order_id=None,
        )


def test_receipt_rejects_version_below_one() -> None:
    with pytest.raises(InvariantViolation):
        Receipt(
            receipt_id=RECEIPT_ID,
            kind=ReceiptKind.SUPPLIER_RECEIPT,
            state=ReceiptState.EXPECTED,
            version=0,
            created_at=datetime.now(UTC),
            origin_order_id=None,
        )
