"""
Shared, pure feed-construction helpers — Stream 2 (Razorpay) + Stream 3 (Bank).

Single source of truth consumed by BOTH feed materialization paths so their
output is byte-identical:

  * POST /demo/auto-generate-feeds   (app.api.demo)      — in-API generation
  * scripts/generate_layer2_feeds.py (offline CLI)       — JSON file writer

Both derive razorpay_settlements + bank_transactions rows from Layer 1's
`extracted_invoices` (integer paise), so amounts always reconcile with what
the VLM actually stored and match the Layer 2 anchor/subset tools.

Per extracted invoice exactly one pair is emitted:
  * one Razorpay settlement: status='processed', reference_id = INV number,
    utr = bank UTR, amount = invoice net (grand_total - tds). The Layer 2
    anchor_node binds reference_id == invoice_number, and its UTR narrows the
    subset search to a single bank credit.
  * one bank CREDIT: utr = same UTR, amount = same net. Phase-1 unique
    subset-sum then matches 1 invoice : 1 credit deterministically.

`build_feeds(..., anomalies=N)` drops the LAST N invoices from both feeds, so
those invoices can never match -> NO_MATCH -> fuzzy -> supervisor -> DLQ
(reconciliation.dlq.events) -> exception_tickets (the Exception Desk path).

Deterministic ids (UTR = 300000000001+idx, payout pout_e2e_%04d) keep retries
idempotent: re-pushing identical rows is absorbed by ON CONFLICT DO NOTHING.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import text

logger = logging.getLogger(__name__)

# Column projection shared by every invoice-fetch below. Kept as one constant
# so the endpoint and the CLI can never drift apart.
_INVOICE_COLUMNS = """
    SELECT e.document_id::text,
           e.invoice_number,
           e.vendor_code,
           e.document_date,
           e.supplier_legal_name,
           (e.grand_total_paise - e.tds_deduction_paise) AS net_paise,
           e.grand_total_paise,
           e.tds_deduction_paise
"""

# Single batch: batch_invoice_items -> extracted_invoices (only reconcilable).
INVOICES_FOR_BATCH_SQL = _INVOICE_COLUMNS + """
    FROM batch_invoice_items i
    JOIN extracted_invoices e ON e.document_id = i.document_id
    WHERE i.batch_id = :bid
      AND e.processing_status = 'VALIDATED'
      AND NOT EXISTS (
          SELECT 1 FROM invoice_reconciliations r
          WHERE r.document_id = e.document_id
      )
    ORDER BY e.invoice_number
"""

# Vendor-wide open invoices (CLI default mode when no --batch-id is given).
ALL_OPEN_INVOICES_SQL = _INVOICE_COLUMNS + """
    FROM extracted_invoices e
    WHERE e.vendor_code = :vc
      AND e.processing_status = 'VALIDATED'
      AND NOT EXISTS (
          SELECT 1 FROM invoice_reconciliations r
          WHERE r.document_id = e.document_id
      )
    ORDER BY e.invoice_number
"""

# Latest COMPLETED batch of a vendor (CLI default when no --batch-id is given).
LATEST_COMPLETED_BATCH_SQL = """
    SELECT batch_id::text
    FROM batch_jobs
    WHERE vendor_code = :vc AND status = 'COMPLETED'
    ORDER BY completed_at DESC NULLS LAST, created_at DESC
    LIMIT 1
"""

# Live batch state, scoped to the owning vendor (demo endpoint gate).
BATCH_STATE_SQL = """
    SELECT b.batch_id::text, b.vendor_code, b.status, b.total_invoices,
           b.processed_count, b.failed_count
    FROM batch_jobs b
    WHERE b.batch_id = :bid AND b.vendor_code = :vc
