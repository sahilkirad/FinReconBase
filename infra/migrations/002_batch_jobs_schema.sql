-- Migration 002: Batch Jobs Schema
-- Adds tables for bulk invoice processing (PDF batch + CSV upload)

-- Batch job status enum
CREATE TYPE batch_job_status AS ENUM (
    'PENDING',
    'VALIDATING',
    'PROCESSING',
    'COMPLETED',
    'FAILED',
    'PARTIAL'
);

-- Batch invoice item status enum
CREATE TYPE batch_item_status AS ENUM (
    'PENDING',
    'VALIDATING',
    'PROCESSING',
    'VALIDATED',
    'EXCEPTION_FLAGGED',
    'COMPLETED',
    'FAILED'
);

-- Main batch jobs table
CREATE TABLE batch_jobs (
    batch_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    vendor_code TEXT NOT NULL,
    source_type TEXT NOT NULL CHECK (source_type IN ('pdf', 'csv')),
    filename TEXT,
    total_invoices INT NOT NULL DEFAULT 0,
    processed_count INT NOT NULL DEFAULT 0,
    failed_count INT NOT NULL DEFAULT 0,
    status batch_job_status NOT NULL DEFAULT 'PENDING',
    validation_summary JSONB DEFAULT '{}'::jsonb,
    error_summary JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ
);

-- Individual invoice items within a batch
CREATE TABLE batch_invoice_items (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    batch_id UUID NOT NULL REFERENCES batch_jobs(batch_id) ON DELETE CASCADE,
    document_id UUID REFERENCES extracted_invoices(document_id),
    row_number INT,  -- For CSV: row number in file; For PDF: page number
    invoice_number TEXT,
    status batch_item_status NOT NULL DEFAULT 'PENDING',
    error_message TEXT,
    processing_time_ms INT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Indexes for batch queries
CREATE INDEX batch_jobs_vendor_status_idx
    ON batch_jobs (vendor_code, status);

CREATE INDEX batch_jobs_created_at_idx
    ON batch_jobs (created_at DESC);

CREATE INDEX batch_invoice_items_batch_id_idx
    ON batch_invoice_items (batch_id);

CREATE INDEX batch_invoice_items_status_idx
    ON batch_invoice_items (batch_id, status);

-- Auto-update updated_at trigger
CREATE TRIGGER batch_jobs_set_updated_at
BEFORE UPDATE ON batch_jobs
FOR EACH ROW
EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER batch_invoice_items_set_updated_at
BEFORE UPDATE ON batch_invoice_items
FOR EACH ROW
EXECUTE FUNCTION set_updated_at();
