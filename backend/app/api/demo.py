"""
Demo auto-feed generator — POST /demo/auto-generate-feeds.

Replaces the manual terminal workflow (scripts/generate_layer2_feeds.py +
curl pushes) with an authenticated API call. The Next.js Command Center fires
it right after the batch upload 202; this endpoint either pushes immediately
(batch already COMPLETED/PARTIAL) or schedules a bounded background task that
polls `batch_jobs` and ingests the razorpay + bank rows the moment Layer 1
extraction finishes — i.e. BEFORE the Layer 2 boundary poller seals the batch
(~3s cadence), so every deterministic pre-node sees the feeds. No race by
construction, no terminal, no JSON files.

Feed rows are persisted with the EXACT idempotent INSERTs used by
POST /webhooks/razorpay(batch) and POST /ingestion/bank (same SQL constants,
same ON CONFLICT DO NOTHING, vendor_code always from the JWT), executed
in-process instead of self-HTTP so there is no loopback/URL fragility and the
whole push is one atomic commit.

Guarantees:
  * deterministic ids (UTR = 300000000001+idx, payout pout_e2e_%04d) make
    retries harmless — duplicates are absorbed, never double-inserted.
  * one background task per batch (in-process set guard).
  * zero changes to the L1/L2/L5 pipeline — this only materializes Streams 2
    & 3 rows that the existing deterministic tools already consume.
"""

import logging
import threading
import time
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import text
from sqlalchemy.orm import Session
from starlette.background import BackgroundTasks

from app.api.ingestion import (
    _INSERT_BANK_SQL,
    _INSERT_RAZORPAY_SQL,
    _razorpay_insert_params,
)
from app.core.config import get_settings
from app.core.security import get_current_user_context
from app.db.session import SessionLocal, get_db
from app.demo.feeds import (
    BATCH_STATE_SQL,
    build_feeds,
    fetch_batch_invoices,
    fetch_batch_state,
)
from app.schemas.demo import (
    AutoGenerateFeedsRequest,
    AutoGenerateFeedsResponse,
    DemoFeedError,
)
from app.schemas.layer2_tools import BankTransactionPayload, RazorpaySettlementPayload

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/demo", tags=["demo"])

# Layer 1 fan-in reaches these terminal states before the L2 seal.
TERMINAL_STATUSES = {"COMPLETED", "PARTIAL"}

# In-process guard: at most one background wait per batch.
_PENDING_LOCK = threading.Lock()
_PENDING_BATCHES: set[str] = set()


def _bank_insert_params(txn, vendor_code: str) -> dict:
    """Bind params mirroring the /ingestion/bank INSERT (single source)."""
    return {
        "transaction_date": txn.transaction_date,
        "narration": txn.narration,
        "utr_number": txn.utr_number,
        "transaction_type": txn.transaction_type,
        "amount_paise": txn.amount_paise,
        "closing_balance_paise": txn.closing_balance_paise,
        "vendor_code": vendor_code,
    }


def _generate_and_push(
    db: Session,
    batch_id: str,
    vendor_code: str,
    anomalies: int,
) -> dict:
    """Fetch open invoices, build the feeds, persist them (one commit).

    Returns an outcome dict; the caller maps it to the HTTP response.
    Raises on DB failure after rolling back (no partial writes).
    """
    row = fetch_batch_state(db, batch_id, vendor_code)
    if row is None:
        return {"outcome": "not_found"}
    current_status = str(row[2])
    if current_status not in TERMINAL_STATUSES:
        return {"outcome": "not_terminal", "status": current_status}

    invoices = fetch_batch_invoices(db, batch_id)
    if not invoices:
        return {"outcome": "no_invoices"}
    if anomalies >= len(invoices):
        return {"outcome": "invalid_anomalies", "open": len(invoices)}

    razorpay, bank = build_feeds(invoices, anomalies)

    razorpay_accepted = 0
    bank_accepted = 0
    try:
        for payload in razorpay:
            model = RazorpaySettlementPayload.model_validate(payload)
            inserted = db.execute(
                text(_INSERT_RAZORPAY_SQL),
                _razorpay_insert_params(model, vendor_code),
            ).first()
            if inserted is not None:
                razorpay_accepted += 1
        for txn in bank:
            model = BankTransactionPayload.model_validate(txn)
            inserted = db.execute(
                text(_INSERT_BANK_SQL),
                _bank_insert_params(model, vendor_code),
            ).first()
            if inserted is not None:
                bank_accepted += 1
        db.commit()
    except Exception:
        db.rollback()
        raise

    return {
        "outcome": "pushed",
        "invoices_generated": len(razorpay),
        "anomalies": anomalies,
        "razorpay_accepted": razorpay_accepted,
        "razorpay_duplicates": len(razorpay) - razorpay_accepted,
        "bank_accepted": bank_accepted,
        "bank_duplicates": len(bank) - bank_accepted,
    }


def _wait_until_terminal(batch_id: str, vendor_code: str) -> str | None:
    """Poll batch_jobs (fresh session per tick) until COMPLETED/PARTIAL.

    Returns the terminal status, or None if the batch vanished or the wait
    budget was exhausted (caller decides how to surface it).
    """
    settings = get_settings()
    deadline = time.monotonic() + settings.auto_feed_wait_s
    while time.monotonic() < deadline:
        db = SessionLocal()
        try:
            row = fetch_batch_state(db, batch_id, vendor_code)
        finally:
            db.close()
        if row is None:
            return None
        current = str(row[2])
        if current in TERMINAL_STATUSES:
            return current
        # Extraction is genuinely done even if the COMPLETED flip hasn't
        # committed yet — push now, ahead of the Layer 2 seal (L2's bounded
        # buffer grace absorbs the race).
        total = int(row[3] or 0)
        done = int(row[4] or 0) + int(row[5] or 0)
        if total > 0 and done >= total:
            return "COMPLETED"
        time.sleep(settings.auto_feed_poll_s)
    return None


