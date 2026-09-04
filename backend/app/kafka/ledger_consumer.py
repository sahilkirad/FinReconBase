"""
Layer 5 — Ledger Kafka consumer (reconciliation.completed.events sink).

Manual offset commits (enable_auto_commit=False):
- COMMITTED / DUPLICATE_EVENT  -> commit the offset
- Fatal guardrail failure      -> HITL ticket + ledger.fatal.dlq.events; the
                                  offset commits ONLY after both succeed
                                  (poison semantics, mirrors InvoiceConsumer:
                                  a lost alert/ticket must be redelivered)
- Transient DB/network error   -> no commit; Kafka redelivers the event
"""

import json
import logging

from kafka import KafkaConsumer

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.kafka.config import KafkaConfig
from app.ledger.writer import (
    LEDGER_FATAL_DLQ_TOPIC,
    LedgerWriteError,
    build_fatal_dlq_event,
    build_ticket_from_fatal_error,
    commit_ledger,
    insert_exception_ticket,
    parse_completed_event,
)
from app.schemas.ledger import LedgerWriteStatus

logger = logging.getLogger(__name__)


class LedgerConsumer:
    """Thread A — the double-entry sink for reconciliation.completed.events."""

    def __init__(self, group_id: str | None = None, producer=None):
        settings = get_settings()
        self.config = KafkaConfig.from_settings()
        self.group_id = group_id or settings.layer5_consumer_group
        self.topic = self.config.reconciliation_completed_topic
        self._producer = producer
        self._consumer: KafkaConsumer | None = None

    def start(self) -> None:
        self._consumer = KafkaConsumer(
            self.topic,
            **self.config.get_consumer_config(self.group_id),
        )
        logger.info(
            "Ledger consumer started",
            extra={"topic": self.topic, "group_id": self.group_id},
        )
        try:
            for message in self._consumer:
                try:
                    self._handle(message)
                except Exception as e:
                    logger.error(
                        "Ledger message handling failed (no commit -> redelivery)",
                        extra={"offset": message.offset, "error": str(e)},
                    )
        finally:
            self.close()

    def _handle(self, message) -> None:
        raw = json.loads(message.value.decode("utf-8"))
        db = SessionLocal()
        try:
            record = parse_completed_event(raw)
            result = commit_ledger(db, record=record)
            if result.status == LedgerWriteStatus.DUPLICATE_EVENT:
                logger.info(
                    "Duplicate ledger event dropped",
                    extra={"event_id": record.event_id},
                )
            self._consumer.commit()
        except LedgerWriteError as e:
            # Poison: ticket + fatal DLQ must both succeed before committing.
            self._handle_fatal(db, raw, e)
            self._consumer.commit()
        finally:
            db.close()

    def _handle_fatal(self, db, raw: dict, error: LedgerWriteError) -> None:
        params = build_ticket_from_fatal_error(raw, error, LEDGER_FATAL_DLQ_TOPIC)
        created = insert_exception_ticket(db, **params)
        if created:
            logger.error(
                "Fatal ledger failure materialized as HITL ticket",
                extra={"event_id": params["event_id"], "reason": error.reason},
            )
        self._publish_fatal_dlq(raw, error)

    def _publish_fatal_dlq(self, raw: dict, error: LedgerWriteError) -> None:
        from app.kafka.producer import get_producer

        producer = self._producer or get_producer()
        event = build_fatal_dlq_event(raw, error)
        future = producer.send(
            topic=LEDGER_FATAL_DLQ_TOPIC,
            key=error.reason.encode("utf-8"),
            value=json.dumps(event).encode("utf-8"),
        )
        future.get(timeout=10)
        logger.error(
            "Fatal ledger payload published to DLQ",
            extra={"reason": error.reason, "event_id": (error.details or {}).get("event_id")},
        )

    def close(self) -> None:
        if self._consumer is not None:
            self._consumer.close()
            self._consumer = None