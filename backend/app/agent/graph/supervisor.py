"""Layer 2 — True ReAct Reconciliation Supervisor (per-invoice sub-graphs).

Pivot from the static deterministic graph to a genuine ReAct (Reason->Act->
Observe) agent, exactly as Core logic.docx prescribes:

    Hierarchical Supervisor Agent (1 LLM router + 5 isolated, deterministic
    Python worker tools).

Per sealed batch, ONE isolated sub-graph per invoice (Map phase):

    thread_id      = "{batch_id}::{document_id}"
    checkpoint_ns  = "reconciliation::{batch_id}"
    recursion_limit = 16  (bounded — an infinite ReAct loop force-halts)

Graph shape (Deterministic First, AI Second):

    START -> pre_check  --already reconciled--> END (token saver)
                |
                v
            context_node   (deterministic context seed: TDS net + razorpay
                            anchor + masked invoice -> SystemMessage +
                            opening HumanMessage)
                |
                v
            deterministic  (FAST PATH: strict 3-phase subset-sum waterfall +
                            ledger commit. Clean SUBSET_MATCHED proof ->
                            Command(goto="__end__") hard short-circuit — the
                            Groq LLM is never woken for resolvable invoices)
              |  (plain return: NO_MATCH / AMBIGUOUS_COLLISION / membership
              |   miss — outcome recorded in state + conversation note)
                v
            agent          (Groq ReAct model, .bind_tools(5 M1 tools))
              |  ^
              v  |          tools_condition: has tool_calls -> tools
            tools          (langgraph.prebuilt.ToolNode -> react_tools.py)
              |
              +-- terminal status set (LEDGER_COMMITTED / ALREADY_COMMITTED /
              |           EXCEPTION_ROUTED) --> END   [hard END]
              +-- no tool calls, no terminal --> finalize (deterministic DLQ
                        route) --> END

The agent NEVER performs arithmetic and NEVER decides a match: the tools
enforce the Deterministic Waterfall order internally via InjectedState
guardrails (PREREQUISITE_FAILED returned to the LLM for self-correction) and
write proofs into state via Command. The commit path can only be reached
after the subset-sum proof exists in LangGraph memory.

Public API (unchanged for recon_supervisor / tests):
    build_recon_graph / build_persisted_graph / get_or_create_thread_saver
    make_thread_config / invoke_invoice / GroqReActModel
"""

import logging
import threading
from typing import Any

logger = logging.getLogger(__name__)

# Thread-local compiled graph + PostgresSaver: one saver connection per pool
# worker thread (psycopg connections are NOT thread-safe).
_thread_local = threading.local()

TERMINAL_STATUSES = {"LEDGER_COMMITTED", "ALREADY_COMMITTED", "EXCEPTION_ROUTED"}


# =============================================================================
# Pure routing helpers (unit-testable without langgraph installed)
# =============================================================================


def is_terminal_status(status: Any) -> bool:
    return status in TERMINAL_STATUSES


def route_after_precheck(state: dict) -> str:
    """END immediately when pre_check short-circuited, else seed context."""
    if state.get("terminal_status") == "ALREADY_COMMITTED":
        return "END"
    return "context"


def last_message_has_tool_calls(state: dict) -> bool:
    """True when the last AIMessage in state requests tool execution."""
    messages = state.get("messages") or []
    for message in reversed(messages):
        tool_calls = getattr(message, "tool_calls", None)
        if tool_calls is None and isinstance(message, dict):
            tool_calls = message.get("tool_calls")
        if tool_calls is not None:
            return len(tool_calls) > 0
    return False


def route_after_tools(state: dict) -> str:
    """Terminal proof written by a tool -> hard END; otherwise loop the agent."""
    if is_terminal_status(state.get("terminal_status")):
        return "END"
    return "agent"


def route_after_deterministic(state: dict) -> str:
    """Plain (non-Command) returns from the deterministic fast-path: a
    terminal (safety net) -> hard END; otherwise hand the unresolved invoice
    to the ReAct agent. Command(goto="__end__") returns never reach here."""
    if is_terminal_status(state.get("terminal_status")):
        return "END"
    return "agent"


