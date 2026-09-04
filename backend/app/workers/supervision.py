"""Supervised consumer runner — a Kafka-boot hiccup can never kill the pipeline.

Kafka consumers constructed while the broker is still booting can raise
``KafkaTimeoutError`` out of ``start()`` (bootstrap ECONNREFUSED). Inside a
multi-threaded worker (``ledger_writer``, ``recon_supervisor``) that exception
used to kill the consumer daemon-thread permanently while the container stayed
"Up" — a transient broker restart silently stopped the pipeline.

This module wraps ``consumer_factory().start()`` in an endless restart loop:

    every crash (or unexpected clean return)
        -> close the consumer
        -> wait a capped exponential backoff
        -> construct a FRESH consumer and start it again

A broker that is down for minutes therefore self-heals the moment it returns —
no manual intervention, and no hot-looping (delay capped at ``max_delay``).

Usage inside a worker thread target:

    def _run_consumer() -> None:
        run_consumer_supervisor("my-consumer", lambda: MyConsumer(), log=logger)
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_BASE_DELAY_S: float = 1.0
DEFAULT_MAX_DELAY_S: float = 30.0


def compute_backoff_delay(
    attempt: int,
    base_delay: float = DEFAULT_BASE_DELAY_S,
    max_delay: float = DEFAULT_MAX_DELAY_S,
) -> float:
    """Capped exponential backoff for restart attempt number ``attempt``.

    delay = min(base_delay * 2 ** (attempt - 1), max_delay)
    """
    if attempt <= 1:
        return min(max(base_delay, 0.0), max_delay)
    return min(base_delay * (2 ** (attempt - 1)), max_delay)


def run_consumer_supervisor(
    name: str,
    consumer_factory: Callable[[], Any],
    *,
    base_delay: float = DEFAULT_BASE_DELAY_S,
    max_delay: float = DEFAULT_MAX_DELAY_S,
    sleep: Callable[[float], Any] = time.sleep,
    log: logging.Logger | None = None,
) -> None:
    """Run ``consumer_factory().start()`` forever, restarting after any failure.

    Blocks until the process terminates — intended as a daemon-thread target.

    Args:
        name: short identifier used in log lines (e.g. "exception-materializer").
        consumer_factory: must return a NEW consumer instance per call, so each
            attempt starts from a clean KafkaConsumer (no stale half-initialized
            state from the crashed attempt).
        base_delay / max_delay: exponential backoff bounds in seconds.
        sleep: injectable sleeper (used by tests); defaults to ``time.sleep``.
        log: logger to emit through; defaults to this module's logger.
    """
    log = log or logger
    attempt = 0
    while True:
        attempt += 1
        consumer = None
        try:
            consumer = consumer_factory()
            consumer.start()
            # start() blocks while healthy; returning means the consume loop
            # ended unexpectedly (e.g. broker closed the connection) — treat
            # it like a failure and restart.
            log.warning("%s stopped unexpectedly; restarting", name)
        except Exception as e:  # noqa: BLE001 - the supervisor must survive anything
            log.error("%s crashed (attempt %d): %s", name, attempt, e)
        finally:
            if consumer is not None:
                try:
                    consumer.close()
                except Exception:  # noqa: BLE001 - best-effort close
                    pass
        delay = compute_backoff_delay(attempt, base_delay=base_delay, max_delay=max_delay)
        log.info("%s restarting in %.1fs", name, delay)
        sleep(delay)
