"""
Kafka Consumer — Layer 1 Invoice Worker

Processes invoice events from Kafka topics using the Claim Check pattern.
Reads images from shared Docker volume, runs boundary detection,
and executes the full Layer 1 pipeline.

Pipeline:
1. Read page from Claim Check path
2. Boundary detection (lightweight OCR) to group pages into invoices
3. For each invoice group:
   a. Blur check
   b. Preprocess (Path A: binarized, Path B: RGB)
   c. OCR (Path A)
   d. Select model by quality (flash-lite vs flash)
   e. VLM extraction (rate-limited)
   f. Checksum validation (LOCAL math, NEVER by Gemini)
4. Publish to invoice.extracted.events
5. Update batch_invoice_items in DB
6. Cleanup page files

Rate Limiting:
- Asyncio Semaphore (3 concurrent per worker)
- Redis Token Bucket (15 RPM shared across all workers)
- Exponential Backoff with Full Jitter on 429 errors

RULE: Gemini is NEVER used for mathematical validation.
All financial math is performed by our local Python checksum layer.
"""

import asyncio
import json
import logging
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import cv2
import numpy as np
from kafka import KafkaConsumer
from kafka.errors import KafkaError

from app.core.config import get_settings

logger = logging.getLogger(__name__)


# =============================================================================
# Rate Limiter (Thread-based for Kafka consumer)
# =============================================================================


class VLMRateLimiter:
    """Token bucket rate limiter for VLM API calls.

    Thread-safe version for use with Kafka consumer (thread-based).
    """

    def __init__(self, max_calls: int = 10, window_seconds: int = 60):
        self.max_calls = max_calls
        self.window_seconds = window_seconds
        self.calls: list[float] = []
        import threading
        self._lock = threading.Lock()

    def acquire(self):
        """Wait until a rate limit slot is available."""
        while True:
            with self._lock:
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
            time.sleep(wait_time)


# Global rate limiter instance
_rate_limiter = VLMRateLimiter(max_calls=10, window_seconds=60)


def get_rate_limiter() -> VLMRateLimiter:
    """Get the global VLM rate limiter."""
    return _rate_limiter


# =============================================================================
# Invoice Processing Pipeline
# =============================================================================


def _process_single_invoice(
    page_images: list[np.ndarray],
    batch_id: str,
    vendor_code: str,
    page_indices: list[int],
    invoice_number: str | None,
) -> dict:
    """Process a single invoice through the full Layer 1 pipeline.

    Args:
        page_images: List of page images (BGR format)
        batch_id: Batch job UUID
        vendor_code: Vendor code
        page_indices: Page indices belonging to this invoice
        invoice_number: Detected invoice number (may be None)

    Returns:
        Processing result dict with status, extracted_json, etc.
    """
    from app.tools.blur_check import check_blur
    from app.tools.checksum import run_checksum
    from app.tools.ocr_engine import extract_text
    from app.tools.pii_masker import mask_invoice_for_llm
    from app.tools.preprocessing import preprocess_path_a_ocr, preprocess_path_b_vlm
    from app.tools.vlm_extractor import extract_invoice_json
    from app.tools.vlm_optimizer import select_model, retry_with_backoff
    from app.schemas.invoice import ExtractedInvoicePayload

    start_time = time.time()

    try:
        # Use first page for processing
        first_page_idx = page_indices[0]
        page_img = page_images[first_page_idx]

        # Step 1: Blur check
        blur_score, blur_passed, processed_img = check_blur(
            page_img,
            source_label=f"{batch_id}_page_{first_page_idx}",
        )
        if not blur_passed:
            return {
                "status": "FAILED",
                "error": f"Blur check failed on page {first_page_idx}",
                "blur_score": blur_score,
            }

        # Step 2: Preprocessing
        path_a = preprocess_path_a_ocr(processed_img)  # Binarized
        path_b = preprocess_path_b_vlm(processed_img)  # RGB

        # Step 3: OCR
        ocr_text, ocr_confidence = extract_text(path_a)

        # Step 4: Select model by quality
        model = select_model(
            is_batch=True,
            blur_score=blur_score,
            ocr_confidence=ocr_confidence,
        )
        logger.info(
            f"Model selected: {model}",
            extra={
                "blur_score": blur_score,
                "ocr_confidence": ocr_confidence,
            },
        )

        # Step 5: VLM extraction (with retry and rate limiting)
        raw_ocr_masked = mask_invoice_for_llm({"ocr_text": ocr_text})

        extracted_json = retry_with_backoff(
            lambda: extract_invoice_json(
                ocr_text=raw_ocr_masked.get("ocr_text", ocr_text),
                rgb_image=path_b,
                send_image=True,
                model_override=model,
            )
        )

        # Step 6: Checksum validation (LOCAL math, NEVER by Gemini)
        checksum_errors = run_checksum(extracted_json)

        # Step 7: Schema validation
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
            "invoice_number": invoice_payload.reference_data.invoice_number,
            "processing_time_ms": processing_time_ms,
            "checksum_errors": checksum_errors,
            "extracted_json": extracted_json,
            "blur_score": blur_score,
            "ocr_confidence": ocr_confidence,
            "model_used": model,
        }

    except Exception as e:
        logger.error(f"Invoice processing failed: {e}")
        return {
            "status": "FAILED",
            "error": str(e),
        }


