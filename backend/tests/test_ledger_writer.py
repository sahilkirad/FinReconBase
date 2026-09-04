"""
TDD tests — Layer 5 Ledger Writer (idempotency gate + double-entry + fatal DLQ).

Covers:
- parse_completed_event: paise exactness, MALFORMED_PAYLOAD / INVALID_PAISE_CASTING,
  explicit debit/credit override (corrupted upstream payload)
- build_double_entry: balanced DR/CR rows; ORPHANED_DEBIT_CREDIT_MISMATCH on imbalance
- commit_ledger: idempotency key + header + exactly 2 entries in ONE tx;
  duplicate event id -> DUPLICATE_EVENT with zero rows written
- build_fatal_dlq_event: CloudEvents envelope shape for ledger.fatal.dlq.events
- insert_exception_ticket: idempotent HITL ticket materialization
- build_ticket_from_dlq_event / build_ticket_from_fatal_error: payload mapping
"""

import pytest
from sqlalchemy.exc import IntegrityError

from app.ledger.writer import (
    EXCEPTION_CONSUMER_NAME,
    LEDGER_FATAL_DLQ_TOPIC,
    LedgerWriteError,
    build_double_entry,
    build_fatal_dlq_event,
    build_ticket_from_dlq_event,
    build_ticket_from_fatal_error,
    commit_ledger,
    insert_exception_ticket,
    parse_completed_event,
)
from app.schemas.ledger import FatalDlqReason, LedgerWriteStatus, ParsedLedgerRecord


# =============================================================================
# Fakes (mirrors test_ledger_entry.py style + db.begin() context manager)
# =============================================================================


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalar_one(self):
        return self._rows[0][0]


