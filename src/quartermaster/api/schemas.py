"""Pydantic v2 request/response models for the HTTP boundary.

Validation that needs no database lives here (non-empty lines, positive
quantities, no duplicate SKUs in one request); everything else is enforced
below HTTP by the domain and the database.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class OrderLineInput(BaseModel):
    sku_id: str = Field(min_length=1)
    qty: int = Field(gt=0)


class CreateOrderRequest(BaseModel):
    lines: list[OrderLineInput] = Field(min_length=1, max_length=100)

    @field_validator("lines")
    @classmethod
    def _no_duplicate_skus(cls, lines: list[OrderLineInput]) -> list[OrderLineInput]:
        skus = [line.sku_id for line in lines]
        if len(set(skus)) != len(skus):
            raise ValueError("duplicate sku_id in order lines")
        return lines


class CreatedLineOut(BaseModel):
    sku_id: str
    ordered: int


class CreateOrderResponse(BaseModel):
    order_id: UUID
    state: str
    lines: list[CreatedLineOut]


class AllocationLineOut(BaseModel):
    sku_id: str
    allocated: int


class AllocateResponse(BaseModel):
    order_id: UUID
    state: str
    lines: list[AllocationLineOut]
    reservation_ids: list[UUID]


class OrderLineView(BaseModel):
    sku_id: str
    ordered: int
    allocated: int
    picked: int
    shipped: int


class OrderResponse(BaseModel):
    order_id: UUID
    state: str
    version: int
    lines: list[OrderLineView]


class PickedLineOut(BaseModel):
    sku_id: str
    picked: int


class PickResponse(BaseModel):
    order_id: UUID
    state: str
    lines: list[PickedLineOut]


class PackResponse(BaseModel):
    order_id: UUID
    state: str


class ShippedLineOut(BaseModel):
    sku_id: str
    shipped: int


class ShipResponse(BaseModel):
    order_id: UUID
    state: str
    lines: list[ShippedLineOut]


class CancelResponse(BaseModel):
    order_id: UUID
    state: str
    released_reservation_ids: list[UUID]


class ErrorResponse(BaseModel):
    error: str
    detail: str


class ReceiptLineInput(BaseModel):
    sku_id: str = Field(min_length=1)
    qty: int = Field(gt=0)


class CreateReceiptRequest(BaseModel):
    lines: list[ReceiptLineInput] = Field(min_length=1, max_length=100)

    @field_validator("lines")
    @classmethod
    def _no_duplicate_skus(cls, lines: list[ReceiptLineInput]) -> list[ReceiptLineInput]:
        skus = [line.sku_id for line in lines]
        if len(set(skus)) != len(skus):
            raise ValueError("duplicate sku_id in receipt lines")
        return lines


class ExpectedLineOut(BaseModel):
    sku_id: str
    expected: int


class CreateReceiptResponse(BaseModel):
    receipt_id: UUID
    kind: str
    state: str
    lines: list[ExpectedLineOut]


class ArriveResponse(BaseModel):
    receipt_id: UUID
    state: str


class ReceiveRequest(BaseModel):
    location_id: str = Field(min_length=1)
    lines: list[ReceiptLineInput] = Field(min_length=1, max_length=100)

    @field_validator("lines")
    @classmethod
    def _no_duplicate_skus(cls, lines: list[ReceiptLineInput]) -> list[ReceiptLineInput]:
        skus = [line.sku_id for line in lines]
        if len(set(skus)) != len(skus):
            raise ValueError("duplicate sku_id in receive lines")
        return lines


class ReceiveLineOut(BaseModel):
    sku_id: str
    received: int


class ReceiveResponse(BaseModel):
    receipt_id: UUID
    state: str
    lines: list[ReceiveLineOut]


class ReceiptLineView(BaseModel):
    sku_id: str
    expected: int
    received: int


class ReceiptResponse(BaseModel):
    receipt_id: UUID
    kind: str
    state: str
    version: int
    lines: list[ReceiptLineView]
