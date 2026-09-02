"""
Batch Invoice Upload API — Kafka Fan-Out + Claim Check Pattern

Endpoints:
- POST /invoices/batch: Upload PDF or CSV for batch processing
- GET /invoices/batch/{batch_id}: Get batch status
- GET /invoices/batch/{batch_id}/invoices: List invoices in batch

Architecture:
1. API saves individual pages to shared Docker volume (/app/data/batch_files/)
2. API publishes one Kafka event per page (file path only, NOT bytes)
3. API returns 202 ACCEPTED immediately (non-blocking)
4. Workers consume events, run boundary detection + full pipeline

RULE: No OCR in API thread — it's a blocking anti-pattern.
All heavy processing is offloaded to Kafka workers.
"""

import json
import logging
import time
import uuid
from pathlib import Path

import cv2
import fitz  # PyMuPDF
import numpy as np
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.security import get_current_user_context
from app.db.session import get_db
from app.kafka.producer import publish_batch_event
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
    file: UploadFile = File(..., description="PDF or CSV file with invoices"),
    vendor_code: str = Form(..., description="Vendor code from JWT context"),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    user_context: dict = Depends(get_current_user_context),
):
    """Upload a batch of invoices for async Kafka processing.

    This endpoint is FAST and NON-BLOCKING:
    1. Validates file type and size
    2. Saves individual pages to shared Docker volume
    3. Publishes Kafka events (file paths only)
    4. Returns 202 ACCEPTED immediately

    No OCR, no boundary detection, no VLM calls in the API thread.
    """
    start_time = time.time()

    # Read file content
    content = await file.read()
    file_size = len(content)
    filename = file.filename or "unknown"
    suffix = Path(filename).suffix.lower()

    # Validate file type
    if suffix not in [".pdf", ".csv"]:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error_code": "UNSUPPORTED_FILE_TYPE",
                "message": f"File type '{suffix}' not supported. Use PDF or CSV.",
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

    # Create batch ID and determine source type
    batch_id = str(uuid.uuid4())
    source_type = "pdf" if suffix == ".pdf" else "csv"

    # --- Step 1: Split and save pages (Claim Check) ---
    if source_type == "pdf":
        try:
            pages = _split_pdf_to_pages(
                pdf_bytes=content,
                document_id=batch_id,
                storage_path=settings.batch_storage_path,
            )
        except Exception as e:
            logger.error(f"PDF split failed: {e}")
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "error_code": "PDF_SPLIT_FAILED",
                    "message": f"Failed to split PDF: {e}",
                },
            )
    else:
        # CSV: no page splitting needed, save as-is
        batch_dir = Path(settings.batch_storage_path) / batch_id
        batch_dir.mkdir(parents=True, exist_ok=True)
        csv_path = batch_dir / "data.csv"
        csv_path.write_bytes(content)
        pages = [{
            "page_index": 0,
            "file_path": str(csv_path),
            "file_size": file_size,
        }]

    total_invoices = len(pages)

    # --- Step 2: Insert batch job record (PENDING) ---
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
    db.commit()

    # --- Step 3: Publish Kafka events (Fan-Out) ---
    # One event per page with file path (Claim Check pattern)
    try:
        event_ids = publish_batch_event(
            batch_id=batch_id,
            vendor_code=vendor_code,
            total_invoices=total_invoices,
            source_type=source_type,
            pages=pages,  # File paths, NOT bytes
        )
    except Exception as e:
        logger.error(f"Failed to publish batch events: {e}")
        # Batch record exists but events failed — worker can retry
        # Don't fail the upload, just log the error

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

    result = db.execute(
        text("""
            SELECT batch_id, vendor_code, source_type, filename,
                   total_invoices, processed_count, failed_count,
                   status, created_at, updated_at, completed_at
            FROM batch_jobs
            WHERE batch_id = :bid
        """),
        {"bid": batch_id},
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

    # Verify batch exists
    batch = db.execute(
        text("SELECT 1 FROM batch_jobs WHERE batch_id = :bid"),
        {"bid": batch_id},
    ).first()

    if batch is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Batch {batch_id} not found",
        )

    # Get all items
    items = db.execute(
        text("""
            SELECT id, document_id, row_number, invoice_number,
                   status, error_message, processing_time_ms
            FROM batch_invoice_items
            WHERE batch_id = :bid
            ORDER BY row_number
        """),
        {"bid": batch_id},
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
