"""
Layer 2 — ReAct Tool Belt (5 tools bound to the Groq supervisor LLM)

This is the pivot from the static graph to a true ReAct agent. The LLM decides
WHICH tool to call and with WHAT arguments; every tool is a thin LangChain
`@tool` wrapper over the untouched M1 deterministic kernels.

Two architectural pillars (Core logic.docx — Tool-4 section + 4-Tier Guardrail):

1. State Guardrails inside the tools (PREREQUISITE_FAILED). Each wrapper
   declares `state: Annotated[dict, InjectedState]` — LangGraph injects the
   current checkpointed graph state and REMOVES the parameter from the LLM
   schema. Guardrail logic inspects that memory FIRST:
       * post_ledger_entry_tool refuses unless a SUBSET_MATCHED proof for this
         invoice exists in state (run_subset_sum_matching_tool must have run).
       * subset tool only accepts a UTR that the deterministic anchor / fuzzy
         leg actually resolved (LLM cannot invent a credit).
       * route_to_human_exception refuses when a subset proof exists (the LLM
         must prefer the ledger; exceptions are for proven non-matches).
   A failed guardrail returns a clear error STRING to the LLM so the ReAct
   loop self-corrects — never an exception, never a silent pass.

2. Proof persistence via Command. On success the tools return
   `Command(update={...})` so deterministic proofs (subset_*, matched_*,
   terminal_*) are written INTO LangGraph state — that IS the durable
   "LangGraph memory" (PostgresSaver) the next tool call inspects. A
   ToolMessage observation (JSON) is embedded in the same Command so the LLM
   sees the outcome of its call.

The pure `_guard_*` helpers at the top are langchain-free so the guardrail
semantics are unit-testable on any host; `build_react_tools()` (lazy) wraps
them for ToolNode execution where langgraph is installed.
"""

import json
import logging

from sqlalchemy import text

from app.agent.tools.common import paise_to_rupees, rupees_to_paise
from app.agent.tools.fuzzy_linker import run_fuzzy_text_linker
from app.agent.tools.human_exception import route_to_human_exception
from app.agent.tools.ledger_entry import post_ledger_entry
from app.agent.tools.subset_sum import run_subset_sum_matching
from app.agent.tools.tds_mdr import calculate_tds_mdr
from app.db.session import SessionLocal
from app.schemas.layer2_tools import (
    FuzzyLinkerInput,
    HumanExceptionInput,
    PostLedgerInput,
    SubsetSumInput,
    SubsetSumResult,
    SubsetSumStatus,
    TdsMdrInput,
)

logger = logging.getLogger(__name__)

TERMINAL_STATUSES = {"LEDGER_COMMITTED", "ALREADY_COMMITTED", "EXCEPTION_ROUTED"}

# deterministic exception reasons the pipeline recognises
_DETERMINISTIC_REASONS = {
    "NO_MATCH",
    "AMBIGUOUS_COLLISION",
    "ENTITY_MISMATCH",
    "NEGATIVE_NET_SETTLEMENT",
    "INVALID_PAISE_CASTING",
}


def _to_rupees(paise: int) -> str:
    return paise_to_rupees(int(paise))


# =============================================================================
# Pure guard helpers (langchain-free — unit-testable)
# =============================================================================


def tds_input_mismatch_reason(
    state: dict,
    *,
    invoice_id: str,
    grand_total_rupees: str,
    tds_deducted_rupees: str,
) -> str | None:
    """Return a PREREQUISITE reason when the LLM's TDS args disagree with the
    deterministic pre-node values (the LLM must not re-derive amounts)."""
    financial = (state.get("masked_payload") or {}).get("financial_summary", {})
    expected_grand = _to_rupees(int(financial.get("grand_total_paise", 0) or 0))
    expected_tds = _to_rupees(int(financial.get("tds_deduction_paise", 0) or 0))
    if invoice_id != state.get("invoice_number"):
        return "PREREQUISITE_FAILED: invoice_id does not match the current reconciliation run."
    if grand_total_rupees != expected_grand or tds_deducted_rupees != expected_tds:
        return (
            "PREREQUISITE_FAILED: amounts must match the validated extraction context "
            f"(grand_total={expected_grand}, tds={expected_tds}); do not recompute from memory."
        )
    return None


def subset_utr_is_allowed(state: dict, bank_utr_number: str | None) -> bool:
    """A UTR the LLM passes must be one the deterministic engine resolved:
    the razorpay anchor leg, or the fuzzy re-anchor. Never a free invention."""
    if bank_utr_number is None:
        return True
    resolved = {state.get("razorpay_utr"), state.get("fuzzy_resolved_utr")}
    return bank_utr_number in {u for u in resolved if u}


