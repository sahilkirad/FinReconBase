"""Layer 2 — Fast Kafka Consumer (Async Handoff Phase 1)

Subscribed to invoice.extracted.events with its own consumer group
(layer2-supervisor-cg). Per message it does exactly three things:

    1. parse the CloudEvents payload
    2. RPUSH it to batch_buffer:{batch_id} (deduped by SADD event-id set),
       or to the single buffer when batch_id is None (immediate run)
    3. commit the offset and poll again

The loop NEVER touches the LLM, Postgres, or the LangGraph execution pool, so
it completes in milliseconds and can never breach max.poll.interval.ms — no
matter how long Groq rate limiting makes the reconciliation sub-graphs wait.
"""

import json
import logging

from kafka import KafkaConsumer

from app.core.config import get_settings
from app.kafka.config import KafkaConfig
from app.kafka.layer2_buffer import Layer2RedisBuffer

logger = logging.getLogger(__name__)

VALID_TYPES = {"invoice.extracted", "invoice.reconciled"}


class Layer2ExtractedConsumer:
    """Millisecond-latency consumer buffering extracted events to Redis."""

    def __init__(
        self,
        group_id: str | None = None,
        buffer: Layer2RedisBuffer | None = None,
    ):
        settings = get_settings()
        self.config = KafkaConfig.from_settings()
        self.group_id = group_id or settings.layer2_consumer_group
        self.buffer = buffer or Layer2RedisBuffer(settings.redis_url)
        self._consumer: KafkaConsumer | None = None

    def start(self) -> None:
        self._consumer = KafkaConsumer(
            self.config.invoice_extracted_topic,
            **self.config.get_consumer_config(self.group_id),
        )
        logger.info(
            "Layer 2 consumer started",
            extra={"topic": self.config.invoice_extracted_topic, "group_id": self.group_id},
        )

        try:
            while True:
                batch = self._consumer.poll(timeout_ms=500, max_records=100)
                for _topic_partition, messages in batch.items():
                    for msg in messages:
                        self._handle(msg)
                # Offsets always advance after the ms-fast buffer append.
                self._consumer.commit()
        finally:
            self.close()

    def _handle(self, msg) -> None:
        event_id = None
        try:
            event = json.loads(msg.value.decode("utf-8"))
            event_id = event.get("id")
            event_type = event.get("type")
            data = event.get("data", {})
            batch_id = data.get("batch_id")

            if event_type not in VALID_TYPES:
                logger.warning("Unknown event type skipped", extra={"type": event_type})
                return

            if batch_id:
                added = self.buffer.push_batch(str(batch_id), event)
            else:
                added = self.buffer.push_single(event)

            logger.debug(
                "Event buffered",
                extra={"event_id": event_id, "batch_id": batch_id, "added": added},
            )
        except Exception as e:
            # A malformed event must never stall the stream — log, commit, continue.
            logger.error(
                "Failed to buffer event",
                extra={"event_id": event_id, "error": str(e)},
            )

    def close(self) -> None:
        if self._consumer is not None:
            self._consumer.close()
            self._consumer = None
        if self.buffer is not None:
            self.buffer.close()
