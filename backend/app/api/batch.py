"""
Batch Invoice Upload API

Endpoints for bulk invoice processing:
- POST /invoices/batch: Upload PDF or CSV for batch processing
- GET /invoices/batch/{batch_id}: Get batch status
- GET /invoices/batch/{batch_id}/invoices: List invoices in batch
"""

import json
import logging
import tempfile
import time
import uuid
from pathlib import Path

import cv2
import fitz  # PyMuPDF
import numpy as np
from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile, status
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


def _pdf_to_images(pdf_bytes: bytes) -> list[np.ndarray]:
    """Convert PDF pages to numpy arrays."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    images = []
    for page in doc:
        mat = fitz.Matrix(200 / 72, 200 / 72)
        pix = page.get_pixmap(matrix=mat)
        img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.h, pix.w, pix.n)
        if pix.n == 4:
            img = cv2.cvtColor(img, cv2.COLOR_RGBA2BGR)
        elif pix.n == 3:
            img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
        images.append(img)
    doc.close()
    return images


def _process_batch_in_background(
    batch_id: str,
    vendor_code: str,
    source_type: str,
    file_content: bytes,
    filename: str,
):
    """
    Background task to process a batch of invoices.
    
    This runs after the API returns the batch_id.
    Processes each invoice and updates batch status.
    """
    from app.tools.boundary_detector import detect_boundaries
    from app.tools.checksum import run_checksum
    from app.tools.csv_parser import parse_csv, to_extracted_payload, validate_row
    from app.tools.vlm_extractor import extract_invoice_json
    from app.tools.ocr_engine import extract_text
    from app.tools.preprocessing import preprocess_path_a_ocr, preprocess_path_b_vlm
    from app.tools.blur_check import check_blur
    from app.tools.pii_masker import mask_invoice_for_llm
    from app.schemas.invoice import ExtractedInvoicePayload
    from app.tools.vlm_optimizer import get_vlm_rate_limiter, retry_with_backoff, select_model
    
    # Get DB session
    from app.db.session import SessionLocal
    db = SessionLocal()
    
    try:
        # Update batch status to PROCESSING
        db.execute(
            text("UPDATE batch_jobs SET status = 'PROCESSING', updated_at = now() WHERE batch_id = :bid"),
            {"bid": batch_id},
        )
        db.commit()
        
        rate_limiter = get_vlm_rate_limiter()
        
        if source_type == "pdf":
            # Process PDF with boundary detection
            page_images = _pdf_to_images(file_content)
            invoice_groups = detect_boundaries(page_images)
            
            for inv_group in invoice_groups:
                # Rate limit
                rate_limiter.acquire()
                
                start_time = time.time()
                
                try:
                    # Get first page image for processing
                    first_page_idx = inv_group.page_indices[0]
                    page_img = page_images[first_page_idx]
                    
                    # Blur check
                    blur_score, blur_passed, processed_img = check_blur(page_img)
                    if not blur_passed:
                        _update_item_status(db, batch_id, inv_group.invoice_number, "FAILED", "Blur check failed")
                        continue
                    
                    # Preprocessing
                    path_a = preprocess_path_a_ocr(processed_img)
                    path_b = preprocess_path_b_vlm(processed_img)
                    
                    # OCR
                    ocr_text, ocr_confidence = extract_text(path_a)
                    
                    # VLM extraction (with retry)
                    raw_ocr_masked = mask_invoice_for_llm({"ocr_text": ocr_text})
                    
                    model = select_model(is_batch=True)
                    extracted_json = retry_with_backoff(
                        lambda: extract_invoice_json(
                            ocr_text=raw_ocr_masked.get("ocr_text", ocr_text),
                            send_image=False,  # PDFs: text only
                        )
                    )
                    
                    # Checksum
                    checksum_errors = run_checksum(extracted_json)
                    
                    # Schema validation
                    try:
                        invoice_payload = ExtractedInvoicePayload(**extracted_json)
                        processing_status = "VALIDATED" if not checksum_errors else "EXCEPTION_FLAGGED"
                    except Exception as e:
                        _update_item_status(db, batch_id, inv_group.invoice_number, "FAILED", str(e))
                        continue
                    
                    # Insert to DB
                    _insert_invoice(db, batch_id, vendor_code, invoice_payload, processing_status, checksum_errors, first_page_idx)
                    
                    # Update batch progress
                    db.execute(
                        text("UPDATE batch_jobs SET processed_count = processed_count + 1, updated_at = now() WHERE batch_id = :bid"),
                        {"bid": batch_id},
                    )
                    db.commit()
                    
                except Exception as e:
                    logger.error(f"Invoice processing failed: {e}")
                    _update_item_status(db, batch_id, inv_group.invoice_number, "FAILED", str(e))
                    db.execute(
                        text("UPDATE batch_jobs SET failed_count = failed_count + 1, updated_at = now() WHERE batch_id = :bid"),
                        {"bid": batch_id},
                    )
                    db.commit()
        
        elif source_type == "csv":
            # Process CSV
            valid_rows, errors, duplicates = validate_csv(file_content.decode("utf-8-sig"))
            
            for row in valid_rows:
                # Rate limit
                rate_limiter.acquire()
                
                try:
                    invoice_number = row.get("invoice_number")
                    
                    # Convert to payload
                    invoice_payload = to_extracted_payload(row)
                    
                    # Checksum
                    checksum_errors = run_checksum(invoice_payload.model_dump())
                    processing_status = "VALIDATED" if not checksum_errors else "EXCEPTION_FLAGGED"
                    
                    # Insert to DB
                    _insert_invoice(db, batch_id, vendor_code, invoice_payload, processing_status, checksum_errors, row.get("_row_number"))
                    
                    # Update batch progress
                    db.execute(
                        text("UPDATE batch_jobs SET processed_count = processed_count + 1, updated_at = now() WHERE batch_id = :bid"),
                        {"bid": batch_id},
                    )
                    db.commit()
                    
                except Exception as e:
                    logger.error(f"CSV row processing failed: {e}")
                    _update_item_status(db, batch_id, row.get("invoice_number"), "FAILED", str(e))
                    db.execute(
                        text("UPDATE batch_jobs SET failed_count = failed_count + 1, updated_at = now() WHERE batch_id = :bid"),
                        {"bid": batch_id},
                    )
                    db.commit()
        
        # Mark batch as completed
        db.execute(
            text("UPDATE batch_jobs SET status = 'COMPLETED', completed_at = now(), updated_at = now() WHERE batch_id = :bid"),
            {"bid": batch_id},
        )
        db.commit()
        
        logger.info(f"Batch {batch_id} processing completed")
        
    except Exception as e:
        logger.error(f"Batch processing failed: {e}")
        db.execute(
            text("UPDATE batch_jobs SET status = 'FAILED', updated_at = now() WHERE batch_id = :bid"),
            {"bid": batch_id},
        )
        db.commit()
    finally:
        db.close()


def _update_item_status(db, batch_id: str, invoice_number: str, status: str, error: str):
    """Update a batch item's status."""
    db.execute(
        text("""
            UPDATE batch_invoice_items 
            SET status = :status, error_message = :error, updated_at = now()
            WHERE batch_id = :bid AND invoice_number = :inv
        """),
        {"bid": batch_id, "inv": invoice_number, "status": status, "error": error},
    )
    db.commit()