def subset_target_ok(state: dict, target_amount_paise: int | None) -> bool:
    """The requested target (if given) must equal the deterministic net the
    pre-node (or razorpay anchor) established for this invoice."""
    if target_amount_paise is None:
        return True
    anchor = state.get("razorpay_amount_paise")
    expected = int(anchor) if anchor else state.get("net_expected_paise")
    return expected is not None and int(target_amount_paise) == int(expected)


def ledger_guard_reason(
    state: dict,
    *,
    bank_utr_number: str,
    matched_invoice_ids: list[str],
    total_reconciled_amount: str,
) -> str | None:
    """State guardrail (Core logic Tool-4): refuse unless the subset-sum proof
    is present in LangGraph memory and the LLM envelope matches it exactly."""
    if state.get("subset_status") != SubsetSumStatus.SUBSET_MATCHED.value:
        return (
            "PREREQUISITE_FAILED: run_subset_sum_matching_tool must return "
            "SUBSET_MATCHED before the ledger can be committed."
        )
    if bank_utr_number != state.get("matched_bank_utr"):
        return (
            "PREREQUISITE_FAILED: bank_utr_number must equal the UTR the "
            f"subset engine matched ({state.get('matched_bank_utr')})."
        )
    proof_ids = set(state.get("matched_invoice_ids") or [])
    if set(matched_invoice_ids or []) != proof_ids:
        return (
            "PREREQUISITE_FAILED: matched_invoice_ids must equal the subset "
            f"proof ({sorted(proof_ids)})."
        )
    if state.get("invoice_number") not in proof_ids:
        return "PREREQUISITE_FAILED: the current invoice is not part of the matched subset."
    try:
        amount_paise = rupees_to_paise(total_reconciled_amount)
    except ValueError:
        return "PREREQUISITE_FAILED: INVALID_PAISE_CASTING for total_reconciled_amount."
    proof_total = state.get("subset_net_total_paise")
    if proof_total is None or amount_paise != int(proof_total):
        return (
            "PREREQUISITE_FAILED: total_reconciled_amount must equal the "
            f"subset proof net total ({_to_rupees(int(proof_total)) if proof_total else 'unknown'})."
        )
    return None


def exception_guard_reason(state: dict) -> str | None:
    """Do not let the LLM route a subset-matched invoice to human review:
    the ledger is the only terminal for a proven match."""
    if state.get("subset_status") == SubsetSumStatus.SUBSET_MATCHED.value:
        return (
            "PREREQUISITE_FAILED: this invoice has a SUBSET_MATCHED proof — "
            "call post_ledger_entry_tool, do not route to human exception."
        )
    return None


def derive_exception_reason(state: dict) -> str:
    """Deterministic exception reason from the tool outcomes in state — the
    LLM may not invent reasons (fallback whitelist enforced below)."""
    if state.get("subset_status") == SubsetSumStatus.NO_MATCH.value:
        return "NO_MATCH"
    if state.get("subset_status") == SubsetSumStatus.AMBIGUOUS_COLLISION.value:
        return "AMBIGUOUS_COLLISION"
    if state.get("fuzzy_attempted") and state.get("fuzzy_status") == "ENTITY_MISMATCH":
        return "ENTITY_MISMATCH"
    if state.get("waterfall_status") and state.get("waterfall_status") != "WATERFALL_CALCULATED":
        return str(state.get("waterfall_status"))
    return "REASON_UNSPECIFIED"


# =============================================================================
# ReAct tool construction (lazy — requires langgraph/langchain installed)
# =============================================================================


