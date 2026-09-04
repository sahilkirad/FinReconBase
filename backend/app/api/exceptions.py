"""
Exception Desk API — Human-in-the-Loop maker/checker queue (Track 4).

Reads and transitions the frozen `exception_tickets` table (the Layer 2 DLQ
materialization). Multi-tenant isolation is enforced everywhere: every query
is scoped by vendor_code taken from the JWT — a caller can never see or touch
another tenant's tickets.

State machine (audited maker/checker flow):
    OPEN --(claim)--> IN_REVIEW --(resolve)--> RESOLVED
                                 --(close)----> CLOSED

Direct jumps (OPEN -> RESOLVED) and re-opens are rejected with 409 so an
exception resolution is always a deliberate, reviewable act.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.security import get_current_user_context
from app.db.session import get_db
from app.schemas.exceptions import (
    ExceptionTicketListResponse,
    ExceptionTicketResponse,
    ExceptionTicketStatus,
    ExceptionTicketTransitionRequest,
)

router = APIRouter(prefix="/exception-tickets", tags=["exceptions"])

# Shared column list (positional contract between SQL and _row_to_ticket).
_TICKET_COLUMNS = """
    ticket_id, vendor_code, source_topic, source_event_id, bank_utr_number,
    flagged_invoice_ids, exception_reason, variance_delta_paise,
    human_readable_message, flagged_payload, status, created_at,
    resolved_at, resolved_by
"""

_SELECT_TICKETS_SQL = text(
    f"""
    SELECT {_TICKET_COLUMNS}
    FROM exception_tickets
    WHERE vendor_code = :vendor_code
      AND (CAST(:status_filter AS text) IS NULL OR CAST(status AS text) = CAST(:status_filter AS text))
    ORDER BY created_at DESC
    LIMIT :limit
    """
)

_SELECT_TICKET_SQL = text(
    f"""
    SELECT {_TICKET_COLUMNS}
    FROM exception_tickets
    WHERE ticket_id = :ticket_id
      AND vendor_code = :vendor_code
    """
)

_UPDATE_TICKET_STATUS_SQL = text(
    f"""
    UPDATE exception_tickets
    SET status = CAST(:new_status AS exception_ticket_status),
        resolved_at = CASE
            WHEN :new_status IN ('RESOLVED', 'CLOSED') THEN now()
            ELSE resolved_at END,
        resolved_by = CASE
            WHEN :new_status IN ('RESOLVED', 'CLOSED') THEN CAST(:resolved_by AS uuid)
            ELSE resolved_by END
    WHERE ticket_id = :ticket_id
      AND vendor_code = :vendor_code
    RETURNING {_TICKET_COLUMNS}
    """
)

# Legal transitions: current -> {allowed next statuses}. Anything else -> 409.
_ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "OPEN": {"IN_REVIEW"},
    "IN_REVIEW": {"RESOLVED", "CLOSED"},
}

_TERMINAL_STATUSES = ("RESOLVED", "CLOSED")


def _validate_ticket_id(ticket_id: str) -> None:
    """Return 404 for non-UUID ticket ids instead of a DB 500 error."""
    try:
        uuid.UUID(ticket_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Exception ticket {ticket_id} not found",
        )


def _row_to_ticket(row) -> ExceptionTicketResponse:
    """Map the shared positional column contract onto the response model."""
    return ExceptionTicketResponse(
        ticket_id=str(row[0]),
        vendor_code=str(row[1]),
        source_topic=str(row[2]),
        source_event_id=str(row[3]) if row[3] is not None else None,
        bank_utr_number=str(row[4]) if row[4] is not None else None,
        flagged_invoice_ids=list(row[5]) if row[5] is not None else [],
        exception_reason=str(row[6]),
        variance_delta_paise=int(row[7]) if row[7] is not None else None,
        human_readable_message=str(row[8]),
        flagged_payload=dict(row[9]) if row[9] is not None else {},
        status=str(row[10]),
        created_at=row[11],
        resolved_at=row[12],
        resolved_by=str(row[13]) if row[13] is not None else None,
    )


@router.get("", response_model=ExceptionTicketListResponse)
def list_exception_tickets(
    status_filter: ExceptionTicketStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=500),
    db: Session = Depends(get_db),
    user_context: dict = Depends(get_current_user_context),
) -> ExceptionTicketListResponse:
    """List this vendor's exception tickets, newest first (optional status filter)."""
    vendor_code = str(user_context["vendor_code"])
    rows = db.execute(
        _SELECT_TICKETS_SQL,
        {"vendor_code": vendor_code, "status_filter": status_filter, "limit": limit},
    ).all()
    items = [_row_to_ticket(row) for row in rows]
    return ExceptionTicketListResponse(
        vendor_code=vendor_code,
        total=len(items),
        items=items,
    )


@router.patch("/{ticket_id}", response_model=ExceptionTicketResponse)
def transition_exception_ticket(
    ticket_id: str,
    payload: ExceptionTicketTransitionRequest,
    db: Session = Depends(get_db),
    user_context: dict = Depends(get_current_user_context),
) -> ExceptionTicketResponse:
    """Maker/checker transition on one of this vendor's tickets."""
    vendor_code = str(user_context["vendor_code"])
    _validate_ticket_id(ticket_id)

    ticket = db.execute(
        _SELECT_TICKET_SQL,
        {"ticket_id": ticket_id, "vendor_code": vendor_code},
    ).first()
    if ticket is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Exception ticket {ticket_id} not found",
        )

    current_status = str(ticket[10])
    new_status = payload.status

    if new_status == current_status or new_status not in _ALLOWED_TRANSITIONS.get(
        current_status, set()
    ):
        allowed = sorted(_ALLOWED_TRANSITIONS.get(current_status, set()))
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error_code": "INVALID_TICKET_TRANSITION",
                "message": (
                    f"Cannot move ticket from '{current_status}' to '{new_status}'. "
                    f"Allowed next states: {allowed or 'none (terminal)'}."
                ),
            },
        )

    resolved_by = (
        str(user_context["sub"]) if new_status in _TERMINAL_STATUSES else None
    )

    updated = db.execute(
        _UPDATE_TICKET_STATUS_SQL,
        {
            "ticket_id": ticket_id,
            "vendor_code": vendor_code,
            "new_status": new_status,
            "resolved_by": resolved_by,
        },
    ).first()
    db.commit()

    if updated is None:  # pragma: no cover — race with a concurrent delete
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Exception ticket {ticket_id} not found",
        )
    return _row_to_ticket(updated)
