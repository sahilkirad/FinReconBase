"""Response models for Layer 2 ingestion endpoints (Streams 2 & 3)."""

from typing import Literal

from pydantic import BaseModel


class RazorpayWebhookResponse(BaseModel):
    settlement_id: str | None = None
    payout_id: str
    status: Literal["recorded", "duplicate"]
    message: str


class BankIngestResponse(BaseModel):
    accepted: int
    duplicates: int
    total: int
    message: str


class IngestionErrorResponse(BaseModel):
    error_code: str
    message: str
