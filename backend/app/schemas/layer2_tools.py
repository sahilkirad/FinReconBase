"""
Layer 2 — Deterministic Tool Contracts (Pydantic)

Strict typed schemas for the 5 deterministic reconciliation tools.
These are the enforcement layer: the LLM (Milestone 2) cannot invoke a tool
unless its arguments validate against these BaseModels, and every tool returns
one of these structured output models — never free-form text.

Toolbelt (5 tools):
1. run_fuzzy_text_linker_tool      — Token Set Ratio + phonetic entity resolution
2. calculate_tds_mdr_tool          — Gross-to-Net statutory waterfall (integer paise)
3. run_subset_sum_matching_tool    — Strict 3-phase amount/entity/chronology match
4. post_ledger_entry_tool          — Atomic invoice_reconciliations + outbox write
5. route_to_human_exception        — Outbox DLQ routing (reconciliation.dlq.events)

All monetary fields are rupee decimal strings (e.g. "104400.00") on input and
integer paise internally.
"""

from enum import Enum

from pydantic import BaseModel, Field, field_validator

# =============================================================================
# Shared value objects / enums
# =============================================================================


class FuzzyLinkStatus(str, Enum):
    ENTITY_RESOLVED = "ENTITY_RESOLVED"
    ENTITY_MISMATCH = "ENTITY_MISMATCH"


class WaterfallStatus(str, Enum):
    WATERFALL_CALCULATED = "WATERFALL_CALCULATED"
    INVALID_PAISE_CASTING = "INVALID_PAISE_CASTING"
    NEGATIVE_NET_SETTLEMENT = "NEGATIVE_NET_SETTLEMENT"


class SubsetSumStatus(str, Enum):
    SUBSET_MATCHED = "SUBSET_MATCHED"
    AMBIGUOUS_COLLISION = "AMBIGUOUS_COLLISION"
    NO_MATCH = "NO_MATCH"


class LedgerStatus(str, Enum):
    LEDGER_COMMITTED = "LEDGER_COMMITTED"
    DUPLICATE_EVENT = "DUPLICATE_EVENT"
    PREREQUISITE_FAILED = "PREREQUISITE_FAILED"


class ExceptionStatus(str, Enum):
    EXCEPTION_LOGGED = "EXCEPTION_LOGGED"


# =============================================================================
# 1. run_fuzzy_text_linker_tool
# =============================================================================


class FuzzyLinkerInput(BaseModel):
    """Arguments the Supervisor passes when resolving an entity name."""

    source_entity_name: str = Field(..., min_length=1, description="supplier_details.legal_name from the extracted invoice")
    target_bank_narration: str = Field(..., min_length=1, description="Razorpay 'narration' or bank statement narration")
    context_vendor_code: str = Field(..., min_length=1, description="supplier_details.vendor_code (account scoping)")
    match_threshold: float = Field(default=0.85, ge=0.0, le=1.0)


class FuzzyDiagnosticTrace(BaseModel):
    normalized_source: str
    normalized_target: str
    phonetic_match: bool = False


class FuzzyLinkerResult(BaseModel):
    """Deterministic output handed back to the agent."""

    status: FuzzyLinkStatus
    confidence_score: float = Field(ge=0.0, le=1.0)
    resolved_vendor_code: str | None = None
    diagnostic_trace: FuzzyDiagnosticTrace


# =============================================================================
# 2. calculate_tds_mdr_tool
# =============================================================================


class TdsMdrInput(BaseModel):
    """Strict typed arguments for the gross-to-net waterfall."""

    invoice_id: str = Field(..., min_length=1)
    grand_total_rupees: str = Field(..., description='Decimal-encoded rupees, e.g. "104400.00"')
    tds_deducted_rupees: str = Field(..., description='Decimal-encoded rupees, e.g. "1800.00"')
    tds_category_code: str = Field(..., min_length=1, description='e.g. "194C", "194J"')
    gateway_fees_paise: int = Field(default=0, ge=0, description="Razorpay MDR fee (integer paise); 0 for direct bank transfer")
    gateway_tax_paise: int = Field(default=0, ge=0, description="GST on the Razorpay fee (integer paise)")

    @field_validator("grand_total_rupees", "tds_deducted_rupees")
    @classmethod
    def _validate_rupees_string(cls, v: str) -> str:
        # Strict: digits with optional up-to-2-decimal fraction. No commas.
        import re

        if not re.fullmatch(r"\d+(\.\d{1,2})?", v):
            raise ValueError(f"Invalid decimal rupees string: {v!r}")
        return v


class WaterfallDeductionBreakdown(BaseModel):
    total_tds_rupees: str
    total_gateway_deductions_rupees: str


