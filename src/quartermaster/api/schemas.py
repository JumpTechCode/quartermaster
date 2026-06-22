"""Pydantic v2 request/response models for the HTTP boundary.

These models give HTTP clients early, field-located 422s for the shape rules
(non-empty lines, positive quantities within the column ceiling, no duplicate
SKUs in one request). The same rules also hold *below* HTTP — command
construction re-checks them (``InvalidCommandLines``) and the storage CHECKs back
them — so callers that bypass the API (workers, the load harness, fixtures) get
the same deterministic rejection rather than a later opaque breach (issue #74).
Everything stateful (existence, state-machine legality, stock guards) is enforced
below HTTP by the domain and the database.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from quartermaster.domain.quantities import MAX_QTY


class OrderLineInput(BaseModel):
    sku_id: str = Field(min_length=1, max_length=64)
    qty: int = Field(gt=0, le=MAX_QTY)


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
    sku_id: str = Field(min_length=1, max_length=64)
    qty: int = Field(gt=0, le=MAX_QTY)


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


class CreateReturnRequest(BaseModel):
    order_id: UUID
    lines: list[ReceiptLineInput] = Field(min_length=1, max_length=100)

    @field_validator("lines")
    @classmethod
    def _no_duplicate_skus(cls, lines: list[ReceiptLineInput]) -> list[ReceiptLineInput]:
        skus = [line.sku_id for line in lines]
        if len(set(skus)) != len(skus):
            raise ValueError("duplicate sku_id in return lines")
        return lines


class ArriveResponse(BaseModel):
    receipt_id: UUID
    state: str


class ReceiveRequest(BaseModel):
    location_id: str = Field(min_length=1, max_length=64)
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
    origin_order_id: UUID | None = None
    lines: list[ReceiptLineView]


class PutawayRequest(BaseModel):
    from_location: str = Field(
        min_length=1, max_length=64, description="The receiving cell the stock is moving from."
    )
    to_location: str = Field(
        min_length=1,
        max_length=64,
        description=(
            "Destination cell. Should be a shelf (pickable) location: allocation only "
            "reserves from shelves, so stock put away to a non-shelf cell is not allocatable."
        ),
    )


class PutawayLineOut(BaseModel):
    sku_id: str
    moved: int


class PutawayResponse(BaseModel):
    receipt_id: UUID
    state: str
    lines: list[PutawayLineOut]


class CloseReceiptResponse(BaseModel):
    receipt_id: UUID
    state: str


class CancelReceiptResponse(BaseModel):
    receipt_id: UUID
    state: str
