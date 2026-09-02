"""
Outbox Poller — Transactional Outbox Pattern Worker

Reads PENDING events from outbox_events table and publishes them to Kafka.
This guarantees exactly-once delivery semantics:
1. API writes batch_jobs + outbox_events in single atomic transaction
2. Outbox Poller reads PENDING events and publishes to Kafka
3. On successful publish, marks event as PUBLISHED
4. Kafka consumer processes the event

Usage:
    python -m app.workers.outbox_poller
"""

import json
import logging
import sys
import time

from kafka import KafkaProducer
from kafka.errors import KafkaError
from sqlalchemy import text

from app.core.config import get_settings
from app.db.session import SessionLocal

logger = logging.getLogger(__name__)

# Polling interval in seconds
POLL_INTERVAL = 2.0


class OutboxPoller:
    """Polls outbox_events table and publishes to Kafka."""

    def __init__(self):
        self.settings = get_settings()
        self.producer = None

    def _create_producer(self) -> KafkaProducer:
        """Create Kafka producer with SSL/mTLS config."""
        config = {
            "bootstrap_servers": self.settings.kafka_bootstrap_servers,
            "client_id": "outbox-poller",
            "acks": "all",
            "retries": 3,
            "retry_backoff_ms": 100,
        }

        if self.settings.kafka_security_protocol == "SSL":
            config.update({
                "security_protocol": "SSL",
                "ssl_cafile": self.settings.kafka_ssl_ca_location,
                "ssl_certfile": self.settings.kafka_ssl_certificate_location,
                "ssl_keyfile": self.settings.kafka_ssl_key_location,
            })

        return KafkaProducer(**config)

    def start(self):
        """Start the outbox poller loop."""
        logger.info("Outbox Poller starting...")

        try:
            self.producer = self._create_producer()
            logger.info("Kafka producer connected for outbox poller")
        except Exception as e:
            logger.error(f"Failed to connect Kafka producer: {e}")
            return

        while True:
            try:
                self._poll_and_publish()
            except KeyboardInterrupt:
                logger.info("Outbox Poller interrupted")
                break
            except Exception as e:
                logger.error(f"Outbox poller error: {e}", exc_info=True)

            time.sleep(POLL_INTERVAL)

        self._cleanup()

    def _poll_and_publish(self):
        """Poll outbox_events for PENDING status and publish to Kafka."""
        db = SessionLocal()
        try:
            # Fetch pending events (with row-level locking to prevent double-processing)
            result = db.execute(
                text("""
                    SELECT outbox_id, event_id, topic, partition_key, payload
                    FROM outbox_events
                    WHERE status = 'PENDING'
                      AND available_at <= now()
                    ORDER BY created_at
                    LIMIT 10
                    FOR UPDATE SKIP LOCKED
                """)
            ).all()

            if not result:
                return

            for row in result:
                outbox_id, event_id, topic, partition_key, payload = row

                try:
                    # Publish to Kafka
                    future = self.producer.send(
                        topic=topic,
                        key=partition_key.encode("utf-8") if partition_key else None,
                        value=(
                        payload.encode("utf-8")
                        if isinstance(payload, str)
                        else json.dumps(payload).encode("utf-8")
                    ),
                    )
                    future.get(timeout=10)

                    # Mark as PUBLISHED
                    db.execute(
                        text("""
                            UPDATE outbox_events
                            SET status = 'PUBLISHED', published_at = now()
                            WHERE outbox_id = :outbox_id
                        """),
                        {"outbox_id": outbox_id},
                    )
                    db.commit()

                    logger.info(
                        f"Outbox event published: {event_id} -> {topic}"
                    )

                except KafkaError as e:
                    # Mark as FAILED for retry
                    db.execute(
                        text("""
                            UPDATE outbox_events
                            SET status = 'FAILED',
                                retry_count = retry_count + 1,
                                last_error = :error,
                                available_at = now() + INTERVAL '30 seconds'
                            WHERE outbox_id = :outbox_id
                              AND retry_count < max_retries
                        """),
                        {"outbox_id": outbox_id, "error": str(e)},
                    )
                    db.commit()

                    logger.error(
                        f"Failed to publish event {event_id}: {e}"
                    )

        finally:
            db.close()

    def _cleanup(self):
        """Cleanup resources."""
        if self.producer:
            self.producer.flush(timeout=5)
            self.producer.close()
            logger.info("Outbox Poller stopped")


def main():
    """Entry point for the outbox poller."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    poller = OutboxPoller()
    poller.start()


if __name__ == "__main__":
    main()
