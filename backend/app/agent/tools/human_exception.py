"""
Tool 5: route_to_human_exception — DLQ Routing (reconciliation.dlq.events)

Wraps the flagged reconciliation record in a CloudEvents exception envelope and
writes it to outbox_events (topic = reconciliation.dlq.events) in one
transaction — the outbox poller publishes it for the Human Auditor Dashboard.

Deterministic robustness:
- If the LLM omits the exception_reason, REASON_UNSPECIFIED is injected so the
  payload can never break downstream schema validation.
- Once this tool succeeds the LangGraph router (Milestone 2) must transition to
  a hard END state — the agent is prohibited from re-running the math.
"""

import json
import logging

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.agent.tools.common import (
    OUTBOX_INSERT_SQL,
    RECONCILIATION_DLQ_TOPIC,
    build_cloud_event,
    new_event_id,
)
from app.schemas.layer2_tools import (
    ExceptionStatus,
    HumanExceptionInput,
    HumanExceptionResult,
)

logger = logging.getLogger(__name__)

REASON_UNSPECIFIED = "REASON_UNSPECIFIED"


def route_to_human_exception(
    db: Session,
    *,
    inp: HumanExceptionInput,
) -> HumanExceptionResult:
    """Persist an exception outbox event for human review."""
    exception_reason = (inp.exception_reason or "").strip() or REASON_UNSPECIFIED
    event_id = new_event_id()

    payload = build_cloud_event(
        event_type="reconciliation.exception",
        source="/layer2/agent",
        data={
            "vendor_code": inp.vendor_code,
            "flagged_invoices": list(inp.flagged_invoice_ids),
            "bank_utr_number": inp.bank_utr_number,
            "exception_reason": exception_reason,
            "variance_delta": inp.variance_delta,
            "human_readable_message": inp.human_readable_message,
            "metadata": {
                "source": "layer2_agent",
                "error": exception_reason,
            },
        },
    )

    db.execute(
        text(OUTBOX_INSERT_SQL),
        {
            "event_id": event_id,
            "aggregate_type": "ReconciliationException",
            "aggregate_id": inp.vendor_code,
            "topic": RECONCILIATION_DLQ_TOPIC,
            "partition_key": inp.vendor_code,
            "event_type": "ReconciliationException",
            "payload": json.dumps(payload),
        },
    )
    db.commit()

    logger.info(
        "Exception routed to DLQ",
        extra={
            "event_id": event_id,
            "vendor_code": inp.vendor_code,
            "exception_reason": exception_reason,
        },
    )
    return HumanExceptionResult(
        status=ExceptionStatus.EXCEPTION_LOGGED,
        kafka_topic=RECONCILIATION_DLQ_TOPIC,
        outbox_event_id=event_id,
        exception_reason=exception_reason,
    )
