"""
TDD tests — ReAct tool State Guardrails (react_tools.py pure helpers).

Core logic.docx (Tool-4): "If the Agent hallucinates and tries to invoke this
tool without having run the Subset-Sum tool first, the tool enforces a State
Guardrail... PREREQUISITE_FAILED."

These tests target the langchain-free guard helpers so they run on any host:
- TDS tool args must match the deterministic pre-node context
- subset tool rejects invented UTRs / targets (only anchor/fuzzy-resolved)
- post_ledger refuses without a SUBSET_MATCHED proof in state (out-of-order)
- post_ledger envelope (utr / invoices / amount) must equal the proof exactly
- route_to_human_exception refuses when a subset proof exists
- exception reason is deterministic (LLM may not invent reasons)
"""

import os

os.environ["GOOGLE_OAUTH_CLIENT_ID"] = "test"
os.environ["JWT_SECRET_KEY"] = "test-secret"
os.environ["GROQ_API_KEY"] = "test"
os.environ["GROQ_MODEL"] = "test"

import pytest

from app.agent.tools.react_tools import (
    derive_exception_reason,
    exception_guard_reason,
    ledger_guard_reason,
    subset_target_ok,
    subset_utr_is_allowed,
    tds_input_mismatch_reason,
)

MATCHED_PROOF_STATE = {
    "invoice_number": "INV-2026-0001",
    "vendor_code": "VEND_TEST_002",
    "subset_status": "SUBSET_MATCHED",
    "matched_invoice_ids": ["INV-2026-0001"],
    "matched_bank_utr": "300000000001",
    "subset_net_total_paise": 104400,
}


class TestTdsGuardrail:
    def test_matching_args_pass(self):
        state = {
            "invoice_number": "INV-2026-0001",
            "masked_payload": {
                "financial_summary": {"grand_total_paise": 106200, "tds_deduction_paise": 1800}
            },
        }
        assert (
            tds_input_mismatch_reason(
                state,
                invoice_id="INV-2026-0001",
                grand_total_rupees="1062.00",
                tds_deducted_rupees="18.00",
            )
            is None
        )

    def test_llm_recomputed_amounts_rejected(self):
        state = {
            "invoice_number": "INV-2026-0001",
            "masked_payload": {
                "financial_summary": {"grand_total_paise": 106200, "tds_deduction_paise": 1800}
            },
        }
        reason = tds_input_mismatch_reason(
            state,
            invoice_id="INV-2026-0001",
            grand_total_rupees="9999.00",  # hallucinated
            tds_deducted_rupees="18.00",
        )
        assert reason is not None
        assert reason.startswith("PREREQUISITE_FAILED")

    def test_wrong_invoice_rejected(self):
        state = {"invoice_number": "INV-2026-0002", "masked_payload": {}}
        assert (
            tds_input_mismatch_reason(
                state, invoice_id="INV-2026-0001",
                grand_total_rupees="0.00", tds_deducted_rupees="0.00",
            )
            is not None
        )


class TestSubsetGuardrail:
    def test_free_utr_rejected_invented(self):
        state = {"razorpay_utr": "300000000001", "fuzzy_resolved_utr": None}
        assert subset_utr_is_allowed(state, "999999999999") is False  # invented
        assert subset_utr_is_allowed(state, "300000000001") is True   # anchor-resolved

    def test_fuzzy_resolved_utr_allowed(self):
        state = {"razorpay_utr": None, "fuzzy_resolved_utr": "300000000007"}
        assert subset_utr_is_allowed(state, "300000000007") is True

    def test_no_utr_means_scan_allowed(self):
        assert subset_utr_is_allowed({}, None) is True

    def test_target_must_equal_deterministic_net(self):
        state = {"net_expected_paise": 104400, "razorpay_amount_paise": None}
        assert subset_target_ok(state, 104400) is True
        assert subset_target_ok(state, 1) is False
        assert subset_target_ok(state, None) is True  # omit -> engine uses state

    def test_anchor_amount_overrides_net(self):
        state = {"net_expected_paise": 1, "razorpay_amount_paise": 104400}
        assert subset_target_ok(state, 104400) is True
        assert subset_target_ok(state, 1) is False


