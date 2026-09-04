"""
Layer 2 Ingestion API — Streams 2 & 3 materialization

Mocks the asynchronous production feeds so Layer 2's deterministic tools can
execute SQL against materialized data:

- POST /webhooks/razorpay   -> razorpay_settlements   (Razorpay webhook)
- POST /ingestion/bank      -> bank_transactions      (SFTP / bank feed)

The vendor_code is ALWAYS taken from the authenticated JWT — never from the
payload. Payloads failing schema validation are rejected 422 before any SQL.
Duplicate deliveries are idempotently absorbed (ON CONFLICT DO NOTHING) and
reported as 'duplicate' (webhook/feed retries must not fail).
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.security import get_current_user_context
from app.db.session import get_db
from app.schemas.ingestion import (
    BankIngestResponse,
    IngestionErrorResponse,
    RazorpayBatchResponse,
    RazorpayWebhookResponse,
)
from app.schemas.layer2_tools import BankTransactionPayload, RazorpaySettlementPayload

logger = logging.getLogger(__name__)

razorpay_router = APIRouter(prefix="/webhooks", tags=["ingestion"])
bank_router = APIRouter(prefix="/ingestion", tags=["ingestion"])

_VENDOR_CHECK_SQL = "SELECT 1 FROM vendor_users WHERE vendor_code = :vc"

_INSERT_RAZORPAY_SQL = """
    INSERT INTO razorpay_settlements (
        payout_id, fund_account_id, amount_paise, currency, status,
        utr, reference_id, narration, fees_paise, tax_paise,
        mode, purpose, vendor_code, event_created_at_epoch
    ) VALUES (
        :payout_id, :fund_account_id, :amount_paise, :currency, :status,
        :utr, :reference_id, :narration, :fees_paise, :tax_paise,
        :mode, :purpose, :vendor_code, :event_created_at_epoch
    )
    ON CONFLICT (payout_id) DO NOTHING
    RETURNING settlement_id
"""

_FETCH_EXISTING_RAZORPAY_SQL = """
    SELECT settlement_id FROM razorpay_settlements
    WHERE payout_id = :payout_id
"""

_INSERT_BANK_SQL = """
    INSERT INTO bank_transactions (
        transaction_date, narration, utr_number, transaction_type,
        amount_paise, closing_balance_paise, vendor_code
    ) VALUES (
        :transaction_date, :narration, :utr_number, :transaction_type,
        :amount_paise, :closing_balance_paise, :vendor_code
    )
    ON CONFLICT (utr_number, transaction_date, amount_paise) DO NOTHING
    RETURNING transaction_id
