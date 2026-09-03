"""
Kafka Producer

Publishes invoice processing events to Kafka topics.
Uses the outbox pattern: write to DB first, then publish.
"""

import json
import logging
import uuid
from datetime import datetime, timezone

from app.kafka.config import KafkaConfig

logger = logging.getLogger(__name__)

# Lazy-initialized producer instance
_producer = None


def get_producer():
    """Get or create a Kafka producer instance."""
    global _producer
    if _producer is None:
        try:
            from kafka import KafkaProducer
            config = KafkaConfig.from_settings()
            _producer = KafkaProducer(**config.get_producer_config())
            logger.info("Kafka producer connected", extra={"bootstrap_servers": config.bootstrap_servers})
        except Exception as e:
            logger.error("Failed to connect Kafka producer", extra={"error": str(e)})
            raise
    return _producer


def publish_invoice_event(
    document_id: str,
    vendor_code: str,
    invoice_number: str,
    processing_status: str,
    topic: str | None = None,
    extracted_json: dict | None = None,
    batch_id: str | None = None,
) -> str:
    """
    Publish an invoice extracted event to Kafka (Fan-In to Layer 2).

    Args:
        document_id: UUID of the extracted invoice
        vendor_code: Vendor code
        invoice_number: Invoice number
        processing_status: VALIDATED or EXCEPTION_FLAGGED
        topic: Kafka topic (defaults to invoice.extracted.events)
        extracted_json: Full extracted invoice payload (JSON) for Layer 2
        batch_id: Batch job UUID (None for single uploads)

    Returns:
        Event ID (for idempotency tracking)
    """
    config = KafkaConfig.from_settings()
    target_topic = topic or config.invoice_extracted_topic

    event_id = f"evt_{uuid.uuid4()}"

    event_payload = {
        "specversion": "1.0",
        "type": "invoice.extracted",
        "source": "/layer1/ingestion",
        "id": event_id,
        "time": datetime.now(timezone.utc).isoformat(),
        "data": {
            "document_id": document_id,
            "vendor_code": vendor_code,
            "invoice_number": invoice_number,
            "processing_status": processing_status,
            "batch_id": batch_id,
            "extracted_invoice": extracted_json,
        },
    }

    try:
        producer = get_producer()
        future = producer.send(
            topic=target_topic,
            key=vendor_code.encode("utf-8"),
            value=json.dumps(event_payload).encode("utf-8"),
        )
        record_metadata = future.get(timeout=10)

        logger.info(
            "Invoice event published",
            extra={
                "event_id": event_id,
                "topic": record_metadata.topic,
                "partition": record_metadata.partition,
                "offset": record_metadata.offset,
                "batch_id": batch_id,
                "document_id": document_id,
            },
        )
        return event_id

    except Exception as e:
        logger.error(
            "Failed to publish invoice event",
            extra={"event_id": event_id, "error": str(e)},
        )
        raise


def close_producer():
    """Close the Kafka producer connection."""
    global _producer
    if _producer is not None:
        _producer.flush()
        _producer.close()
        _producer = None
        logger.info("Kafka producer closed")