"""Layer 2 — LangGraph state definitions for the per-invoice reconciliation graph.

State is deliberately MINIMAL and PII-safe: only masked invoice payloads and
deterministic tool results live in the checkpointer. The PII rehydration vault
is referenced by run_token (RAM-only) — never serialized.

Since the ReAct pivot, the state also carries the `messages` channel (the
ReAct conversation log) and the deterministic proof channels that tools
write via Command — those channels ARE the "LangGraph memory" the
PREREQUISITE_FAILED guardrails inspect (Core logic.docx, Tool-4 section).
"""

import operator
from typing import Annotated, Any, TypedDict

try:  # langgraph installed (Docker/runtime)
    from langgraph.graph.message import add_messages as _add_messages
except Exception:  # pragma: no cover — offline import guard (host unit tests)
    def _add_messages(left: Any, right: Any) -> Any:  # type: ignore[no-redef]
        combined = list(left or [])
        for item in right or []:
            if not any(
                getattr(existing, "id", None) is not None
                and getattr(existing, "id", None) == getattr(item, "id", None)
                for existing in combined
            ):
                combined.append(item)
        return combined


class ReconciliationState(TypedDict, total=False):
    """Per-invoice graph state.

    Only JSON-serializable primitives — the checkpointer serializes this.
    run_token stays OUT of state and rides in RunnableConfig instead.

    The `messages` channel is the ReAct conversation log (AIMessages with
    tool_calls + ToolMessage observations). The deterministic proof fields
    below (subset_*, matched_*, fuzzy_*, terminal_*) are written by tools via
    Command(update=...) and inspected by the PREREQUISITE_FAILED guardrails.
    """

    # ReAct conversation log (reducer: langgraph add_messages)
    messages: Annotated[list[Any], _add_messages]

    # context + masked payload
    batch_id: str
    vendor_code: str
    document_id: str
    invoice_number: str
    masked_payload: dict[str, Any]

    # razorpay leg anchor (written by context_node, deterministic DB lookup)
    razorpay_payout_id: str | None
    razorpay_utr: str | None
    razorpay_amount_paise: int | None
    razorpay_gateway_paise: int | None
    razorpay_narration: str | None

    # deterministic tool outputs
    waterfall_status: str | None
    net_expected_paise: int | None
    waterfall_flags: list[str]

    subset_status: str | None
    matched_invoice_ids: Annotated[list[str], operator.add]
    matched_bank_utr: str | None
    bank_transaction_date: str | None
    phase_applied: int | None
    subset_net_total_paise: int | None
    subset_message: str

    fuzzy_status: str | None
    fuzzy_score: float | None
    fuzzy_message: str
    fuzzy_attempted: bool
    # deterministic fuzzy re-anchor (written by run_fuzzy_text_linker_tool)
    fuzzy_resolved_utr: str | None
    fuzzy_resolved_payout_id: str | None
    fuzzy_resolved_amount_paise: int | None

    # terminal
    terminal_status: str | None  # LEDGER_COMMITTED | ALREADY_COMMITTED | EXCEPTION_ROUTED | BLOCKED | ERROR
    terminal_detail: str
    terminal_utr: str | None
    terminal_payout_id: str | None
    outbox_event_id: str | None
