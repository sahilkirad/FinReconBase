"""
VLM Optimization Layer

Provides model routing, Redis-based distributed rate limiting,
and Full Jitter exponential backoff for VLM API calls.

3-Pillar Rate Limiting Architecture:
1. Threading Semaphore — limits concurrent in-flight requests per worker
2. Redis Token Bucket — enforces RPM across ALL workers (distributed)
3. Exponential Backoff with Full Jitter — handles 429 retries safely

All workers share the same Redis instance, so the RPM limit
is enforced globally across the consumer group.

Model Selection:
- gemini-3.5-flash-lite: Standard digital PDFs and clear scans
- gemini-3.7-flash: Heavily degraded invoice photos

RULE: Gemini is NEVER used for mathematical validation.
All financial math is performed by our local Python checksum layer.
"""

import logging
import random
import re
import threading
import time
from typing import Optional

from app.core.config import get_settings

logger = logging.getLogger(__name__)


# =============================================================================
# Model Selection
# =============================================================================


def select_model(
    is_batch: bool = False,
    blur_score: float = 0.0,
    ocr_confidence: float = 100.0,
) -> str:
    """Select the appropriate VLM model based on image quality.

    Routing Logic:
        - blur_score >= threshold AND ocr_confidence >= 70% → flash-lite (fast, cheap)
        - Otherwise → flash (accurate for degraded images)

    RULE: Gemini NEVER does math. Checksum is always local Python.

    Args:
        is_batch: True if processing batch invoices
        blur_score: Laplacian variance score from blur check
        ocr_confidence: Average OCR confidence (0-100)

    Returns:
        Model name string
    """
    settings = get_settings()

    blur_ok = blur_score >= settings.blur_threshold
    ocr_ok = ocr_confidence >= settings.ocr_confidence_threshold

    if blur_ok and ocr_ok:
        # Standard quality → use fast/cheap model
        return settings.gemini_model_fast
    else:
        # Degraded quality → use accurate model
        return settings.gemini_model_fallback


# =============================================================================
# Redis Token Bucket Rate Limiter (Distributed)
# =============================================================================


class RedisTokenBucketRateLimiter:
    """Distributed token bucket rate limiter using Redis.

    All workers share the same Redis instance, so RPM is enforced
    globally across the consumer group. This prevents any single worker
    from consuming the entire API quota.

    Uses atomic Redis DECR to prevent race conditions across workers.
    Synchronous implementation for the thread-based Kafka consumer.
    """

    def __init__(
        self,
        rpm: int = 15,
        redis_url: str = "redis://redis:6379/0",
        bucket_key: str = "gemini:rate_limit:tokens",
    ):
        self.rpm = rpm
        self.redis_url = redis_url
        self.bucket_key = bucket_key
        self.redis = None

    def connect(self):
        """Connect to Redis and initialize the token bucket.

        The bucket is only initialized if it does not already exist,
        so a worker joining mid-window does not reset the shared quota.
        """
        import redis

        self.redis = redis.Redis.from_url(
            self.redis_url,
            decode_responses=True,
        )
        self.redis.ping()

        if not self.redis.exists(self.bucket_key):
            pipe = self.redis.pipeline()
            pipe.set(self.bucket_key, self.rpm)
            pipe.expire(self.bucket_key, 60)
            pipe.execute()

        logger.info(
            "Redis rate limiter connected",
            extra={"rpm": self.rpm, "bucket_key": self.bucket_key},
        )

    def acquire(self) -> None:
        """Acquire a token from the bucket.

        Blocks until a token is available. Uses atomic Redis DECR to
        prevent race conditions across workers. If the bucket is empty,
        the caller sleeps until the 60-second window resets.
        """
        while True:
            # Atomic decrement
            tokens = self.redis.decr(self.bucket_key)

            if tokens >= 0:
                # Token acquired successfully
                return

            # No tokens available — restore the decremented value
            self.redis.incr(self.bucket_key)

            # Check TTL on the window
            ttl = self.redis.ttl(self.bucket_key)
            if ttl <= 0:
                # Window expired, reset bucket
                pipe = self.redis.pipeline()
                pipe.set(self.bucket_key, self.rpm)
                pipe.expire(self.bucket_key, 60)
                pipe.execute()
                continue

            # Wait for the window to reset
            wait_time = ttl + 0.1  # Small buffer to avoid edge case
            logger.info(
                "Rate limit reached, waiting",
                extra={
                    "wait_seconds": round(wait_time, 1),
                    "remaining_tokens": max(0, tokens),
                },
            )
            time.sleep(wait_time)

    def close(self):
        """Close Redis connection."""
        if self.redis:
            self.redis.close()
            self.redis = None


