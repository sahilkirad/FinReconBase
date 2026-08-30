CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS citext;

CREATE TYPE vendor_user_role AS ENUM ('ADMIN', 'VIEWER');

CREATE TYPE invoice_processing_status AS ENUM (
    'PENDING',
    'VALIDATED',
    'EXCEPTION_FLAGGED',
    'PUBLISHED'
);

CREATE TYPE outbox_status AS ENUM (
    'PENDING',
    'PUBLISHING',
    'PUBLISHED',
    'FAILED'
);

CREATE TYPE ledger_entry_type AS ENUM ('DEBIT', 'CREDIT');

CREATE TYPE ledger_account_type AS ENUM (
    'ASSET',
    'LIABILITY',
    'EXPENSE',
    'REVENUE',
    'EQUITY'
);

CREATE TYPE exception_ticket_status AS ENUM (
    'OPEN',
    'IN_REVIEW',
    'RESOLVED',
    'CLOSED'
);

CREATE TABLE vendor_users (
    user_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email CITEXT NOT NULL UNIQUE,
    google_subject_id TEXT NOT NULL UNIQUE,
    vendor_code TEXT NOT NULL,
    role vendor_user_role NOT NULL DEFAULT 'VIEWER',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE extracted_invoices (
    document_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    irn TEXT UNIQUE,
    vendor_code TEXT NOT NULL,
    invoice_number TEXT NOT NULL,
    document_type_code TEXT NOT NULL,
    po_number TEXT,
    document_date DATE,
    due_date DATE,
    supplier_legal_name TEXT NOT NULL,
    supplier_gstin TEXT,
    supplier_pan TEXT,
    buyer_legal_name TEXT,
    buyer_gstin TEXT,
    currency_code TEXT NOT NULL DEFAULT 'INR',
    grand_total_paise BIGINT NOT NULL,
    tds_deduction_paise BIGINT NOT NULL DEFAULT 0,
    processing_status invoice_processing_status NOT NULL DEFAULT 'PENDING',
    parsed_payload JSONB NOT NULL,
    validation_errors JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    published_at TIMESTAMPTZ,
    CONSTRAINT extracted_invoices_vendor_invoice_unique
        UNIQUE (vendor_code, invoice_number),
    CONSTRAINT extracted_invoices_grand_total_non_negative
        CHECK (grand_total_paise >= 0),
    CONSTRAINT extracted_invoices_tds_non_negative
        CHECK (tds_deduction_paise >= 0),
    CONSTRAINT extracted_invoices_payload_object
        CHECK (jsonb_typeof(parsed_payload) = 'object')
);

CREATE TABLE langgraph_checkpoints (
    thread_id TEXT NOT NULL,
    checkpoint_ns TEXT NOT NULL DEFAULT '',
    checkpoint_id TEXT NOT NULL,
    type TEXT,
    checkpoint BYTEA NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
);

CREATE TABLE outbox_events (
    outbox_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_id TEXT NOT NULL UNIQUE,
    aggregate_type TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    topic TEXT NOT NULL,
    partition_key TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload JSONB NOT NULL,
    status outbox_status NOT NULL DEFAULT 'PENDING',
    retry_count INTEGER NOT NULL DEFAULT 0,
    max_retries INTEGER NOT NULL DEFAULT 3,
    last_error TEXT,
    available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    published_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT outbox_retry_count_check CHECK (retry_count >= 0),
    CONSTRAINT outbox_max_retries_check CHECK (max_retries > 0),
    CONSTRAINT outbox_payload_object CHECK (jsonb_typeof(payload) = 'object')
);

CREATE TABLE idempotency_keys (
    event_id TEXT PRIMARY KEY,
    source_topic TEXT NOT NULL,
    consumer_name TEXT NOT NULL,
    processed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE reconciliation_batches (
    batch_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    idempotency_event_id TEXT NOT NULL UNIQUE,
    vendor_code TEXT NOT NULL,
    utr_number TEXT NOT NULL,
    razorpay_payout_id TEXT,
    total_reconciled_amount_paise BIGINT NOT NULL,
    matched_invoice_ids TEXT[] NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT reconciliation_amount_positive
        CHECK (total_reconciled_amount_paise >= 0),
    CONSTRAINT reconciliation_has_invoices
        CHECK (array_length(matched_invoice_ids, 1) IS NOT NULL)
);

CREATE TABLE ledger_entries (
    entry_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    batch_id UUID NOT NULL REFERENCES reconciliation_batches(batch_id),
    account_type ledger_account_type NOT NULL,
    account_name TEXT NOT NULL,
    entry_type ledger_entry_type NOT NULL,
    amount_paise BIGINT NOT NULL,
    cleared_invoice_ids TEXT[] NOT NULL,
    utr_number TEXT NOT NULL,
    vendor_code TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ledger_amount_non_negative CHECK (amount_paise >= 0),
    CONSTRAINT ledger_has_cleared_invoices
        CHECK (array_length(cleared_invoice_ids, 1) IS NOT NULL)
);

CREATE TABLE exception_tickets (
    ticket_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    vendor_code TEXT NOT NULL,
    source_topic TEXT NOT NULL,
    source_event_id TEXT,
    bank_utr_number TEXT,
    flagged_invoice_ids TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    exception_reason TEXT NOT NULL,
    variance_delta_paise BIGINT,
    human_readable_message TEXT NOT NULL,
    flagged_payload JSONB NOT NULL,
    status exception_ticket_status NOT NULL DEFAULT 'OPEN',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at TIMESTAMPTZ,
    resolved_by UUID REFERENCES vendor_users(user_id),
    CONSTRAINT exception_payload_object
        CHECK (jsonb_typeof(flagged_payload) = 'object')
);

CREATE INDEX vendor_users_vendor_code_idx
    ON vendor_users (vendor_code);

CREATE INDEX extracted_invoices_vendor_status_idx
    ON extracted_invoices (vendor_code, processing_status);

CREATE INDEX extracted_invoices_invoice_number_idx
    ON extracted_invoices (invoice_number);

CREATE INDEX extracted_invoices_irn_idx
    ON extracted_invoices (irn);

CREATE INDEX extracted_invoices_parsed_payload_gin_idx
    ON extracted_invoices USING GIN (parsed_payload);

CREATE INDEX outbox_events_pending_idx
    ON outbox_events (status, available_at)
    WHERE status IN ('PENDING', 'FAILED');

CREATE INDEX outbox_events_topic_idx
    ON outbox_events (topic, status);

CREATE INDEX idempotency_consumer_idx
    ON idempotency_keys (consumer_name, processed_at);

CREATE INDEX reconciliation_batches_vendor_idx
    ON reconciliation_batches (vendor_code, created_at);

CREATE INDEX reconciliation_batches_utr_idx
    ON reconciliation_batches (utr_number);

CREATE INDEX ledger_entries_batch_idx
    ON ledger_entries (batch_id);

CREATE INDEX ledger_entries_vendor_idx
    ON ledger_entries (vendor_code, created_at);

CREATE INDEX exception_tickets_vendor_status_idx
    ON exception_tickets (vendor_code, status, created_at);

CREATE INDEX exception_tickets_flagged_payload_gin_idx
    ON exception_tickets USING GIN (flagged_payload);

CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER outbox_events_set_updated_at
BEFORE UPDATE ON outbox_events
FOR EACH ROW
EXECUTE FUNCTION set_updated_at();

CREATE OR REPLACE FUNCTION prevent_immutable_table_mutation()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'Table % is append-only and cannot be updated or deleted', TG_TABLE_NAME;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER reconciliation_batches_prevent_update
BEFORE UPDATE OR DELETE ON reconciliation_batches
FOR EACH ROW
EXECUTE FUNCTION prevent_immutable_table_mutation();

CREATE TRIGGER ledger_entries_prevent_update
BEFORE UPDATE OR DELETE ON ledger_entries
FOR EACH ROW
EXECUTE FUNCTION prevent_immutable_table_mutation();