"""Layer 2 — Batch Boundary Poller helpers (deterministic, DB-driven).

Implements the "seal" semantics used by Thread B of recon-supervisor:

- A batch becomes reconcilable ONLY when batch_jobs.status = 'COMPLETED'
  (Layer 1's own atomic fan-in already guarantees every extracted event was
  published BEFORE the final worker flipped the status — zero Layer 1 changes).
- layer2_batch_runs is the durable, crash-safe launch marker: claim via
  INSERT ... ON CONFLICT DO NOTHING, so two pollers can never double-launch.
- After the run finishes, the same row is closed with matched/exception counts
  (COMPLETED when everything was matched or routed; PARTIAL when a shortfall
  occurred or a sub-graph error surfaced).
"""

import json
import logging

from sqlalchemy import text

from app.db.session import SessionLocal

logger = logging.getLogger(__name__)

# batch_jobs statuses that seal a Layer 1 batch for reconciliation
SEALING_STATUS = "COMPLETED"

# run row states
SEALED = "SEALED"
RUNNING = "RUNNING"
COMPLETED = "COMPLETED"
PARTIAL = "PARTIAL"

_CLAIM_RUN_SQL = """
    INSERT INTO layer2_batch_runs (batch_id, vendor_code, run_type, status, total_extracted)
    VALUES (:batch_id, :vendor_code, :run_type, 'SEALED', :total_extracted)
    ON CONFLICT (batch_id) DO NOTHING
    RETURNING batch_id
"""

_START_RUN_SQL = """
    UPDATE layer2_batch_runs
    SET status = 'RUNNING', started_at = now()
    WHERE batch_id = :batch_id AND status IN ('SEALED', 'RUNNING')
"""

_CLOSE_RUN_SQL = """
    UPDATE layer2_batch_runs
    SET status = :status,
        matched_count = :matched_count,
        exception_count = :exception_count,
        shortfall = :shortfall,
        last_error = :last_error,
        completed_at = now()
    WHERE batch_id = :batch_id
      AND status IN ('SEALED', 'RUNNING')
"""

_FIND_RUNNABLE_BATCHES_SQL = """
    SELECT b.batch_id, b.vendor_code, b.total_invoices
    FROM batch_jobs b
    WHERE b.status = 'COMPLETED'
      AND NOT EXISTS (
          SELECT 1 FROM layer2_batch_runs r
          WHERE r.batch_id = b.batch_id::text
      )
    ORDER BY b.completed_at
    LIMIT :limit
"""

_FIND_STALE_RUNS_SQL = """
    SELECT batch_id, vendor_code, run_type
    FROM layer2_batch_runs
    WHERE status IN ('SEALED', 'RUNNING')
      AND (started_at IS NULL OR started_at < now() - interval '15 minutes')
    ORDER BY created_at
    LIMIT :limit
"""

# DB fallback reconstruction: rebuild per-invoice inputs straight from Postgres
# (crash resume / Redis loss) — parses the stored extracted payloads.
_FALLBACK_INVOICES_SQL = """
    SELECT e.document_id::text, e.invoice_number, e.vendor_code, e.parsed_payload
    FROM batch_invoice_items i
    JOIN extracted_invoices e ON e.document_id = i.document_id
    WHERE i.batch_id = :batch_id
      AND e.processing_status IN ('VALIDATED', 'EXCEPTION_FLAGGED')
    ORDER BY i.row_number
"""


# =============================================================================
# Run-row lifecycle (single atomic statements, no SELECT-then-UPDATE races)
# =============================================================================


def claim_run(batch_id: str, vendor_code: str, run_type: str, total: int, db=None) -> bool:
    """Atomically claim a batch/single run. True only for the winning caller."""
    own_db = db is None
    if own_db:
        db = SessionLocal()
    try:
        row = db.execute(
            text(_CLAIM_RUN_SQL),
            {
                "batch_id": batch_id,
                "vendor_code": vendor_code,
                "run_type": run_type,
                "total_extracted": int(total),
            },
        ).first()
        db.commit()
        claimed = row is not None
        logger.info(
            "RUN_CLAIMED" if claimed else "RUN_CLAIM_RACE_LOST",
            extra={"batch_id": batch_id, "vendor_code": vendor_code, "run_type": run_type},
        )
        return claimed
    finally:
        if own_db:
            db.close()


