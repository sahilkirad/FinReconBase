"""
Tool 4: post_ledger_entry_tool — Atomic Per-Invoice Commit (Outbox Pattern)

The ONLY terminal-success write path. Executes ONE PostgreSQL transaction:

    1. INSERT INTO invoice_reconciliations   (per matched invoice: the final
       reconciled financial record — matched UTR, net amount, vendor ID)
    2. INSERT INTO outbox_events             (topic = reconciliation.completed.events)

It NEVER publishes directly to Kafka. A separate process (the outbox poller)
reads the PENDING row and publishes it — exactly-once delivery.

Guardrails:
- PREREQUISITE_FAILED: invoked without a SUBSET_MATCHED proof from the
  subset-sum engine (state guardrail — math must run first), or when the
  reconciled envelope is internally inconsistent (amount mismatch).
- DUPLICATE_EVENT: idempotency guard. invoice_reconciliations.document_id is
  UNIQUE and idempotency_event_id is UNIQUE; a second commit attempt for the
  same invoice (LLM double-call / Kafka replay) hits IntegrityError and the
  entire transaction rolls back — an invoice can never be credited twice.
"""

import json
import logging

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.agent.tools.common import (
    OUTBOX_INSERT_SQL,
    RECONCILIATION_COMPLETED_TOPIC,
    build_cloud_event,
    new_event_id,
    paise_to_rupees,
    rupees_to_paise,
)
from app.schemas.layer2_tools import (
    LedgerStatus,
    PostLedgerInput,
    PostLedgerResult,
    SubsetSumResult,
    SubsetSumStatus,
)

logger = logging.getLogger(__name__)

_FETCH_INVOICES_SQL = """
    SELECT e.document_id::text, e.invoice_number,
           (e.grand_total_paise - e.tds_deduction_paise) AS net_paise
    FROM extracted_invoices e
    WHERE e.vendor_code = :vendor_code
      AND e.invoice_number IN ({placeholders})
    ORDER BY e.invoice_number
"""

_INSERT_RECONCILIATION_SQL = """
    INSERT INTO invoice_reconciliations (
        idempotency_event_id, batch_id, document_id, invoice_number,
        vendor_code, utr_number, razorpay_payout_id,
        net_settled_amount_paise
    ) VALUES (
        :idempotency_event_id, :batch_id, :document_id, :invoice_number,
        :vendor_code, :utr_number, :razorpay_payout_id,
        :net_settled_amount_paise
    )
"""


def post_ledger_entry(
    db: Session,
    *,
    inp: PostLedgerInput,
    proof: SubsetSumResult,
    batch_id: str | None = None,
) -> PostLedgerResult:
    """Atomically persist the per-invoice reconciled record + outbox event."""
    if proof is None or proof.status != SubsetSumStatus.SUBSET_MATCHED:
        return PostLedgerResult(
            status=LedgerStatus.PREREQUISITE_FAILED,
            message="State guardrail: run_subset_sum_matching_tool must return SUBSET_MATCHED before committing.",
        )

    if set(inp.matched_invoice_ids) != set(proof.matched_invoice_ids):
        return PostLedgerResult(
            status=LedgerStatus.PREREQUISITE_FAILED,
            message="Envelope mismatch: matched invoices differ from the subset-sum proof.",
        )

    try:
        total_paise = rupees_to_paise(inp.total_reconciled_amount)
    except ValueError:
        return PostLedgerResult(
            status=LedgerStatus.PREREQUISITE_FAILED,
            message="INVALID_PAISE_CASTING: total_reconciled_amount is not strict decimal rupees.",
        )

    if total_paise != proof.net_total_paise:
        return PostLedgerResult(
            status=LedgerStatus.PREREQUISITE_FAILED,
            message=(
                "AMOUNT_MISMATCH: reconciled total must equal the subset-sum "
                "proof net total before the ledger is committed."
            ),
        )

    # Materialize the invoice rows the proof actually matched.
    invoice_numbers = sorted(set(inp.matched_invoice_ids))
    placeholders = ", ".join(f":inv_{i}" for i in range(len(invoice_numbers)))
    fetch_params: dict = {"vendor_code": inp.vendor_code}
    fetch_params.update({f"inv_{i}": num for i, num in enumerate(invoice_numbers)})
    rows = db.execute(
        text(_FETCH_INVOICES_SQL.format(placeholders=placeholders)),
        fetch_params,
    ).all()
    if len(rows) != len(inp.matched_invoice_ids):
        return PostLedgerResult(
            status=LedgerStatus.PREREQUISITE_FAILED,
            message="Some matched invoices are not VALIDATED records of this vendor.",
        )

    invoice_nets = {str(r[1]): int(r[2]) for r in rows}
    if sum(invoice_nets.values()) != proof.net_total_paise:
        return PostLedgerResult(
            status=LedgerStatus.PREREQUISITE_FAILED,
            message="AMOUNT_MISMATCH: invoice net totals disagree with the subset-sum proof.",
        )

    # ---- Single atomic transaction: reconciliations + outbox events ----
    reconciliation_ids: list[str] = []
    outbox_event_ids: list[str] = []

    try:
        for invoice_number, net_paise in sorted(invoice_nets.items()):
            row = next(r for r in rows if str(r[1]) == invoice_number)
            document_id = str(row[0])
            event_id = new_event_id()

            db.execute(
                text(_INSERT_RECONCILIATION_SQL),
                {
                    "idempotency_event_id": event_id,
                    "batch_id": batch_id,
                    "document_id": document_id,
                    "invoice_number": invoice_number,
                    "vendor_code": inp.vendor_code,
                    "utr_number": inp.bank_utr_number,
                    "razorpay_payout_id": inp.razorpay_payout_id,
                    "net_settled_amount_paise": net_paise,
                },
            )

            payload = build_cloud_event(
                event_type="invoice.reconciled",
                source="/layer2/agent",
                data={
                    "vendor_code": inp.vendor_code,
                    "matched_invoices": [invoice_number],
                    "razorpay_payout_id": inp.razorpay_payout_id,
                    "bank_utr_number": inp.bank_utr_number,
                    "total_reconciled_amount": paise_to_rupees(net_paise),
                },
            )

            db.execute(
                text(OUTBOX_INSERT_SQL),
                {
                    "event_id": event_id,
                    "aggregate_type": "InvoiceReconciliation",
                    "aggregate_id": document_id,
                    "topic": RECONCILIATION_COMPLETED_TOPIC,
                    "partition_key": inp.vendor_code,
                    "event_type": "InvoiceReconciled",
                    "payload": json.dumps(payload),
                },
            )

            reconciliation_ids.append(event_id)  # idempotency key == outbox event id
            outbox_event_ids.append(event_id)

        db.commit()
        logger.info(
            "Ledger entry committed",
            extra={
                "vendor_code": inp.vendor_code,
                "utr": inp.bank_utr_number,
                "invoices": len(outbox_event_ids),
            },
        )
        return PostLedgerResult(
            status=LedgerStatus.LEDGER_COMMITTED,
            reconciliation_ids=reconciliation_ids,
            outbox_event_ids=outbox_event_ids,
            message="Reconciled records + outbox events committed atomically.",
        )

    except IntegrityError:
        db.rollback()
        logger.warning("Duplicate ledger commit rejected (rollback)", exc_info=True)
        return PostLedgerResult(
            status=LedgerStatus.DUPLICATE_EVENT,
            message="Idempotency guard: this invoice/event is already reconciled. Commit rolled back.",
        )
    except Exception:
        db.rollback()
        logger.error("Ledger commit failed (rollback)", exc_info=True)
        raise