class _FakeSession:
    """Captures SQL + params; supports with db.begin(): semantics."""

    def __init__(self, idempotency_conflict=False):
        self.idempotency_conflict = idempotency_conflict
        self.executed: list[tuple[str, dict]] = []
        self.commits = 0
        self.rollbacks = 0

    def begin(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.commits += 1
        else:
            self.rollbacks += 1
        return False

    def execute(self, sql, params=None):
        sql_text = str(sql)
        params = dict(params or {})
        self.executed.append((sql_text, params))
        if self.idempotency_conflict and "INSERT INTO idempotency_keys" in sql_text:
            raise IntegrityError(
                "stmt", params,
                Exception('duplicate key value violates unique constraint "idempotency_keys_pkey"'),
            )
        if "RETURNING batch_id" in sql_text:
            return _Result([("batch-111",)])
        return _Result([])

    def rollback(self):
        self.rollbacks += 1

    @property
    def idempotency_inserts(self):
        return [p for s, p in self.executed if "idempotency_keys" in s]

    @property
    def header_inserts(self):
        return [p for s, p in self.executed if "reconciliation_batches" in s]

    @property
    def entry_inserts(self):
        return [p for s, p in self.executed if "ledger_entries" in s]

    @property
    def ticket_inserts(self):
        return [p for s, p in self.executed if "exception_tickets" in s]


# =============================================================================
# Builders
# =============================================================================


def _completed_event(data: dict | None = None, **overrides) -> dict:
    base = {
        "specversion": "1.0",
        "type": "invoice.reconciled",
        "source": "/layer2/agent",
        "id": "evt_100",
        "time": "2026-09-04T10:00:00Z",
        "data": {
            "vendor_code": "VEND_NEXUS_001",
            "matched_invoices": ["INV-1"],
            "razorpay_payout_id": "pout_1",
            "bank_utr_number": "UTR123",
            "total_reconciled_amount": "315400.00",
        },
    }
    if data:
        base["data"].update(data)
    base.update(overrides)
    return base


def _record(**overrides) -> ParsedLedgerRecord:
    base = {
        "event_id": "evt_100",
        "vendor_code": "VEND_NEXUS_001",
        "matched_invoice_ids": ["INV-1"],
        "razorpay_payout_id": "pout_1",
        "bank_utr_number": "UTR123",
        "debit_amount_paise": 31540000,
        "credit_amount_paise": 31540000,
    }
    base.update(overrides)
    return ParsedLedgerRecord(**base)


# =============================================================================
# 1. Parse & normalize
# =============================================================================


class TestParse:
    def test_happy_path_paise_exactness(self):
        rec = parse_completed_event(_completed_event())
        assert rec.event_id == "evt_100"
        assert rec.vendor_code == "VEND_NEXUS_001"
        assert rec.matched_invoice_ids == ["INV-1"]
        assert rec.bank_utr_number == "UTR123"
        assert rec.razorpay_payout_id == "pout_1"
        assert rec.debit_amount_paise == 31540000
        assert rec.credit_amount_paise == 31540000

    def test_missing_vendor_is_malformed(self):
        with pytest.raises(LedgerWriteError) as ei:
            parse_completed_event(_completed_event(data={"vendor_code": None}))
        assert ei.value.reason == FatalDlqReason.MALFORMED_PAYLOAD

    def test_empty_matched_invoices_is_malformed(self):
        with pytest.raises(LedgerWriteError) as ei:
            parse_completed_event(_completed_event(data={"matched_invoices": []}))
        assert ei.value.reason == FatalDlqReason.MALFORMED_PAYLOAD

    def test_missing_total_is_malformed(self):
        with pytest.raises(LedgerWriteError) as ei:
            parse_completed_event(_completed_event(data={"total_reconciled_amount": None}))
        assert ei.value.reason == FatalDlqReason.MALFORMED_PAYLOAD

    def test_corrupted_amount_is_invalid_paise_casting(self):
        with pytest.raises(LedgerWriteError) as ei:
            parse_completed_event(_completed_event(data={"total_reconciled_amount": "104,400.00"}))
        assert ei.value.reason == FatalDlqReason.INVALID_PAISE_CASTING

    def test_explicit_debit_credit_override(self):
        """Corrupted payload commanding DR 1,00,000 vs CR 98,000 is parsed so the
        balance guardrail can catch it."""
        rec = parse_completed_event(
            _completed_event(data={"debit_amount": "100000.00", "credit_amount": "98000.00"})
        )
        assert rec.debit_amount_paise == 10000000
        assert rec.credit_amount_paise == 9800000


# =============================================================================
# 2. Double-entry generation & balance guardrail
# =============================================================================


class TestDoubleEntry:
    def test_balanced_entries(self):
        dr, cr = build_double_entry(_record())
        assert dr["entry_type"] == "DEBIT"
        assert dr["account_type"] == "LIABILITY"
        assert dr["account_name"] == "Accounts Payable - VEND_NEXUS_001"
        assert dr["amount_paise"] == 31540000
        assert cr["entry_type"] == "CREDIT"
        assert cr["account_type"] == "ASSET"
        assert cr["account_name"] == "HDFC Corporate Current Account"
        assert cr["amount_paise"] == 31540000

    def test_imbalance_raises_orphaned_debit_credit_mismatch(self):
        with pytest.raises(LedgerWriteError) as ei:
            build_double_entry(_record(debit_amount_paise=10000000, credit_amount_paise=9800000))
        assert ei.value.reason == FatalDlqReason.ORPHANED_DEBIT_CREDIT_MISMATCH
        assert ei.value.details["variance_delta_paise"] == 200000


# =============================================================================
# 3. ACID commit
# =============================================================================


class TestCommit:
    def test_atomic_commit_writes_all_three(self):
        db = _FakeSession()
        result = commit_ledger(db, record=_record())
        assert result.status == LedgerWriteStatus.COMMITTED
        assert result.batch_id == "batch-111"
        assert db.commits == 1
        assert len(db.idempotency_inserts) == 1
        assert len(db.header_inserts) == 1
        assert len(db.entry_inserts) == 2

        header = db.header_inserts[0]
        assert header["idempotency_event_id"] == "evt_100"
        assert header["vendor_code"] == "VEND_NEXUS_001"
        assert header["utr_number"] == "UTR123"
        assert header["total_reconciled_amount_paise"] == 31540000
        assert header["matched_invoice_ids"] == ["INV-1"]

        types = [(p["entry_type"], p["account_type"], p["amount_paise"]) for p in db.entry_inserts]
        assert types == [("DEBIT", "LIABILITY", 31540000), ("CREDIT", "ASSET", 31540000)]
        assert all(p["batch_id"] == "batch-111" for p in db.entry_inserts)
        assert all(p["utr_number"] == "UTR123" for p in db.entry_inserts)

    def test_duplicate_event_dropped(self):
        """Zombie retry: same event_id redelivered -> DUPLICATE_EVENT, zero rows."""
        db = _FakeSession(idempotency_conflict=True)
        result = commit_ledger(db, record=_record())
        assert result.status == LedgerWriteStatus.DUPLICATE_EVENT
        assert db.commits == 0
        assert len(db.header_inserts) == 0
        assert len(db.entry_inserts) == 0

    def test_unbalanced_record_refuses_commit(self):
        db = _FakeSession()
        with pytest.raises(LedgerWriteError) as ei:
            commit_ledger(
                db,
                record=_record(debit_amount_paise=10000000, credit_amount_paise=9800000),
            )
        assert ei.value.reason == FatalDlqReason.ORPHANED_DEBIT_CREDIT_MISMATCH
        assert db.commits == 0
        assert db.executed == []  # guardrail fires before any DB statement


# =============================================================================
# 4. Fatal DLQ + HITL ticket
# =============================================================================


class TestFatalDlq:
    def test_fatal_envelope_shape(self):
        error = LedgerWriteError(
            FatalDlqReason.ORPHANED_DEBIT_CREDIT_MISMATCH,
            "boom",
            {"event_id": "evt_100", "variance_delta_paise": 200000},
        )
        env = build_fatal_dlq_event(_completed_event(), error)
        assert env["type"] == "ledger.fatal"
        assert env["source"] == "/layer5/ledger-writer"
        assert env["metadata"]["source"] == "layer5_ledger_writer"
        assert env["metadata"]["error"] == "ORPHANED_DEBIT_CREDIT_MISMATCH"
        assert env["data"]["error_code"] == "ORPHANED_DEBIT_CREDIT_MISMATCH"
        assert env["data"]["original_event"]["id"] == "evt_100"
        assert env["data"]["details"]["variance_delta_paise"] == 200000

    def test_ticket_insert_is_idempotent(self):
        kwargs = dict(
            event_id="evt_100",
            source_topic=LEDGER_FATAL_DLQ_TOPIC,
            vendor_code="VEND_NEXUS_001",
            bank_utr_number="UTR123",
            flagged_invoice_ids=["INV-1"],
            exception_reason="ORPHANED_DEBIT_CREDIT_MISMATCH",
            variance_delta_paise=200000,
            human_readable_message="boom",
            flagged_payload={"id": "evt_100"},
        )
        db = _FakeSession()
        assert insert_exception_ticket(db, **kwargs) is True
        assert db.commits == 1
        assert len(db.ticket_inserts) == 1
        assert db.ticket_inserts[0]["variance_delta_paise"] == 200000

        db2 = _FakeSession(idempotency_conflict=True)
        assert insert_exception_ticket(db2, **kwargs) is False
        assert db2.commits == 0
        assert len(db2.ticket_inserts) == 0

    def test_ticket_from_fatal_error_mapping(self):
        error = LedgerWriteError(
            FatalDlqReason.ORPHANED_DEBIT_CREDIT_MISMATCH,
            "Unbalanced double-entry for event evt_100",
            {"event_id": "evt_100", "vendor_code": "VEND_NEXUS_001", "variance_delta_paise": 200000},
        )
        params = build_ticket_from_fatal_error(_completed_event(), error, LEDGER_FATAL_DLQ_TOPIC)
        assert params["event_id"] == "evt_100"
        assert params["source_topic"] == LEDGER_FATAL_DLQ_TOPIC
        assert params["vendor_code"] == "VEND_NEXUS_001"
        assert params["exception_reason"] == "ORPHANED_DEBIT_CREDIT_MISMATCH"
        assert params["variance_delta_paise"] == 200000
        assert params["flagged_invoice_ids"] == ["INV-1"]
        assert params["flagged_payload"] is not None


# =============================================================================
# 5. Exception ticket materializer (reconciliation.dlq.events -> exception_tickets)
# =============================================================================


class TestExceptionMaterializer:
    def test_dlq_envelope_mapping(self):
        raw = {
            "specversion": "1.0",
            "type": "reconciliation.exception",
            "source": "/layer2/agent",
            "id": "evt_dlq_1",
            "time": "2026-09-04T10:00:00Z",
            "data": {
                "vendor_code": "VEND_NEXUS_001",
                "flagged_invoices": ["INV-9"],
                "bank_utr_number": "UTR999",
                "exception_reason": "NO_MATCH",
                "variance_delta": "500.00",
                "human_readable_message": "No UTR re-anchored by the agent.",
            },
        }
        params = build_ticket_from_dlq_event(raw, "reconciliation.dlq.events")
        assert params["event_id"] == "evt_dlq_1"
        assert params["source_topic"] == "reconciliation.dlq.events"
        assert params["vendor_code"] == "VEND_NEXUS_001"
        assert params["flagged_invoice_ids"] == ["INV-9"]
        assert params["exception_reason"] == "NO_MATCH"
        assert params["variance_delta_paise"] == 50000  # "500.00" rupees -> paise
        assert params["flagged_payload"] is raw

    def test_missing_fields_get_safe_fallbacks(self):
        raw = {"id": "evt_dlq_2", "data": {"vendor_code": "V", "variance_delta": "bad"}}
        params = build_ticket_from_dlq_event(raw, "reconciliation.dlq.events")
        assert params["exception_reason"] == "REASON_UNSPECIFIED"
        assert params["variance_delta_paise"] is None  # garbage variance never crashes
        assert params["flagged_invoice_ids"] == []
        assert params["human_readable_message"] == "No message provided."

    def test_materialized_ticket_insert(self):
        raw = {
            "id": "evt_dlq_3",
            "data": {
                "vendor_code": "VEND_NEXUS_001",
                "flagged_invoices": ["INV-9"],
                "bank_utr_number": "UTR999",
                "exception_reason": "NO_MATCH",
                "variance_delta": "500.00",
                "human_readable_message": "No UTR re-anchored by the agent.",
            },
        }
        db = _FakeSession()
        params = build_ticket_from_dlq_event(raw, "reconciliation.dlq.events")
        created = insert_exception_ticket(db, **params, consumer_name=EXCEPTION_CONSUMER_NAME)
        assert created is True
        assert db.commits == 1
        ticket = db.ticket_inserts[0]
        assert ticket["vendor_code"] == "VEND_NEXUS_001"
        assert ticket["source_topic"] == "reconciliation.dlq.events"
        assert ticket["source_event_id"] == "evt_dlq_3"
        assert ticket["exception_reason"] == "NO_MATCH"
        assert ticket["variance_delta_paise"] == 50000