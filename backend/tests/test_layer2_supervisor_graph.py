"""
TDD tests — True ReAct supervisor topology (per-invoice sub-graph).

Pure routing helpers (no langgraph required) run on any host:
    - thread config carries recursion_limit = 12 and per-invoice isolation
    - last_message_has_tool_calls drives the ReAct loop (tools_condition)
    - route_after_tools: terminal proof -> hard END; guardrail error -> agent

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
    invoke_invoice,
    is_terminal_status,
    last_message_has_tool_calls,
    make_thread_config,
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

    def test_recursion_limit_is_12_for_react_loop(self):
        """A genuine ReAct round (agent + tools) consumes 2 recursion steps;
        self-correcting guardrail loops need headroom. 12 stays bounded."""
        cfg = make_thread_config("b1", "doc-42")
        assert cfg["recursion_limit"] == 12

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


class TestGraphBuild:
    def test_react_graph_compiles_without_llm(self):
        pytest.importorskip("langgraph", reason="langgraph not installed")

        from app.agent.graph.supervisor import build_recon_graph

        graph = build_recon_graph(None)  # deterministic fallback agent
        nodes = graph.get_graph().nodes
        for expected in ("pre_check", "context", "agent", "tools", "finalize"):
            assert expected in nodes

    def test_hard_end_edges_exist(self):
        pytest.importorskip("langgraph", reason="langgraph not installed")

        from langgraph.graph import START

        from app.agent.graph.supervisor import build_recon_graph

        edges = {(e.source, e.target) for e in build_recon_graph(None).get_graph().edges}
        assert ("finalize", "__end__") in edges
        assert ("context", "agent") in edges
        assert (START, "pre_check") in edges


class TestInvokeContract:
    def test_invoke_requires_config_with_recursion_cap(self):
        cfg = make_thread_config("b1", "d1")
        assert cfg["recursion_limit"] == 12
        calls = {}

        class StubGraph:
            def invoke(self, state, config=None):
                calls["config"] = config
                return dict(state, terminal_status="END")

        result = invoke_invoice(StubGraph(), state={"document_id": "d1"}, config=cfg)
        assert calls["config"] == cfg
        assert result["terminal_status"] == "END"
