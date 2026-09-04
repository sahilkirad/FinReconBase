"""
Layer 2 — RateLimitedGroqClient

Wraps the Groq Llama 3.3 70B supervisor LLM with the same 3-pillar rate
governance proven in Layer 1:

1. Threading Semaphore        — bounds concurrent in-flight LLM calls
   (layer2_max_concurrent; sub-graphs share the pool, so this is a per-process
   ceiling on top of the pool's fan-out).
2. Redis Token Bucket         — GROQ_RPM_LIMIT (default 28, buffer under the
   30 RPM free tier) enforced GLOBALLY across every sub-graph in the process
   and across any scaled supervisor replicas (shared Redis bucket key
   "groq:rate_limit:tokens"). Fanning out 50 sub-graphs simultaneously can
   never nuke the free tier: the bucket throttles them deterministically.
3. Exponential Backoff with Full Jitter — wait = uniform(0, base * 2^attempt);
   a provider Retry-After header, when present, overrides the math.

The distributed limiter is imported read-only from Layer 1
(app.tools.vlm_optimizer.RedisTokenBucketRateLimiter) — no Layer 1 code is
modified.

Invocation model: LangGraph bindings (bind_tools / with_structured_output)
produce derived runnables over the same inner ChatGroq. Every actual API call
goes through `invoke(runnable, payload)`, so the rate limit is enforced at the
real network boundary no matter which binding is used. Unit tests inject a
fake runnable — no network, no Redis.
"""

import logging
import random
import re
import threading
import time

from app.core.config import get_settings

logger = logging.getLogger(__name__)

# Local Retry-After parser (seconds, fractional tolerated) — L1 helper regex can't match raw headers.
_RETRY_AFTER_RE = re.compile(r"retry[- ]after\s*:\s*(\d+(?:\.\d+)?)", re.IGNORECASE)


def _extract_retry_after(error_str: str) -> float | None:
    """Return the Retry-After seconds advertised by the provider, if any."""
    if not error_str:
        return None
    match = _RETRY_AFTER_RE.search(error_str)
    if match:
        try:
            return float(match.group(1))
        except ValueError:
            return None
    return None


class RateLimitedGroqClient:
    """Synchronous rate-limited wrapper over ChatGroq (thread-based worker)."""

    def __init__(
        self,
        *,
        model: str | None = None,
        api_key: str | None = None,
        rpm: int | None = None,
        redis_url: str | None = None,
        max_concurrent: int = 1,
        max_retries: int = 5,
        base_delay: float = 1.0,
        temperature: float = 0.0,
    ):
        settings = get_settings()
        self.model_name = model or settings.groq_model
        self.api_key = api_key or settings.groq_api_key
        self.rpm = rpm or settings.groq_rpm_limit
        self.redis_url = redis_url or settings.redis_url
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.temperature = temperature

        # Pillar 1: concurrency ceiling per process
        self.semaphore = threading.BoundedSemaphore(max_concurrent)

        # Pillar 2: distributed Redis token bucket (Groq-specific key)
        from app.tools.vlm_optimizer import RedisTokenBucketRateLimiter

        self.rate_limiter = RedisTokenBucketRateLimiter(
            rpm=self.rpm,
            redis_url=self.redis_url,
            bucket_key="groq:rate_limit:tokens",
        )
        self._redis_ok = False
        self.llm = None  # lazy ChatGroq

    # ---- lifecycle -------------------------------------------------------

    def initialize(self):
        """Connect Redis and materialize the ChatGroq model.

        If Redis is unreachable, rate limiting degrades to the per-process
        semaphore only (logged loudly) so the pipeline never hard-fails on
        infra startup ordering.
        """
        try:
            self.rate_limiter.connect()
            self._redis_ok = True
            logger.info(
                "Groq rate limiter connected",
                extra={"rpm": self.rpm, "bucket_key": "groq:rate_limit:tokens"},
            )
        except Exception as e:
            self._redis_ok = False
            logger.warning(
                "Redis unavailable — Groq rate limiting degraded to semaphore only",
                extra={"error": str(e)},
            )

        from langchain_groq import ChatGroq

        self.llm = ChatGroq(
            model=self.model_name,
            api_key=self.api_key,
            temperature=self.temperature,
        )

    @property
    def model(self):
        """Raw ChatGroq for building LangGraph bindings."""
        if self.llm is None:
            self.initialize()
        return self.llm

    # ---- LangChain binding surface --------------------------------------

    def bind_tools(self, tools):
        """Return a tool-bound runnable over the inner model."""
        return self.model.bind_tools(tools)

    def with_structured_output(self, schema, *, method: str = "json_mode", **kwargs):
        """Strict structured-output binding for state transitions."""
        return self.model.with_structured_output(schema, method=method, **kwargs)

    # ---- rate-limited invocation ----------------------------------------

    def invoke(self, runnable, payload):
        """Execute `runnable.invoke(payload)` under all three rate pillars.

        Args:
            runnable: Any LangChain Runnable (bound ChatGroq, structured-output
                      runnable, etc.) whose .invoke() performs the API call.
            payload:  Input dict/messages passed to runnable.invoke.

        Returns:
            runnable output.

        Raises:
            RuntimeError: after max_retries on persistent 429/rate-limit.
            Original exception: non-rate-limit errors propagate immediately.
        """
        if runnable is None:
            runnable = self.model

        last_exception: Exception | None = None

        for attempt in range(self.max_retries):
            # Pillar 1: bounded concurrency
            with self.semaphore:
                # Pillar 2: distributed token bucket
                if self._redis_ok:
                    self.rate_limiter.acquire()

                try:
                    return runnable.invoke(payload)
                except Exception as e:
                    last_exception = e
                    error_str = str(e).lower()
                    if "429" in error_str or "rate limit" in error_str or "quota" in error_str:
                        # Pillar 3: full jitter / Retry-After (provider header wins)
                        retry_after = _extract_retry_after(str(e))
                        wait_time = (
                            retry_after
                            if retry_after is not None
                            else random.uniform(
                                0,
                                min(self.base_delay * (2 ** attempt), 30.0),
                            )
                        )
                        logger.warning(
                            "Groq rate limited, retrying with backoff",
                            extra={
                                "attempt": attempt + 1,
                                "wait_seconds": round(wait_time, 2),
                                "error": error_str[:200],
                            },
                        )
                        time.sleep(wait_time)
                    else:
                        raise

        raise RuntimeError(
            f"Groq call failed after {self.max_retries} retries: {last_exception}"
        )

    def close(self):
        """Release the Redis connection."""
        if self.rate_limiter is not None:
            try:
                self.rate_limiter.close()
            except Exception as e:  # pragma: no cover
                logger.warning(f"Groq limiter close failed: {e}")
            self.rate_limiter = None


# process-wide singleton, built from settings
_client = None
_client_lock = threading.Lock()


def get_groq_client(max_concurrent: int | None = None) -> RateLimitedGroqClient:
    """Get the process-wide RateLimitedGroqClient (initialized once)."""
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                settings = get_settings()
                _client = RateLimitedGroqClient(
                    max_concurrent=max_concurrent or settings.layer2_max_concurrent,
                )
                _client.initialize()
    return _client