# =============================================================================
# Deterministic fast-path pure helpers (deterministic-first / AI-second)
# =============================================================================


def fast_path_target_paise(state: dict) -> int | None:
    """Net target the deterministic subset engine must reconcile for this
    invoice: the razorpay-anchored payout amount wins, else the verified
    TDS-waterfall net. Mirrors the deterministic fallback agent's choice."""
    anchor = state.get("razorpay_amount_paise")
    if anchor:
        return int(anchor)
    net = state.get("net_expected_paise")
    if net is not None:
        return int(net)
    return None


def can_fast_path_commit(
    result_status: str, matched_invoice_ids: list, invoice_number: str
) -> bool:
    """A waterfall proof may be committed deterministically ONLY when it names
    THIS invoice (membership guard — never commit a sibling's match)."""
    return (
        result_status == "SUBSET_MATCHED"
        and invoice_number in (matched_invoice_ids or [])
    )


def fast_path_outcome_text(state: dict, status: str, message: str) -> str:
    """Conversation note appended when the deterministic fast-path hands an
    unresolved invoice to the ReAct agent, so the LLM never blindly re-runs
    the subset engine."""
    invoice = state.get("invoice_number") or "?"
    utr = state.get("razorpay_utr") or state.get("fuzzy_resolved_utr")
    net = fast_path_target_paise(state)
    lines = [
        f"Deterministic pre-node outcome for {invoice}: "
        f"run_subset_sum_matching_tool returned {status}.",
    ]
    if message:
        lines.append(f"Engine message: {message[:300]}")
    if utr:
        lines.append(f"UTR attempted by the pre-node: {utr}.")
    if net is not None:
        whole, rem = divmod(int(net), 100)
        lines.append(f"Deterministic net target used: Rs.{whole}.{rem:02d}.")
    lines.append(
        "Do NOT call run_subset_sum_matching_tool again unless "
        "run_fuzzy_text_linker_tool resolves a NEW bank UTR for this invoice. "
        "Call run_fuzzy_text_linker_tool first; on ENTITY_MISMATCH call "
        "route_to_human_exception_tool; on ENTITY_RESOLVED re-run the subset "
        "tool with the resolved UTR, then post_ledger_entry_tool."
    )
    return "\n".join(lines)


# =============================================================================
# Groq ReAct model proxy (rate-limited at the real network boundary)
# =============================================================================


class GroqReActModel:
    """Callable(state) -> AIMessage updates, running the tool-bound Groq model
    through RateLimitedGroqClient (semaphore + Redis token bucket + jitter)."""

    def __init__(self, client: Any):
        self._client = client
        self._bound = None
        self._tools = None

    def __call__(self, state: dict) -> dict:
        if self._bound is None:
            from app.agent.tools.react_tools import build_react_tools

            self._tools = build_react_tools()
            self._bound = self._client.bind_tools(self._tools)
        result = self._client.invoke(self._bound, state["messages"])
        return {"messages": [result]}


