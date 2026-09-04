"""
TDD tests — route_to_human_exception (outbox DLQ routing).

Covers:
- Happy path: outbox row written to reconciliation.dlq.events, committed
- REASON_UNSPECIFIED injected when the caller omits the exception_reason
- Full exception envelope carries the variance delta + message
"""

import json

from app.agent.tools.common import RECONCILIATION_DLQ_TOPIC
from app.agent.tools.human_exception import REASON_UNSPECIFIED, route_to_human_exception
from app.schemas.layer2_tools import ExceptionStatus, HumanExceptionInput


class _Result:
    def all(self):
        return []


class _FakeSession:
    def __init__(self):
        self.executed: list[tuple[str, dict]] = []
        self.committed = False

    def execute(self, sql, params):
        self.executed.append((str(sql), dict(params)))
        return _Result()

    def commit(self):
        self.committed = True


class TestHappyPath:
    def test_exception_routed_to_dlq_topic(self):
        db = _FakeSession()
        result = route_to_human_exception(
            db,
            inp=HumanExceptionInput(
                vendor_code="VEND_NEXUS_001",
                flagged_invoice_ids=["INV-445", "INV-446"],
                bank_utr_number="HDFCN202608249999",
                exception_reason="TOLERANCE_EXCEEDED",
                variance_delta="500.00",
                human_readable_message="Net expected amount is ₹500.00 less than the bank deposit.",
            ),
        )
        assert result.status == ExceptionStatus.EXCEPTION_LOGGED
        assert result.kafka_topic == RECONCILIATION_DLQ_TOPIC
        assert result.exception_reason == "TOLERANCE_EXCEEDED"
        assert db.committed is True

        sql, params = db.executed[0]
        assert "outbox_events" in sql
        assert params["topic"] == RECONCILIATION_DLQ_TOPIC
        assert params["event_type"] == "ReconciliationException"
        assert params["partition_key"] == "VEND_NEXUS_001"

        payload = json.loads(params["payload"])
        assert payload["type"] == "reconciliation.exception"
        data = payload["data"]
        assert data["exception_reason"] == "TOLERANCE_EXCEEDED"
        assert data["variance_delta"] == "500.00"
        assert data["flagged_invoices"] == ["INV-445", "INV-446"]
        assert data["bank_utr_number"] == "HDFCN202608249999"
        assert data["metadata"]["source"] == "layer2_agent"
        assert data["metadata"]["error"] == "TOLERANCE_EXCEEDED"


class TestMissingContextInjection:
    def test_missing_reason_injects_reason_unspecified(self):
        """The LLM forgets the reason => REASON_UNSPECIFIED injected so the
        payload can never break downstream schema validation."""
        db = _FakeSession()
        result = route_to_human_exception(
            db,
            inp=HumanExceptionInput(
                vendor_code="VEND_NEXUS_001",
                flagged_invoice_ids=["INV-445"],
                bank_utr_number="UTR99",
                exception_reason=None,
                human_readable_message="Could not reconcile.",
            ),
        )
        assert result.status == ExceptionStatus.EXCEPTION_LOGGED
        assert result.exception_reason == REASON_UNSPECIFIED

        _, params = db.executed[0]
        payload = json.loads(params["payload"])
        assert payload["data"]["exception_reason"] == REASON_UNSPECIFIED
