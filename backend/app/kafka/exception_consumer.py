"""
Layer 5 — Exception Ticket Materializer consumer (reconciliation.dlq.events).

Materializes the Layer 2 Dead Letter Queue into exception_tickets so the
Human-in-the-Loop Auditor Dashboard can render failed matches as action cards.
Idempotent via idempotency_keys (consumer = layer5-exception-materializer).
"""

import json
import logging

from kafka import KafkaConsumer

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.kafka.config import KafkaConfig
from app.ledger.writer import (
    EXCEPTION_CONSUMER_NAME,
    build_ticket_from_dlq_event,
    insert_exception_ticket,
)

logger = logging.getLogger(__name__)


class ExceptionTicketConsumer:
    """Thread B — reconciliation.dlq.events -> exception_tickets."""

    def __init__(self, group_id: str | None = None):
        settings = get_settings()
        self.config = KafkaConfig.from_settings()
        self.group_id = group_id or settings.layer5_exception_consumer_group
        self.topic = self.config.reconciliation_dlq_topic
        self._consumer: KafkaConsumer | None = None

    def start(self) -> None:
        self._consumer = KafkaConsumer(
            self.topic,
            **self.config.get_consumer_config(self.group_id),
        )
        logger.info(
            "Exception materializer started",
            extra={"topic": self.topic, "group_id": self.group_id},
        )
        try:
            for message in self._consumer:
                try:
                    self._handle(message)
                    self._consumer.commit()
                except Exception as e:
                    logger.error(
                        "Exception materialization failed (no commit -> redelivery)",
                        extra={"offset": message.offset, "error": str(e)},
                    )
        finally:
            self.close()

    def _handle(self, message) -> bool:
        raw = json.loads(message.value.decode("utf-8"))
        params = build_ticket_from_dlq_event(raw, self.topic)
        db = SessionLocal()
        try:
            created = insert_exception_ticket(db, **params, consumer_name=EXCEPTION_CONSUMER_NAME)
        finally:
            db.close()
        if created:
            logger.info(
                "Exception ticket materialized",
                extra={"event_id": params["event_id"], "reason": params["exception_reason"]},
            )
        else:
            logger.info("Duplicate exception event skipped", extra={"event_id": params["event_id"]})
        return created

    def close(self) -> None:
        if self._consumer is not None:
            self._consumer.close()
            self._consumer = None