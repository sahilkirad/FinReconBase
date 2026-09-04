"""Response models for the Track 4 dashboard read APIs.

- GET /batches/{batch_id}/telemetry            (Live Batch Telemetry)
- GET /batches/{batch_id}/telemetry/events     (ReAct terminal stream)
- GET /ledger/entries                          (Immutable Ledger view)

All read-only: nothing here writes to the frozen Core-logic tables.
"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel


# =============================================================================
# Batch telemetry
# =============================================================================


class Layer2RunSummary(BaseModel):
    status: str
    run_type: str
    total_extracted: int
    matched_count: int
    exception_count: int
    shortfall: int
    last_error: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None


class InvoiceTelemetryItem(BaseModel):
    row_number: int | None = None
    invoice_number: str | None = None
    document_id: str | None = None
    l1_status: str | None = None          # batch_invoice_items.status
    error_message: str | None = None
    processing_status: str | None = None  # extracted_invoices.processing_status
    # Settlement outcome (Layer 2 -> Layer 5 committed)
    utr_number: str | None = None
    razorpay_payout_id: str | None = None
    net_settled_amount_paise: int | None = None
    # Extracted invoice net (grand_total - tds) — present once Layer 1 validated
    net_paise: int | None = None
    reconciled_at: datetime | None = None
    # Exception desk linkage (flagged via exception_tickets)
    exception_reason: str | None = None
    # Live telemetry (Redis) — null when no events were captured
    path: str | None = None               # fast_path | agent | deterministic_fallback
    llm_invoked: bool | None = None
    tool_calls: list[str] | None = None


class TelemetryFunnel(BaseModel):
    total: int
    settled: int
    exceptions: int
    open: int
    fast_path: int | None = None      # LLM-free settlements (Redis-derived)
    agent_routed: int | None = None   # ReAct-agent invoices (Redis-derived)


class BatchTelemetryResponse(BaseModel):
    batch_id: str
    vendor_code: str
    source_type: str | None = None
    filename: str | None = None
    status: str
    total_invoices: int
    processed_count: int
    failed_count: int
    created_at: datetime
    completed_at: datetime | None = None
    layer2: Layer2RunSummary | None = None
    funnel: TelemetryFunnel
    invoices: list[InvoiceTelemetryItem]


class TelemetryEventsResponse(BaseModel):
    batch_id: str
    total: int
    events: list[dict[str, Any]]


# =============================================================================
# Immutable ledger view
# =============================================================================


class LedgerEntryLine(BaseModel):
    entry_type: str        # DEBIT | CREDIT
    account_type: str      # LIABILITY | ASSET | ...
    account_name: str
    amount_paise: int
    cleared_invoice_ids: list[str]
    created_at: datetime


class LedgerBatchView(BaseModel):
    batch_id: str
    idempotency_event_id: str
    vendor_code: str
    utr_number: str
    razorpay_payout_id: str | None = None
    total_reconciled_amount_paise: int
    matched_invoice_ids: list[str]
    created_at: datetime
    entries: list[LedgerEntryLine]
    # Double-entry proof: total DR - total CR across this batch's lines.
    imbalance_paise: int


class LedgerEntriesResponse(BaseModel):
    vendor_code: str
    total: int
    items: list[LedgerBatchView]


# =============================================================================
# Latest batch (top-nav rehydration after a fresh sign-in)
# =============================================================================


class LatestBatchResponse(BaseModel):
    batch_id: str
    status: str
    total_invoices: int
    created_at: datetime
    completed_at: datetime | None = None
