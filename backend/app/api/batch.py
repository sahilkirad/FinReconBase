"""
Batch Invoice Upload API — Kafka Fan-Out + Claim Check Pattern

Endpoints:
- POST /invoices/batch: Upload PDF or image (JPEG/PNG) for batch processing
- GET /invoices/batch/{batch_id}: Get batch status
- GET /invoices/batch/{batch_id}/invoices: List invoices in batch

Architecture:
1. Guardrail (MIME/Size + ONNX classification + anchor scan) runs pre-flight
2. API saves individual pages to shared Docker volume (/app/data/batch_files/)
3. API publishes one Kafka event per page (file path only, NOT bytes)
4. API returns 202 ACCEPTED immediately (non-blocking)
5. Workers consume events, run boundary detection + full pipeline

RULE: No OCR in API thread — it's a blocking anti-pattern.
All heavy processing is offloaded to Kafka workers.
"""

import json
import logging
import tempfile
import time
import uuid
from pathlib import Path
from uuid import UUID

import cv2
import fitz  # PyMuPDF
import numpy as np
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.security import get_current_user_context
from app.db.session import get_db
from app.tools.guardrail import create_guardrail
from app.schemas.batch import (
    BatchErrorResponse,
    BatchInvoiceItemResponse,
    BatchInvoicesResponse,
    BatchStatusResponse,
    BatchUploadResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/invoices/batch", tags=["batch"])


# =============================================================================
# Batch ID validation
# =============================================================================


def _validate_batch_id(batch_id: str) -> None:
    """Return 404 for non-UUID batch IDs instead of a DB 500 error.

    batch_jobs.batch_id is a UUID column; passing arbitrary strings (e.g.
    a vendor code) would raise InvalidTextRepresentation from Postgres.
    """
    try:
        UUID(batch_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Batch {batch_id} not found",
        )


# =============================================================================
# Page Splitting (Fan-Out) — No OCR, No Heavy Processing
# =============================================================================


def _split_pdf_to_pages(
    pdf_bytes: bytes,
    document_id: str,
    storage_path: str,
) -> list[dict]:
    """Split PDF into individual page JPEG files.

    This is the ONLY work the API does for PDF batches.
    No OCR, no boundary detection — just render and save.

    Args:
        pdf_bytes: Raw PDF file bytes
        document_id: UUID for this batch (used as directory name)
        storage_path: Base storage directory (shared Docker volume)

    Returns:
        List of dicts: [{"page_index": 0, "file_path": "...", "file_size": 12345}, ...]
    """
    batch_dir = Path(storage_path) / document_id
    batch_dir.mkdir(parents=True, exist_ok=True)

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    pages = []

    for page_idx, page in enumerate(doc):
        # Render at 200 DPI (worker will rescale to 300 if needed)
        mat = fitz.Matrix(200 / 72, 200 / 72)
        pix = page.get_pixmap(matrix=mat)
        img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
            pix.h, pix.w, pix.n
        )

        # Convert to BGR for OpenCV
        if pix.n == 4:
            img = cv2.cvtColor(img, cv2.COLOR_RGBA2BGR)
        elif pix.n == 3:
            img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)

        # Save as JPEG (smaller than PNG, ~33% smaller than base64)
        file_path = batch_dir / f"page_{page_idx}.jpg"
        cv2.imwrite(str(file_path), img, [cv2.IMWRITE_JPEG_QUALITY, 95])

        file_size = file_path.stat().st_size
        pages.append({
            "page_index": page_idx,
            "file_path": str(file_path),
            "file_size": file_size,
        })

    doc.close()

    logger.info(
        "PDF split into pages",
        document_id=document_id,
        page_count=len(pages),
        storage_path=str(batch_dir),
    )

    return pages


def _save_single_image(
    image_bytes: bytes,
    document_id: str,
    filename: str,
    storage_path: str,
) -> list[dict]:
    """Save single image (JPEG/PNG) to shared volume.

    Args:
        image_bytes: Raw image file bytes
        document_id: UUID for this batch
        filename: Original filename (for error messages)
        storage_path: Base storage directory

    Returns:
        List with single dict: page_index=0, file_path, file_size
    """
    batch_dir = Path(storage_path) / document_id
    batch_dir.mkdir(parents=True, exist_ok=True)

    nparr = np.frombuffer(image_bytes, np.uint8)
    img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError(f"Cannot decode image: {filename}")

    file_path = batch_dir / f"page_0.jpg"
    cv2.imwrite(str(file_path), img, [cv2.IMWRITE_JPEG_QUALITY, 95])

    file_size = file_path.stat().st_size
    return [{
        "page_index": 0,
        "file_path": str(file_path),
        "file_size": file_size,
    }]


# =============================================================================
# API Endpoints
# =============================================================================


