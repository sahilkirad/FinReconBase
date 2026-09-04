"""Response/request models for the Exception Desk API (Track 4 HITL).

Maps 1:1 onto the frozen `exception_tickets` table — no new columns.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

ExceptionTicketStatus = Literal["OPEN", "IN_REVIEW", "RESOLVED", "CLOSED"]


class ExceptionTicketResponse(BaseModel):
    ticket_id: str
    vendor_code: str
    source_topic: str
    source_event_id: str | None = None
    bank_utr_number: str | None = None
    flagged_invoice_ids: list[str]
    exception_reason: str
    variance_delta_paise: int | None = None
    human_readable_message: str
    flagged_payload: dict
    status: ExceptionTicketStatus
    created_at: datetime
    resolved_at: datetime | None = None
    resolved_by: str | None = None


class ExceptionTicketListResponse(BaseModel):
    vendor_code: str
    total: int
    items: list[ExceptionTicketResponse]


class ExceptionTicketTransitionRequest(BaseModel):
    """Target status for the maker/checker transition (PATCH)."""

    status: ExceptionTicketStatus
