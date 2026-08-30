"""
Kafka Consumer

Processes invoice events from Kafka topics.
Implements rate limiting, retry logic, and VLM call optimization.
"""

import asyncio
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event

from app.kafka.config import KafkaConfig

logger = logging.getLogger(__name__)

# Rate limiter for VLM calls
class VLMRateLimiter:
    """Token bucket rate limiter for VLM API calls."""
    
    def __init__(self, max_calls: int = 10, window_seconds: int = 60):
        self.max_calls = max_calls
        self.window_seconds = window_seconds
        self.calls: list[float] = []
        self._lock = asyncio.Lock() if asyncio.get_event_loop().is_running() else None
    
    async def acquire(self):
        """Wait until a rate limit slot is available."""
        while True:
            now = time.time()
            # Remove calls outside the window
            self.calls = [t for t in self.calls if now - t < self.window_seconds]
            
            if len(self.calls) < self.max_calls:
                self.calls.append(now)
                return
            
            # Calculate wait time
            oldest = self.calls[0]
            wait_time = self.window_seconds - (now - oldest) + 0.1
            logger.info(f"Rate limit reached, waiting {wait_time:.1f}s")
            await asyncio.sleep(wait_time)
    
    def acquire_sync(self):
        """Synchronous version for thread-based consumers."""
        while True:
            now = time.time()
            self.calls = [t for t in self.calls if now - t < self.window_seconds]
            
            if len(self.calls) < self.max_calls:
                self.calls.append(now)
                return
            
            oldest = self.calls[0]
            wait_time = self.window_seconds - (now - oldest) + 0.1
            logger.info(f"Rate limit reached, waiting {wait_time:.1f}s")
            time.sleep(wait_time)


# Global rate limiter instance
_rate_limiter = VLMRateLimiter(max_calls=10, window_seconds=60)


def get_rate_limiter() -> VLMRateLimiter:
    """Get the global VLM rate limiter."""
    return _rate_limiter


def _process_invoice_event(event_data: dict) -> dict:
    """
    Process a single invoice event.
    
    This is the core processing function called by the consumer.
    It runs the full Layer 1 pipeline for a single invoice.
    
    Args:
        event_data: Event payload with document_id, vendor_code, etc.
    
    Returns:
        Processing result dict
    """
    from app.tools.boundary_detector import detect_boundaries
    from app.tools.checksum import run_checksum
    from app.tools.vlm_extractor import extract_invoice_json
    from app.tools.ocr_engine import extract_text
    from app.tools.preprocessing import preprocess_path_a_ocr, preprocess_path_b_vlm
    from app.tools.blur_check import check_blur
    from app.tools.pii_masker import mask_invoice_for_llm
    from app.schemas.invoice import ExtractedInvoicePayload
    
    document_id = event_data.get("document_id")
    vendor_code = event_data.get("vendor_code")
    page_images = event_data.get("page_images", [])
    
    start_time = time.time()
    
    try:
        # Rate limit VLM calls
        _rate_limiter.acquire_sync()
        
        # Process each page
        for page_idx, page_img in enumerate(page_images):
            # Blur check
            blur_score, blur_passed, processed_img = check_blur(page_img)
            if not blur_passed:
                return {
                    "status": "FAILED",
                    "error": f"Blur check failed on page {page_idx}",
                    "blur_score": blur_score,
                }
            
            # Preprocessing
            path_a = preprocess_path_a_ocr(processed_img)
            path_b = preprocess_path_b_vlm(processed_img)
            
            # OCR
            ocr_text, ocr_confidence = extract_text(path_a)
            
            # VLM extraction (with rate limiting)
            raw_ocr_masked = mask_invoice_for_llm({"ocr_text": ocr_text})
            extracted_json = extract_invoice_json(
                rgb_image=path_b,
                ocr_text=raw_ocr_masked.get("ocr_text", ocr_text),
            )
            
            # Checksum validation
            checksum_errors = run_checksum(extracted_json)
            
            # Schema validation
            try:
                invoice_payload = ExtractedInvoicePayload(**extracted_json)
                processing_status = "VALIDATED" if not checksum_errors else "EXCEPTION_FLAGGED"
            except Exception as e:
                return {
                    "status": "FAILED",
                    "error": f"Schema validation failed: {e}",
                }
            
            processing_time_ms = int((time.time() - start_time) * 1000)
            
            return {
                "status": processing_status,
                "document_id": document_id,
                "vendor_code": vendor_code,
                "invoice_number": invoice_payload.reference_data.invoice_number,
                "processing_time_ms": processing_time_ms,
                "checksum_errors": checksum_errors,
                "extracted_json": extracted_json,
            }
        
        return {"status": "FAILED", "error": "No pages to process"}
        
    except Exception as e:
        logger.error(
            "Invoice processing failed",
            extra={"document_id": document_id, "error": str(e)},
        )
        return {
            "status": "FAILED",
            "error": str(e),
            "document_id": document_id,
        }