class TestLedgerGuardrail:
    def test_out_of_order_call_prerequisite_failed(self):
        """Ledger before subset-sum: the exact Core logic Tool-4 guarantee."""
        state = {"invoice_number": "INV-2026-0001"}  # no proof in memory
        reason = ledger_guard_reason(
            state,
            bank_utr_number="300000000001",
            matched_invoice_ids=["INV-2026-0001"],
            total_reconciled_amount="1044.00",
        )
        assert reason is not None
        assert "PREREQUISITE_FAILED" in reason
        assert "run_subset_sum_matching_tool" in reason

    def test_matching_envelope_passes(self):
        reason = ledger_guard_reason(
            MATCHED_PROOF_STATE,
            bank_utr_number="300000000001",
            matched_invoice_ids=["INV-2026-0001"],
            total_reconciled_amount="1044.00",
        )
        assert reason is None

    def test_wrong_utr_rejected(self):
        reason = ledger_guard_reason(
            MATCHED_PROOF_STATE,
            bank_utr_number="000000000000",
            matched_invoice_ids=["INV-2026-0001"],
            total_reconciled_amount="1044.00",
        )
        assert reason is not None and "bank_utr_number" in reason

    def test_invoice_ids_must_equal_proof(self):
        reason = ledger_guard_reason(
            MATCHED_PROOF_STATE,
            bank_utr_number="300000000001",
            matched_invoice_ids=["INV-2026-9999"],  # LLM swapped the invoice
            total_reconciled_amount="1044.00",
        )
        assert reason is not None and "matched_invoice_ids" in reason

    def test_amount_must_equal_proof_net(self):
        reason = ledger_guard_reason(
            MATCHED_PROOF_STATE,
            bank_utr_number="300000000001",
            matched_invoice_ids=["INV-2026-0001"],
            total_reconciled_amount="10.00",  # mismatch
        )
        assert reason is not None and "total_reconciled_amount" in reason

    def test_invalid_rupees_string_rejected(self):
        reason = ledger_guard_reason(
            MATCHED_PROOF_STATE,
            bank_utr_number="300000000001",
            matched_invoice_ids=["INV-2026-0001"],
            total_reconciled_amount="1,044.00",  # commas invalid
        )
        assert reason is not None and "INVALID_PAISE_CASTING" in reason


class TestExceptionGuardrail:
    def test_matched_invoice_cannot_route_to_human(self):
        reason = exception_guard_reason({"subset_status": "SUBSET_MATCHED"})
        assert reason is not None
        assert "post_ledger_entry_tool" in reason

    def test_unresolved_invoice_can_route(self):
        assert exception_guard_reason({"subset_status": "NO_MATCH"}) is None
        assert exception_guard_reason({}) is None

    def test_reason_derivation_is_deterministic(self):
        assert derive_exception_reason({"subset_status": "NO_MATCH"}) == "NO_MATCH"
        assert derive_exception_reason({"subset_status": "AMBIGUOUS_COLLISION"}) == "AMBIGUOUS_COLLISION"
        assert derive_exception_reason({"fuzzy_attempted": True, "fuzzy_status": "ENTITY_MISMATCH"}) == "ENTITY_MISMATCH"
        assert derive_exception_reason({"waterfall_status": "NEGATIVE_NET_SETTLEMENT"}) == "NEGATIVE_NET_SETTLEMENT"
        assert derive_exception_reason({}) == "REASON_UNSPECIFIED"

    @pytest.mark.parametrize("invented", ["THE LLM THINKS", "some random reason", "ERROR"])
    def test_deterministic_reason_whitelist_contract(self, invented):
        """The wrapper whitelist lives beside derive_exception_reason; assert
        the vocabulary of recognisable deterministic reasons is stable."""
        from app.agent.tools.react_tools import _DETERMINISTIC_REASONS

        assert invented not in _DETERMINISTIC_REASONS
        assert {
            "NO_MATCH",
            "AMBIGUOUS_COLLISION",
            "ENTITY_MISMATCH",
            "NEGATIVE_NET_SETTLEMENT",
            "INVALID_PAISE_CASTING",
        } == _DETERMINISTIC_REASONS
