"""
TDD tests — ReAct supervisor topology with the deterministic fast-path
(per-invoice sub-graphs, deterministic-first / AI-second).

Pure routing + decision helpers (no langgraph required) run on any host:
    - thread config carries recursion_limit = 16 and per-invoice isolation
    - last_message_has_tool_calls drives the ReAct loop (tools_condition)
    - route_after_deterministic: terminal (safety net) -> hard END; otherwise
      the unresolved invoice flows to the ReAct agent
    - fast-path pure helpers: target selection, membership commit guard,
      handoff note builder
    - system prompt contract: every tool is described; invention forbidden

Graph-build tests require langgraph + langchain-core (Docker image) and skip
on hosts without them.
"""

import os

os.environ["GOOGLE_OAUTH_CLIENT_ID"] = "test"
os.environ["JWT_SECRET_KEY"] = "test-secret"
os.environ["GROQ_API_KEY"] = "test"
os.environ["GROQ_MODEL"] = "test"

import pytest

from app.agent.graph.supervisor import (
    can_fast_path_commit,
    fast_path_outcome_text,
    fast_path_target_paise,
    invoke_invoice,
    is_terminal_status,
    last_message_has_tool_calls,
    make_thread_config,
    route_after_deterministic,
    route_after_precheck,
    route_after_tools,
)


class _FakeAI:
    def __init__(self, tool_calls):
        self.tool_calls = tool_calls


class TestThreadConfig:
    def test_thread_id_batch_document(self):
        cfg = make_thread_config("b1", "doc-42")
        assert cfg["configurable"]["thread_id"] == "b1::doc-42"

    def test_checkpoint_namespace_batch_scoped(self):
        cfg = make_thread_config("b1", "doc-42")
        assert cfg["configurable"]["checkpoint_ns"] == "reconciliation::b1"

    def test_recursion_limit_is_16_for_react_loop(self):
        """A genuine ReAct round (agent + tools) consumes 2 recursion steps and
        the deterministic fast-path adds one prefix node (pre_check -> context
        -> deterministic) before the loop; self-correcting guardrail loops need
        headroom. 16 stays bounded (an infinite loop still force-halts)."""
        cfg = make_thread_config("b1", "doc-42")
        assert cfg["recursion_limit"] == 16

    def test_single_run_uses_marker_namespace(self):
        cfg = make_thread_config("single_doc-1", "doc-1")
        assert cfg["configurable"]["thread_id"] == "single_doc-1::doc-1"

    def test_run_token_rides_configurable(self):
        cfg = make_thread_config("b1", "d1", run_token="run_abc")
        assert cfg["configurable"]["run_token"] == "run_abc"
        cfg2 = make_thread_config("b1", "d1")
        assert "run_token" not in cfg2["configurable"]


class TestPureRouting:
    def test_precheck_short_circuits_to_end(self):
        assert route_after_precheck({"terminal_status": "ALREADY_COMMITTED"}) == "END"
        assert route_after_precheck({}) == "context"

    def test_terminal_statuses(self):
        for status in ("LEDGER_COMMITTED", "ALREADY_COMMITTED", "EXCEPTION_ROUTED"):
            assert is_terminal_status(status) is True
            assert route_after_tools({"terminal_status": status}) == "END"

    def test_guardrail_error_loops_back_to_agent(self):
        """PREREQUISITE_FAILED is an observation, NOT a terminal: the ReAct
        loop must hand it back to the agent for self-correction."""
        assert route_after_tools({}) == "agent"
        assert route_after_tools({"subset_status": "NO_MATCH"}) == "agent"

    def test_last_message_tool_calls_detection(self):
        assert last_message_has_tool_calls({"messages": [_FakeAI(tool_calls=[{"name": "x"}])]}) is True
        assert last_message_has_tool_calls({"messages": [_FakeAI(tool_calls=None)]}) is False
        assert last_message_has_tool_calls({"messages": [_FakeAI([])]}) is False
        assert last_message_has_tool_calls({"messages": [{"tool_calls": [{"name": "x"}]}]}) is True
        assert last_message_has_tool_calls({}) is False

    def test_deterministic_fast_path_routes_to_end_on_terminal(self):
        """Safety net: if a plain (non-Command) deterministic return ever
        carried a terminal, END; otherwise the invoice goes to the agent."""
        assert route_after_deterministic({"terminal_status": "LEDGER_COMMITTED"}) == "END"
        assert route_after_deterministic({}) == "agent"
        assert route_after_deterministic({"subset_status": "NO_MATCH"}) == "agent"


