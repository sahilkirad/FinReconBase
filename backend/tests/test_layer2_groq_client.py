"""
TDD tests — RateLimitedGroqClient (Groq rate governance).

- semaphore bounds concurrent calls
- 429/rate-limit retries with full-jitter sleep (Retry-After honored)
- non-rate-limit exceptions propagate immediately
- success path calls runnable.invoke exactly once
- without Redis, rate limiting degrades (semaphore still active)
"""

import os
import threading
import time

os.environ["GOOGLE_OAUTH_CLIENT_ID"] = "test"
os.environ["JWT_SECRET_KEY"] = "test-secret"
os.environ["GROQ_API_KEY"] = "test-key"
os.environ["GROQ_MODEL"] = "llama-test"

import pytest

from app.agent.llm.groq_client import RateLimitedGroqClient


class _Runnable:
    """Canned runnable double: raises until call N then returns value."""

    def __init__(self, failures_before_success=0, exc_value="429 rate limit"):
        self.calls = 0
        self.failures_before_success = failures_before_success
        self.exc_value = exc_value

    def invoke(self, payload):
        self.calls += 1
        if self.calls <= self.failures_before_success:
            raise RuntimeError(self.exc_value)
        return {"ok": True, "payload": payload}


@pytest.fixture()
def client():
    return RateLimitedGroqClient(
        model="llama-test",
        api_key="test-key",
        rpm=28,
        redis_url="redis://127.0.0.1:1/0",  # unreachable -> no redis
        max_concurrent=2,
        max_retries=3,
        base_delay=0.05,
    )


class TestInvocation:
    def test_success_calls_once(self, client):
        runnable = _Runnable()
        result = client.invoke(runnable, {"x": 1})
        assert result["ok"] is True
        assert runnable.calls == 1

    def test_429_retries_then_succeeds(self, client, monkeypatch):
        sleeps: list[float] = []
        monkeypatch.setattr(time, "sleep", lambda s: sleeps.append(s))
        runnable = _Runnable(failures_before_success=2, exc_value="429 Too Many Requests")
        result = client.invoke(runnable, {"x": 1})
        assert result["ok"] is True
        assert runnable.calls == 3  # 2 failures + success
        assert len(sleeps) == 2
        assert all(0.0 <= s < 0.1 for s in sleeps)  # full jitter within base*2^n

    def test_retry_after_header_used(self, client, monkeypatch):
        sleeps: list[float] = []
        monkeypatch.setattr(time, "sleep", lambda s: sleeps.append(s))
        runnable = _Runnable(
            failures_before_success=1,
            exc_value="429 retry-after: 1.5 seconds",
        )
        client.invoke(runnable, {})
        assert sleeps == [1.5]

    def test_exhausted_retries_raises(self, client, monkeypatch):
        monkeypatch.setattr(time, "sleep", lambda s: None)
        runnable = _Runnable(failures_before_success=99, exc_value="429 quota")
        with pytest.raises(RuntimeError):
            client.invoke(runnable, {})
        assert runnable.calls == client.max_retries  # 3

    def test_non_rate_error_raises_immediately(self, client, monkeypatch):
        sleeps: list[float] = []
        monkeypatch.setattr(time, "sleep", lambda s: sleeps.append(s))
        runnable = _Runnable(failures_before_success=1, exc_value="ValueError: bad schema")
        with pytest.raises(RuntimeError):
            client.invoke(runnable, {})
        assert runnable.calls == 1
        assert sleeps == []


class TestSemaphore:
    def test_concurrency_bounded(self, client):
        """Even when a call hangs, at most max_concurrent invocations run."""
        active = 0
        peak = 0
        lock = threading.Lock()

        class _SlowRunnable:
            def invoke(self, payload):
                nonlocal active, peak
                with lock:
                    active += 1
                    peak = max(peak, active)
                time.sleep(0.05)
                with lock:
                    active -= 1
                return {"ok": True}

        results = []
        threads = []
        for _ in range(6):
            t = threading.Thread(target=lambda: results.append(client.invoke(_SlowRunnable(), {})))
            threads.append(t)
            t.start()
        for t in threads:
            t.join()
        assert len(results) == 6
        assert peak <= 2  # max_concurrent=2


class TestOfflineDegradation:
    def test_uninitialized_client_still_invokes(self):
        """Before initialize() (no Redis, no model built) the wrapper still
        invokes injected runnables under the semaphore — the Kafka consumer
        never hard-fails when infra startup ordering lags."""
        offline = RateLimitedGroqClient(
            model="llama-test",
            api_key="test-key",
            rpm=28,
            redis_url="redis://127.0.0.1:1/0",
            max_concurrent=2,
            max_retries=3,
            base_delay=0.05,
        )
        runnable = _Runnable()
        result = offline.invoke(runnable, {})
        assert result["ok"] is True
        assert runnable.calls == 1