def _background_wait_and_push(batch_id: str, vendor_code: str, anomalies: int) -> None:
    """Background task: wait for L1 extraction, then ingest feeds pre-seal."""
    with _PENDING_LOCK:
        if batch_id in _PENDING_BATCHES:
            return  # another task already owns this batch
        _PENDING_BATCHES.add(batch_id)
    try:
        terminal = _wait_until_terminal(batch_id, vendor_code)
        if terminal is None:
            logger.warning(
                "Auto-feed generation aborted — batch never reached a terminal state",
                extra={"batch_id": batch_id, "vendor_code": vendor_code},
            )
            return
        db = SessionLocal()
        try:
            result = _generate_and_push(db, batch_id, vendor_code, anomalies)
        finally:
            db.close()
        logger.info(
            "Auto-generated settlement feeds pushed",
            extra={"batch_id": batch_id, **result},
        )
    except Exception:
        logger.exception(
            "Auto-feed generation failed",
            extra={"batch_id": batch_id, "vendor_code": vendor_code},
        )
    finally:
        with _PENDING_LOCK:
            _PENDING_BATCHES.discard(batch_id)


def _batch_not_found(batch_id: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={
            "error_code": "BATCH_NOT_FOUND",
            "message": f"Batch {batch_id} not found for this vendor.",
        },
    )


@router.post(
    "/auto-generate-feeds",
    response_model=AutoGenerateFeedsResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        404: {"model": DemoFeedError},
        409: {"model": DemoFeedError},
        422: {"model": DemoFeedError},
    },
)
def auto_generate_feeds(
    req: AutoGenerateFeedsRequest,
    response: Response,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    user_context: dict = Depends(get_current_user_context),
) -> AutoGenerateFeedsResponse:
    """Materialize Streams 2 & 3 for a Layer 1 batch, before the L2 seal."""
    vendor_code = str(user_context["vendor_code"])
    try:
        UUID(req.batch_id)
    except ValueError:
        raise _batch_not_found(req.batch_id)

    row = db.execute(
        text(BATCH_STATE_SQL),
        {"bid": str(req.batch_id), "vc": vendor_code},
    ).first()
    if row is None:
        raise _batch_not_found(req.batch_id)

    current_status = str(row[2])
    total_invoices = int(row[3])

    # Batch still extracting: wait server-side (bounded) and push pre-seal.
    if current_status not in TERMINAL_STATUSES:
        background_tasks.add_task(
            _background_wait_and_push,
            str(req.batch_id),
            vendor_code,
            req.anomalies,
        )
        logger.info(
            "Auto-feed generation scheduled (waiting on Layer 1)",
            extra={
                "batch_id": str(req.batch_id),
                "vendor_code": vendor_code,
                "status": current_status,
                "total_invoices": total_invoices,
            },
        )
        return AutoGenerateFeedsResponse(
            batch_id=str(req.batch_id),
            status="WAITING",
            message=(
                f"Batch {req.batch_id} is {current_status} "
                f"({total_invoices} invoices). Settlement feeds will be "
                "generated automatically the moment extraction completes."
            ),
            anomalies=req.anomalies,
        )

    result = _generate_and_push(db, str(req.batch_id), vendor_code, req.anomalies)

    if result["outcome"] == "not_found":
        raise _batch_not_found(str(req.batch_id))
    if result["outcome"] == "not_terminal":
        # Vanishingly rare race (status changed between reads) — reschedule.
        background_tasks.add_task(
            _background_wait_and_push,
            str(req.batch_id),
            vendor_code,
            req.anomalies,
        )
        return AutoGenerateFeedsResponse(
            batch_id=str(req.batch_id),
            status="WAITING",
            message=(
                f"Batch {req.batch_id} moved to '{result['status']}' — "
                "auto-generation rescheduled in the background."
            ),
            anomalies=req.anomalies,
        )
    if result["outcome"] == "no_invoices":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error_code": "NO_RECONCILABLE_INVOICES",
                "message": (
                    f"Batch {req.batch_id} has no open VALIDATED invoices "
                    "(already reconciled or none extracted). Nothing to feed."
                ),
            },
        )
    if result["outcome"] == "invalid_anomalies":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error_code": "INVALID_ANOMALIES",
                "message": (
                    f"anomalies ({req.anomalies}) must be smaller than the "
                    f"number of open invoices ({result['open']})."
                ),
            },
        )

    response.status_code = status.HTTP_200_OK
    logger.info(
        "Auto-generated settlement feeds pushed",
        extra={"batch_id": str(req.batch_id), "vendor_code": vendor_code, **result},
    )
    return AutoGenerateFeedsResponse(
        batch_id=str(req.batch_id),
        status="PUSHED",
        message=(
            f"Ingested {result['razorpay_accepted']} razorpay + "
            f"{result['bank_accepted']} bank records for "
            f"{result['invoices_generated']} invoices "
            f"({result['anomalies']} left unmatched for the Exception Desk)."
        ),
        invoices_generated=result["invoices_generated"],
        anomalies=result["anomalies"],
        razorpay_accepted=result["razorpay_accepted"],
        razorpay_duplicates=result["razorpay_duplicates"],
        bank_accepted=result["bank_accepted"],
        bank_duplicates=result["bank_duplicates"],
    )
