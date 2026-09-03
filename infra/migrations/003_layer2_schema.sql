-- Migration 003: Layer 2 — Reconciliation Input Streams & Per-Invoice Outcomes
-- Adds tables for deterministic Layer 2 matching (Milestone 1).
-- NOTE: Existing Core-logic tables (ledger_entries, reconciliation_batches,
-- exception_tickets, idempotency_keys, outbox_events) are NOT modified.
-- ledger_entries / reconciliation_batches remain exclusively owned by the
-- Layer 5 Ledger Writer milestone.

-- Stream 2: Razorpay Payout/Settlement entities (materialized via POST /webhooks/razorpay)
CREATE TABLE razorpay_settlements (
    settlement_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    payout_id TEXT NOT NULL UNIQUE,          -- Razorpay payout "id" (e.g. pout_...)
    fund_account_id TEXT,
    amount_paise BIGINT NOT NULL,
    currency TEXT NOT NULL DEFAULT 'INR',
    status TEXT NOT NULL,
    utr TEXT,                                -- populated once the payout clears (links to bank_transactions)
    reference_id TEXT,                       -- internal invoice reference (INV-...)
    narration TEXT,
    fees_paise BIGINT NOT NULL DEFAULT 0,    -- Razorpay MDR fee (integer paise)
    tax_paise BIGINT NOT NULL DEFAULT 0,     -- GST on the fee (integer paise)
    mode TEXT,
    purpose TEXT,
    vendor_code TEXT NOT NULL,
    event_created_at_epoch BIGINT,           -- Razorpay "created_at" unix epoch
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT razorpay_amount_non_negative CHECK (amount_paise >= 0),
    CONSTRAINT razorpay_fees_non_negative CHECK (fees_paise >= 0),
    CONSTRAINT razorpay_tax_non_negative CHECK (tax_paise >= 0)
);

-- Stream 3: Bank statement records (materialized via POST /ingestion/bank)
CREATE TABLE bank_transactions (
    transaction_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    transaction_date DATE NOT NULL,
    narration TEXT NOT NULL,
    utr_number TEXT,                         -- NPCI UTR — the link to razorpay_settlements.utr
    transaction_type TEXT NOT NULL CHECK (transaction_type IN ('CREDIT', 'DEBIT')),
    amount_paise BIGINT NOT NULL,
    closing_balance_paise BIGINT,
    vendor_code TEXT NOT NULL,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT bank_amount_non_negative CHECK (amount_paise >= 0),
    CONSTRAINT bank_balance_non_negative CHECK (closing_balance_paise IS NULL OR closing_balance_paise >= 0),
    -- Webhook/SFTP idempotency: identical re-delivery is treated as a duplicate
    CONSTRAINT bank_transaction_unique
        UNIQUE (utr_number, transaction_date, amount_paise)
);

-- Layer 2 per-invoice reconciled outcome (written atomically by post_ledger_entry_tool
-- together with its outbox_events row). One row per extracted invoice; UNIQUE
-- (document_id) is the exactly-once guard that prevents double-crediting an invoice.
CREATE TABLE invoice_reconciliations (
    reconciliation_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    idempotency_event_id TEXT NOT NULL UNIQUE,   -- outbox event_id (exactly-once)
    batch_id UUID,                               -- Layer 1 batch_jobs.batch_id (audit linkage)
    document_id UUID NOT NULL UNIQUE REFERENCES extracted_invoices(document_id),
    invoice_number TEXT NOT NULL,
    vendor_code TEXT NOT NULL,
    utr_number TEXT NOT NULL,                    -- bank UTR that cleared the money
    razorpay_payout_id TEXT,
    net_settled_amount_paise BIGINT NOT NULL,
    reconciled_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT recon_amount_non_negative CHECK (net_settled_amount_paise >= 0)
);

-- Indexes
CREATE INDEX razorpay_settlements_vendor_status_idx
    ON razorpay_settlements (vendor_code, status);

CREATE INDEX razorpay_settlements_utr_idx
    ON razorpay_settlements (utr);

CREATE INDEX razorpay_settlements_reference_idx
    ON razorpay_settlements (reference_id);

CREATE INDEX bank_transactions_vendor_date_idx
    ON bank_transactions (vendor_code, transaction_date);

CREATE INDEX bank_transactions_utr_idx
    ON bank_transactions (utr_number);

CREATE INDEX invoice_reconciliations_vendor_idx
    ON invoice_reconciliations (vendor_code, reconciled_at);

CREATE INDEX invoice_reconciliations_utr_idx
    ON invoice_reconciliations (utr_number);

-- WORM (append-only) trigger: a reconciled outcome is immutable once committed.
-- Human corrections flow through the exception/resolution path, never UPDATE.
CREATE TRIGGER invoice_reconciliations_prevent_update
BEFORE UPDATE OR DELETE ON invoice_reconciliations
FOR EACH ROW
EXECUTE FUNCTION prevent_immutable_table_mutation();