def _build_react_graph(supervisor_model: Any | None, checkpointer: Any | None = None) -> Any:
    """Compile the per-invoice ReAct StateGraph (optionally persisted).

    supervisor_model: GroqReActModel (rate-limited Groq) or None -> the graph
    compiles with a deterministic fallback agent so the pipeline still runs
    end-to-end when Groq is unreachable at startup.
    """
    from langgraph.graph import END, START, StateGraph
    from langgraph.prebuilt import ToolNode, tools_condition

    from app.agent.graph import nodes
    from app.agent.graph.state import ReconciliationState
    from app.agent.tools.react_tools import build_react_tools

    graph = StateGraph(ReconciliationState)

    if supervisor_model is not None:
        def _agent(state: dict) -> dict:
            return _invoke_agent(supervisor_model, state)

        agent_impl = _agent
    else:
        agent_impl = _deterministic_agent

    graph.add_node("pre_check", nodes.pre_check)
    graph.add_node("context", nodes.context_node)
    graph.add_node("deterministic", deterministic_fast_path)
    graph.add_node("agent", agent_impl)
    graph.add_node("tools", ToolNode(build_react_tools()))
    graph.add_node("finalize", _finalize_node)

    graph.add_edge(START, "pre_check")
    graph.add_conditional_edges("pre_check", route_after_precheck, {"END": END, "context": "context"})
    graph.add_edge("context", "deterministic")
    # Deterministic fast-path: Command(goto="__end__") hard short-circuits
    # committed invoices (LLM never woken); plain returns (NO_MATCH / collision)
    # flow to the ReAct agent via route_after_deterministic.
    graph.add_conditional_edges(
        "deterministic", route_after_deterministic, {"END": END, "agent": "agent"}
    )
    # Standard ReAct routing: tool calls -> tools; otherwise -> finalize.
    graph.add_conditional_edges("agent", tools_condition, {"tools": "tools", "__end__": "finalize"})
    graph.add_conditional_edges("tools", route_after_tools, {"END": END, "agent": "agent"})
    graph.add_edge("finalize", END)

    return graph.compile(checkpointer=checkpointer)


def build_recon_graph(supervisor_model: Any | None = None) -> Any:
    """Compile a plain (in-memory) ReAct graph — tests / offline runs."""
    return _build_react_graph(supervisor_model, checkpointer=None)


def build_persisted_graph(supervisor_model: Any | None = None) -> Any:
    """Compile the ReAct graph bound to THIS thread's PostgresSaver."""
    graph = getattr(_thread_local, "recon_graph", None)
    if graph is None:
        saver = get_or_create_thread_saver()
        graph = _build_react_graph(supervisor_model, checkpointer=saver)
        _thread_local.recon_graph = graph
    return graph


def get_or_create_thread_saver() -> Any:
    """Thread-local PostgresSaver (psycopg connections are not thread-safe)."""
    saver = getattr(_thread_local, "recon_saver", None)
    if saver is None:
        from langgraph.checkpoint.postgres import PostgresSaver

        from app.core.config import get_settings

        import psycopg
        from psycopg.rows import dict_row

        url = get_settings().database_url
        dsn = url.replace("postgresql+psycopg://", "postgresql://")
        # from_conn_string() is a @contextmanager in langgraph-checkpoint-postgres
        # 3.1.2 and closes the connection on exit — build the saver on our own
        # long-lived, thread-local psycopg connection instead.
        conn = psycopg.connect(
            dsn, autocommit=True, prepare_threshold=0, row_factory=dict_row
        )
        saver = PostgresSaver(conn)
        # Idempotent (IF NOT EXISTS) — tables pre-created by migration 004.
        saver.setup()
        _thread_local.recon_saver = saver
    return saver


# =============================================================================
# Runtime config + invocation (Map leaf)
# =============================================================================

_RECURSION_LIMIT = 16  # bounded ReAct loop; +1 for the deterministic
# fast-path prefix node; hard END after exception routing


def make_thread_config(batch_id: str, document_id: str, *, run_token: str | None = None) -> dict:
    """Per-invoice RunnableConfig (thread isolation + PII vault run token)."""
    configurable: dict[str, Any] = {
        "thread_id": f"{batch_id}::{document_id}",
        "checkpoint_ns": f"reconciliation::{batch_id}",
    }
    if run_token:
        configurable["run_token"] = run_token
    return {
        "configurable": configurable,
        "recursion_limit": _RECURSION_LIMIT,
    }


def invoke_invoice(graph: Any, *, state: dict, config: dict) -> dict:
    """Run one invoice sub-graph (Map leaf) with strict recursion cap."""
    result = graph.invoke(state, config=config)
    if isinstance(result, dict):
        return result
    return {}


# =============================================================================
# Deterministic fast-path node (deterministic-first / AI-second)
# =============================================================================