# =============================================================================
# In-Memory Rate Limiter (Fallback for single-worker / testing)
# =============================================================================


class TokenBucketRateLimiter:
    """In-memory token bucket rate limiter.

    Used as fallback when Redis is not available.
    NOT distributed — each process has its own bucket.
    """

    def __init__(self, max_tokens: int = 10, refill_rate: float = 10 / 60):
        self.max_tokens = max_tokens
        self.refill_rate = refill_rate
        self.tokens = max_tokens
        self.last_refill = time.time()

    def acquire(self, timeout: float = 60.0) -> bool:
        """Acquire a token, waiting if necessary."""
        start_time = time.time()

        while True:
            self._refill()

            if self.tokens >= 1:
                self.tokens -= 1
                return True

            # Calculate wait time for next token
            wait_time = (1 - self.tokens) / self.refill_rate

            if time.time() - start_time + wait_time > timeout:
                logger.warning("Rate limiter timeout reached")
                return False

            time.sleep(min(wait_time, 1.0))

    def _refill(self):
        """Refill tokens based on elapsed time."""
        now = time.time()
        elapsed = now - self.last_refill
        self.tokens = min(self.max_tokens, self.tokens + elapsed * self.refill_rate)
        self.last_refill = now


# Global in-memory rate limiter (fallback)
_vlm_rate_limiter = TokenBucketRateLimiter(max_tokens=10, refill_rate=10 / 60)


def get_vlm_rate_limiter() -> TokenBucketRateLimiter:
    """Get the global in-memory VLM rate limiter (fallback)."""
    return _vlm_rate_limiter


# =============================================================================
# Full Jitter Exponential Backoff
# =============================================================================


def retry_with_backoff(
    func,
    max_retries: int = 5,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
):
    """Retry a function with Exponential Backoff and Full Jitter.

    Full Jitter Formula:
        wait = random_uniform(0, base_delay * 2^attempt)

    This prevents "Thundering Herd" / "Retry Storm" when multiple
    workers fail simultaneously.

    If the API returns a Retry-After header, we use that value instead.

    Handles:
        - 429 Rate Limit errors
        - 503 Service Unavailable
        - Network timeouts

    Args:
        func: Callable to retry
        max_retries: Maximum retry attempts
        base_delay: Initial delay in seconds
        max_delay: Maximum delay cap

    Returns:
        Function result

    Raises:
        Last exception if all retries fail
    """
    last_exception = None

    for attempt in range(max_retries):
        try:
            return func()
        except Exception as e:
            last_exception = e
            error_str = str(e).lower()

            # Check if retryable error
            retryable = any(
                indicator in error_str
                for indicator in [
                    "429",
                    "rate limit",
                    "quota",
                    "503",
                    "service unavailable",
                    "timeout",
                    "connection",
                    "deadline exceeded",
                ]
            )

            if not retryable:
                raise

            # Check for Retry-After header in error response
            retry_after = _extract_retry_after(error_str)

            if retry_after is not None:
                total_delay = retry_after
            else:
                # Full Jitter: wait = random_uniform(0, base * 2^attempt)
                total_delay = random.uniform(
                    0,
                    min(base_delay * (2 ** attempt), max_delay),
                )

            logger.warning(
                f"Retryable error, attempt {attempt + 1}/{max_retries}, "
                f"waiting {total_delay:.1f}s: {e}"
            )
            time.sleep(total_delay)

    raise last_exception


def _extract_retry_after(error_str: str) -> Optional[float]:
    """Extract Retry-After header value from error response."""
    match = re.search(r"retry-after[:\\s]+(\\d+)", error_str, re.IGNORECASE)
    if match:
        return float(match.group(1))
    return None


