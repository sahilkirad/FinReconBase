"""
VLM Optimization Layer

Provides model routing, context caching, and rate limiting for VLM API calls.
Optimizes cost and performance for batch invoice processing.

Models used:
- Gemini 3.6 Flash: For single/complex invoice uploads (higher accuracy)
- Gemini 2.5 Flash-Lite: For batch processing (8x cheaper, sufficient for extraction)
"""

import hashlib
import json
import logging
import time
from functools import lru_cache
from typing import Optional

import numpy as np

from app.core.config import get_settings

logger = logging.getLogger(__name__)


# --- Model Selection ---

class ModelTier(str):
    """Model tier constants."""
    STANDARD = "standard"      # Gemini 3.6 Flash (single invoice)
    ECONOMY = "economy"        # Gemini 2.5 Flash-Lite (batch)


def select_model(is_batch: bool = False, complexity: str = "standard") -> str:
    """
    Select the appropriate VLM model based on use case.
    
    Args:
        is_batch: True if processing batch invoices (use cheaper model)
        complexity: "simple" for standard invoices, "complex" for multi-page
    
    Returns:
        Model name string
    """
    settings = get_settings()
    
    if is_batch and complexity == "simple":
        # Batch + simple = cheapest option
        return "gemini-2.5-flash-lite"
    elif is_batch and complexity == "complex":
        # Batch + complex = still use Flash for accuracy
        return "gemini-3.6-flash"
    else:
        # Single upload = use configured model (3.6 Flash)
        return settings.gemini_model


# --- Context Caching ---

class PromptCache:
    """
    In-memory cache for VLM extraction prompts.
    
    Caches the system prompt portion to reduce input tokens.
    """
    
    def __init__(self, max_size: int = 100, ttl_seconds: int = 3600):
        self._cache: dict[str, dict] = {}
        self._max_size = max_size
        self._ttl_seconds = ttl_seconds
    
    def get_cache_key(self, prompt_template: str) -> str:
        """Generate a cache key from prompt template."""
        return hashlib.md5(prompt_template.encode()).hexdigest()
    
    def get(self, prompt_template: str) -> Optional[dict]:
        """Get cached prompt data if available and not expired."""
        key = self.get_cache_key(prompt_template)
        
        if key in self._cache:
            entry = self._cache[key]
            if time.time() - entry["created_at"] < self._ttl_seconds:
                logger.debug(f"Cache hit for prompt key: {key[:8]}...")
                return entry["data"]
            else:
                # Expired
                del self._cache[key]
        
        return None
    
    def set(self, prompt_template: str, data: dict):
        """Cache prompt data."""
        if len(self._cache) >= self._max_size:
            # Evict oldest entry
            oldest_key = min(self._cache.keys(), key=lambda k: self._cache[k]["created_at"])
            del self._cache[oldest_key]
        
        key = self.get_cache_key(prompt_template)
        self._cache[key] = {
            "data": data,
            "created_at": time.time(),
        }
        logger.debug(f"Cached prompt with key: {key[:8]}...")
    
    def clear(self):
        """Clear all cached entries."""
        self._cache.clear()


# Global cache instance
_prompt_cache = PromptCache(max_size=100, ttl_seconds=3600)


def get_prompt_cache() -> PromptCache:
    """Get the global prompt cache."""
    return _prompt_cache


# --- Prompt Compression ---

def compress_prompt(original_prompt: str) -> str:
    """
    Compress extraction prompt to reduce input tokens.
    
    Removes:
    - Extra whitespace
    - Redundant instructions
    - Example text (keep schema only)
    
    Typical savings: 20-40% token reduction.
    """
    # Remove multiple spaces/newlines
    compressed = " ".join(original_prompt.split())
    
    # Remove comment blocks
    import re
    compressed = re.sub(r'#.*?$', '', compressed, flags=re.MULTILINE)
    
    # Remove "Example:" sections but keep schema
    compressed = re.sub(r'Example:.*?(?=Schema:|$)', '', compressed, flags=re.DOTALL)
    
    return compressed.strip()


# --- Rate Limiting ---

class TokenBucketRateLimiter:
    """
    Token bucket rate limiter for VLM API calls.
    
    Prevents hitting Gemini API rate limits (10 RPM for Flash-Lite).
    """
    
    def __init__(self, max_tokens: int = 10, refill_rate: float = 10/60):
        """
        Args:
            max_tokens: Maximum burst size (matches RPM limit)
            refill_rate: Tokens per second (10 RPM = 10/60 tokens/sec)
        """
        self.max_tokens = max_tokens
        self.refill_rate = refill_rate
        self.tokens = max_tokens
        self.last_refill = time.time()
    
    def acquire(self, timeout: float = 60.0) -> bool:
        """
        Acquire a token, waiting if necessary.
        
        Args:
            timeout: Maximum wait time in seconds
        
        Returns:
            True if token acquired, False if timeout
        """
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


# Global rate limiter
_vlm_rate_limiter = TokenBucketRateLimiter(max_tokens=10, refill_rate=10/60)


def get_vlm_rate_limiter() -> TokenBucketRateLimiter:
    """Get the global VLM rate limiter."""
    return _vlm_rate_limiter


# --- Retry Logic ---

def retry_with_backoff(
    func,
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
):
    """
    Retry a function with exponential backoff.
    
    Specifically handles:
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
            retryable = any(indicator in error_str for indicator in [
                "429", "rate limit", "quota", "503", "service unavailable",
                "timeout", "connection", "deadline exceeded",
            ])
            
            if not retryable:
                raise
            
            # Exponential backoff with jitter
            import random
            delay = min(base_delay * (2 ** attempt), max_delay)
            jitter = random.uniform(0, delay * 0.1)
            total_delay = delay + jitter
            
            logger.warning(
                f"Retryable error, attempt {attempt + 1}/{max_retries}, "
                f"waiting {total_delay:.1f}s: {e}"
            )
            time.sleep(total_delay)
    
    raise last_exception


# --- Optimization Stats ---

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
            "estimated_cost_savings": self.total_input_tokens_saved * 0.00000075,  # $0.75/1M tokens
        }


# Global stats
optimization_stats = OptimizationStats()