"""


def _row_to_invoice(r) -> dict:
    """Map a DB row (8 columns) to the canonical invoice dict."""
    net_paise = int(r[5])
    return {
        "document_id": str(r[0]),
        "invoice_number": str(r[1]),
        "vendor_code": str(r[2]),
        "document_date": r[3],
        "supplier_legal_name": str(r[4]),
        "net_paise": net_paise,
        "grand_total_paise": int(r[6]),
        "tds_deduction_paise": int(r[7]),
    }


def fetch_batch_invoices(db, batch_id: str) -> list[dict]:
    """VALIDATED, not-yet-reconciled invoices of one Layer 1 batch.

    Non-reconcilable nets (net_paise <= 0) are skipped — they could never
    match a credit. `db` may be a SQLAlchemy Session or Engine connection.
    """
    rows = db.execute(text(INVOICES_FOR_BATCH_SQL), {"bid": str(batch_id)}).all()
    return [inv for inv in (_row_to_invoice(r) for r in rows) if inv["net_paise"] > 0]


def fetch_all_open_invoices(db, vendor_code: str) -> list[dict]:
    """All VALIDATED, not-yet-reconciled invoices of a vendor."""
    rows = db.execute(text(ALL_OPEN_INVOICES_SQL), {"vc": vendor_code}).all()
    return [inv for inv in (_row_to_invoice(r) for r in rows) if inv["net_paise"] > 0]


def fetch_latest_completed_batch(db, vendor_code: str) -> str | None:
    """Latest COMPLETED batch_id of the vendor (or None)."""
    row = db.execute(
        text(LATEST_COMPLETED_BATCH_SQL), {"vc": vendor_code}
    ).first()
    return str(row[0]) if row is not None else None


def fetch_batch_state(db, batch_id: str, vendor_code: str):
    """(batch_id, vendor_code, status, total_invoices) row or None."""
    return db.execute(
        text(BATCH_STATE_SQL), {"bid": str(batch_id), "vc": vendor_code}
    ).first()


def _bank_date(inv: dict) -> date:
    """Credit lands a couple of days after the invoice date (inside the
    ±7-day phase-3 tolerance window; phase 1 already wins, so this only
    matters if a collision forces chronology)."""
    base = inv.get("document_date") or date(2026, 8, 1)
    return base + timedelta(days=2)


def build_feeds(
    invoices: list[dict],
    anomalies: int,
    scenario: str = "clean",
) -> tuple[list[dict], list[dict]]:
    """Return (razorpay_payloads, bank_payloads) matching the invoices.

    anomalies: drop the LAST N invoices from BOTH feeds so they can never
               match (they surface on the Exception Desk as NO_MATCH).
    scenario:  'clean' (deterministic fast-path) or 'agent-fallback'
               (anomalies >= 5 forced — those cases are forwarded to the
               Groq ReAct agent).
    """
    if scenario not in {"clean", "agent-fallback"}:
        raise ValueError(
            f"Unsupported scenario '{scenario}'. "
            "Use 'clean' or 'agent-fallback'."
        )

    if scenario == "agent-fallback" and anomalies == 0:
        anomalies = 5
    usable = invoices[: len(invoices) - anomalies] if anomalies else invoices

    razorpay: list[dict] = []
    bank: list[dict] = []
    running_balance_paise = 0

    for idx, inv in enumerate(usable, start=1):
        net_paise = inv["net_paise"]
        utr = f"{300000000001 + idx:012d}"
        payout_id = f"pout_e2e_{idx:04d}"
        fund_account_id = f"fa_e2e_{idx:04d}"
        tx_date = _bank_date(inv)
        supplier = inv["supplier_legal_name"]
        invoice_number = inv["invoice_number"]
        epoch = int(
            datetime(tx_date.year, tx_date.month, tx_date.day, tzinfo=timezone.utc).timestamp()
        )

        razorpay.append({
            "payout_id": payout_id,
            "fund_account_id": fund_account_id,
            "amount_paise": net_paise,
            "currency": "INR",
            "status": "processed",  # anchor_node only binds status='processed'
            "utr": utr,
            "reference_id": invoice_number,  # anchor_node binds ref == invoice number
            "narration": f"{supplier.upper()} - PAYOUT {invoice_number}",
            "fees_paise": 0,
            "tax_paise": 0,
            "mode": "IMPS",
            "purpose": "payout",
            "event_created_at_epoch": epoch,
        })

        running_balance_paise += net_paise
        bank.append({
            "transaction_date": tx_date.isoformat(),
            "narration": f"CREDIT/IMPS/{utr}/{supplier.upper()}/{invoice_number}",
            "utr_number": utr,
            "transaction_type": "CREDIT",
            "amount_paise": net_paise,
            "closing_balance_paise": running_balance_paise,
        })

    logger.info(
        "FEEDS_BUILT",
        extra={
            "razorpay": len(razorpay),
            "bank": len(bank),
            "anomalies": anomalies,
            "scenario": scenario,
        },
    )
    return razorpay, bank