def _subset_for_state(state: dict):
    """Run the strict 3-phase subset engine for THIS invoice (own DB session).
    Target = razorpay-anchored payout amount, else the verified waterfall net;
    UTR restricted to the context-seeded razorpay anchor."""
    from app.db.session import SessionLocal
    from app.agent.tools.subset_sum import run_subset_sum_matching
    from app.schemas.layer2_tools import SubsetSumInput

    target = fast_path_target_paise(state)
    inp = SubsetSumInput(
        vendor_code=state["vendor_code"],
        target_amount_paise=int(target or 0),
        bank_utr_number=state.get("razorpay_utr") or None,
        date_tolerance_days=7,
    )
    db = SessionLocal()
    try:
        return run_subset_sum_matching(inp, db)
    finally:
        db.close()


def _commit_proof_for_state(state: dict, proof):
    """Deterministic ledger commit of a SUBSET_MATCHED proof (own DB session)."""
    from app.db.session import SessionLocal
    from app.agent.tools.ledger_entry import post_ledger_entry
    from app.agent.tools.react_tools import _to_rupees
    from app.schemas.layer2_tools import PostLedgerInput

    inp = PostLedgerInput(
        vendor_code=state["vendor_code"],
        matched_invoice_ids=list(proof.matched_invoice_ids or []),
        razorpay_payout_id=state.get("razorpay_payout_id"),
        bank_utr_number=proof.matched_bank_utr or "",
        total_reconciled_amount=_to_rupees(int(proof.net_total_paise or 0)),
    )
    db = SessionLocal()
    try:
        return post_ledger_entry(db, inp=inp, proof=proof, batch_id=state.get("batch_id"))
    finally:
        db.close()


def deterministic_fast_path(state: dict) -> dict:
    """Pre-LLM deterministic waterfall (the ~90% fast path).

    On a clean SUBSET_MATCHED proof naming this invoice it commits the ledger
    immediately and hard short-circuits to END via Command(goto="__end__") —
    the Groq LLM is never woken (state saved, transaction logged, execution
    terminates). On NO_MATCH / AMBIGUOUS_COLLISION (or a membership miss) it
    records the deterministic outcome in state AND appends a conversation
    note, then flows to the ReAct agent for exception resolution.

    Guarded by idempotency end-to-end: if the commit raced another sub-graph
    the kernel returns DUPLICATE_EVENT -> ALREADY_COMMITTED terminal, and a
    crash mid-node replays to the same result (UNIQUE(document_id)).
    """
    from langchain_core.messages import HumanMessage
    from langgraph.types import Command

    from app.schemas.layer2_tools import SubsetSumStatus

    result = _subset_for_state(state)
    status = result.status.value
    matched = list(result.matched_invoice_ids or [])

    if can_fast_path_commit(status, matched, state.get("invoice_number")):
        committed = _commit_proof_for_state(state, result)
        terminal = (
            "LEDGER_COMMITTED"
            if committed.status.value == "LEDGER_COMMITTED"
            else "ALREADY_COMMITTED"
        )
        return Command(
            update={
                "subset_status": status,
                "subset_message": result.message,
                "matched_invoice_ids": matched,
                "matched_bank_utr": result.matched_bank_utr,
                "bank_transaction_date": result.bank_transaction_date,
                "phase_applied": result.phase_applied,
                "subset_net_total_paise": result.net_total_paise,
                "terminal_status": terminal,
                "terminal_detail": committed.message,
                "terminal_utr": result.matched_bank_utr,
                "terminal_payout_id": state.get("razorpay_payout_id"),
                "outbox_event_id": (committed.outbox_event_ids or [None])[0],
            },
            goto="__end__",
        )

    # Membership miss (waterfall resolved to other invoices) reads as NO_MATCH
    # for THIS invoice — the sub-graph must never commit a sibling's match.
    handoff_status = status
    handoff_message = result.message
    if status == SubsetSumStatus.SUBSET_MATCHED.value:
        handoff_status = SubsetSumStatus.NO_MATCH.value
        handoff_message = (
            "Collision resolved to other open invoice(s); this invoice is not "
            "part of the matched subset."
        )
    updates: dict = {
        "subset_status": handoff_status,
        "subset_message": handoff_message,
    }
    updates["messages"] = [
        HumanMessage(content=fast_path_outcome_text(state, handoff_status, handoff_message))
    ]
    return updates