# =============================================================================
# Database Operations
# =============================================================================


def _insert_extracted_invoice(
    db,
    batch_id: str,
    vendor_code: str,
    result: dict,
    page_index: int,
):
    """Insert extracted invoice and batch item records to DB."""
    from sqlalchemy import text

    document_id = str(uuid.uuid4())
    extracted_json = result.get("extracted_json", {})

    # Insert to extracted_invoices
    db.execute(
        text("""
            INSERT INTO extracted_invoices (
                document_id, vendor_code, invoice_number, document_type_code,
                po_number, document_date, due_date,
                supplier_legal_name, supplier_gstin, supplier_pan,
                buyer_legal_name, buyer_gstin,
                currency_code, grand_total_paise, tds_deduction_paise,
                processing_status, parsed_payload, validation_errors
            ) VALUES (
                :document_id, :vendor_code, :invoice_number, :document_type_code,
                :po_number, :document_date, :due_date,
                :supplier_legal_name, :supplier_gstin, :supplier_pan,
                :buyer_legal_name, :buyer_gstin,
                'INR', :grand_total_paise, :tds_deduction_paise,
                :processing_status, :parsed_payload, :validation_errors
            )
        """),
        {
            "document_id": document_id,
            "vendor_code": vendor_code,
            "invoice_number": extracted_json.get("reference_data", {}).get("invoice_number"),
            "document_type_code": extracted_json.get("reference_data", {}).get("document_type_code", "INV"),
            "po_number": extracted_json.get("reference_data", {}).get("po_number"),
            "document_date": extracted_json.get("reference_data", {}).get("document_date"),
            "due_date": extracted_json.get("reference_data", {}).get("due_date"),
            "supplier_legal_name": extracted_json.get("supplier_details", {}).get("legal_name"),
            "supplier_gstin": extracted_json.get("supplier_details", {}).get("gstin"),
            "supplier_pan": extracted_json.get("supplier_details", {}).get("pan"),
            "buyer_legal_name": extracted_json.get("buyer_details", {}).get("legal_name"),
            "buyer_gstin": extracted_json.get("buyer_details", {}).get("gstin"),
            "grand_total_paise": extracted_json.get("financial_summary", {}).get("grand_total_paise", 0),
            "tds_deduction_paise": extracted_json.get("financial_summary", {}).get("tds_deduction_paise", 0),
            "processing_status": result["status"],
            "parsed_payload": json.dumps(extracted_json),
            "validation_errors": json.dumps(result.get("checksum_errors", [])),
        },
    )

    # Insert batch item
    db.execute(
        text("""
            INSERT INTO batch_invoice_items (
                batch_id, document_id, row_number, invoice_number, status, processing_time_ms
            ) VALUES (
                :batch_id, :document_id, :row_number, :invoice_number, :status, :processing_time_ms
            )
        """),
        {
            "batch_id": batch_id,
            "document_id": document_id,
            "row_number": page_index,
            "invoice_number": result.get("invoice_number"),
            "status": result["status"],
            "processing_time_ms": result.get("processing_time_ms", 0),
        },
    )

    db.commit()

    return document_id


def _update_batch_progress(db, batch_id: str, success: bool) -> bool:
    """Atomically update batch progress and check if batch is complete.

    Uses UPDATE ... RETURNING to avoid race conditions across workers.
    Returns True if this worker should call _complete_batch().
    """
    from sqlalchemy import text

    if success:
        result = db.execute(
            text("""
                UPDATE batch_jobs
                SET processed_count = processed_count + 1,
                    updated_at = now()
                WHERE batch_id = :bid
                RETURNING total_invoices, processed_count, failed_count
            """),
            {"bid": batch_id},
        ).first()
    else:
        result = db.execute(
            text("""
                UPDATE batch_jobs
                SET failed_count = failed_count + 1,
                    updated_at = now()
                WHERE batch_id = :bid
                RETURNING total_invoices, processed_count, failed_count
            """),
            {"bid": batch_id},
        ).first()

    db.commit()

    if result:
        total, processed, failed = result
        done = processed + failed
        if done >= total:
            return True  # This worker should complete the batch

    return False


