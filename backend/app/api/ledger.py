"""
Immutable Ledger API — Layer 5 audit trail view (Track 4).

- GET /ledger/entries   read-only double-entry ledger, vendor-scoped.

Serves reconciliation_batches + their paired ledger_entries. The WORM
immutability is enforced at the DB layer (prevent_immutable_table_mutation
triggers); this API only reads. Every returned batch carries its double-entry
proof: DEBIT total - CREDIT total = imbalance_paise (always 0 for a healthy
ledger — the frontend renders it as the audit footer).
"""

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.security import get_current_user_context
from app.db.session import get_db
from app.schemas.dashboard import (
    LedgerBatchView,
    LedgerEntriesResponse,
    LedgerEntryLine,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ledger", tags=["ledger"])

_BATCHES_SQL = text(
    """
    SELECT batch_id::text, idempotency_event_id, vendor_code, utr_number,
           razorpay_payout_id, total_reconciled_amount_paise,
           matched_invoice_ids, created_at
    FROM reconciliation_batches
    WHERE vendor_code = :vendor_code
      AND (CAST(:utr_filter AS text) IS NULL OR utr_number = CAST(:utr_filter AS text))
    ORDER BY created_at DESC
    LIMIT :limit
    """
)

_ENTRIES_SQL = text(
    """
    SELECT entry_type, account_type, account_name, amount_paise,
           cleared_invoice_ids, created_at
    FROM ledger_entries
    WHERE batch_id = :batch_id
    ORDER BY created_at
    """
)


def _validate_batch_id(batch_id: str) -> None:
    """Return 400 for non-UUID ledger batch ids instead of a DB 500 error."""
    try:
        uuid.UUID(batch_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Ledger batch id '{batch_id}' is not a valid UUID",
        )


@router.get("/entries", response_model=LedgerEntriesResponse)
def list_ledger_entries(
    utr_number: str | None = Query(default=None, max_length=64),
    batch_id: str | None = Query(default=None, description="Filter to one ledger batch"),
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
    user_context: dict = Depends(get_current_user_context),
) -> LedgerEntriesResponse:
    """Read-only double-entry ledger, newest first (vendor-scoped)."""
    vendor_code = str(user_context["vendor_code"])

    if batch_id is not None:
        _validate_batch_id(batch_id)

    batches = db.execute(
        _BATCHES_SQL,
        {
            "vendor_code": vendor_code,
            "utr_filter": utr_number,
            "limit": limit,
        },
    ).all()

    items: list[LedgerBatchView] = []
    for r in batches:
        bid = str(r[0])
        if batch_id is not None and bid != batch_id:
            continue
        entry_rows = db.execute(_ENTRIES_SQL, {"batch_id": bid}).all()
        lines = [
            LedgerEntryLine(
                entry_type=str(e[0]),
                account_type=str(e[1]),
                account_name=str(e[2]),
                amount_paise=int(e[3] or 0),
                cleared_invoice_ids=list(e[4]) if e[4] is not None else [],
                created_at=e[5],
            )
            for e in entry_rows
        ]
        debit = sum(l.amount_paise for l in lines if l.entry_type == "DEBIT")
        credit = sum(l.amount_paise for l in lines if l.entry_type == "CREDIT")
        items.append(
            LedgerBatchView(
                batch_id=bid,
                idempotency_event_id=str(r[1]),
                vendor_code=str(r[2]),
                utr_number=str(r[3]),
                razorpay_payout_id=str(r[4]) if r[4] is not None else None,
                total_reconciled_amount_paise=int(r[5] or 0),
                matched_invoice_ids=list(r[6]) if r[6] is not None else [],
                created_at=r[7],
                entries=lines,
                imbalance_paise=debit - credit,
            )
        )

    logger.info(
        "LEDGER_ENTRIES_READ",
        extra={"vendor_code": vendor_code, "total": len(items), "utr_filter": utr_number},
    )
    return LedgerEntriesResponse(vendor_code=vendor_code, total=len(items), items=items)