class TdsMdrResult(BaseModel):
    """Output of the waterfall. status != WATERFALL_CALCULATED means the record
    must NOT enter the reconciliation pool (blocked with the reason in status)."""

    status: WaterfallStatus
    invoice_id: str
    net_expected_settlement: str | None = None
    deduction_breakdown: WaterfallDeductionBreakdown | None = None
    flags: list[str] = Field(default_factory=list)
    message: str = ""


# =============================================================================
# 3. run_subset_sum_matching_tool
# =============================================================================


class SubsetSumInput(BaseModel):
    """Arguments for the strict 3-phase waterfall (DB-driven)."""

    vendor_code: str = Field(..., min_length=1)
    target_amount_paise: int = Field(..., ge=0, description="Bank credit amount (integer paise) to reconcile")
    bank_utr_number: str | None = Field(default=None, description="Candidate bank UTR; None = scan unmatched credits")
    date_tolerance_days: int = Field(default=7, ge=1, le=365)


class SubsetSumResult(BaseModel):
    """MATCHED_UTR_xxx or AMBIGUOUS_COLLISION — never a guess."""

    status: SubsetSumStatus
    matched_invoice_ids: list[str] = Field(default_factory=list)
    matched_bank_utr: str | None = None
    bank_transaction_date: str | None = None
    phase_applied: int | None = Field(default=None, ge=1, le=3, description="Which waterfall phase resolved the match")
    net_total_paise: int | None = None
    message: str = ""


# =============================================================================
# 4. post_ledger_entry_tool
# =============================================================================


class PostLedgerInput(BaseModel):
    """Per-invoice atomic commit arguments (doc Tool-4 style envelope data)."""

    vendor_code: str = Field(..., min_length=1)
    matched_invoice_ids: list[str] = Field(default_factory=list, description="Invoice numbers (INV-...) matched by the subset-sum engine")
    razorpay_payout_id: str | None = None
    bank_utr_number: str = Field(..., min_length=1)
    total_reconciled_amount: str = Field(..., description='Decimal-encoded rupees, e.g. "315400.00"')

    @field_validator("total_reconciled_amount")
    @classmethod
    def _validate_rupees_string(cls, v: str) -> str:
        import re

        if not re.fullmatch(r"\d+(\.\d{1,2})?", v):
            raise ValueError(f"Invalid decimal rupees string: {v!r}")
        return v


class PostLedgerResult(BaseModel):
    status: LedgerStatus
    reconciliation_ids: list[str] = Field(default_factory=list)
    outbox_event_ids: list[str] = Field(default_factory=list)
    message: str = ""


# =============================================================================
# 5. route_to_human_exception
# =============================================================================


class HumanExceptionInput(BaseModel):
    vendor_code: str = Field(..., min_length=1)
    flagged_invoice_ids: list[str] = Field(default_factory=list)
    bank_utr_number: str | None = None
    exception_reason: str | None = Field(default=None, description="REASON_UNSPECIFIED injected when omitted")
    variance_delta: str | None = Field(default=None, description='Optional decimal-encoded rupees, e.g. "500.00"')
    human_readable_message: str = Field(..., min_length=1)


class HumanExceptionResult(BaseModel):
    status: ExceptionStatus
    kafka_topic: str = "reconciliation.dlq.events"
    outbox_event_id: str
    exception_reason: str
    action_required: str = "Human review required on Auditor Dashboard."


# =============================================================================
# Ingestion payload contracts (Streams 2 & 3)
# =============================================================================


class RazorpaySettlementPayload(BaseModel):
    """Body for POST /webhooks/razorpay — mirrors the approved razorpay_settlements DDL."""

    payout_id: str = Field(..., min_length=1)
    fund_account_id: str | None = None
    amount_paise: int = Field(..., ge=0)
    currency: str = Field(default="INR")
    status: str = Field(..., min_length=1)
    utr: str | None = None
    reference_id: str | None = None
    narration: str | None = None
    fees_paise: int = Field(default=0, ge=0)
    tax_paise: int = Field(default=0, ge=0)
    mode: str | None = None
    purpose: str | None = None
    event_created_at_epoch: int | None = None


class BankTransactionPayload(BaseModel):
    """One record for POST /ingestion/bank — mirrors the approved bank_transactions DDL."""

    transaction_date: str = Field(..., description="YYYY-MM-DD")
    narration: str = Field(..., min_length=1)
    utr_number: str | None = None
    transaction_type: str = Field(..., pattern="^(CREDIT|DEBIT)$")
    amount_paise: int = Field(..., ge=0)
    closing_balance_paise: int | None = Field(default=None, ge=0)