# =============================================================================
# Agent invocation guard + deterministic fallback agent + finalize node
# =============================================================================


def _invoke_agent(model: Any, state: dict) -> dict:
    """Run the Groq ReAct model; a single-invoice failure must never fail the
    whole batch — degrade to a no-tool answer so the deterministic finalize
    node routes the invoice to the DLQ instead."""
    try:
        updates = model(state)
    except Exception as e:
        logger.warning("Groq agent invocation failed; deterministic finalize", extra={"error": str(e)})
        from langchain_core.messages import AIMessage

        return {
            "messages": [
                AIMessage(content="Groq invocation failed — deterministic finalize.")
            ]
        }
    if not isinstance(updates, dict) or not updates.get("messages"):
        from langchain_core.messages import AIMessage

        return {"messages": [AIMessage(content=str(updates or ""))]}
    return updates


def _deterministic_agent(state: dict) -> dict:
    """LLM-free fallback: executes the waterfall deterministically in ONE node
    so the pipeline never blocks when Groq is unreachable at startup, and acts
    as the finalize safety net (never a silent drop).

    Mirrors exactly what the ReAct loop would do with a model:
        subset (anchored) -> membership guard -> ledger | human exception.
    """
    from langchain_core.messages import AIMessage

    from app.db.session import SessionLocal
    from app.agent.tools.human_exception import route_to_human_exception
    from app.agent.tools.react_tools import derive_exception_reason
    from app.schemas.layer2_tools import HumanExceptionInput

    result = _subset_for_state(state)
    updates: dict = {"messages": []}

    if can_fast_path_commit(
        result.status.value,
        list(result.matched_invoice_ids or []),
        state.get("invoice_number"),
    ):
        committed = _commit_proof_for_state(state, result)
        if committed.status.value == "LEDGER_COMMITTED":
            updates.update({
                "terminal_status": "LEDGER_COMMITTED",
                "terminal_detail": committed.message,
                "terminal_utr": result.matched_bank_utr,
                "terminal_payout_id": state.get("razorpay_payout_id"),
                "outbox_event_id": (committed.outbox_event_ids or [None])[0],
            })
            updates["messages"].append(AIMessage(content=f"Deterministic fallback: LEDGER_COMMITTED {committed.message}"))
            return updates
        # DUPLICATE_EVENT race lost — already committed elsewhere
        updates["terminal_status"] = "ALREADY_COMMITTED"
        updates["terminal_detail"] = committed.message
        updates["messages"].append(AIMessage(content=f"Deterministic fallback: {committed.message}"))
        return updates

    # No subset match — deterministic DLQ route (no LLM involved).
    db = SessionLocal()
    try:
        exc = route_to_human_exception(
            db,
            inp=HumanExceptionInput(
                vendor_code=state["vendor_code"],
                flagged_invoice_ids=[state["invoice_number"]],
                bank_utr_number=state.get("razorpay_utr") or None,
                exception_reason=derive_exception_reason(state),
                human_readable_message=(
                    (result.message or "")[:500]
                    or "Unresolved after deterministic waterfall — human review required."
                ),
            ),
        )
    finally:
        db.close()
    updates.update({
        "terminal_status": "EXCEPTION_ROUTED",
        "terminal_detail": exc.action_required,
        "terminal_utr": state.get("razorpay_utr") or None,
        "outbox_event_id": exc.outbox_event_id,
    })
    updates["messages"].append(AIMessage(content=f"Deterministic fallback: {exc.exception_reason} routed to DLQ."))
    return updates


def _finalize_node(state: dict) -> dict:
    """Terminal router for agent answers that request no further tools.

    If a tool already wrote a terminal status the graph ends cleanly;
    otherwise the invoice is deterministically routed to the DLQ (the agent
    is prohibited from silently dropping an unresolved invoice). Hard END.
    """
    if is_terminal_status(state.get("terminal_status")):
        return {}
    return _deterministic_agent(state)


# Alias kept for callers that used the earlier name
build_supervisor_graph = build_recon_graph
