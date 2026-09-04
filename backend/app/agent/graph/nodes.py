"""Layer 2 — Deterministic graph nodes (ReAct pivot + deterministic fast-path).

The per-invoice sub-graph has THREE deterministic nodes:

    pre_check     -> short-circuit if this document is already reconciled
    context_node  -> deterministic context seed (the ONLY numbers the agent
                     sees): runs the gross-to-net TDS waterfall on the masked
                     payload and binds the razorpay -> UTR anchor leg
                     (razorpay_settlements.reference_id == invoice number).
                     Writes those proofs into state and emits the agent's
                     opening SystemMessage + HumanMessage.
    deterministic -> (supervisor.py) the ~90% fast path: runs the strict
                     3-phase subset-sum waterfall and, on a clean proof,
                     commits the ledger and short-circuits to END via
                     Command(goto="__end__") — the Groq LLM is never woken.

Everything after those nodes is the true ReAct loop (supervisor.py): the
Groq agent reasons, calls the 5 bound tools (react_tools.py), and the tools
enforce the waterfall order via InjectedState guardrails. No tool is called
from a graph edge anymore.
"""

import logging

from sqlalchemy import text

from app.agent.graph.state import ReconciliationState
from app.db.session import SessionLocal

logger = logging.getLogger(__name__)

# =============================================================================
# ReAct agent system prompt (the ONLY policy the LLM ever receives)
# =============================================================================

RECON_SYSTEM_PROMPT = """You are the exception-resolution agent of a production-grade financial reconciliation engine (Layer 2). You are invoked ONLY when the deterministic reconciliation waterfall has already run and could not resolve the current invoice on its own. "Deterministic First, AI Second" governs everything you do:

1. Deterministic rules decide every match. You never perform arithmetic, never decide an amount, UTR, date, or invoice number, and never claim a match that a tool observation did not produce.
2. Your only job is to resolve the invoice the deterministic engine flagged, using the five bound tools — then stop with a terminal tool call.

HARD RULES (never violated):
- Never invent, guess, or reformat monetary amounts, UTRs, dates, payout ids, or invoice numbers. Use exactly the values the context and the tool observations provide.
- Never claim SUBSET_MATCHED, ENTITY_RESOLVED, LEDGER_COMMITTED, or EXCEPTION_ROUTED without the matching tool observation in your history.
- The deterministic gross-to-net TDS waterfall is computed and verified BEFORE you are called; calculate_tds_mdr_tool is optional re-verification only and its arguments must exactly equal the grand total and TDS shown in the context.
- Once route_to_human_exception_tool succeeds, execution hard-ends. Never attempt to re-run the math or "fix" an exception.
- Every invoice you handle must end in exactly one terminal tool call: post_ledger_entry_tool, route_to_human_exception_tool, or the ALREADY_COMMITTED observation. Never finish with prose instead of a terminal tool call.

TOOL PROTOCOL — what each tool does, its outputs, and when it may be called:

1. run_fuzzy_text_linker_tool — Entity-resolution leg. When the deterministic subset waterfall reports NO_MATCH or AMBIGUOUS_COLLISION for this invoice (you will be told its exact outcome), call this FIRST. It fuzzy-matches THIS invoice's supplier legal name against the vendor's razorpay payout narrations (Token Set Ratio + phonetic, threshold 0.85). Outputs: ENTITY_RESOLVED (with resolved_utr / resolved_payout_id / resolved_amount_paise — this is the ONLY tool that may introduce a NEW UTR into the run) or ENTITY_MISMATCH.

2. run_subset_sum_matching_tool — The strict 3-phase deterministic waterfall (amount -> entity -> chronological within +/-7 days) over the vendor's unmatched bank credits and open invoices. Call it ONLY when run_fuzzy_text_linker_tool has just returned ENTITY_RESOLVED and you must re-run the waterfall bound to that resolved UTR. Pass bank_utr_number only if the deterministic engine resolved it (razorpay anchor or fuzzy); pass target_amount_paise only if it equals the context net. Outputs: SUBSET_MATCHED (proof: matched_invoice_ids, matched_bank_utr, bank_transaction_date, phase_applied, net_total_paise), NO_MATCH, or AMBIGUOUS_COLLISION. A SUBSET_MATCHED proof naming this invoice authorizes the ledger commit.

3. calculate_tds_mdr_tool — Optional re-verification of the gross-to-net statutory waterfall. invoice_id, grand_total_rupees and tds_deducted_rupees MUST equal the validated values in the context (enforced by a guardrail). Output: WATERFALL_CALCULATED with net_expected_settlement.

4. post_ledger_entry_tool — The ONLY terminal success path. It atomically writes invoice_reconciliations plus the reconciliation.completed.events outbox event in one PostgreSQL transaction. It is REFUSED (PREREQUISITE_FAILED) unless a SUBSET_MATCHED proof for this invoice exists in LangGraph memory and your envelope (bank_utr_number, matched_invoice_ids, total_reconciled_amount) equals that proof exactly. Call it immediately after a subset proof names this invoice. It can never double-credit: a second attempt returns DUPLICATE_EVENT (ALREADY_COMMITTED).

5. route_to_human_exception_tool — Terminal path for genuinely UNRESOLVED invoices only: call it after run_fuzzy_text_linker_tool returns ENTITY_MISMATCH, or after an AMBIGUOUS_COLLISION that fuzzy could not break. It is REFUSED when a SUBSET_MATCHED proof exists. The exception_reason is derived deterministically (NO_MATCH / AMBIGUOUS_COLLISION / ENTITY_MISMATCH); your human_readable_message may add context for the reviewer.

EXECUTION PROTOCOL for the invoice described in the next message:
1. Read the context: the verified net, the razorpay anchor / attempted UTR, and the deterministic pre-node outcome.
2. If the pre-node outcome is NO_MATCH or AMBIGUOUS_COLLISION -> call run_fuzzy_text_linker_tool once.
   - ENTITY_RESOLVED -> call run_subset_sum_matching_tool with the resolved UTR; on SUBSET_MATCHED call post_ledger_entry_tool with the exact proof values.
   - ENTITY_MISMATCH -> call route_to_human_exception_tool.
3. Never call run_subset_sum_matching_tool twice in a row without a newly resolved UTR, and never call post_ledger_entry_tool before a SUBSET_MATCHED proof exists.

Be terse. Make exactly the tool calls needed, observe their results, and stop with the terminal tool call."""


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


