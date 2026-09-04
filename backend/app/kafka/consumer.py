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
   e. VLM extraction (rate-limited via Redis token bucket)
   f. Checksum validation (LOCAL math, NEVER by Gemini)
4. Fan-In: publish to invoice.extracted.events for Layer 2
5. Update batch_invoice_items in DB
6. Cleanup page files

Rate Limiting (3-Pillar Architecture):
- Threading Semaphore (layer1_max_concurrent per worker)
- Redis Token Bucket (GEMINI_RPM_LIMIT shared across ALL workers)
- Exponential Backoff with Full Jitter on 429 errors

Poison Message Handling:
- Terminal errors (duplicate invoice, tenant mismatch, missing file)
  are published to reconciliation.dlq.events and the offset is committed
  to prevent infinite redelivery loops.

RULE: Gemini is NEVER used for mathematical validation.
All financial math is performed by our local Python checksum layer.
"""

import json
import logging
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from threading import Event

import cv2
import numpy as np
from kafka import KafkaConsumer
from sqlalchemy.exc import IntegrityError

from app.core.config import get_settings
from app.kafka.producer import publish_invoice_event

logger = logging.getLogger(__name__)

# Max batch entries held in the boundary-detection cache per worker.
# Evicted FIFO beyond this to bound memory growth.
MAX_BATCH_CACHE_SIZE = 25


# =============================================================================
# Terminal Processing Error (Poison Message)
# =============================================================================


class TerminalProcessingError(Exception):
    """A message-level error that can never succeed on retry.

    These are published to the DLQ and the offset is committed to
    prevent infinite redelivery loops.
    """

    def __init__(self, error_type: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.error_type = error_type
        self.details = details or {}


# =============================================================================
# Rate-Limited VLM Client (Redis token bucket driven by config)
# =============================================================================

_vlm_client = None


def get_vlm_client():
    """Get the global rate-limited Gemini client.

    Driven by environment config:
        - layer1_max_concurrent -> per-worker semaphore
        - gemini_rpm_limit      -> distributed Redis token bucket
    """
    global _vlm_client
    if _vlm_client is None:
        from app.tools.vlm_optimizer import RateLimitedGeminiClient

        settings = get_settings()
        _vlm_client = RateLimitedGeminiClient(
            max_concurrent=settings.layer1_max_concurrent,
            rpm=settings.gemini_rpm_limit,
            redis_url=settings.redis_url,
        )
        _vlm_client.initialize()
    return _vlm_client


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
        page_images: List of all page images (BGR format) of the batch,
                     indexed by page position
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
    from app.tools.vlm_optimizer import select_model
    from app.schemas.invoice import ExtractedInvoicePayload

    start_time = time.time()

    try:
        # Use first page of the group for processing
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

        # Step 5: PII mask OCR text, then VLM extraction
        # (rate-limited via Redis token bucket + semaphore + Full Jitter)
        raw_ocr_masked = mask_invoice_for_llm({"ocr_text": ocr_text})

        extracted_json = get_vlm_client().call(
            extract_invoice_json,
            ocr_text=raw_ocr_masked.get("ocr_text", ocr_text),
            rgb_image=path_b,
            send_image=True,
            model_override=model,
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


def _cleanup_page_file(file_path: str):
    """Delete a page file from the shared volume after processing."""
    try:
        Path(file_path).unlink(missing_ok=True)
    except Exception as e:
        logger.warning(f"Failed to cleanup file: {file_path}, error: {e}")


# =============================================================================
# Kafka Consumer
# =============================================================================


class InvoiceConsumer:
    """Kafka consumer for invoice processing events.

    Features:
    - Claim Check pattern (reads from file paths)
    - Boundary detection (groups pages into invoices, cached per batch)
    - Distributed Redis rate limiting for VLM API calls
    - Exponential backoff with Full Jitter
    - Fan-In publish to invoice.extracted.events for Layer 2
    - DLQ for poison messages (terminal errors commit the offset)
    - Manual offset commit after successful processing
    - Graceful shutdown
    """

    def __init__(self, group_id: str | None = None):
        from app.kafka.config import KafkaConfig
        self.config = KafkaConfig.from_settings()
        self.group_id = group_id or self.config.invoice_consumer_group
        self._stop_event = Event()
        self._consumer = None
        # batch_id -> {"groups": [InvoiceGroup], "images": [np.ndarray]}
        self._batch_cache: dict[str, dict] = {}

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

                event_data = None
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

                except TerminalProcessingError as e:
                    # Poison message: publish to DLQ, record failure, commit
                    # offset to prevent infinite redelivery.
                    try:
                        self._publish_to_dlq(event_data, e)
                        self._mark_event_failed(event_data)
                    except Exception as dlq_e:
                        logger.error(
                            f"DLQ handling failed for offset {message.offset}: {dlq_e}"
                        )
                    else:
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

        # --- Tenant verification (event vendor vs DB vendor) ---
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

            # Redelivery guard: a page already recorded skips re-run, double-insert,
            # and double-count (cleaned page files become silent skips, not DLQ poisons).
            already = db.execute(
                text("""
                    SELECT 1 FROM batch_invoice_items
                    WHERE batch_id = :bid AND row_number = :rn
                    LIMIT 1
                """),
                {"bid": batch_id, "rn": page_index},
            ).first()
        finally:
            db.close()

        if already is not None:
            logger.info(
                "Redelivered page event already processed; skipping",
                extra={"batch_id": batch_id, "page_index": page_index},
            )
            _cleanup_page_file(file_path)
            return

        if database_vendor_code is None:
            raise TerminalProcessingError(
                "batch_not_found",
                f"Batch not found: {batch_id}",
                {"batch_id": batch_id, "page_index": page_index},
            )

        if event_vendor_code != database_vendor_code:
            raise TerminalProcessingError(
                "tenant_mismatch",
                f"Tenant mismatch for batch {batch_id}: "
                f"event={event_vendor_code}, database={database_vendor_code}",
                {"batch_id": batch_id, "page_index": page_index},
            )

        vendor_code = database_vendor_code

        logger.info(
            "Processing page event",
            extra={"batch_id": batch_id, "page_index": page_index, "file_path": file_path},
        )

        # --- Claim Check: verify the page file exists ---
        if not file_path or not Path(file_path).exists():
            raise TerminalProcessingError(
                "file_not_found",
                f"File not found: {file_path}",
                {"batch_id": batch_id, "page_index": page_index, "file_path": file_path},
            )

        if cv2.imread(file_path) is None:
            raise TerminalProcessingError(
                "image_decode_failed",
                f"Failed to read image: {file_path}",
                {"batch_id": batch_id, "page_index": page_index, "file_path": file_path},
            )

        # --- Boundary detection: group pages of the same invoice ---
        batch_pages = self._get_batch_pages(batch_id)
        groups = batch_pages["groups"]
        group = next(
            (g for g in groups if page_index in g.page_indices),
            None,
        )

        if group is None or page_index != group.page_indices[0]:
            # Continuation page of a multi-page invoice: extraction is owned
            # by the first page's event. Mark progress and clean up.
            logger.info(
                "Page is continuation of a grouped invoice; skipping extraction",
                extra={"batch_id": batch_id, "page_index": page_index},
            )
            db = SessionLocal()
            try:
                is_complete = _update_batch_progress(db, batch_id, success=True)
                if is_complete:
                    _complete_batch(db, batch_id)
                    self._evict_batch_cache(batch_id)
                    logger.info(f"Batch {batch_id} completed!")
            finally:
                db.close()
            _cleanup_page_file(file_path)
            return

        # --- Process the invoice group (owned by its first page event) ---
        result = _process_single_invoice(
            page_images=batch_pages["images"],
            batch_id=batch_id,
            vendor_code=vendor_code,
            page_indices=group.page_indices,
            invoice_number=group.invoice_number,
        )

        db = SessionLocal()
        try:
            if result["status"] != "FAILED":
                try:
                    document_id = _insert_extracted_invoice(
                        db, batch_id, vendor_code, result, page_index
                    )
                except IntegrityError as e:
                    if "unique" in str(e).lower():
                        raise TerminalProcessingError(
                            "duplicate_invoice",
                            f"Duplicate invoice for batch {batch_id} "
                            f"(page {page_index}): {e}",
                            {
                                "batch_id": batch_id,
                                "page_index": page_index,
                                "file_path": file_path,
                            },
                        ) from e
                    raise

                # Fan-In: publish the extracted JSON payload to
                # invoice.extracted.events for Layer 2 (LangGraph).
                try:
                    publish_invoice_event(
                        document_id=document_id,
                        vendor_code=vendor_code,
                        invoice_number=result.get("invoice_number"),
                        processing_status=result["status"],
                        extracted_json=result.get("extracted_json"),
                        batch_id=batch_id,
                    )
                except Exception as pub_e:
                    logger.error(
                        f"Fan-in publish failed for document {document_id}: {pub_e}"
                    )

                is_complete = _update_batch_progress(db, batch_id, success=True)
            else:
                # P2: a permanently failed extraction is an operational event -
                # record it (idempotently) so batch counts + dashboard stay honest.
                from sqlalchemy import text
                insert_res = db.execute(
                    text("""
                        INSERT INTO batch_invoice_items (
                            batch_id, document_id, row_number, invoice_number,
                            status, error_message, processing_time_ms
                        )
                        SELECT :bid, NULL, :row_number, NULL, 'FAILED',
                               LEFT(:error, 500), :pt_ms
                        WHERE NOT EXISTS (
                            SELECT 1 FROM batch_invoice_items
                            WHERE batch_id = :bid AND row_number = :row_number
                              AND status = 'FAILED'
                        )
                    """),
                    {
                        "bid": batch_id,
                        "row_number": page_index,
                        "error": result.get("error") or "Unknown extraction failure",
                        "pt_ms": result.get("processing_time_ms", 0),
                    },
                )
                # Only advance the failed counter if this delivery wrote the row -
                # a redelivered failure must not double-count (fixes 53 > 50 drift).
                is_complete = False
                if insert_res.rowcount:
                    is_complete = _update_batch_progress(db, batch_id, success=False)
                logger.warning(
                    "Page processing failed",
                    extra={
                        "batch_id": batch_id,
                        "page_index": page_index,
                        "error": result.get("error"),
                    },
                )

            # If this worker completed the batch, mark it done
            if is_complete:
                _complete_batch(db, batch_id)
                self._evict_batch_cache(batch_id)
                logger.info(f"Batch {batch_id} completed!")
        finally:
            db.close()

        # Cleanup page file
        _cleanup_page_file(file_path)

    def _handle_batch_event(self, event_data: dict):
        """Handle batch processing started event."""
        data = event_data.get("data", {})
        batch_id = data.get("batch_id")
        logger.info(f"Batch processing started: {batch_id}")
        # Batch processing logic is handled by page events

    # --- Boundary detection helpers (cached per batch) ---

    def _get_batch_pages(self, batch_id: str) -> dict:
        """Load all page images of a batch and detect invoice boundaries.

        Result is cached per batch: boundary detection (lightweight OCR
        over every page) runs once, subsequent page events reuse the
        grouping. All page files exist before any event is published
        (single atomic TX at upload time), so the cache is never stale.

        Returns:
            {"groups": [InvoiceGroup], "images": [np.ndarray]}
        """
        if batch_id in self._batch_cache:
            return self._batch_cache[batch_id]

        settings = get_settings()
        batch_dir = Path(settings.batch_storage_path) / batch_id

        if not batch_dir.exists():
            raise TerminalProcessingError(
                "batch_pages_missing",
                f"Batch directory not found: {batch_dir}",
                {"batch_id": batch_id},
            )

        page_files = sorted(
            batch_dir.glob("page_*.jpg"),
            key=lambda p: int(p.stem.split("_")[1]),
        )

        images = []
        for pf in page_files:
            img = cv2.imread(str(pf))
            if img is None:
                logger.warning(
                    f"Failed to load page image for boundary detection: {pf}"
                )
                continue
            images.append(img)

        if not images:
            raise TerminalProcessingError(
                "no_pages_found",
                f"No readable page images for batch {batch_id}",
                {"batch_id": batch_id},
            )

        from app.tools.boundary_detector import detect_boundaries
        groups = detect_boundaries(images)

        entry = {"groups": groups, "images": images}
        self._batch_cache[batch_id] = entry

        # Bound cache growth: evict oldest batch FIFO
        if len(self._batch_cache) > MAX_BATCH_CACHE_SIZE:
            oldest_batch_id = next(iter(self._batch_cache))
            self._batch_cache.pop(oldest_batch_id, None)
            logger.info(
                "Boundary cache evicted oldest batch",
                extra={"batch_id": oldest_batch_id},
            )

        return entry

    def _evict_batch_cache(self, batch_id: str):
        """Drop cached pages/groups once a batch completes."""
        self._batch_cache.pop(batch_id, None)

    # --- DLQ (poison message) handling ---

    def _publish_to_dlq(self, event_data: dict | None, error: TerminalProcessingError):
        """Publish a poison message to reconciliation.dlq.events.

        Payload carries metadata tags (source, error_type) so the
        Exception Dashboard can filter by origin.
        """
        from app.kafka.producer import get_producer

        event_data = event_data or {}
        data = event_data.get("data", {})
        dlq_event_id = f"dlq_{uuid.uuid4()}"

        dlq_payload = {
            "specversion": "1.0",
            "type": "invoice.processing.dlq",
            "source": "layer1_extractor",
            "id": dlq_event_id,
            "time": datetime.now(timezone.utc).isoformat(),
            "data": {
                "original_event": event_data,
                "error_type": error.error_type,
                "error": str(error),
                "details": error.details,
                "batch_id": data.get("batch_id"),
                "page_index": data.get("page_index"),
            },
            "metadata": {
                "source": "layer1_extractor",
                "error": error.error_type,
            },
        }

        producer = get_producer()
        future = producer.send(
            topic=self.config.reconciliation_dlq_topic,
            key=error.error_type.encode("utf-8"),
            value=json.dumps(dlq_payload).encode("utf-8"),
        )
        future.get(timeout=10)

        logger.error(
            "Poison message published to DLQ",
            extra={
                "dlq_event_id": dlq_event_id,
                "topic": self.config.reconciliation_dlq_topic,
                "error_type": error.error_type,
                "batch_id": data.get("batch_id"),
                "page_index": data.get("page_index"),
            },
        )

    def _mark_event_failed(self, event_data: dict | None):
        """Record a DLQ'd event as a batch failure.

        Ensures batch_jobs progress reaches total_invoices even when
        a page event is terminal, so the batch does not stay PENDING.
        """
        data = (event_data or {}).get("data", {})
        batch_id = data.get("batch_id")
        if not batch_id:
            return

        from sqlalchemy import text
        from app.db.session import SessionLocal

        db = SessionLocal()
        try:
            # Only the first delivery of a poison event may advance the
            # failed counter (kills the 52+35 > 50 drift on redelivery).
            already = db.execute(
                text("""
                    SELECT 1 FROM batch_invoice_items
                    WHERE batch_id = :bid AND row_number = :rn
                    LIMIT 1
                """),
                {"bid": batch_id, "rn": data.get("page_index")},
            ).first()
            if already is not None:
                return
            is_complete = _update_batch_progress(db, batch_id, success=False)
            if is_complete:
                _complete_batch(db, batch_id)
                self._evict_batch_cache(batch_id)
                logger.info(f"Batch {batch_id} completed (after DLQ failure).")
        finally:
            db.close()

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