@router.post(
    "",
    response_model=BatchUploadResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        413: {"model": BatchErrorResponse},
        422: {"model": BatchErrorResponse},
    },
)
async def upload_batch(
    file: UploadFile = File(..., description="PDF or image (JPEG/PNG) file with invoices"),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    user_context: dict = Depends(get_current_user_context),
):
    """Upload a batch of invoices for async Kafka processing.

    This endpoint is FAST and NON-BLOCKING:
    1. Validates file type and size (CSV is rejected)
    2. Runs the document guardrail (MIME/Size + ONNX classification + anchors)
    3. Saves individual pages to shared Docker volume
    4. Publishes Kafka events (file paths only)
    5. Returns 202 ACCEPTED immediately

    No OCR, no boundary detection, no VLM calls in the API thread.
    """
    start_time = time.time()
    vendor_code = str(user_context["vendor_code"])

    # Read file content
    content = await file.read()
    file_size = len(content)
    filename = file.filename or "unknown"
    suffix = Path(filename).suffix.lower()

    # Validate file type (CSV support removed — PDF and images only)
    if suffix not in settings.allowed_batch_extensions:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error_code": "UNSUPPORTED_FILE_TYPE",
                "message": f"File type '{suffix}' not supported. "
                           f"Accepted types: {', '.join(settings.allowed_batch_extensions)}. "
                           "CSV uploads are not supported.",
            },
        )

    # Validate file size (max 100MB for batch)
    max_batch_bytes = settings.max_batch_size_mb * 1024 * 1024
    if file_size > max_batch_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail={
                "error_code": "FILE_TOO_LARGE",
                "message": f"Batch file size {file_size} exceeds max {settings.max_batch_size_mb}MB",
            },
        )

    # Validate vendor exists
    vendor_check = db.execute(
        text("SELECT 1 FROM vendor_users WHERE vendor_code = :vc"),
        {"vc": vendor_code},
    ).first()
    if vendor_check is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Vendor '{vendor_code}' is not onboarded.",
        )

    # --- Guardrail: pre-flight gate BEFORE the Fan-Out split ---
    # Non-invoice documents fail fast here instead of consuming Kafka
    # partitions and worker cycles.
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(content)
        tmp_path = Path(tmp.name)
    try:
        guardrail = create_guardrail()
        guardrail.run_guardrail(
            tmp_path,
            file_size,
            file.content_type or "application/octet-stream",
            max_size_mb=settings.max_batch_size_mb,
        )
    finally:
        tmp_path.unlink(missing_ok=True)

    logger.info(
        "BATCH_GUARDRAIL_PASSED",
        extra={"vendor_code": vendor_code, "filename": filename, "suffix": suffix, "file_size": file_size},
    )

    # Create batch ID and determine source type
    batch_id = str(uuid.uuid4())
    # NOTE: batch_jobs.source_type has a CHECK constraint allowing only
    # 'pdf' | 'csv'. CSV support is removed, so image uploads are also
    # recorded as 'pdf' (schema is immutable per Layer 1 constraints).
    source_type = "pdf"

    # --- Step 1: Split and save pages (Claim Check) ---
    try:
        if suffix == ".pdf":
            pages = _split_pdf_to_pages(
                pdf_bytes=content,
                document_id=batch_id,
                storage_path=settings.batch_storage_path,
            )
        else:
            pages = _save_single_image(
                image_bytes=content,
                document_id=batch_id,
                filename=filename,
                storage_path=settings.batch_storage_path,
            )
    except Exception as e:
        logger.error(f"Document split/save failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error_code": "DOCUMENT_SPLIT_FAILED",
                "message": f"Failed to process document: {e}",
            },
        )

    total_invoices = len(pages)

    # Step 2: atomic TX — write batch_jobs + outbox_events together, then publish to Kafka.
    # (get_db() session is autocommit=False, so a transaction is already active.)
    try:
        # Insert batch_jobs record
        db.execute(
            text("""
                INSERT INTO batch_jobs (batch_id, vendor_code, source_type, filename, total_invoices, status)
                VALUES (:batch_id, :vendor_code, :source_type, :filename, :total_invoices, 'PENDING')
            """),
            {
                "batch_id": batch_id,
                "vendor_code": vendor_code,
                "source_type": source_type,
                "filename": filename,
                "total_invoices": total_invoices,
            },
        )

        # Insert outbox_events — one per page (Claim Check pattern)
        # A separate outbox poller will publish these to Kafka
        for page in pages:
            event_id = f"evt_outbox_{uuid.uuid4()}"
            outbox_payload = {
                "specversion": "1.0",
                "type": "batch.page.ingestion",
                "source": "/layer1/batch",
                "id": event_id,
                "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "data": {
                    "batch_id": batch_id,
                    "vendor_code": vendor_code,
                    "source_type": source_type,
                    "page_index": page["page_index"],
                    "file_path": page["file_path"],  # Claim Check!
                    "file_size": page["file_size"],
                },
            }

            db.execute(
                text("""
                    INSERT INTO outbox_events (
                        event_id, aggregate_type, aggregate_id,
                        topic, partition_key, event_type,
                        payload, status
                    ) VALUES (
                        :event_id, 'BatchJob', :aggregate_id,
                        :topic, :partition_key, 'BatchPageIngestion',
                        :payload, 'PENDING'
                    )
                """),
                {
                    "event_id": event_id,
                    "aggregate_id": batch_id,
                    "topic": settings.raw_ingestion_topic,
                    # Unique key per page so events spread across partitions (identical keys pin to one worker).
                    "partition_key": f"{batch_id}:{page['page_index']}",
                    "payload": json.dumps(outbox_payload),
                },
            )

        # Commit atomic transaction
        db.commit()

        logger.info(
            f"Batch + outbox committed atomically: {batch_id}, {len(pages)} events"
        )

    except Exception as e:
        db.rollback()
        logger.error(f"Failed to insert batch + outbox: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "error_code": "OUTBOX_WRITE_FAILED",
                "message": f"Failed to persist batch: {e}",
            },
        )

    processing_time_ms = int((time.time() - start_time) * 1000)

    logger.info(
        "Batch upload completed",
        batch_id=batch_id,
        vendor_code=vendor_code,
        total_invoices=total_invoices,
        processing_time_ms=processing_time_ms,
    )

    return BatchUploadResponse(
        batch_id=batch_id,
        vendor_code=vendor_code,
        source_type=source_type,
        filename=filename,
        total_invoices=total_invoices,
        valid_invoices=total_invoices,
        invalid_invoices=0,
        status="PENDING",
        message=f"Batch of {total_invoices} pages queued for parallel extraction. Processing time: {processing_time_ms}ms",
    )