def mark_running(batch_id: str, db=None) -> None:
    own_db = db is None
    if own_db:
        db = SessionLocal()
    try:
        db.execute(text(_START_RUN_SQL), {"batch_id": batch_id})
        db.commit()
        logger.info("RUN_MARKED_RUNNING", extra={"batch_id": batch_id})
    finally:
        if own_db:
            db.close()


def close_run(
    batch_id: str,
    *,
    status: str,
    matched_count: int,
    exception_count: int,
    shortfall: int = 0,
    last_error: str | None = None,
    db=None,
) -> None:
    own_db = db is None
    if own_db:
        db = SessionLocal()
    try:
        db.execute(
            text(_CLOSE_RUN_SQL),
            {
                "batch_id": batch_id,
                "status": status,
                "matched_count": int(matched_count),
                "exception_count": int(exception_count),
                "shortfall": int(shortfall),
                "last_error": last_error,
            },
        )
        db.commit()
    finally:
        if own_db:
            db.close()


# =============================================================================
# Poller queries
# =============================================================================


def find_sealed_batches(limit: int = 5, db=None) -> list[dict]:
    """Layer 1 batches that completed and have no Layer 2 run row yet."""
    own_db = db is None
    if own_db:
        db = SessionLocal()
    try:
        rows = db.execute(
            text(_FIND_RUNNABLE_BATCHES_SQL), {"limit": limit}
        ).all()
        return [
            {"batch_id": str(r[0]), "vendor_code": r[1], "total_invoices": int(r[2])}
            for r in rows
        ]
    finally:
        if own_db:
            db.close()


def find_stale_runs(limit: int = 5, db=None) -> list[dict]:
    """SEALED/RUNNING rows that crashed >15 min ago — resume candidates."""
    own_db = db is None
    if own_db:
        db = SessionLocal()
    try:
        rows = db.execute(text(_FIND_STALE_RUNS_SQL), {"limit": limit}).all()
        return [
            {"batch_id": str(r[0]), "vendor_code": r[1], "run_type": r[2]}
            for r in rows
        ]
    finally:
        if own_db:
            db.close()


def is_batch_sealed(batch_id: str, db=None) -> bool:
    """True when Layer 1 marked the batch COMPLETED."""
    own_db = db is None
    if own_db:
        db = SessionLocal()
    try:
        status = db.execute(
            text("SELECT status FROM batch_jobs WHERE batch_id = :batch_id"),
            {"batch_id": batch_id},
        ).scalar_one_or_none()
        return status == SEALING_STATUS
    finally:
        if own_db:
            db.close()


# =============================================================================
# Input materialization (fan-in payloads per invoice)
# =============================================================================


def build_invoice_inputs_from_db(batch_id: str, db=None) -> list[dict]:
    """Rebuild per-invoice graph inputs from Postgres (crash-safe fallback).

    Each input carries the masked-safe extracted payload (parsed_payload)
    plus the identity anchors the graph needs. PII token masking is applied
    by the supervisor worker before the payload enters graph state.
    """
    own_db = db is None
    if own_db:
        db = SessionLocal()
    try:
        rows = db.execute(text(_FALLBACK_INVOICES_SQL), {"batch_id": batch_id}).all()
    finally:
        if own_db:
            db.close()

    inputs: list[dict] = []
    for r in rows:
        document_id, invoice_number, vendor_code, payload = r[0], r[1], r[2], r[3]
        try:
            payload_dict = json.loads(payload) if isinstance(payload, str) else dict(payload)
        except (TypeError, ValueError):
            payload_dict = {}
        inputs.append(
            {
                "batch_id": str(batch_id),
                "document_id": str(document_id),
                "invoice_number": str(invoice_number),
                "vendor_code": str(vendor_code),
                "payload": payload_dict,
            }
        )
    return inputs