def _insert_invoice(db, batch_id: str, vendor_code: str, payload, status: str, errors: list, row_num: int):
    """Insert extracted invoice and batch item records."""
    document_id = str(uuid.uuid4())
    event_id = f"evt_{uuid.uuid4()}"
    
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
            "invoice_number": payload.reference_data.invoice_number,
            "document_type_code": payload.reference_data.document_type_code,
            "po_number": payload.reference_data.po_number,
            "document_date": payload.reference_data.document_date,
            "due_date": payload.reference_data.due_date,
            "supplier_legal_name": payload.supplier_details.legal_name,
            "supplier_gstin": payload.supplier_details.gstin,
            "supplier_pan": payload.supplier_details.pan,
            "buyer_legal_name": payload.buyer_details.legal_name,
            "buyer_gstin": payload.buyer_details.gstin,
            "grand_total_paise": payload.financial_summary.grand_total_paise,
            "tds_deduction_paise": payload.financial_summary.tds_deduction_paise,
            "processing_status": status,
            "parsed_payload": payload.model_dump_json(),
            "validation_errors": json.dumps(errors),
        },
    )
    
    # Insert batch item
    db.execute(
        text("""
            INSERT INTO batch_invoice_items (
                batch_id, document_id, row_number, invoice_number, status, processing_time_ms
            ) VALUES (
                :batch_id, :document_id, :row_number, :invoice_number, :status, 0
            )
        """),
        {
            "batch_id": batch_id,
            "document_id": document_id,
            "row_number": row_num,
            "invoice_number": payload.reference_data.invoice_number,
            "status": status,
        },
    )
    
    db.commit()


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
    background_tasks: BackgroundTasks = BackgroundTasks(),
):
    """Upload a batch of invoices (PDF or CSV) for async processing."""
    
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
    max_batch_size = 100 * 1024 * 1024
    if file_size > max_batch_size:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail={
                "error_code": "FILE_TOO_LARGE",
                "message": f"Batch file size {file_size} exceeds max 100MB",
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
    
    # Create batch job record
    batch_id = str(uuid.uuid4())
    source_type = "pdf" if suffix == ".pdf" else "csv"
    
    # Quick count for initial response
    if source_type == "csv":
        try:
            rows = content.decode("utf-8-sig").strip().split("\n")
            total_invoices = max(0, len(rows) - 1)  # Subtract header
        except Exception:
            total_invoices = 0
    else:
        # For PDF, estimate page count
        try:
            doc = fitz.open(stream=content, filetype="pdf")
            total_invoices = len(doc)
            doc.close()
        except Exception:
            total_invoices = 0
    
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
    
    # Start background processing
    background_tasks.add_task(
        _process_batch_in_background,
        batch_id=batch_id,
        vendor_code=vendor_code,
        source_type=source_type,
        file_content=content,
        filename=filename,
    )
    
    # Publish Kafka event
    try:
        publish_batch_event(batch_id, vendor_code, total_invoices, source_type)
    except Exception as e:
        logger.warning(f"Failed to publish batch event: {e}")
    
    return BatchUploadResponse(
        batch_id=batch_id,
        vendor_code=vendor_code,
        source_type=source_type,
        filename=filename,
        total_invoices=total_invoices,
        valid_invoices=total_invoices,  # Will be updated during processing
        invalid_invoices=0,
        status="PENDING",
        message=f"Batch of {total_invoices} invoices queued for processing",
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