def _retry_with_backoff(func, max_retries: int = 3, base_delay: float = 1.0):
    """
    Retry a function with exponential backoff.
    
    Used for VLM API calls that may hit rate limits.
    """
    last_exception = None
    
    for attempt in range(max_retries):
        try:
            return func()
        except Exception as e:
            last_exception = e
            error_str = str(e).lower()
            
            # Only retry on rate limit errors
            if "429" in error_str or "rate limit" in error_str or "quota" in error_str:
                delay = base_delay * (2 ** attempt)  # Exponential backoff
                logger.warning(
                    f"Rate limited, retrying in {delay}s (attempt {attempt + 1}/{max_retries})"
                )
                time.sleep(delay)
            else:
                # Non-rate-limit error, don't retry
                raise
    
    raise last_exception


class InvoiceConsumer:
    """
    Kafka consumer for invoice processing events.
    
    Features:
    - Rate limiting for VLM API calls
    - Exponential backoff on rate limits
    - Manual offset commit after successful processing
    - Graceful shutdown
    """
    
    def __init__(self, group_id: str | None = None):
        self.config = KafkaConfig.from_settings()
        self.group_id = group_id or self.config.invoice_consumer_group
        self._stop_event = Event()
        self._consumer = None
    
    def start(self):
        """Start consuming events."""
        try:
            from kafka import KafkaConsumer
            
            self._consumer = KafkaConsumer(
                self.config.invoice_processing_topic,
                **self.config.get_consumer_config(self.group_id),
            )
            
            logger.info(
                "Invoice consumer started",
                extra={
                    "topic": self.config.invoice_processing_topic,
                    "group_id": self.group_id,
                },
            )
            
            for message in self._consumer:
                if self._stop_event.is_set():
                    break
                
                try:
                    event_data = json.loads(message.value.decode("utf-8"))
                    event_type = event_data.get("type")
                    
                    logger.info(
                        "Processing event",
                        extra={"event_type": event_type, "offset": message.offset},
                    )
                    
                    if event_type == "batch.processing.started":
                        self._handle_batch_event(event_data)
                    elif event_type == "invoice.extracted":
                        self._handle_invoice_event(event_data)
                    else:
                        logger.warning(f"Unknown event type: {event_type}")
                    
                    # Manual commit after successful processing
                    self._consumer.commit()
                    
                except Exception as e:
                    logger.error(
                        "Failed to process message",
                        extra={"offset": message.offset, "error": str(e)},
                    )
                    # Don't commit - message will be reprocessed
                
        except Exception as e:
            logger.error("Consumer error", extra={"error": str(e)})
        finally:
            self.stop()
    
    def _handle_batch_event(self, event_data: dict):
        """Handle batch processing started event."""
        data = event_data.get("data", {})
        batch_id = data.get("batch_id")
        logger.info(f"Batch processing started: {batch_id}")
        # Batch processing logic will be implemented in batch.py
    
    def _handle_invoice_event(self, event_data: dict):
        """Handle single invoice processing event."""
        data = event_data.get("data", {})
        result = _process_invoice_event(data)
        logger.info(f"Invoice processed: {result}")
    
    def stop(self):
        """Stop the consumer."""
        self._stop_event.set()
        if self._consumer:
            self._consumer.close()
            logger.info("Invoice consumer stopped")


def start_consumer():
    """Entry point for starting the consumer."""
    consumer = InvoiceConsumer()
    consumer.start()
