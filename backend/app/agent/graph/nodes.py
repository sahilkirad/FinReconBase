"""Layer 2 — Deterministic graph nodes (ReAct pivot).

The per-invoice sub-graph now has only TWO deterministic nodes:

    pre_check    -> short-circuit if this document is already reconciled
    context_node -> deterministic context seed (the ONLY numbers the agent
                    sees): runs the gross-to-net TDS waterfall on the masked
                    payload and binds the razorpay -> UTR anchor leg
                    (razorpay_settlements.reference_id == invoice number).
                    Writes those proofs into state and emits the agent's
                    opening HumanMessage with the deterministic context.

Everything after context_node is the true ReAct loop (supervisor.py): the
Groq agent reasons, calls the 5 bound tools (react_tools.py), and the tools
enforce the waterfall order via InjectedState guardrails. No tool is called
from a graph edge anymore.
"""

import logging

from sqlalchemy import text

from app.agent.graph.state import ReconciliationState
from app.db.session import SessionLocal

logger = logging.getLogger(__name__)

_CHECK_RECONCILED_SQL = """
    SELECT 1 FROM invoice_reconciliations
    WHERE document_id = CAST(:document_id AS uuid)
    LIMIT 1
"""


# =============================================================================
# Short-circuit pre-check (saves LLM tokens / prevents double-processing)
# =============================================================================


def pre_check(state: ReconciliationState) -> dict:
    """If this document already has a reconciliation row, END immediately."""
    document_id = state["document_id"]
    db = SessionLocal()
    try:
        exists = db.execute(
            text(_CHECK_RECONCILED_SQL),
            {"document_id": document_id},
        ).first()
    finally:
        db.close()

    if exists:
        return {
            "terminal_status": "ALREADY_COMMITTED",
            "terminal_detail": "document already present in invoice_reconciliations",
        }
    return {}


# =============================================================================
# Deterministic context seed (pre-LLM)
# =============================================================================

_ANCHOR_SQL = """
    SELECT r.payout_id, r.utr, r.amount_paise,
           (r.fees_paise + r.tax_paise) AS gateway_paise,
           r.narration
    FROM razorpay_settlements r
    WHERE r.vendor_code = :vendor_code
      AND r.reference_id = :invoice_number
      AND r.status = 'processed'
    ORDER BY r.ingested_at DESC
    LIMIT 1
"""


def _to_rupees(paise: int) -> str:
    whole, rem = divmod(int(paise), 100)
    return f"{whole}.{rem:02d}"


def context_node(state: ReconciliationState) -> dict:
    """Deterministic context seed: TDS waterfall + razorpay anchor + opening
    message for the ReAct agent.

    The masked payload (tokens only, never raw PII) was vault-masked before
    the run; financial fields are integers in paise. The agent is told the
    verified net and (when present) the razorpay-bound UTR — the ONLY inputs
    the subset tool will accept.
    """
    from langchain_core.messages import HumanMessage

    financial = (state.get("masked_payload") or {}).get("financial_summary", {})
    grand_total_paise = int(financial.get("grand_total_paise", 0) or 0)
    tds_paise = int(financial.get("tds_deduction_paise", 0) or 0)

    # Razorpay leg anchor (reference_id == invoice number)
    db = SessionLocal()
    try:
        row = db.execute(
            text(_ANCHOR_SQL),
            {
                "vendor_code": state["vendor_code"],
                "invoice_number": state["invoice_number"],
            },
        ).first()
    finally:
        db.close()

    updates: dict = {
        "waterfall_status": "WATERFALL_CALCULATED",
        "net_expected_paise": max(0, grand_total_paise - tds_paise),
        "waterfall_flags": [],
    }
    if row is not None:
        payout_id, utr, amount_paise, gateway_paise, narration = row
        updates["razorpay_payout_id"] = payout_id
        updates["razorpay_gateway_paise"] = int(gateway_paise or 0)
        if utr:
            updates["razorpay_utr"] = utr
            updates["razorpay_amount_paise"] = int(amount_paise)
            updates["razorpay_narration"] = narration or ""

    net_rupees = _to_rupees(max(0, grand_total_paise - tds_paise))
    anchor_note = (
        f"Razorpay payout {updates.get('razorpay_payout_id')} is bound to this "
        f"invoice with UTR {updates.get('razorpay_utr')} "
        f"(amount {_to_rupees(int(updates.get('razorpay_amount_paise', 0)))})."
        if updates.get("razorpay_utr")
        else "No razorpay payout references this invoice (direct transfer path)."
    )

    supplier = (state.get("masked_payload") or {}).get("supplier_details", {})
    context_text = (
        f"Reconcile ONE invoice: {state['invoice_number']} (vendor "
        f"{state['vendor_code']}, document {state['document_id']}). "
        f"Supplier: {supplier.get('legal_name') or 'see extracted payload'}. "
        f"Deterministic TDS waterfall net: Rs.{net_rupees} "
        f"(grand total Rs.{_to_rupees(grand_total_paise)} − TDS Rs.{_to_rupees(tds_paise)}). "
        f"{anchor_note} "
        "Use the deterministic tools in the correct order: calculate_tds_mdr_tool "
        "(verify), run_subset_sum_matching_tool (match a bank credit to this "
        "invoice's net — pass the resolved UTR when provided), then "
        "post_ledger_entry_tool with the exact matched_invoice_ids + UTR + net "
        "amount. If the waterfall reports NO_MATCH / AMBIGUOUS_COLLISION, call "
        "run_fuzzy_text_linker_tool once; if it cannot re-anchor a UTR, call "
        "route_to_human_exception_tool. Never invent amounts, UTRs, or invoices."
    )

    updates["messages"] = [
        HumanMessage(content=context_text),
    ]
    return updates
