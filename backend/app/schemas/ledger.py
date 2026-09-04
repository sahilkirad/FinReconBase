"""
Layer 5 — Ledger Writer Contracts (Pydantic)

Strict typed contracts for the Layer 5 Ledger Writer microservice:

- CompletedEventEnvelope : CloudEvents envelope published by Layer 2
  (reconciliation.completed.events)
- ParsedLedgerRecord      : validated, normalized record after parsing
  (all amounts cast to integer paise — floating point is forbidden)
- LedgerCommitResult      : outcome of the ACID double-entry commit

The ORPHANED_DEBIT_CREDIT_MISMATCH guardrail is enforced by build_double_entry()
in app.ledger.writer: the parsed record may carry explicit debit/credit amounts
(e.g. a corrupted upstream payload), and any imbalance refuses the commit.
"""

from enum import Enum

from pydantic import BaseModel, Field


class LedgerWriteStatus(str, Enum):
    COMMITTED = "COMMITTED"
    DUPLICATE_EVENT = "DUPLICATE_EVENT"


class FatalDlqReason(str, Enum):
    ORPHANED_DEBIT_CREDIT_MISMATCH = "ORPHANED_DEBIT_CREDIT_MISMATCH"
    MALFORMED_PAYLOAD = "MALFORMED_PAYLOAD"
    INVALID_PAISE_CASTING = "INVALID_PAISE_CASTING"


class CompletedEventEnvelope(BaseModel):
    """CNCF CloudEvents 1.0 envelope (Layer 2 -> reconciliation.completed.events)."""

    specversion: str = "1.0"
    type: str = "invoice.reconciled"
    source: str
    id: str = Field(..., min_length=1, description="event_id = the idempotency key")
    time: str | None = None
    data: dict = Field(default_factory=dict)


class ParsedLedgerRecord(BaseModel):
    """Normalized, validated record ready for the double-entry commit."""

    event_id: str
    vendor_code: str
    matched_invoice_ids: list[str]
    razorpay_payout_id: str | None
    bank_utr_number: str
    debit_amount_paise: int
    credit_amount_paise: int


class LedgerCommitResult(BaseModel):
    status: LedgerWriteStatus
    batch_id: str | None = None
    message: str = ""