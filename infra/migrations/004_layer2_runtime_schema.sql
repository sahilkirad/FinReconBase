-- Migration 004: Layer 2 — LangGraph Runtime Schema
-- Two additive concerns. 001/003 tables are NOT modified.
--
-- 1) PostgresSaver checkpoint tables (pinned langgraph-checkpoint-postgres==3.1.2).
--    The 001 `langgraph_checkpoints` table predates the saver's required schema
--    (missing parent_checkpoint_id / JSONB checkpoint / companion tables), and
--    ALTERing it would violate the no-schema-change rule — so the saver gets its
--    own official tables here. Columns below are the FINAL end-state produced by
--    the library's internal MIGRATIONS chain (index 0..9) at that pinned version.
--    The worker still calls checkpointer.setup() on boot: it is idempotent and
--    reconciles any future library-owned migration.
--
-- 2) layer2_batch_runs — durable seal/close audit record for the batch boundary
--    poller + execution pool (crash-safe launch idempotency and lifecycle close).

-- ============================================================================
-- LangGraph PostgresSaver tables (final schema, pinned 3.1.2)
-- ============================================================================

CREATE TABLE IF NOT EXISTS checkpoint_migrations (
    v INTEGER PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS checkpoints (
    thread_id TEXT NOT NULL,
    checkpoint_ns TEXT NOT NULL DEFAULT '',
    checkpoint_id TEXT NOT NULL,
    parent_checkpoint_id TEXT,
    type TEXT,
    checkpoint JSONB NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}',
    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
);

CREATE TABLE IF NOT EXISTS checkpoint_blobs (
    thread_id TEXT NOT NULL,
    checkpoint_ns TEXT NOT NULL DEFAULT '',
    channel TEXT NOT NULL,
    version TEXT NOT NULL,
    type TEXT NOT NULL,
    blob BYTEA,
    PRIMARY KEY (thread_id, checkpoint_ns, channel, version)
);

CREATE TABLE IF NOT EXISTS checkpoint_writes (
    thread_id TEXT NOT NULL,
    checkpoint_ns TEXT NOT NULL DEFAULT '',
    checkpoint_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    idx INTEGER NOT NULL,
    channel TEXT NOT NULL,
    type TEXT,
    blob BYTEA NOT NULL,
    task_path TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx)
);

CREATE INDEX IF NOT EXISTS checkpoints_thread_id_idx
    ON checkpoints (thread_id);

CREATE INDEX IF NOT EXISTS checkpoint_blobs_thread_id_idx
    ON checkpoint_blobs (thread_id);

CREATE INDEX IF NOT EXISTS checkpoint_writes_thread_id_idx
    ON checkpoint_writes (thread_id);

-- ============================================================================
-- Layer 2 batch run audit (seal / RUNNING / terminal close)
-- ============================================================================
-- batch_id is TEXT (not FK): it holds real batch_jobs.batch_id UUIDs for
-- BATCH runs and implicit "single_{document_id}" keys for single-invoice
-- runs (batch_id = None events from Layer 1). run_type distinguishes them.
CREATE TABLE IF NOT EXISTS layer2_batch_runs (
    batch_id TEXT PRIMARY KEY,
    vendor_code TEXT NOT NULL,
    run_type TEXT NOT NULL CHECK (run_type IN ('BATCH', 'SINGLE')),
    status TEXT NOT NULL DEFAULT 'SEALED'
        CHECK (status IN ('SEALED', 'RUNNING', 'COMPLETED', 'PARTIAL')),
    total_extracted INT NOT NULL DEFAULT 0,
    matched_count INT NOT NULL DEFAULT 0,
    exception_count INT NOT NULL DEFAULT 0,
    shortfall INT NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX layer2_batch_runs_status_idx
    ON layer2_batch_runs (status, created_at);

CREATE INDEX layer2_batch_runs_vendor_idx
    ON layer2_batch_runs (vendor_code, created_at);

-- Auto-update updated_at on progress transitions
CREATE TRIGGER layer2_batch_runs_set_updated_at
BEFORE UPDATE ON layer2_batch_runs
FOR EACH ROW
EXECUTE FUNCTION set_updated_at();
