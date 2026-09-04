"""
Layer 5 — Ledger Writer Microservice (dual-thread sink).

THREAD A — Ledger Consumer
    reconciliation.completed.events -> idempotency gate -> double-entry
    (reconciliation_batches + ledger_entries) in one ACID transaction.
    Guardrail failures (ORPHANED_DEBIT_CREDIT_MISMATCH / malformed / bad paise)
    -> exception_tickets (HITL) + ledger.fatal.dlq.events (infra alert).

THREAD B — Exception Ticket Materializer
    reconciliation.dlq.events -> exception_tickets (idempotent).

Manual offset commits only after DB success (or after poison handling).

Usage:
    python -m app.workers.ledger_writer
"""

import logging
import sys
import threading
import time

from app.core.config import get_settings

logger = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    get_settings()  # fail fast on missing/invalid env
    logger.info("Starting Ledger Writer...")

    ledger_thread = threading.Thread(
        target=_run_ledger_consumer,
        name="ledger-consumer",
        daemon=True,
    )
    exception_thread = threading.Thread(
        target=_run_exception_consumer,
        name="exception-materializer",
        daemon=True,
    )

    try:
        ledger_thread.start()
        exception_thread.start()
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Ledger writer interrupted")
    finally:
        logger.info("Ledger writer stopped")


def _run_ledger_consumer() -> None:
    from app.kafka.ledger_consumer import LedgerConsumer

    consumer = LedgerConsumer()
    try:
        consumer.start()
    except Exception as e:
        logger.error("Ledger consumer crashed", extra={"error": str(e)})


def _run_exception_consumer() -> None:
    from app.kafka.exception_consumer import ExceptionTicketConsumer

    consumer = ExceptionTicketConsumer()
    try:
        consumer.start()
    except Exception as e:
        logger.error("Exception materializer crashed", extra={"error": str(e)})


if __name__ == "__main__":
    main()