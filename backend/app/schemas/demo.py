"""Schemas for the demo auto-feed generator (POST /demo/auto-generate-feeds)."""

from typing import Literal

from pydantic import BaseModel, Field


class AutoGenerateFeedsRequest(BaseModel):
    """Ask the API to materialize Streams 2 & 3 for a Layer 1 batch."""

    batch_id: str = Field(..., min_length=1, description="Layer 1 batch UUID (from the 202 upload response)")
    anomalies: int = Field(
        default=4,
        ge=0,
        le=200,
        description=(
            "Leave the last N validated invoices unmatched (dropped from both "
            "feeds) so they surface on the Exception Desk as NO_MATCH tickets."
        ),
    )


class AutoGenerateFeedsResponse(BaseModel):
    """202 WAITING (extraction still running) or 200 PUSHED (feeds ingested)."""

    batch_id: str
    status: Literal["WAITING", "PUSHED"]
    message: str
    invoices_generated: int | None = None
    anomalies: int | None = None
    razorpay_accepted: int | None = None
    razorpay_duplicates: int | None = None
    bank_accepted: int | None = None
    bank_duplicates: int | None = None


class DemoFeedError(BaseModel):
    """Structured error body (mirrors the ingestion error convention)."""

    error_code: str
    message: str