def build_react_tools() -> list:
    """Return the five ReAct tools bound for ToolNode execution.

    Imports are intentionally lazy: the module stays importable on hosts where
    langgraph is not installed (pure guard helpers above remain testable).
    """
    from langchain_core.messages import ToolMessage
    from langchain_core.tools import tool
    from langgraph.prebuilt import InjectedState, ToolRuntime
    from langgraph.types import Command
    from typing import Annotated

    from app.core.config import get_settings

    def _observe(runtime: ToolRuntime, payload: dict) -> ToolMessage:
        return ToolMessage(
            content=json.dumps(payload, default=str),
            tool_call_id=runtime.tool_call_id,
        )

    def _db_error_message(runtime: ToolRuntime, exc: Exception) -> ToolMessage:
        logger.error("ReAct tool DB failure", extra={"error": str(exc)})
        return _observe(
            runtime,
            {"status": "TOOL_ERROR", "message": f"database error: {exc}"},
        )

    # ------------------------------------------------------------------ 1/5
    @tool
    def calculate_tds_mdr_tool(
        invoice_id: str,
        grand_total_rupees: str,
        tds_deducted_rupees: str,
        state: Annotated[dict, InjectedState()],
        runtime: ToolRuntime,
    ) -> ToolMessage | Command:
        """Recompute the statutory gross-to-net waterfall for THIS invoice.
        Args must equal the validated extraction values shown in the context.
        Writes net_expected_paise + WATERFALL_CALCULATED into state on success."""
        mismatch = tds_input_mismatch_reason(
            state,
            invoice_id=invoice_id,
            grand_total_rupees=grand_total_rupees,
            tds_deducted_rupees=tds_deducted_rupees,
        )
        if mismatch:
            return _observe(runtime, {"status": "PREREQUISITE_FAILED", "message": mismatch})

        result = calculate_tds_mdr(
            TdsMdrInput(
                invoice_id=invoice_id,
                grand_total_rupees=grand_total_rupees,
                tds_deducted_rupees=tds_deducted_rupees,
                tds_category_code=get_settings().layer2_tds_category,
                gateway_fees_paise=0,
                gateway_tax_paise=0,
            )
        )
        payload = {
            "status": result.status.value,
            "invoice_id": result.invoice_id,
            "net_expected_settlement": result.net_expected_settlement,
            "flags": list(result.flags),
            "message": result.message,
        }
        if result.status.value != "WATERFALL_CALCULATED" or not result.net_expected_settlement:
            return _observe(runtime, payload)

        rupees, _, fraction = result.net_expected_settlement.partition(".")
        net_paise = int(rupees) * 100 + int((fraction or "00").ljust(2, "0")[:2])
        payload["net_expected_paise"] = net_paise
        return Command(
            update={
                "waterfall_status": result.status.value,
                "net_expected_paise": net_paise,
                "waterfall_flags": list(result.flags),
                "messages": [_observe(runtime, payload)],
            }
        )

    # ------------------------------------------------------------------ 2/5
    @tool
    def run_subset_sum_matching_tool(
        bank_utr_number: str | None,
        target_amount_paise: int | None,
        state: Annotated[dict, InjectedState()],
        runtime: ToolRuntime,
    ) -> ToolMessage | Command:
        """Run the strict 3-phase subset-sum waterfall against open bank
        credits for this vendor. bank_utr_number must be a UTR the engine
        resolved (anchor/fuzzy). Writes the SUBSET_MATCHED proof into state."""
        if not subset_utr_is_allowed(state, bank_utr_number):
            return _observe(
                runtime,
                {
                    "status": "PREREQUISITE_FAILED",
                    "message": "bank_utr_number was not resolved by the deterministic anchor/fuzzy leg; omit it or use the resolved UTR from the context.",
                },
            )
        if not subset_target_ok(state, target_amount_paise):
            return _observe(
                runtime,
                {
                    "status": "PREREQUISITE_FAILED",
                    "message": "target_amount_paise must equal the deterministic net for this invoice.",
                },
            )

        if state.get("razorpay_amount_paise"):
            target = int(state["razorpay_amount_paise"])
        elif target_amount_paise is not None:
            target = int(target_amount_paise)
        elif state.get("net_expected_paise") is not None:
            target = int(state["net_expected_paise"])
        else:
            return _observe(
                runtime,
                {"status": "PREREQUISITE_FAILED", "message": "run calculate_tds_mdr_tool first (net_expected_paise missing)."},
            )

        inp = SubsetSumInput(
            vendor_code=str(state["vendor_code"]),
            target_amount_paise=target,
            bank_utr_number=bank_utr_number,
            date_tolerance_days=7,
        )
        db = SessionLocal()
        try:
            result = run_subset_sum_matching(inp, db)
        except Exception as exc:
            return _db_error_message(runtime, exc)
        finally:
            db.close()

        matched = list(result.matched_invoice_ids or [])
        observation = {
            "status": result.status.value,
            "matched_invoice_ids": matched,
            "matched_bank_utr": result.matched_bank_utr,
            "bank_transaction_date": result.bank_transaction_date,
            "phase_applied": result.phase_applied,
            "net_total_paise": result.net_total_paise,
            "message": result.message,
        }
        # Membership guard: a match resolved to OTHER invoices is NO_MATCH for
        # this run — the sub-graph must never commit a sibling's match.
        if result.status.value == "SUBSET_MATCHED" and state.get("invoice_number") not in matched:
            observation["status"] = "NO_MATCH"
            observation["message"] = (
                "Collision resolved to other open invoice(s); this invoice is not part of the matched subset."
            )
            return _observe(runtime, observation)

        if result.status.value != "SUBSET_MATCHED":
            return _observe(runtime, observation)

        return Command(
            update={
                "subset_status": SubsetSumStatus.SUBSET_MATCHED.value,
                "matched_invoice_ids": matched,
                "matched_bank_utr": result.matched_bank_utr,
                "bank_transaction_date": result.bank_transaction_date,
                "phase_applied": result.phase_applied,
                "subset_net_total_paise": result.net_total_paise,
                "subset_message": result.message,
                "messages": [_observe(runtime, observation)],
            }
        )

    # ------------------------------------------------------------------ 3/5
    @tool
    def run_fuzzy_text_linker_tool(
        state: Annotated[dict, InjectedState()],
        runtime: ToolRuntime,
    ) -> ToolMessage | Command:
        """Entity-resolution leg: fuzzy-match THIS invoice's supplier against
        the vendor's razorpay payout narrations (Token Set Ratio + phonetic).
        On a deterministic hit it re-anchors the run on the resolved payout UTR."""
        supplier = (state.get("masked_payload") or {}).get("supplier_details", {})
        source_entity = supplier.get("legal_name") or state.get("invoice_number")

        db = SessionLocal()
        try:
            rows = db.execute(
                text(
                    """
                    SELECT payout_id, utr, amount_paise, narration
                    FROM razorpay_settlements
                    WHERE vendor_code = :vendor_code
                      AND utr IS NOT NULL
                      AND narration IS NOT NULL
                      AND narration <> ''
                    ORDER BY ingested_at DESC
                    LIMIT 20
                    """
                ),
                {"vendor_code": state.get("vendor_code")},
            ).all()
        except Exception as exc:
            db.close()
            return _db_error_message(runtime, exc)

        best: tuple[float, dict] | None = None
        for payout_id, utr, amount_paise, narration in rows:
            result = run_fuzzy_text_linker(
                FuzzyLinkerInput(
                    source_entity_name=str(source_entity),
                    target_bank_narration=str(narration or ""),
                    context_vendor_code=str(state.get("vendor_code")),
                    match_threshold=0.85,
                )
            )
            if result.status.value == "ENTITY_RESOLVED":
                if best is None or result.confidence_score > best[0]:
                    best = (result.confidence_score, {
                        "payout_id": str(payout_id),
                        "utr": str(utr),
                        "amount_paise": int(amount_paise),
                        "confidence": result.confidence_score,
                    })
        db.close()

        if best is None:
            observation = {
                "status": "ENTITY_MISMATCH",
                "message": "No razorpay payout narration resolved the supplier; route to human exception.",
            }
            return Command(
                update={
                    "fuzzy_status": "ENTITY_MISMATCH",
                    "fuzzy_attempted": True,
                    "fuzzy_score": 0.0,
                    "fuzzy_message": observation["message"],
                    "messages": [_observe(runtime, observation)],
                }
            )

        score, resolved = best
        observation = {
            "status": "ENTITY_RESOLVED",
            "resolved_payout_id": resolved["payout_id"],
            "resolved_utr": resolved["utr"],
            "resolved_amount_paise": resolved["amount_paise"],
            "confidence_score": round(resolved["confidence"], 4),
            "message": "Re-anchored on razorpay payout UTR — re-run run_subset_sum_matching_tool with this UTR.",
        }
        return Command(
            update={
                "fuzzy_status": "ENTITY_RESOLVED",
                "fuzzy_attempted": True,
                "fuzzy_score": float(score),
                "fuzzy_resolved_utr": resolved["utr"],
                "fuzzy_resolved_payout_id": resolved["payout_id"],
                "fuzzy_resolved_amount_paise": int(resolved["amount_paise"]),
                "fuzzy_message": observation["message"],
                "messages": [_observe(runtime, observation)],
            }
        )

    # ------------------------------------------------------------------ 4/5
    @tool
    def post_ledger_entry_tool(
        bank_utr_number: str,
        matched_invoice_ids: list[str],
        total_reconciled_amount: str,
        state: Annotated[dict, InjectedState()],
        runtime: ToolRuntime,
    ) -> ToolMessage | Command:
        """Commit the reconciled record atomically (invoice_reconciliations +
        outbox_events). State guardrail: SUBSET_MATCHED proof must exist in
        LangGraph memory and the envelope must match it exactly."""
        guard = ledger_guard_reason(
            state,
            bank_utr_number=bank_utr_number,
            matched_invoice_ids=matched_invoice_ids,
            total_reconciled_amount=total_reconciled_amount,
        )
        if guard:
            return _observe(runtime, {"status": "PREREQUISITE_FAILED", "message": guard})

        proof = SubsetSumResult(
            status=SubsetSumStatus.SUBSET_MATCHED,
            matched_invoice_ids=list(state.get("matched_invoice_ids") or []),
            matched_bank_utr=state.get("matched_bank_utr"),
            bank_transaction_date=state.get("bank_transaction_date"),
            phase_applied=state.get("phase_applied"),
            net_total_paise=state.get("subset_net_total_paise"),
            message=state.get("subset_message", ""),
        )
        inp = PostLedgerInput(
            vendor_code=str(state["vendor_code"]),
            matched_invoice_ids=list(state.get("matched_invoice_ids") or []),
            razorpay_payout_id=(
                state.get("razorpay_payout_id")
                or state.get("fuzzy_resolved_payout_id")
            ),
            bank_utr_number=bank_utr_number,
            total_reconciled_amount=total_reconciled_amount,
        )

        db = SessionLocal()
        try:
            result = post_ledger_entry(
                db,
                inp=inp,
                proof=proof,
                batch_id=state.get("batch_id"),
            )
        except Exception as exc:
            return _db_error_message(runtime, exc)
        finally:
            db.close()

        observation = {
            "status": result.status.value,
            "outbox_event_id": (result.outbox_event_ids or [None])[0],
            "message": result.message,
        }
        if result.status.value == "LEDGER_COMMITTED":
            terminal = "LEDGER_COMMITTED"
        elif result.status.value == "DUPLICATE_EVENT":
            terminal = "ALREADY_COMMITTED"
        else:  # PREREQUISITE_FAILED — observation only, let the LLM self-correct
            return _observe(runtime, observation)

        return Command(
            update={
                "terminal_status": terminal,
                "terminal_detail": result.message,
                "terminal_utr": bank_utr_number,
                "terminal_payout_id": inp.razorpay_payout_id,
                "outbox_event_id": observation["outbox_event_id"],
                "messages": [_observe(runtime, observation)],
            }
        )

    # ------------------------------------------------------------------ 5/5
    @tool
    def route_to_human_exception_tool(
        exception_reason: str | None,
        human_readable_message: str | None,
        state: Annotated[dict, InjectedState()],
        runtime: ToolRuntime,
    ) -> ToolMessage | Command:
        """Route an UNRESOLVED invoice to the DLQ (reconciliation.dlq.events
        via outbox). Guardrail: refuses when a SUBSET_MATCHED proof exists —
        the ledger is the only terminal for a proven match. Hard END follows."""
        guard = exception_guard_reason(state)
        if guard:
            return _observe(runtime, {"status": "PREREQUISITE_FAILED", "message": guard})

        # Deterministic reason wins; the LLM may only enrich the human message.
        reason = derive_exception_reason(state)
        if exception_reason and exception_reason not in _DETERMINISTIC_REASONS:
            logger.warning(
                "Rejecting non-deterministic LLM exception reason",
                extra={"llm_reason": exception_reason, "used": reason},
            )
        message = (human_readable_message or "").strip() or (
            state.get("subset_message")
            or state.get("terminal_detail")
            or "Unresolved after deterministic 3-phase waterfall — human review required."
        )

        inp = HumanExceptionInput(
            vendor_code=str(state["vendor_code"]),
            flagged_invoice_ids=[str(state["invoice_number"])],
            bank_utr_number=state.get("matched_bank_utr") or state.get("fuzzy_resolved_utr"),
            exception_reason=reason,
            human_readable_message=message[:500],
        )
        db = SessionLocal()
        try:
            result = route_to_human_exception(db, inp=inp)
        except Exception as exc:
            return _db_error_message(runtime, exc)
        finally:
            db.close()

        observation = {
            "status": "EXCEPTION_ROUTED",
            "outbox_event_id": result.outbox_event_id,
            "exception_reason": result.exception_reason,
            "action_required": result.action_required,
        }
        return Command(
            update={
                "terminal_status": "EXCEPTION_ROUTED",
                "terminal_detail": message[:500],
                "terminal_utr": inp.bank_utr_number,
                "outbox_event_id": result.outbox_event_id,
                "messages": [_observe(runtime, observation)],
            }
        )

    return [
        calculate_tds_mdr_tool,
        run_subset_sum_matching_tool,
        run_fuzzy_text_linker_tool,
        post_ledger_entry_tool,
        route_to_human_exception_tool,
    ]
