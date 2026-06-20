"""Validation tests for the API request schemas."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from quartermaster.api.schemas import CreateOrderRequest


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
