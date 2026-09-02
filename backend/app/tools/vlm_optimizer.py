"""
VLM Optimization Layer

Provides model routing, Redis-based distributed rate limiting,
and Full Jitter exponential backoff for VLM API calls.

3-Pillar Rate Limiting Architecture:
1. Asyncio Semaphore — limits concurrent in-flight requests per worker
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

import asyncio
import logging
import random
import re
import time
from typing import Optional

import numpy as np

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

    async def connect(self):
        """Connect to Redis and initialize the token bucket."""
        import redis.asyncio as aioredis

        self.redis = aioredis.from_url(
            self.redis_url,
            decode_responses=True,
        )

        # Initialize bucket with full tokens
        pipe = self.redis.pipeline()
        pipe.set(self.bucket_key, self.rpm)
        pipe.expire(self.bucket_key, 60)
        await pipe.execute()

        logger.info("Redis rate limiter connected", rpm=self.rpm)

    async def acquire(self) -> float:
        """Acquire a token from the bucket.

        Returns the wait time (0.0 if token available immediately).
        If bucket is empty, sleeps until a token is available.

        Uses atomic Redis DECR to prevent race conditions across workers.
        """
        while True:
            # Atomic decrement
            tokens = await self.redis.decr(self.bucket_key)

            if tokens >= 0:
                # Token acquired successfully
                return 0.0

            # No tokens available — restore the decremented value
            await self.redis.incr(self.bucket_key)

            # Check TTL on the window
            ttl = await self.redis.ttl(self.bucket_key)
            if ttl <= 0:
                # Window expired, reset bucket
                pipe = self.redis.pipeline()
                pipe.set(self.bucket_key, self.rpm)
                pipe.expire(self.bucket_key, 60)
                await pipe.execute()
                continue

            # Wait for the window to reset
            wait_time = ttl + 0.1  # Small buffer to avoid edge case
            logger.info(
                "Rate limit reached, waiting",
                wait_seconds=round(wait_time, 1),
                remaining_tokens=max(0, tokens),
            )
            await asyncio.sleep(wait_time)

    async def close(self):
        """Close Redis connection."""
        if self.redis:
            await self.redis.close()


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
    match = re.search(r"retry-after[:\s]+(\d+)", error_str, re.IGNORECASE)
    if match:
        return float(match.group(1))
    return None


# =============================================================================
# Async Rate-Limited VLM Client (for Worker)
# =============================================================================


class RateLimitedGeminiClient:
    """Async wrapper combining Semaphore + Redis Token Bucket + Full Jitter.

    Usage in worker:
        client = RateLimitedGeminiClient(max_concurrent=3, rpm=15)
        await client.initialize()
        result = await client.call_with_retry(my_async_function, arg1, arg2)
        await client.close()
    """

    def __init__(
        self,
        max_concurrent: int = 3,
        rpm: int = 15,
        redis_url: str = "redis://redis:6379/0",
        max_retries: int = 5,
        base_delay: float = 1.0,
    ):
        # Pillar 1: Asyncio Semaphore (concurrency control)
        self.semaphore = asyncio.Semaphore(max_concurrent)

        # Pillar 2: Redis Token Bucket (RPM enforcement)
        self.rate_limiter = RedisTokenBucketRateLimiter(
            rpm=rpm,
            redis_url=redis_url,
        )

        # Pillar 3: Backoff config
        self.max_retries = max_retries
        self.base_delay = base_delay

    async def initialize(self):
        """Connect to Redis."""
        try:
            await self.rate_limiter.connect()
        except Exception as e:
            logger.warning(
                "Redis not available, falling back to in-memory rate limiter",
                error=str(e),
            )
            self.rate_limiter = None

    async def call_with_retry(self, func, *args, **kwargs):
        """Execute an async function with all 3 rate limiting pillars.

        Args:
            func: Async function to call
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
            async with self.semaphore:
                # Pillar 2: Acquire token from rate limiter
                if self.rate_limiter:
                    await self.rate_limiter.acquire()
                else:
                    # Fallback: in-memory rate limiter
                    _vlm_rate_limiter.acquire()

                try:
                    result = await func(*args, **kwargs)
                    return result

                except Exception as e:
                    last_exception = e
                    error_str = str(e).lower()

                    # Check if it's a 429 rate limit error
                    if "429" in error_str or "rate" in error_str.lower():
                        # Pillar 3: Exponential Backoff with Full Jitter
                        retry_after = _extract_retry_after(error_str)

                        if retry_after is not None:
                            wait_time = retry_after
                        else:
                            # Full Jitter formula:
                            # Wait = random_uniform(0, Base_Delay * 2^Attempt)
                            wait_time = random.uniform(
                                0,
                                self.base_delay * (2 ** attempt),
                            )

                        logger.warning(
                            "Rate limited, retrying with backoff",
                            attempt=attempt + 1,
                            max_retries=self.max_retries,
                            wait_seconds=round(wait_time, 2),
                            error=error_str[:200],
                        )
                        await asyncio.sleep(wait_time)
                    else:
                        # Non-rate-limit error — raise immediately
                        raise

        raise RuntimeError(
            f"Gemini API call failed after {self.max_retries} retries: "
            f"{last_exception}"
        )

    async def close(self):
        """Cleanup connections."""
        if self.rate_limiter:
            await self.rate_limiter.close()


# =============================================================================
# Optimization Stats
# =============================================================================


class OptimizationStats:
    """Track optimization metrics for monitoring."""

    def __init__(self):
        self.total_calls = 0
        self.cache_hits = 0
        self.cache_misses = 0
        self.rate_limit_waits = 0
        self.retries = 0
        self.total_input_tokens_saved = 0

    def record_call(self, cached: bool = False, tokens_saved: int = 0):
        """Record a VLM call."""
        self.total_calls += 1
        if cached:
            self.cache_hits += 1
        else:
            self.cache_misses += 1
        self.total_input_tokens_saved += tokens_saved

    def get_summary(self) -> dict:
        """Get optimization summary."""
        return {
            "total_calls": self.total_calls,
            "cache_hit_rate": self.cache_hits / max(self.total_calls, 1),
            "total_tokens_saved": self.total_input_tokens_saved,
            "estimated_cost_savings": self.total_input_tokens_saved * 0.00000075,
        }


# Global stats
optimization_stats = OptimizationStats()
