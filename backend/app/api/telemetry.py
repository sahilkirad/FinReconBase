"""
Batch Telemetry API — Live Batch Telemetry page (Track 4).

- GET /batches/{batch_id}/telemetry         composite funnel over Postgres
- GET /batches/{batch_id}/telemetry/events  ReAct terminal stream (Redis)

All queries are scoped by vendor_code from the JWT (multi-tenant isolation).
The per-invoice path split (fast_path vs agent) comes from the ephemeral Redis
telemetry stream written by the recon supervisor worker; when no events exist
(e.g. worker restarted and Redis evicted), those fields degrade to null while
the DB-derived funnel stays accurate.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.security import get_current_user_context
from app.db.session import get_db
from app.schemas.dashboard import (
    BatchTelemetryResponse,
    InvoiceTelemetryItem,
    Layer2RunSummary,
    TelemetryEventsResponse,
    TelemetryFunnel,
)

router = APIRouter(prefix="/batches", tags=["telemetry"])

_BATCH_SQL = text(
    """
    SELECT batch_id::text, vendor_code, source_type, filename,
           total_invoices, processed_count, failed_count,
           status, created_at, completed_at
    FROM batch_jobs
    WHERE batch_id = :batch_id
      AND vendor_code = :vendor_code
    """
)

_RUN_SQL = text(
    """
    SELECT status, run_type, total_extracted, matched_count,
           exception_count, shortfall, last_error, started_at, completed_at
    FROM layer2_batch_runs
    WHERE batch_id = :batch_id
      AND vendor_code = :vendor_code
    """
)

_ITEMS_SQL = text(
    """
    SELECT bii.row_number, bii.invoice_number, bii.document_id::text,
           bii.status, bii.error_message,
           ei.processing_status,
           ir.utr_number, ir.razorpay_payout_id, ir.net_settled_amount_paise,
           ir.reconciled_at,
           (ei.grand_total_paise - ei.tds_deduction_paise) AS net_paise
    FROM batch_invoice_items bii
    LEFT JOIN extracted_invoices ei ON ei.document_id = bii.document_id
    LEFT JOIN invoice_reconciliations ir ON ir.document_id = bii.document_id
    WHERE bii.batch_id = :batch_id
    ORDER BY bii.row_number NULLS LAST
    """
)

# Tickets overlap a batch only via flagged invoice numbers + vendor + time
# window (exception_tickets has no batch column in the frozen schema).
_TICKETS_SQL = text(
    """
    SELECT flagged_invoice_ids, exception_reason, status, created_at
    FROM exception_tickets
    WHERE vendor_code = :vendor_code
      AND created_at >= :window_start
    ORDER BY created_at DESC
    """
)

_TERMINAL_STAGES = {"LEDGER_COMMITTED", "ALREADY_COMMITTED", "EXCEPTION_ROUTED"}


def _validate_batch_id(batch_id: str) -> None:
    try:
        uuid.UUID(batch_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Batch {batch_id} not found",
        )


def _ticket_reasons_for_invoice(tickets, invoice_numbers: set[str]) -> dict[str, str]:
    """invoice_number -> exception_reason from OPEN tickets (dedup, newest wins)."""
    reasons: dict[str, str] = {}
    for flagged_ids, reason, _tstatus, _created in tickets:
        for inv in flagged_ids or []:
            if inv in invoice_numbers:
                reasons[str(inv)] = str(reason)
    return reasons


def _read_telemetry_map(batch_id: str, redis_url: str) -> dict[str, dict]:
    """invoice_number -> {path, llm_invoked, tool_calls} from Redis events."""
    from app.telemetry import events as telemetry_module

    writer = telemetry_module.BatchTelemetryWriter(redis_url)
    by_invoice: dict[str, dict] = {}
    terminal_by_invoice: dict[str, dict] = {}
    for event in writer.read(batch_id):
        invoice = event.get("invoice")
        if not invoice:
            continue
        stage = event.get("stage")
        if stage in ("started", "deterministic", "agent", "tool_called"):
            by_invoice.setdefault(str(invoice), {"path": None, "llm_invoked": None, "tool_calls": []})
            if stage == "tool_called":
                by_invoice[str(invoice)]["tool_calls"].append(str(event.get("detail") or ""))
            elif stage == "agent":
                by_invoice[str(invoice)]["llm_invoked"] = True
                by_invoice[str(invoice)]["path"] = "agent"
            elif stage == "deterministic":
                detail = str(event.get("detail") or "")
                if "never invoked" in detail or "-> SUBSET_MATCHED" in detail:
                    by_invoice[str(invoice)]["llm_invoked"] = False
        elif stage == "terminal":
            terminal_by_invoice[str(invoice)] = event
    # Finalize path from the terminal event: agent path overrides if tools/LLM seen.
    for invoice, ev in terminal_by_invoice.items():
        entry = by_invoice.setdefault(str(invoice), {"path": None, "llm_invoked": None, "tool_calls": []})
        tools = entry.get("tool_calls") or []
        llm = bool(entry.get("llm_invoked"))
        ts = ev.get("terminal_status")
        if ts == "EXCEPTION_ROUTED":
            entry["path"] = "agent" if (llm or tools) else "deterministic_fallback"
        elif ts in _TERMINAL_STAGES:
            entry["path"] = "agent" if (llm or tools) else "fast_path"
        entry["llm_invoked"] = llm
    return by_invoice


@router.get("/{batch_id}/telemetry", response_model=BatchTelemetryResponse)
def get_batch_telemetry(
    batch_id: str,
    db: Session = Depends(get_db),
    user_context: dict = Depends(get_current_user_context),
    settings: Settings = Depends(get_settings),
) -> BatchTelemetryResponse:
    """Composite funnel + per-invoice state for the Live Telemetry page."""
    _validate_batch_id(batch_id)
    vendor_code = str(user_context["vendor_code"])

    batch = db.execute(
        _BATCH_SQL, {"batch_id": batch_id, "vendor_code": vendor_code}
    ).first()
    if batch is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Batch {batch_id} not found",
        )

    run_row = db.execute(_RUN_SQL, {"batch_id": batch_id, "vendor_code": vendor_code}).first()
    layer2 = None
    if run_row is not None:
        layer2 = Layer2RunSummary(
            status=str(run_row[0]),
            run_type=str(run_row[1]),
            total_extracted=int(run_row[2] or 0),
            matched_count=int(run_row[3] or 0),
            exception_count=int(run_row[4] or 0),
            shortfall=int(run_row[5] or 0),
            last_error=str(run_row[6]) if run_row[6] is not None else None,
            started_at=run_row[7],
            completed_at=run_row[8],
        )

    item_rows = db.execute(_ITEMS_SQL, {"batch_id": batch_id}).all()

    invoice_numbers = {str(r[1]) for r in item_rows if r[1] is not None}
    tickets = db.execute(
        _TICKETS_SQL,
        {
            "vendor_code": vendor_code,
            "window_start": batch[8],  # batch_jobs.created_at
        },
    ).all()
    reasons = _ticket_reasons_for_invoice(tickets, invoice_numbers)
    telemetry_map = _read_telemetry_map(batch_id, settings.redis_url)

    invoices: list[InvoiceTelemetryItem] = []
    for r in item_rows:
        invoice_number = str(r[1]) if r[1] is not None else None
        tel = telemetry_map.get(invoice_number or "") if invoice_number else None
        invoices.append(
            InvoiceTelemetryItem(
                row_number=int(r[0]) if r[0] is not None else None,
                invoice_number=invoice_number,
                document_id=str(r[2]) if r[2] is not None else None,
                l1_status=str(r[3]) if r[3] is not None else None,
                error_message=str(r[4]) if r[4] is not None else None,
                processing_status=str(r[5]) if r[5] is not None else None,
                utr_number=str(r[6]) if r[6] is not None else None,
                razorpay_payout_id=str(r[7]) if r[7] is not None else None,
                net_settled_amount_paise=int(r[8]) if r[8] is not None else None,
                net_paise=int(r[10]) if r[10] is not None else None,
                reconciled_at=r[9],
                exception_reason=reasons.get(invoice_number or "") if invoice_number else None,
                path=tel.get("path") if tel else None,
                llm_invoked=tel.get("llm_invoked") if tel else None,
                tool_calls=tel.get("tool_calls") if tel else None,
            )
        )

    settled = sum(1 for i in invoices if i.utr_number)
    exception_count = sum(1 for i in invoices if i.exception_reason)
    open_count = max(0, len(invoices) - settled - exception_count)
    fast_path = sum(1 for i in invoices if i.path == "fast_path")
    agent_routed = sum(1 for i in invoices if i.path in ("agent", "deterministic_fallback"))

    return BatchTelemetryResponse(
        batch_id=str(batch[0]),
        vendor_code=str(batch[1]),
        source_type=str(batch[2]) if batch[2] is not None else None,
        filename=str(batch[3]) if batch[3] is not None else None,
        status=str(batch[7]),
        total_invoices=int(batch[4] or 0),
        processed_count=int(batch[5] or 0),
        failed_count=int(batch[6] or 0),
        created_at=batch[8],
        completed_at=batch[9],
        layer2=layer2,
        funnel=TelemetryFunnel(
            total=len(invoices),
            settled=settled,
            exceptions=exception_count,
            open=open_count,
            fast_path=fast_path if telemetry_map else None,
            agent_routed=agent_routed if telemetry_map else None,
        ),
        invoices=invoices,
    )


@router.get("/{batch_id}/telemetry/events", response_model=TelemetryEventsResponse)
def get_batch_telemetry_events(
    batch_id: str,
    db: Session = Depends(get_db),
    user_context: dict = Depends(get_current_user_context),
    settings: Settings = Depends(get_settings),
) -> TelemetryEventsResponse:
    """ReAct terminal stream for one batch (Redis, ephemeral 2h window)."""
    _validate_batch_id(batch_id)
    vendor_code = str(user_context["vendor_code"])

    batch = db.execute(
        _BATCH_SQL, {"batch_id": batch_id, "vendor_code": vendor_code}
    ).first()
    if batch is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Batch {batch_id} not found",
        )

    from app.telemetry import events as telemetry_module

    writer = telemetry_module.BatchTelemetryWriter(settings.redis_url)
    events = writer.read(batch_id)
    return TelemetryEventsResponse(batch_id=batch_id, total=len(events), events=events)