def context_prompt_text(
    state: dict,
    *,
    grand_total_paise: int,
    tds_paise: int,
    anchor_note: str,
) -> str:
    """Build the agent's opening instruction (pure — no DB, unit-testable).

    Frames the deterministic-first / AI-second contract: the deterministic
    pre-node runs next and commits resolvable invoices itself; the LLM is only
    reached when that waterfall fails and must follow the system prompt.
    """
    net_paise = max(0, grand_total_paise - tds_paise)
    supplier = (state.get("masked_payload") or {}).get("supplier_details", {})
    return (
        f"Reconcile ONE invoice: {state['invoice_number']} (vendor "
        f"{state['vendor_code']}, document {state['document_id']}). "
        f"Supplier: {supplier.get('legal_name') or 'see extracted payload'}. "
        f"Deterministic TDS waterfall net: Rs.{_to_rupees(net_paise)} "
        f"(grand total Rs.{_to_rupees(grand_total_paise)} - TDS Rs.{_to_rupees(tds_paise)}). "
        f"{anchor_note} "
        "A deterministic pre-node now runs the strict 3-phase subset-sum "
        "waterfall for this invoice (bound to the anchored UTR when present). "
        "If it resolves a match it commits the ledger and this run ends — you "
        "will not be invoked. You are only invoked when that deterministic "
        "waterfall CANNOT resolve the invoice; you will be given its exact "
        "outcome and must follow your system instructions to resolve it. "
        "Never invent amounts, UTRs, or invoices."
    )


def context_node(state: ReconciliationState) -> dict:
    """Deterministic context seed: TDS waterfall + razorpay anchor + opening
    SystemMessage + HumanMessage for the ReAct agent.

    The masked payload (tokens only, never raw PII) was vault-masked before
    the run; financial fields are integers in paise. The agent is told the
    verified net and (when present) the razorpay-bound UTR — the ONLY inputs
    the subset tool will accept.
    """
    from langchain_core.messages import HumanMessage, SystemMessage

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

    anchor_note = (
        f"Razorpay payout {updates.get('razorpay_payout_id')} is bound to this "
        f"invoice with UTR {updates.get('razorpay_utr')} "
        f"(amount {_to_rupees(int(updates.get('razorpay_amount_paise', 0)))})."
        if updates.get("razorpay_utr")
        else "No razorpay payout references this invoice (direct transfer path)."
    )

    updates["messages"] = [
        SystemMessage(content=RECON_SYSTEM_PROMPT),
        HumanMessage(
            content=context_prompt_text(
                state,
                grand_total_paise=grand_total_paise,
                tds_paise=tds_paise,
                anchor_note=anchor_note,
            )
        ),
    ]
    return updates
