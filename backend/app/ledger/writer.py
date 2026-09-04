"""
Layer 5 — Ledger Writer core kernels.

Implements the three absolute guardrails from the Core logic LEDGER ENGINE spec:

1. Idempotency Gate — the CloudEvents `id` is inserted into idempotency_keys
   (PRIMARY KEY) as the FIRST statement of the ACID block. A Unique Constraint
   Violation means Kafka redelivered an old event -> the payload is dropped
   (DUPLICATE_EVENT) and the offset is committed.
2. Double-Entry Generation & Balance Guardrail — every event becomes two paired
   rows in ledger_entries (DEBIT Accounts Payable / CREDIT Cash), amounts are
   integer paise, and the hard validation Debit - Credit == 0 runs before any
   commit. An imbalance raises ORPHANED_DEBIT_CREDIT_MISMATCH.
3. ACID Transaction Block — idempotency key + reconciliation_batches header +
   both ledger_entries commit in ONE `with db.begin():` block. Kafka offsets
   are committed manually only after this succeeds.

Fatal guardrail failures are routed to ledger.fatal.dlq.events AND materialized
into exception_tickets (HITL dashboard) — see app.workers.ledger_writer.
"""

import json
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.agent.tools.common import InvalidPaiseStringError, rupees_to_paise
from app.schemas.ledger import (
    CompletedEventEnvelope,
    FatalDlqReason,
    LedgerCommitResult,
    LedgerWriteStatus,
    ParsedLedgerRecord,
)

logger = logging.getLogger(__name__)

LEDGER_FATAL_DLQ_TOPIC = "ledger.fatal.dlq.events"
LEDGER_CONSUMER_NAME = "layer5-ledger-writer"
EXCEPTION_CONSUMER_NAME = "layer5-exception-materializer"
COMPLETED_SOURCE_TOPIC = "reconciliation.completed.events"

# Documented double-entry constants (Core logic, LEDGER ENGINE)
DR_ACCOUNT_TYPE = "LIABILITY"
DR_ACCOUNT_NAME_TMPL = "Accounts Payable - {vendor_code}"
CR_ACCOUNT_TYPE = "ASSET"
CR_ACCOUNT_NAME = "HDFC Corporate Current Account"

_IDEMPOTENCY_INSERT_SQL = """
    INSERT INTO idempotency_keys (event_id, source_topic, consumer_name)
    VALUES (:event_id, :source_topic, :consumer_name)
"""

_HEADER_INSERT_SQL = """
    INSERT INTO reconciliation_batches (
        idempotency_event_id, vendor_code, utr_number, razorpay_payout_id,
        total_reconciled_amount_paise, matched_invoice_ids
    ) VALUES (
        :idempotency_event_id, :vendor_code, :utr_number, :razorpay_payout_id,
        :total_reconciled_amount_paise, :matched_invoice_ids
    )
    RETURNING batch_id
"""

_ENTRY_INSERT_SQL = """
    INSERT INTO ledger_entries (
        batch_id, account_type, account_name, entry_type,
        amount_paise, cleared_invoice_ids, utr_number, vendor_code
    ) VALUES (
        :batch_id, :account_type, :account_name, :entry_type,
        :amount_paise, :cleared_invoice_ids, :utr_number, :vendor_code
    )
"""

_TICKET_INSERT_SQL = """
    INSERT INTO exception_tickets (
        vendor_code, source_topic, source_event_id, bank_utr_number,
        flagged_invoice_ids, exception_reason, variance_delta_paise,
        human_readable_message, flagged_payload
    ) VALUES (
        :vendor_code, :source_topic, :source_event_id, :bank_utr_number,
        :flagged_invoice_ids, :exception_reason, :variance_delta_paise,
        :human_readable_message, :flagged_payload
    )
"""


