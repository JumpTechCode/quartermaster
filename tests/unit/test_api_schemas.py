"""Validation tests for the API request schemas."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from quartermaster.api.schemas import (
    CreateOrderRequest,
    CreateReceiptRequest,
    PutawayRequest,
    ReceiptLineInput,
    ReceiveRequest,
)


def test_valid_create_request() -> None:
    req = CreateOrderRequest(lines=[{"sku_id": "A", "qty": 5}])
    assert req.lines[0].qty == 5


def test_empty_lines_rejected() -> None:
    with pytest.raises(ValidationError):
        CreateOrderRequest(lines=[])


def test_too_many_lines_rejected() -> None:
    with pytest.raises(ValidationError):
        CreateOrderRequest(lines=[{"sku_id": f"SKU-{i}", "qty": 1} for i in range(101)])


def test_oversized_sku_id_rejected() -> None:
    with pytest.raises(ValidationError):
        CreateOrderRequest(lines=[{"sku_id": "A" * 65, "qty": 1}])


def test_nonpositive_qty_rejected() -> None:
    with pytest.raises(ValidationError):
        CreateOrderRequest(lines=[{"sku_id": "A", "qty": 0}])


def test_qty_above_32bit_column_max_rejected() -> None:
    # 2_147_483_648 == one past the signed 32-bit ceiling the qty columns hold;
    # without an upper bound it slips past validation and fails at INSERT as a 500.
    with pytest.raises(ValidationError):
        CreateOrderRequest(lines=[{"sku_id": "A", "qty": 2_147_483_648}])
    with pytest.raises(ValidationError):
        ReceiptLineInput(sku_id="A", qty=2_147_483_648)


def test_qty_at_32bit_column_max_accepted() -> None:
    req = CreateOrderRequest(lines=[{"sku_id": "A", "qty": 2_147_483_647}])
    assert req.lines[0].qty == 2_147_483_647
    assert ReceiptLineInput(sku_id="A", qty=2_147_483_647).qty == 2_147_483_647


def test_blank_sku_rejected() -> None:
    with pytest.raises(ValidationError):
        CreateOrderRequest(lines=[{"sku_id": "", "qty": 1}])


def test_duplicate_sku_rejected() -> None:
    with pytest.raises(ValidationError):
        CreateOrderRequest(lines=[{"sku_id": "A", "qty": 1}, {"sku_id": "A", "qty": 2}])


def test_create_receipt_request_rejects_duplicate_skus() -> None:
    with pytest.raises(ValidationError):
        CreateReceiptRequest(
            lines=[ReceiptLineInput(sku_id="A", qty=1), ReceiptLineInput(sku_id="A", qty=2)]
        )


def test_receipt_line_input_rejects_nonpositive_qty() -> None:
    with pytest.raises(ValidationError):
        ReceiptLineInput(sku_id="A", qty=0)


def test_receipt_line_input_rejects_oversized_sku_id() -> None:
    with pytest.raises(ValidationError):
        ReceiptLineInput(sku_id="A" * 65, qty=1)


def test_receive_request_requires_location() -> None:
    with pytest.raises(ValidationError):
        ReceiveRequest(location_id="", lines=[ReceiptLineInput(sku_id="A", qty=1)])


def test_receive_request_rejects_oversized_location() -> None:
    with pytest.raises(ValidationError):
        ReceiveRequest(location_id="A" * 65, lines=[ReceiptLineInput(sku_id="A", qty=1)])


def test_putaway_request_valid() -> None:
    req = PutawayRequest(from_location="RCV", to_location="A1")
    assert (req.from_location, req.to_location) == ("RCV", "A1")


def test_putaway_request_rejects_blank_locations() -> None:
    with pytest.raises(ValidationError):
        PutawayRequest(from_location="RCV", to_location="")
    with pytest.raises(ValidationError):
        PutawayRequest(from_location="", to_location="A1")


def test_putaway_request_rejects_oversized_locations() -> None:
    with pytest.raises(ValidationError):
        PutawayRequest(from_location="A" * 65, to_location="A1")
    with pytest.raises(ValidationError):
        PutawayRequest(from_location="RCV", to_location="A" * 65)