# =============================================================================
# Synchronous Rate-Limited VLM Client (for Kafka Consumer)
# =============================================================================


class RateLimitedGeminiClient:
    """Synchronous wrapper combining Semaphore + Redis Token Bucket + Full Jitter.

    The Kafka consumer is a thread-based (synchronous) loop, so this client
    is synchronous. Concurrency is controlled per process with a threading
    semaphore; the global RPM quota is enforced across ALL workers via the
    shared Redis token bucket.

    Usage in worker:
        client = RateLimitedGeminiClient(max_concurrent=3, rpm=15)
        client.initialize()
        result = client.call(my_function, arg1, arg2)
        client.close()
    """

    def __init__(
        self,
        max_concurrent: int = 3,
        rpm: int = 15,
        redis_url: str = "redis://redis:6379/0",
        max_retries: int = 5,
        base_delay: float = 1.0,
    ):
        # Pillar 1: Threading Semaphore (concurrency control)
        self.semaphore = threading.BoundedSemaphore(max_concurrent)

        # Pillar 2: Redis Token Bucket (RPM enforcement, shared across workers)
        self.rate_limiter = RedisTokenBucketRateLimiter(
            rpm=rpm,
            redis_url=redis_url,
        )

        # Pillar 3: Backoff config
        self.max_retries = max_retries
        self.base_delay = base_delay

    def initialize(self):
        """Connect to Redis.

        If Redis is unavailable, falls back to the in-memory rate limiter
        so the pipeline keeps running (RPM enforcement degrades to
        per-process instead of global).
        """
        try:
            self.rate_limiter.connect()
        except Exception as e:
            logger.warning(
                "Redis not available, falling back to in-memory rate limiter",
                extra={"error": str(e)},
            )
            self.rate_limiter = None

    def call(self, func, *args, **kwargs):
        """Execute a synchronous function with all 3 rate limiting pillars.

        Args:
            func: Callable to invoke
            *args: Positional arguments
            **kwargs: Keyword arguments

        Returns:
            Result of func()

        Raises:
            RuntimeError: After max_retries exhausted
        """
        last_exception = None

        for attempt in range(self.max_retries):
            # Pillar 1: Acquire semaphore (limits concurrent requests)
            with self.semaphore:
                # Pillar 2: Acquire token from rate limiter
                if self.rate_limiter:
                    self.rate_limiter.acquire()
                else:
                    # Fallback: in-memory rate limiter
                    _vlm_rate_limiter.acquire()

                try:
                    return func(*args, **kwargs)

                except Exception as e:
                    last_exception = e
                    error_str = str(e).lower()

                    # Transient failures are retryable: 429/quota/503, timeouts,
                    # deadline exceeded, connection resets (mirrors retry_with_backoff)
                    retryable = any(
                        indicator in error_str
                        for indicator in (
                            "429", "rate limit", "quota", "503",
                            "service unavailable", "timeout",
                            "deadline exceeded", "connection",
                        )
                    )
                    if retryable:
                        # Pillar 3: Exponential Backoff with Full Jitter
                        retry_after = _extract_retry_after(error_str)

                        if retry_after is not None:
                            wait_time = retry_after
                        else:
                            # Full Jitter formula:
                            # Wait = random_uniform(0, Base_Delay * 2^Attempt)
                            wait_time = random.uniform(
                                0,
                                min(self.base_delay * (2 ** attempt), 30.0),
                            )

                        logger.warning(
                            "Rate limited, retrying with backoff",
                            extra={
                                "attempt": attempt + 1,
                                "max_retries": self.max_retries,
                                "wait_seconds": round(wait_time, 2),
                                "error": error_str[:200],
                            },
                        )
                        time.sleep(wait_time)
                    else:
                        # Non-rate-limit error — raise immediately
                        raise

        raise RuntimeError(
            f"Gemini API call failed after {self.max_retries} retries: "
            f"{last_exception}"
        )

    def close(self):
        """Cleanup connections."""
        if self.rate_limiter:
            self.rate_limiter.close()