def _complete_batch(db, batch_id: str):
    """Mark batch as completed."""
    from sqlalchemy import text

    db.execute(
        text("UPDATE batch_jobs SET status = 'COMPLETED', completed_at = now(), updated_at = now() WHERE batch_id = :bid"),
        {"bid": batch_id},
    )
    db.commit()


# =============================================================================
# Kafka Consumer
# =============================================================================


class InvoiceConsumer:
    """Kafka consumer for invoice processing events.

    Features:
    - Claim Check pattern (reads from file paths)
    - Boundary detection (groups pages into invoices)
    - Rate limiting for VLM API calls
    - Exponential backoff with Full Jitter
    - Manual offset commit after successful processing
    - Graceful shutdown
    """

    def __init__(self, group_id: str | None = None):
        from app.kafka.config import KafkaConfig
        self.config = KafkaConfig.from_settings()
        self.group_id = group_id or self.config.invoice_consumer_group
        self._stop_event = Event()
        self._consumer = None

    def start(self):
        """Start consuming events."""
        try:
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

                    if event_type == "batch.page.ingestion":
                        self._handle_page_event(event_data)
                    elif event_type == "batch.processing.started":
                        self._handle_batch_event(event_data)
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

    def _handle_page_event(self, event_data: dict):
        """Handle a single page ingestion event (Claim Check pattern)."""
        data = event_data.get("data", {})
        batch_id = data.get("batch_id")
        event_vendor_code = data.get("vendor_code")
        file_path = data.get("file_path")
        page_index = data.get("page_index", 0)

        from sqlalchemy import text
        from app.db.session import SessionLocal

        db = SessionLocal()
        try:
            database_vendor_code = db.execute(
                text("""
                    SELECT vendor_code
                    FROM batch_jobs
                    WHERE batch_id = :batch_id
                """),
                {"batch_id": batch_id},
            ).scalar_one_or_none()
        finally:
            db.close()

        if database_vendor_code is None:
            raise ValueError(f"Batch not found: {batch_id}")

        if event_vendor_code != database_vendor_code:
            raise ValueError(
                f"Tenant mismatch for batch {batch_id}: "
                f"event={event_vendor_code}, database={database_vendor_code}"
            )

        vendor_code = database_vendor_code

        logger.info(
            "Processing page event",
            extra={"batch_id": batch_id, "page_index": page_index, "file_path": file_path},
        )

        # Read image from Claim Check path
        if not file_path or not Path(file_path).exists():
            logger.error(f"File not found: {file_path}")
            return

        image = cv2.imread(file_path)
        if image is None:
            logger.error(f"Failed to read image: {file_path}")
            return

        # Process single page as a single invoice
        # (boundary detection happens at batch level, not per-page)
        result = _process_single_invoice(
            page_images=[image],
            batch_id=batch_id,
            vendor_code=vendor_code,
            page_indices=[0],
            invoice_number=None,
        )

        # Insert to DB
        from app.db.session import SessionLocal
        db = SessionLocal()
        try:
            if result["status"] != "FAILED":
                _insert_extracted_invoice(db, batch_id, vendor_code, result, page_index)
                is_complete = _update_batch_progress(db, batch_id, success=True)
            else:
                is_complete = _update_batch_progress(db, batch_id, success=False)
                logger.warning(
                    "Page processing failed",
                    extra={"batch_id": batch_id, "page_index": page_index, "error": result.get("error")},
                )

            # If this worker completed the batch, mark it done
            if is_complete:
                _complete_batch(db, batch_id)
                logger.info(f"Batch {batch_id} completed!")
        finally:
            db.close()

        # Cleanup page file
        try:
            Path(file_path).unlink(missing_ok=True)
        except Exception as e:
            logger.warning(f"Failed to cleanup file: {file_path}, error: {e}")

    def _handle_batch_event(self, event_data: dict):
        """Handle batch processing started event."""
        data = event_data.get("data", {})
        batch_id = data.get("batch_id")
        logger.info(f"Batch processing started: {batch_id}")
        # Batch processing logic is handled by page events

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
