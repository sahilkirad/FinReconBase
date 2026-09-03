"""
TDD tests — post_ledger_entry_tool (atomic per-invoice ledger + outbox commit).

Covers:
- Happy path: invoice_reconciliations + outbox_events rows in ONE tx, committed
- PREREQUISITE_FAILED: invoked without a SUBSET_MATCHED proof
- AMOUNT_MISMATCH: reconciled total != subset-sum proof net
- DUPLICATE_EVENT: IntegrityError rolls the whole transaction back
"""

import json

import pytest
from sqlalchemy.exc import IntegrityError

from app.agent.tools.common import RECONCILIATION_COMPLETED_TOPIC
from app.agent.tools.ledger_entry import post_ledger_entry
from app.schemas.layer2_tools import (
    LedgerStatus,
    PostLedgerInput,
    SubsetSumResult,
    SubsetSumStatus,
)


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeSession:
    """Captures SQL + params; feeds canned rows for the invoice fetch."""

    def __init__(self, invoice_rows=None, fail_with_integrity=False):
        self.invoice_rows = invoice_rows or [
            ("doc-1", "INV-1", 1000000),
            ("doc-2", "INV-2", 500000),
        ]
        self.fail_with_integrity = fail_with_integrity
        self.executed: list[tuple[str, dict]] = []
        self.committed = False
        self.rolled_back = False

    def execute(self, sql, params):
        sql_text = str(sql)
        self.executed.append((sql_text, dict(params)))
        if "extracted_invoices" in sql_text and "IN" in sql_text:
            return _Result(self.invoice_rows)
        if self.fail_with_integrity and "invoice_reconciliations" in sql_text:
            raise IntegrityError("stmt", params, Exception("duplicate key"))
        return _Result([])

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    @property
    def reconciliation_inserts(self):
        return [p for sql, p in self.executed if "invoice_reconciliations" in sql]

    @property
    def outbox_inserts(self):
        return [p for sql, p in self.executed if "outbox_events" in sql]


def _proof(invoice_ids=None, net_total=1500000, status=SubsetSumStatus.SUBSET_MATCHED) -> SubsetSumResult:
    return SubsetSumResult(
        status=status,
        matched_invoice_ids=invoice_ids or ["INV-1", "INV-2"],
        matched_bank_utr="UTR123",
        net_total_paise=net_total,
        phase_applied=1,
        message="matched",
    )


def _input(**overrides) -> PostLedgerInput:
    base = {
        "vendor_code": "VEND_NEXUS_001",
        "matched_invoice_ids": ["INV-1", "INV-2"],
        "razorpay_payout_id": "pout_123",
        "bank_utr_number": "UTR123",
        "total_reconciled_amount": "15000.00",
    }
    base.update(overrides)
    return PostLedgerInput(**base)


class TestHappyPath:
    def test_atomic_commit_writes_both_tables(self):
        db = _FakeSession()
        result = post_ledger_entry(
            db,
            inp=_input(),
            proof=_proof(),
            batch_id="batch-1",
        )
        assert result.status == LedgerStatus.LEDGER_COMMITTED
        assert len(result.reconciliation_ids) == 2
        assert len(result.outbox_event_ids) == 2
        assert db.committed is True
        assert db.rolled_back is False

        # One invoice_reconciliations row per matched invoice, with UTR + net
        recons = db.reconciliation_inserts
        assert len(recons) == 2
        assert {r["invoice_number"] for r in recons} == {"INV-1", "INV-2"}
        assert all(r["utr_number"] == "UTR123" for r in recons)
        assert all(r["vendor_code"] == "VEND_NEXUS_001" for r in recons)
        assert sum(r["net_settled_amount_paise"] for r in recons) == 1500000

        # One outbox event per invoice on the Layer-2 success topic
        outboxes = db.outbox_inserts
        assert len(outboxes) == 2
        assert all(o["topic"] == RECONCILIATION_COMPLETED_TOPIC for o in outboxes)
        assert all(o["partition_key"] == "VEND_NEXUS_001" for o in outboxes)
        payload = json.loads(outboxes[0]["payload"])
        assert payload["type"] == "invoice.reconciled"
        assert payload["source"] == "/layer2/agent"
        assert payload["data"]["bank_utr_number"] == "UTR123"


class TestPrerequisiteGuardrail:
    def test_no_proof_is_prerequisite_failed(self):
        db = _FakeSession()
        result = post_ledger_entry(db, inp=_input(), proof=None)
        assert result.status == LedgerStatus.PREREQUISITE_FAILED
        assert db.committed is False

    def test_non_matched_proof_is_prerequisite_failed(self):
        db = _FakeSession()
        result = post_ledger_entry(
            db,
            inp=_input(),
            proof=_proof(status=SubsetSumStatus.AMBIGUOUS_COLLISION),
        )
        assert result.status == LedgerStatus.PREREQUISITE_FAILED

    def test_amount_mismatch_refuses_commit(self):
        """Tool must never commit an amount that disagrees with the math proof."""
        db = _FakeSession()
        result = post_ledger_entry(
            db,
            inp=_input(total_reconciled_amount="9999.00"),
            proof=_proof(net_total=1500000),
        )
        assert result.status == LedgerStatus.PREREQUISITE_FAILED
        assert "AMOUNT_MISMATCH" in result.message
        assert db.committed is False

    def test_invoice_ids_must_match_proof(self):
        db = _FakeSession()
        result = post_ledger_entry(
            db,
            inp=_input(matched_invoice_ids=["INV-1", "INV-X"]),
            proof=_proof(invoice_ids=["INV-1", "INV-2"]),
        )
        assert result.status == LedgerStatus.PREREQUISITE_FAILED


class TestIdempotency:
    def test_duplicate_event_rolls_back_everything(self):
        """IntegrityError (document_id already reconciled) => DUPLICATE_EVENT,
        entire tx rolled back — an invoice can never be credited twice."""
        db = _FakeSession(fail_with_integrity=True)
        result = post_ledger_entry(db, inp=_input(), proof=_proof())
        assert result.status == LedgerStatus.DUPLICATE_EVENT
        assert db.rolled_back is True
        assert db.committed is False