@router.get("/{batch_id}", response_model=BatchStatusResponse)
async def get_batch_status(
    batch_id: str,
    db: Session = Depends(get_db),
    user_context: dict = Depends(get_current_user_context),
):
    """Get the status of a batch processing job."""

    _validate_batch_id(batch_id)

    result = db.execute(
        text("""
            SELECT batch_id, vendor_code, source_type, filename,
                   total_invoices, processed_count, failed_count,
                   status, created_at, updated_at, completed_at
            FROM batch_jobs
            WHERE batch_id = :bid
            AND vendor_code = :vendor_code
        """),
        {
        "bid": batch_id,
        "vendor_code": str(user_context["vendor_code"]),
        }
    ).first()

    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Batch {batch_id} not found",
        )

    total = result[4] or 0
    processed = result[5] or 0
    progress = (processed / total * 100) if total > 0 else 0

    return BatchStatusResponse(
        batch_id=result[0],
        vendor_code=result[1],
        source_type=result[2],
        filename=result[3],
        total_invoices=total,
        processed_count=processed,
        failed_count=result[6] or 0,
        status=result[7],
        created_at=result[8],
        updated_at=result[9],
        completed_at=result[10],
        progress_percent=round(progress, 1),
    )


@router.get("/{batch_id}/invoices", response_model=BatchInvoicesResponse)
async def get_batch_invoices(
    batch_id: str,
    db: Session = Depends(get_db),
    user_context: dict = Depends(get_current_user_context),
):
    """List all invoices in a batch with their processing status."""

    _validate_batch_id(batch_id)

    # Verify batch exists
    batch = db.execute(
    text("""
        SELECT 1
        FROM batch_jobs
        WHERE batch_id = :bid
          AND vendor_code = :vendor_code
    """),
    {
        "bid": batch_id,
        "vendor_code": str(user_context["vendor_code"]),
    },
    ).first()

    if batch is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Batch {batch_id} not found",
        )

    # Get all items
    items = db.execute(
        text("""
            SELECT bii.id,
            bii.document_id,
            bii.row_number,
            bii.invoice_number,
            bii.status,
            bii.error_message,
            bii.processing_time_ms
        FROM batch_invoice_items bii
        JOIN batch_jobs bj ON bj.batch_id = bii.batch_id
        WHERE bii.batch_id = :bid
        AND bj.vendor_code = :vendor_code
        ORDER BY bii.row_number
        """),
        {
            "bid": batch_id,
            "vendor_code": str(user_context["vendor_code"]),
        }
    ).all()

    return BatchInvoicesResponse(
        batch_id=batch_id,
        total_items=len(items),
        items=[
            BatchInvoiceItemResponse(
                id=item[0],
                document_id=item[1],
                row_number=item[2],
                invoice_number=item[3],
                status=item[4],
                error_message=item[5],
                processing_time_ms=item[6],
            )
            for item in items
        ],
    )