"""


def _require_onboarded_vendor(db: Session, vendor_code: str) -> None:
    if db.execute(text(_VENDOR_CHECK_SQL), {"vc": vendor_code}).first() is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Vendor '{vendor_code}' is not onboarded.",
        )


def _razorpay_insert_params(
    payload: RazorpaySettlementPayload,
    vendor_code: str,
) -> dict:
    """Shared bind params for the idempotent razorpay_settlements INSERT."""
    return {
        "payout_id": payload.payout_id,
        "fund_account_id": payload.fund_account_id,
        "amount_paise": payload.amount_paise,
        "currency": payload.currency,
        "status": payload.status,
        "utr": payload.utr,
        "reference_id": payload.reference_id,
        "narration": payload.narration,
        "fees_paise": payload.fees_paise,
        "tax_paise": payload.tax_paise,
        "mode": payload.mode,
        "purpose": payload.purpose,
        "vendor_code": vendor_code,
        "event_created_at_epoch": payload.event_created_at_epoch,
    }


# =============================================================================
# Stream 2: Razorpay webhook
# =============================================================================


@razorpay_router.post(
    "/razorpay",
    response_model=RazorpayWebhookResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses={403: {"model": IngestionErrorResponse}, 422: {"model": IngestionErrorResponse}},
)
def ingest_razorpay_settlement(
    payload: RazorpaySettlementPayload,
    db: Session = Depends(get_db),
    user_context: dict = Depends(get_current_user_context),
) -> RazorpayWebhookResponse:
    """Record one Razorpay payout/settlement entity (idempotent by payout_id)."""
    vendor_code = str(user_context["vendor_code"])
    _require_onboarded_vendor(db, vendor_code)

    try:
        row = db.execute(
            text(_INSERT_RAZORPAY_SQL),
            _razorpay_insert_params(payload, vendor_code),
        ).first()
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.error("Razorpay ingestion failed", extra={"error": str(exc)})
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "error_code": "RAZORPAY_INGESTION_FAILED",
                "message": f"Failed to persist settlement: {exc}",
            },
        )

    if row is not None:
        logger.info(
            "Razorpay settlement recorded",
            extra={"payout_id": payload.payout_id, "vendor_code": vendor_code},
        )
        return RazorpayWebhookResponse(
            settlement_id=str(row[0]),
            payout_id=payload.payout_id,
            status="recorded",
            message="Settlement recorded.",
        )

    # Idempotent re-delivery: absorb and report.
    existing = db.execute(
        text(_FETCH_EXISTING_RAZORPAY_SQL),
        {"payout_id": payload.payout_id},
    ).first()
    return RazorpayWebhookResponse(
        settlement_id=str(existing[0]) if existing else None,
        payout_id=payload.payout_id,
        status="duplicate",
        message="Duplicate webhook delivery ignored (payout already recorded).",
    )


@razorpay_router.post(
    "/razorpay/batch",
    response_model=RazorpayBatchResponse,
    status_code=status.HTTP_200_OK,
    responses={403: {"model": IngestionErrorResponse}, 422: {"model": IngestionErrorResponse}},
)
def ingest_razorpay_settlement_batch(
    payload: list[RazorpaySettlementPayload],
    db: Session = Depends(get_db),
    user_context: dict = Depends(get_current_user_context),
) -> RazorpayBatchResponse:
    """Record many Razorpay settlements in one call (Track 4 feed upload).

    Mirrors the bank feed semantics: per-record idempotency via
    ON CONFLICT (payout_id) DO NOTHING; re-deliveries are absorbed and
    reported as duplicates so webhook replays never fail.
    """
    vendor_code = str(user_context["vendor_code"])
    _require_onboarded_vendor(db, vendor_code)

    accepted = 0
    try:
        for settlement in payload:
            row = db.execute(
                text(_INSERT_RAZORPAY_SQL),
                _razorpay_insert_params(settlement, vendor_code),
            ).first()
            if row is not None:
                accepted += 1
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.error("Razorpay batch ingestion failed", extra={"error": str(exc)})
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "error_code": "RAZORPAY_BATCH_INGESTION_FAILED",
                "message": f"Failed to persist Razorpay settlements: {exc}",
            },
        )

    duplicates = len(payload) - accepted
    logger.info(
        "Razorpay batch ingested",
        extra={"accepted": accepted, "duplicates": duplicates, "vendor_code": vendor_code},
    )
    return RazorpayBatchResponse(
        accepted=accepted,
        duplicates=duplicates,
        total=len(payload),
        message=f"Recorded {accepted} of {len(payload)} Razorpay settlements ({duplicates} duplicates).",
    )


# =============================================================================
# Stream 3: Bank statement feed
# =============================================================================


@bank_router.post(
    "/bank",
    response_model=BankIngestResponse,
    status_code=status.HTTP_200_OK,
    responses={403: {"model": IngestionErrorResponse}, 422: {"model": IngestionErrorResponse}},
)
def ingest_bank_transactions(
    payload: list[BankTransactionPayload],
    db: Session = Depends(get_db),
    user_context: dict = Depends(get_current_user_context),
) -> BankIngestResponse:
    """Record a bank statement batch (idempotent per unique transaction row)."""
    vendor_code = str(user_context["vendor_code"])
    _require_onboarded_vendor(db, vendor_code)

    accepted = 0
    try:
        for txn in payload:
            row = db.execute(
                text(_INSERT_BANK_SQL),
                {
                    "transaction_date": txn.transaction_date,
                    "narration": txn.narration,
                    "utr_number": txn.utr_number,
                    "transaction_type": txn.transaction_type,
                    "amount_paise": txn.amount_paise,
                    "closing_balance_paise": txn.closing_balance_paise,
                    "vendor_code": vendor_code,
                },
            ).first()
            if row is not None:
                accepted += 1
        db.commit()
    except ValidationError as exc:  # pragma: no cover — FastAPI validates before the handler
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error_code": "INVALID_BANK_PAYLOAD", "message": str(exc)},
        )
    except Exception as exc:
        db.rollback()
        logger.error("Bank ingestion failed", extra={"error": str(exc)})
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "error_code": "BANK_INGESTION_FAILED",
                "message": f"Failed to persist bank transactions: {exc}",
            },
        )

    duplicates = len(payload) - accepted
    logger.info(
        "Bank feed ingested",
        extra={"accepted": accepted, "duplicates": duplicates, "vendor_code": vendor_code},
    )
    return BankIngestResponse(
        accepted=accepted,
        duplicates=duplicates,
        total=len(payload),
        message=f"Ingested {accepted} of {len(payload)} bank transactions.",
    )
