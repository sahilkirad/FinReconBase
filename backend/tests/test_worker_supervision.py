"""TDD tests — supervised consumer restart loop (app/workers/supervision.py).

Covers:
- compute_backoff_delay: capped exponential backoff math
- run_consumer_supervisor: crash -> fresh consumer + backoff restart
- run_consumer_supervisor: unexpected clean return -> restart too
- run_consumer_supervisor: consumer_factory failure (no consumer) -> retried safely
- run_consumer_supervisor: every crashed consumer is closed before restart
"""

import pytest

from app.workers.supervision import (
    DEFAULT_BASE_DELAY_S,
    DEFAULT_MAX_DELAY_S,
    compute_backoff_delay,
    run_consumer_supervisor,
)


# =============================================================================
# Test doubles
# =============================================================================


class _StopTest(Exception):
    """Raised by the injected sleeper to break the endless supervisor loop."""


class _BoundedSleep:
    """Records sleep delays; raises _StopTest after ``limit`` recordings."""

    def __init__(self, limit: int = 1):
        self.limit = limit
        self.calls: list[float] = []

    def __call__(self, delay: float) -> None:
        self.calls.append(delay)
        if len(self.calls) >= self.limit:
            raise _StopTest()


class _RecorderLog:
    def __init__(self):
        self.error_messages: list[str] = []
        self.warning_messages: list[str] = []
        self.info_messages: list[str] = []

    def error(self, msg, *args, **kwargs):
        self.error_messages.append(msg % args if args else msg)

    def warning(self, msg, *args, **kwargs):
        self.warning_messages.append(msg % args if args else msg)

    def info(self, msg, *args, **kwargs):
        self.info_messages.append(msg % args if args else msg)


class _StubConsumer:
    def __init__(self, crash: bool = True):
        self.crash = crash
        self.starts = 0
        self.closed = 0

    def start(self) -> None:
        self.starts += 1
        if self.crash:
            raise RuntimeError("KafkaTimeoutError: Unable to bootstrap from kafka:9093")

    def close(self) -> None:
        self.closed += 1


# =============================================================================
# 1. Backoff math
# =============================================================================


class TestBackoffDelay:
    def test_first_attempt_uses_base_delay(self):
        assert compute_backoff_delay(1) == DEFAULT_BASE_DELAY_S == 1.0

    def test_grows_exponentially(self):
        assert compute_backoff_delay(2) == 2.0
        assert compute_backoff_delay(3) == 4.0
        assert compute_backoff_delay(4) == 8.0

    def test_capped_at_max_delay(self):
        assert compute_backoff_delay(100) == DEFAULT_MAX_DELAY_S == 30.0

    def test_custom_bounds_respected(self):
        assert compute_backoff_delay(1, base_delay=0.5, max_delay=3.0) == 0.5
        # 0.5 * 2 ** 2 = 2.0 capped at 3.0
        assert compute_backoff_delay(3, base_delay=0.5, max_delay=3.0) == 2.0
        # 0.5 * 2 ** 3 = 4.0 capped at 3.0
        assert compute_backoff_delay(4, base_delay=0.5, max_delay=3.0) == 3.0

    def test_never_returns_negative(self):
        assert compute_backoff_delay(0) == 1.0
        assert compute_backoff_delay(1, base_delay=-5.0) == 0.0


# =============================================================================
# 2. Supervisor loop
# =============================================================================


class TestSupervisor:
    def test_crash_restarts_with_fresh_consumer_and_backoff(self):
        """Three consecutive crashes -> 3 fresh consumers, delays 1s/2s/4s."""
        created: list[_StubConsumer] = []

        def factory():
            c = _StubConsumer(crash=True)
            created.append(c)
            return c

        sleeper = _BoundedSleep(limit=3)
        log = _RecorderLog()
        with pytest.raises(_StopTest):
            run_consumer_supervisor("t", factory, sleep=sleeper, log=log)

        assert len(created) == 3  # a fresh consumer per attempt
        assert all(c.starts == 1 for c in created)
        assert sleeper.calls == [1.0, 2.0, 4.0]
        assert "crashed" in log.error_messages[0]
        assert "KafkaTimeoutError" in log.error_messages[0]
        assert len(log.error_messages) == 3

    def test_crashed_consumer_is_closed_before_restart(self):
        created: list[_StubConsumer] = []

        def factory():
            c = _StubConsumer(crash=True)
            created.append(c)
            return c

        sleeper = _BoundedSleep(limit=2)
        with pytest.raises(_StopTest):
            run_consumer_supervisor("t", factory, sleep=sleeper, log=_RecorderLog())

        assert all(c.closed == 1 for c in created)

    def test_clean_return_is_treated_as_restart(self):
        """start() returning without raising is abnormal for a consume loop."""
        created: list[_StubConsumer] = []

        def factory():
            c = _StubConsumer(crash=False)
            created.append(c)
            return c

        sleeper = _BoundedSleep(limit=1)
        log = _RecorderLog()
        with pytest.raises(_StopTest):
            run_consumer_supervisor("t", factory, sleep=sleeper, log=log)

        assert len(created) == 1
        assert created[0].starts == 1
        assert created[0].closed == 1
        assert sleeper.calls == [1.0]
        assert log.error_messages == []
        assert "stopped unexpectedly" in log.warning_messages[0]

    def test_factory_failure_is_retried_without_close(self):
        """factory() itself raising (consumer never constructed) is retried."""
        factory_calls: list[int] = []

        def factory():
            factory_calls.append(1)
            raise RuntimeError("consumer construction failed")

        sleeper = _BoundedSleep(limit=2)
        log = _RecorderLog()
        with pytest.raises(_StopTest):
            run_consumer_supervisor("t", factory, sleep=sleeper, log=log)

        assert len(factory_calls) == 2
        assert sleeper.calls == [1.0, 2.0]
        assert "consumer construction failed" in log.error_messages[0]

    def test_custom_backoff_bounds_used(self):
        created: list[_StubConsumer] = []

        def factory():
            c = _StubConsumer(crash=True)
            created.append(c)
            return c

        sleeper = _BoundedSleep(limit=3)
        with pytest.raises(_StopTest):
            run_consumer_supervisor(
                "t",
                factory,
                base_delay=0.5,
                max_delay=2.0,
                sleep=sleeper,
                log=_RecorderLog(),
            )

        # 0.5, 1.0, then capped at 2.0 (0.5 * 2**2 = 2.0)
        assert sleeper.calls == [0.5, 1.0, 2.0]
