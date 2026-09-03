"""
Tests for VLM Optimization Layer

Tests cover:
- Model selection based on quality metrics
- Token bucket rate limiter (in-memory)
- Full Jitter exponential backoff
- Retry logic for rate limit errors
"""

import pytest
import time
from unittest.mock import patch, MagicMock

from app.tools.vlm_optimizer import (
    select_model,
    TokenBucketRateLimiter,
    retry_with_backoff,
    _extract_retry_after,
    RateLimitedGeminiClient,
)


class TestModelSelection:
    """Test model routing based on image quality."""

    def test_high_quality_uses_flash_lite(self):
        """Standard quality images should use flash-lite (fast, cheap)."""
        with patch('app.tools.vlm_optimizer.get_settings') as mock_settings:
            mock_settings.return_value.blur_threshold = 100.0
            mock_settings.return_value.ocr_confidence_threshold = 70.0
            mock_settings.return_value.gemini_model_fast = "gemini-3.5-flash-lite"
            mock_settings.return_value.gemini_model_fallback = "gemini-3.7-flash"

            model = select_model(
                is_batch=True,
                blur_score=150.0,  # Above threshold
                ocr_confidence=85.0,  # Above 70%
            )

            assert model == "gemini-3.5-flash-lite"

    def test_low_blur_uses_flash(self):
        """Blurry images should use flash (accurate)."""
        with patch('app.tools.vlm_optimizer.get_settings') as mock_settings:
            mock_settings.return_value.blur_threshold = 100.0
            mock_settings.return_value.ocr_confidence_threshold = 70.0
            mock_settings.return_value.gemini_model_fast = "gemini-3.5-flash-lite"
            mock_settings.return_value.gemini_model_fallback = "gemini-3.7-flash"

            model = select_model(
                is_batch=True,
                blur_score=50.0,  # Below threshold
                ocr_confidence=85.0,
            )

            assert model == "gemini-3.7-flash"

    def test_low_ocr_uses_flash(self):
        """Low OCR confidence should use flash (accurate)."""
        with patch('app.tools.vlm_optimizer.get_settings') as mock_settings:
            mock_settings.return_value.blur_threshold = 100.0
            mock_settings.return_value.ocr_confidence_threshold = 70.0
            mock_settings.return_value.gemini_model_fast = "gemini-3.5-flash-lite"
            mock_settings.return_value.gemini_model_fallback = "gemini-3.7-flash"

            model = select_model(
                is_batch=True,
                blur_score=150.0,
                ocr_confidence=50.0,  # Below 70%
            )

            assert model == "gemini-3.7-flash"

    def test_both_low_uses_flash(self):
        """Both low blur and low OCR should use flash."""
        with patch('app.tools.vlm_optimizer.get_settings') as mock_settings:
            mock_settings.return_value.blur_threshold = 100.0
            mock_settings.return_value.ocr_confidence_threshold = 70.0
            mock_settings.return_value.gemini_model_fast = "gemini-3.5-flash-lite"
            mock_settings.return_value.gemini_model_fallback = "gemini-3.7-flash"

            model = select_model(
                is_batch=True,
                blur_score=50.0,
                ocr_confidence=50.0,
            )

            assert model == "gemini-3.7-flash"


class TestTokenBucketRateLimiter:
    """Test in-memory token bucket rate limiter."""

    def test_acquire_immediately_when_tokens_available(self):
        """Should acquire token immediately when bucket has tokens."""
        limiter = TokenBucketRateLimiter(max_tokens=5, refill_rate=10)

        result = limiter.acquire(timeout=1.0)

        assert result is True
        assert limiter.tokens < 5  # One token consumed

    def test_acquire_waits_when_bucket_empty(self):
        """Should wait when bucket is empty."""
        limiter = TokenBucketRateLimiter(max_tokens=1, refill_rate=10)

        # First acquire succeeds
        limiter.acquire(timeout=1.0)

        # Second acquire should wait (but we set short timeout)
        # With refill_rate=10, it should refill quickly
        result = limiter.acquire(timeout=2.0)

        assert result is True

    def test_acquire_timeout_returns_false(self):
        """Should return False when timeout expires."""
        limiter = TokenBucketRateLimiter(max_tokens=1, refill_rate=0.001)  # Very slow refill

        # First acquire consumes the token
        limiter.acquire(timeout=1.0)

        # Second acquire should timeout
        result = limiter.acquire(timeout=0.1)

        assert result is False

    def test_refill_does_not_exceed_max(self):
        """Tokens should not exceed max_tokens after refill."""
        limiter = TokenBucketRateLimiter(max_tokens=5, refill_rate=100)

        # Consume all tokens
        for _ in range(5):
            limiter.acquire(timeout=1.0)

        # Wait for refill
        time.sleep(0.1)

        # Refill should not exceed max
        limiter._refill()
        assert limiter.tokens <= limiter.max_tokens