class LedgerWriteError(Exception):
    """Terminal payload-level error -> ledger.fatal.dlq.events (never retried)."""

    def __init__(self, reason: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.reason = reason
        self.details = details or {}


# =============================================================================
# 1. Parse & normalize (strict, integer paise)
# =============================================================================


def parse_completed_event(raw: dict) -> ParsedLedgerRecord:
    """Validate the CloudEvents envelope into a normalized ledger record.

    Raises LedgerWriteError(MALFORMED_PAYLOAD / INVALID_PAISE_CASTING).
    Explicit `debit_amount` / `credit_amount` override the single
    `total_reconciled_amount` so a corrupted upstream payload that commands
    divergent DR/CR values is caught by the balance guardrail.
    """
    try:
        env = CompletedEventEnvelope(**raw)
    except Exception as e:  # pydantic ValidationError
        raise LedgerWriteError(
            FatalDlqReason.MALFORMED_PAYLOAD,
            f"Envelope schema invalid: {e}",
            {"event_id": raw.get("id")},
        ) from e

    data = env.data or {}
    event_id = env.id
    vendor_code = data.get("vendor_code")
    matched = data.get("matched_invoices")
    bank_utr = data.get("bank_utr_number")
    total = data.get("total_reconciled_amount")

    if not vendor_code or not isinstance(vendor_code, str):
        raise LedgerWriteError(
            FatalDlqReason.MALFORMED_PAYLOAD, "data.vendor_code missing", {"event_id": event_id}
        )
    if not isinstance(matched, list) or not matched or not all(isinstance(x, str) and x for x in matched):
        raise LedgerWriteError(
            FatalDlqReason.MALFORMED_PAYLOAD,
            "data.matched_invoices must be a non-empty string list",
            {"event_id": event_id},
        )
    if not bank_utr or not isinstance(bank_utr, str):
        raise LedgerWriteError(
            FatalDlqReason.MALFORMED_PAYLOAD, "data.bank_utr_number missing", {"event_id": event_id}
        )

    debit_src = data.get("debit_amount", total)
    credit_src = data.get("credit_amount", total)
    if debit_src is None or credit_src is None:
        raise LedgerWriteError(
            FatalDlqReason.MALFORMED_PAYLOAD,
            "data.total_reconciled_amount missing",
            {"event_id": event_id, "vendor_code": vendor_code},
        )

    try:
        debit_paise = rupees_to_paise(str(debit_src))
        credit_paise = rupees_to_paise(str(credit_src))
    except InvalidPaiseStringError as e:
        raise LedgerWriteError(
            FatalDlqReason.INVALID_PAISE_CASTING, str(e), {"event_id": event_id, "vendor_code": vendor_code}
        ) from e

    return ParsedLedgerRecord(
        event_id=event_id,
        vendor_code=vendor_code,
        matched_invoice_ids=list(matched),
        razorpay_payout_id=data.get("razorpay_payout_id"),
        bank_utr_number=bank_utr,
        debit_amount_paise=debit_paise,
        credit_amount_paise=credit_paise,
    )


# =============================================================================
# 2. Double-entry generation & balance guardrail
# =============================================================================


def build_double_entry(record: ParsedLedgerRecord) -> tuple[dict, dict]:
    """Generate the paired DR/CR rows and enforce Debit - Credit == 0."""
    dr = {
        "account_type": DR_ACCOUNT_TYPE,
        "account_name": DR_ACCOUNT_NAME_TMPL.format(vendor_code=record.vendor_code),
        "entry_type": "DEBIT",
        "amount_paise": record.debit_amount_paise,
    }
    cr = {
        "account_type": CR_ACCOUNT_TYPE,
        "account_name": CR_ACCOUNT_NAME,
        "entry_type": "CREDIT",
        "amount_paise": record.credit_amount_paise,
    }

    variance = dr["amount_paise"] - cr["amount_paise"]
    if variance != 0:
        raise LedgerWriteError(
            FatalDlqReason.ORPHANED_DEBIT_CREDIT_MISMATCH,
            (
                f"Unbalanced double-entry for event {record.event_id}: "
                f"Debit {dr['amount_paise']} paise != Credit {cr['amount_paise']} paise"
            ),
            {
                "event_id": record.event_id,
                "vendor_code": record.vendor_code,
                "debit_paise": dr["amount_paise"],
                "credit_paise": cr["amount_paise"],
                "variance_delta_paise": variance,
            },
        )
    return dr, cr


# =============================================================================
# 3. ACID commit (idempotency gate + header + lines)
# =============================================================================


def commit_ledger(
    db: Session,
    *,
    record: ParsedLedgerRecord,
    consumer_name: str = LEDGER_CONSUMER_NAME,
    source_topic: str = COMPLETED_SOURCE_TOPIC,
) -> LedgerCommitResult:
    """Commit header + double-entry lines + idempotency key in ONE transaction.

    Returns DUPLICATE_EVENT when the idempotency PK already exists (Kafka
    redelivery) — the whole transaction is dropped and rolled back.
    Raises LedgerWriteError on guardrail failures (nothing was written).
    """
    dr, cr = build_double_entry(record)  # ORPHANED_DEBIT_CREDIT_MISMATCH gate

    try:
        with db.begin():
            db.execute(
                text(_IDEMPOTENCY_INSERT_SQL),
                {"event_id": record.event_id, "source_topic": source_topic, "consumer_name": consumer_name},
            )
            batch_id = db.execute(
                text(_HEADER_INSERT_SQL),
                {
                    "idempotency_event_id": record.event_id,
                    "vendor_code": record.vendor_code,
                    "utr_number": record.bank_utr_number,
                    "razorpay_payout_id": record.razorpay_payout_id,
                    "total_reconciled_amount_paise": record.debit_amount_paise,
                    "matched_invoice_ids": record.matched_invoice_ids,
                },
            ).scalar_one()
            for entry in (dr, cr):
                db.execute(
                    text(_ENTRY_INSERT_SQL),
                    {
                        "batch_id": str(batch_id),
                        "account_type": entry["account_type"],
                        "account_name": entry["account_name"],
                        "entry_type": entry["entry_type"],
                        "amount_paise": entry["amount_paise"],
                        "cleared_invoice_ids": record.matched_invoice_ids,
                        "utr_number": record.bank_utr_number,
                        "vendor_code": record.vendor_code,
                    },
                )
    except IntegrityError as e:
        db.rollback()
        if "idempotency_keys" in str(e).lower():
            logger.info("Duplicate event dropped", extra={"event_id": record.event_id})
            return LedgerCommitResult(
                status=LedgerWriteStatus.DUPLICATE_EVENT,
                message="Duplicate event id dropped (idempotency gate).",
            )
        raise

    logger.info("Ledger committed", extra={"event_id": record.event_id, "batch_id": str(batch_id)})
    return LedgerCommitResult(
        status=LedgerWriteStatus.COMMITTED,
        batch_id=str(batch_id),
        message="Header + double-entry lines committed atomically.",
    )


# =============================================================================
# 4. HITL ticket materialization (idempotent) + fatal DLQ envelope
# =============================================================================


def insert_exception_ticket(
    db: Session,
    *,
    event_id: str,
    source_topic: str,
    vendor_code: str,
    bank_utr_number: str | None,
    flagged_invoice_ids: list[str],
    exception_reason: str,
    variance_delta_paise: int | None,
    human_readable_message: str,
    flagged_payload: dict,
    consumer_name: str = LEDGER_CONSUMER_NAME,
) -> bool:
    """Idempotently materialize a HITL exception ticket.

    The idempotency gate (event_id PK) is the first statement of the same ACID
    block; a Unique Constraint Violation means the event was already ticket-ed
    and returns False. Other failures propagate (redelivery).

    Returns False when the event_id was already ticket-ed (idempotency gate).
    """
    try:
        with db.begin():
            db.execute(
                text(_IDEMPOTENCY_INSERT_SQL),
                {"event_id": event_id, "source_topic": source_topic, "consumer_name": consumer_name},
            )
            db.execute(
                text(_TICKET_INSERT_SQL),
                {
                    "vendor_code": vendor_code,
                    "source_topic": source_topic,
                    "source_event_id": event_id,
                    "bank_utr_number": bank_utr_number,
                    "flagged_invoice_ids": flagged_invoice_ids,
                    "exception_reason": exception_reason,
                    "variance_delta_paise": variance_delta_paise,
                    "human_readable_message": human_readable_message,
                    "flagged_payload": json.dumps(flagged_payload),
                },
            )
    except IntegrityError as e:
        db.rollback()
        if "idempotency_keys" in str(e).lower():
            return False
        raise
    return True


def variance_to_paise(value) -> int | None:
    """Best-effort rupees-string -> paise for ticket variance (None on garbage)."""
    if value is None:
        return None
    try:
        return rupees_to_paise(str(value))
    except InvalidPaiseStringError:
        return None


def build_ticket_from_dlq_event(raw: dict, source_topic: str) -> dict:
    """Map a reconciliation.dlq.events envelope into ticket insert params."""
    data = raw.get("data") or {}
    flagged = data.get("flagged_invoices") or []
    return {
        "event_id": raw.get("id") or "no-id",
        "source_topic": source_topic,
        "vendor_code": data.get("vendor_code") or "UNKNOWN",
        "bank_utr_number": data.get("bank_utr_number"),
        "flagged_invoice_ids": list(flagged) if isinstance(flagged, list) else [],
        "exception_reason": data.get("exception_reason") or "REASON_UNSPECIFIED",
        "variance_delta_paise": variance_to_paise(data.get("variance_delta")),
        "human_readable_message": data.get("human_readable_message") or "No message provided.",
        "flagged_payload": raw,
    }


def build_ticket_from_fatal_error(raw: dict, error: LedgerWriteError, source_topic: str) -> dict:
    """Map a fatal ledger failure into ticket insert params (Q1 = both paths)."""
    data = raw.get("data") or {}
    details = error.details or {}
    flagged = data.get("matched_invoices") or []
    return {
        "event_id": details.get("event_id") or raw.get("id") or "no-id",
        "source_topic": source_topic,
        "vendor_code": details.get("vendor_code") or data.get("vendor_code") or "UNKNOWN",
        "bank_utr_number": data.get("bank_utr_number"),
        "flagged_invoice_ids": list(flagged) if isinstance(flagged, list) else [],
        "exception_reason": error.reason,
        "variance_delta_paise": details.get("variance_delta_paise"),
        "human_readable_message": str(error),
        "flagged_payload": raw,
    }


def build_fatal_dlq_event(original: dict, error: LedgerWriteError) -> dict:
    """CloudEvents envelope for ledger.fatal.dlq.events (infrastructure alert)."""
    return {
        "specversion": "1.0",
        "type": "ledger.fatal",
        "source": "/layer5/ledger-writer",
        "id": f"fatal_{uuid.uuid4()}",
        "time": datetime.now(timezone.utc).isoformat(),
        "data": {
            "original_event": original,
            "error_code": error.reason,
            "error": str(error),
            "details": error.details,
        },
        "metadata": {
            "source": "layer5_ledger_writer",
            "error": error.reason,
        },
    }