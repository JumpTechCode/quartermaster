"""Validation tests for the API request schemas."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from quartermaster.api.schemas import (
    CreateOrderRequest,
    CreateReceiptRequest,
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


def test_nonpositive_qty_rejected() -> None:
    with pytest.raises(ValidationError):
        CreateOrderRequest(lines=[{"sku_id": "A", "qty": 0}])


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


def test_receive_request_requires_location() -> None:
    with pytest.raises(ValidationError):
        ReceiveRequest(location_id="", lines=[ReceiptLineInput(sku_id="A", qty=1)])