class TestRetryWithBackoff:
    """Test exponential backoff with Full Jitter."""

    def test_success_on_first_try(self):
        """Should return result on first successful call."""
        func = MagicMock(return_value="success")

        result = retry_with_backoff(func, max_retries=3)

        assert result == "success"
        assert func.call_count == 1

    def test_retries_on_rate_limit_error(self):
        """Should retry on 429 rate limit error."""
        func = MagicMock(side_effect=[
            Exception("429 Rate limit exceeded"),
            Exception("429 Rate limit exceeded"),
            "success",
        ])

        with patch('app.tools.vlm_optimizer.time.sleep'):
            result = retry_with_backoff(func, max_retries=3)

        assert result == "success"
        assert func.call_count == 3

    def test_raises_on_non_retryable_error(self):
        """Should raise immediately on non-retryable error."""
        func = MagicMock(side_effect=ValueError("Invalid input"))

        with pytest.raises(ValueError):
            retry_with_backoff(func, max_retries=3)

        assert func.call_count == 1

    def test_raises_after_max_retries(self):
        """Should raise after max retries exhausted."""
        func = MagicMock(side_effect=Exception("429 Rate limit"))

        with patch('app.tools.vlm_optimizer.time.sleep'):
            with pytest.raises(Exception):
                retry_with_backoff(func, max_retries=2)

        assert func.call_count == 2

    def test_full_jitter_provides_random_delay(self):
        """Full Jitter should provide random delay within bounds."""
        delays = []

        def mock_sleep(delay):
            delays.append(delay)

        func = MagicMock(side_effect=[
            Exception("429 Rate limit"),
            Exception("429 Rate limit"),
            "success",
        ])

        with patch('app.tools.vlm_optimizer.time.sleep', mock_sleep):
            result = retry_with_backoff(
                func,
                max_retries=3,
                base_delay=1.0,
                max_delay=10.0,
            )

        assert result == "success"
        # Check that delays were within bounds
        for delay in delays:
            assert 0 <= delay <= 10.0


class TestExtractRetryAfter:
    """Test Retry-After header extraction."""

    def test_extracts_numeric_value(self):
        """Should extract numeric Retry-After value."""
        error_str = "429 Rate limit exceeded. Retry-After: 30"
        result = _extract_retry_after(error_str)
        assert result == 30.0

    def test_extracts_from_headers(self):
        """Should extract from response headers."""
        error_str = "rate limit retry-after: 60 seconds"
        result = _extract_retry_after(error_str)
        assert result == 60.0

    def test_returns_none_if_not_found(self):
        """Should return None if no Retry-After header."""
        error_str = "429 Rate limit exceeded"
        result = _extract_retry_after(error_str)
        assert result is None


class TestRateLimitedGeminiClient:
    """Test the synchronous rate-limited Gemini client.

    Redis is not available in unit tests: the client is pointed at the
    in-memory fallback limiter (rate_limiter = None) exactly as it would
    be after initialize() fails to reach Redis.
    """

    def _make_client(self, max_retries: int = 3):
        client = RateLimitedGeminiClient(
            max_concurrent=2,
            rpm=15,
            max_retries=max_retries,
            base_delay=0.01,
        )
        client.rate_limiter = None  # Use in-memory fallback
        return client

    def test_call_success_on_first_try(self):
        """Should return result on first successful call."""
        client = self._make_client()
        func = MagicMock(return_value={"ok": True})

        result = client.call(func)

        assert result == {"ok": True}
        assert func.call_count == 1

    def test_call_retries_on_rate_limit_error(self):
        """Should retry on 429 rate limit error."""
        client = self._make_client(max_retries=3)
        func = MagicMock(side_effect=[
            Exception("429 rate limit exceeded"),
            "success",
        ])

        with patch('app.tools.vlm_optimizer.time.sleep'):
            result = client.call(func)

        assert result == "success"
        assert func.call_count == 2

    def test_call_raises_on_non_retryable_error(self):
        """Should raise immediately on non-rate-limit error."""
        client = self._make_client()
        func = MagicMock(side_effect=ValueError("Invalid input"))

        with pytest.raises(ValueError):
            client.call(func)

        assert func.call_count == 1

    def test_call_raises_after_max_retries(self):
        """Should raise RuntimeError after max retries exhausted."""
        client = self._make_client(max_retries=2)
        func = MagicMock(side_effect=Exception("429 rate limit"))

        with patch('app.tools.vlm_optimizer.time.sleep'):
            with pytest.raises(RuntimeError):
                client.call(func)

        assert func.call_count == 2