class TestDeterministicFastPathHelpers:
    def test_target_prefers_razorpay_anchor_over_waterfall_net(self):
        state = {"razorpay_amount_paise": 50000, "net_expected_paise": 40000}
        assert fast_path_target_paise(state) == 50000

    def test_target_falls_back_to_waterfall_net(self):
        assert fast_path_target_paise({"net_expected_paise": 40000}) == 40000

    def test_target_none_when_neither_anchor_nor_net(self):
        assert fast_path_target_paise({}) is None

    def test_commit_guard_requires_membership_in_proof(self):
        assert can_fast_path_commit("SUBSET_MATCHED", ["INV-1"], "INV-1") is True
        assert can_fast_path_commit("SUBSET_MATCHED", ["INV-2"], "INV-1") is False
        assert can_fast_path_commit("NO_MATCH", ["INV-1"], "INV-1") is False
        assert can_fast_path_commit("AMBIGUOUS_COLLISION", [], "INV-1") is False

    def test_commit_guard_tolerates_missing_proof_list(self):
        assert can_fast_path_commit("SUBSET_MATCHED", None, "INV-1") is False

    def test_outcome_note_carries_status_and_invoice(self):
        state = {"invoice_number": "INV-7", "razorpay_utr": "UTR-1", "net_expected_paise": 104400}
        note = fast_path_outcome_text(state, "NO_MATCH", "No subset sum matches.")
        assert "INV-7" in note
        assert "NO_MATCH" in note
        assert "UTR-1" in note
        assert "Rs.1044.00" in note

    def test_outcome_note_forbids_blind_subset_rerun(self):
        note = fast_path_outcome_text({"invoice_number": "INV-7"}, "NO_MATCH", "")
        assert "Do NOT call run_subset_sum_matching_tool again" in note
        assert "run_fuzzy_text_linker_tool" in note

    def test_outcome_note_omits_utr_when_absent(self):
        note = fast_path_outcome_text({"invoice_number": "INV-7"}, "NO_MATCH", "")
        assert "UTR attempted" not in note


class TestSystemPrompt:
    """The ReAct agent must receive ONE policy layer that explains every tool
    and forbids invention (deterministic-first / AI-second)."""

    def _prompt(self):
        from app.agent.graph.nodes import RECON_SYSTEM_PROMPT

        return RECON_SYSTEM_PROMPT

    def test_prompt_defines_every_bound_tool(self):
        prompt = self._prompt()
        for tool in (
            "calculate_tds_mdr_tool",
            "run_subset_sum_matching_tool",
            "run_fuzzy_text_linker_tool",
            "post_ledger_entry_tool",
            "route_to_human_exception_tool",
        ):
            assert tool in prompt

    def test_prompt_forbids_inventing_values(self):
        prompt = self._prompt()
        assert "Never invent" in prompt
        assert "never" in prompt.lower()

    def test_prompt_enforces_ledger_proof_order(self):
        prompt = self._prompt()
        assert "SUBSET_MATCHED" in prompt
        assert "never call post_ledger_entry_tool before a SUBSET_MATCHED proof exists" in prompt

    def test_prompt_restricts_subset_rerun_to_resolved_utr(self):
        prompt = self._prompt()
        assert "newly resolved UTR" in prompt

    def test_context_prompt_text_is_pure_and_self_contained(self):
        from app.agent.graph.nodes import context_prompt_text

        text = context_prompt_text(
            {
                "invoice_number": "INV-9",
                "vendor_code": "VEND_TEST_002",
                "document_id": "doc-9",
                "masked_payload": {},
            },
            grand_total_paise=106200,
            tds_paise=1800,
            anchor_note="No razorpay payout references this invoice (direct transfer path).",
        )
        assert "INV-9" in text
        assert "VEND_TEST_002" in text
        assert "Rs.1044.00" in text  # 106200 - 1800 paise
        assert "deterministic pre-node" in text.lower()
        assert "Never invent amounts, UTRs, or invoices." in text


class TestGraphBuild:
    def test_react_graph_compiles_without_llm(self):
        pytest.importorskip("langgraph", reason="langgraph not installed")

        from app.agent.graph.supervisor import build_recon_graph

        graph = build_recon_graph(None)  # deterministic fallback agent
        nodes = graph.get_graph().nodes
        for expected in ("pre_check", "context", "deterministic", "agent", "tools", "finalize"):
            assert expected in nodes

    def test_hard_end_and_fast_path_edges_exist(self):
        pytest.importorskip("langgraph", reason="langgraph not installed")

        from langgraph.graph import START

        from app.agent.graph.supervisor import build_recon_graph

        edges = {(e.source, e.target) for e in build_recon_graph(None).get_graph().edges}
        assert ("finalize", "__end__") in edges
        assert ("context", "deterministic") in edges  # fast-path pre-node
        assert (START, "pre_check") in edges


class TestInvokeContract:
    def test_invoke_requires_config_with_recursion_cap(self):
        cfg = make_thread_config("b1", "d1")
        assert cfg["recursion_limit"] == 16
        calls = {}

        class StubGraph:
            def invoke(self, state, config=None):
                calls["config"] = config
                return dict(state, terminal_status="END")

        result = invoke_invoice(StubGraph(), state={"document_id": "d1"}, config=cfg)
        assert calls["config"] == cfg
        assert result["terminal_status"] == "END"
