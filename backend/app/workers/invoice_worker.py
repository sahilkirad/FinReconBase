"""
Invoice Worker — Standalone Kafka Consumer Entry Point

This module is the entry point for the Layer 1 invoice worker.
It wraps the Kafka consumer and can be run as a standalone process.

Usage:
    python -m app.workers.invoice_worker

Architecture:
- Consumes from invoice.processing.events topic
- Reads images from shared Docker volume (Claim Check pattern)
- Runs boundary detection + full Layer 1 pipeline
- Publishes results to invoice.extracted.events
- Updates batch progress in DB

Rate Limiting (3-Pillar Architecture):
- Threading Semaphore (layer1_max_concurrent)
- Redis Token Bucket (gemini_rpm_limit, shared across all workers)
- Exponential Backoff with Full Jitter on 429 errors

Environment Variables:
- KAFKA_BOOTSTRAP_SERVERS: Kafka broker address
- KAFKA_SECURITY_PROTOCOL: SSL or PLAINTEXT
- KAFKA_SSL_*: SSL certificate paths
- LAYER1_CONSUMER_GROUP: Consumer group ID
"""

import logging
import sys

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


def main():
    """Main entry point for the invoice worker."""
    logger.info("Starting Invoice Worker...")

    try:
        from app.kafka.consumer import InvoiceConsumer

        consumer = InvoiceConsumer()
        consumer.start()

    except KeyboardInterrupt:
        logger.info("Worker interrupted by user")
    except Exception as e:
        logger.error(f"Worker failed: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
