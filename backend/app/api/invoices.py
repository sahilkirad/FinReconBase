"""
Invoice Upload API - Layer 1 Entry Point

Orchestrates: Guardrail -> Page Stitching -> Blur Check ->
Dual-Path Preprocessing -> OCR -> VLM -> Checksum -> DB + Outbox
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
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.errors import Layer1ErrorCode, raise_guardrail_error
from app.core.security import get_current_user_context
from app.db.session import get_db
from app.schemas.invoice import (
    ExtractedInvoicePayload,
    ProcessedPage,
    UploadErrorResponse,
    UploadResponse,
)
from app.tools.checksum import run_checksum
from app.tools.guardrail import DocumentGuardrail, create_guardrail
from app.tools.blur_check import check_blur
from app.tools.preprocessing import preprocess_path_a_ocr, preprocess_path_b_vlm
from app.tools.ocr_engine import extract_text
from app.tools.vlm_extractor import extract_invoice_json
from app.tools.pii_masker import mask_invoice_for_llm

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/invoices", tags=["invoices"])


def _pdf_to_images(pdf_bytes: bytes) -> list[np.ndarray]:
    """Convert PDF pages to numpy arrays (BGR format for OpenCV)."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    images = []
    for page in doc:
        # Render at 200 DPI for processing (will be rescaled to 300 later)
        mat = fitz.Matrix(200 / 72, 200 / 72)
        pix = page.get_pixmap(matrix=mat)
        img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.h, pix.w, pix.n)
        if pix.n == 4:  # RGBA -> BGR
            img = cv2.cvtColor(img, cv2.COLOR_RGBA2BGR)
        elif pix.n == 3:  # RGB -> BGR
            img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
        images.append(img)
    doc.close()
    return images


def _image_bytes_to_numpy(image_bytes: bytes, filename: str) -> np.ndarray:
    """Convert uploaded image bytes to numpy array."""
    nparr = np.frombuffer(image_bytes, np.uint8)
    img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    if img is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error_code": "IMAGE_DECODE_FAILED", "message": f"Cannot decode {filename}"},
        )
    return img


@router.post(
    "",
    response_model=UploadResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        409: {"model": UploadErrorResponse},
        413: {"model": UploadErrorResponse},
        422: {"model": UploadErrorResponse},
    },
)
async def upload_invoice(
    file: UploadFile = File(..., description="Invoice PDF, JPEG, or PNG"),
    vendor_code: str = Form(..., description="Vendor code from JWT context"),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    user_context: dict = Depends(get_current_user_context),
):
    """Upload an invoice document for Layer 1 ingestion and extraction."""
    start_time = time.time()
    document_id = uuid.uuid4()
    filename = file.filename or "unknown"

    # --- Step 0: Read file content early for size/type checks ---
    content = await file.read()
    file_size = len(content)
    content_type = file.content_type or "application/octet-stream"
    suffix = Path(filename).suffix.lower() or ".pdf"

    # --- Step 0a: Pre-flight size check (before DB query) ---
    max_bytes = settings.max_upload_size_mb * 1024 * 1024
    if file_size > max_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail={
                "error_code": "FILE_TOO_LARGE",
                "message": f"File size {file_size} exceeds max {settings.max_upload_size_mb}MB",
            },
        )

    # --- Step 0b: Pre-flight extension check (before DB query) ---
    if suffix.lstrip('.') not in [ext.lstrip('.') for ext in settings.allowed_upload_extensions]:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error_code": "UNSUPPORTED_FILE_TYPE",
                "message": f"File type '{suffix}' not allowed. Use: {settings.allowed_upload_extensions}",
            },
        )

    # --- Step 0c: Validate vendor exists in vendor_users table ---
    vendor_check = db.execute(
        text("SELECT 1 FROM vendor_users WHERE vendor_code = :vc"),
        {"vc": vendor_code},
    ).first()
    if vendor_check is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Vendor '{vendor_code}' is not onboarded. Contact admin.",
        )
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(content)
        tmp_path = Path(tmp.name)

    processed_pages = []

    try:
        # --- Step 1: Pre-flight Guardrail (MIME + Classification + Anchor) ---
        guardrail = create_guardrail()
        classification_label, classification_score, anchor_found = guardrail.run_guardrail(
            tmp_path, file_size, content_type
        )

        # --- Step 2: Convert to images (page stitching) ---
        if suffix == ".pdf":
            page_images = _pdf_to_images(content)
        else:
            page_images = [_image_bytes_to_numpy(content, filename)]

        page_count = len(page_images)

        # --- Step 3-4: Per-page processing ---
        for page_idx, page_img in enumerate(page_images):
            # Laplacian blur check
            blur_score, blur_passed, processed_img = check_blur(
                page_img, source_label=f"{filename}_page_{page_idx}"
            )
            if not blur_passed:
                raise_guardrail_error(
                    Layer1ErrorCode.BLUR_FAILED,
                    f"Page {page_idx + 1}: Blur score {blur_score:.2f} below threshold",
                    {"blur_score": blur_score, "threshold": settings.blur_threshold},
                )

            # Path A: Binarized for OCR
            path_a = preprocess_path_a_ocr(processed_img)
            # Path B: Clean RGB for VLM
            path_b = preprocess_path_b_vlm(processed_img)

            # Save processed images to temp files for downstream
            path_a_file = tempfile.NamedTemporaryFile(delete=False, suffix=".png")
            path_b_file = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
            cv2.imwrite(path_a_file.name, path_a)
            cv2.imwrite(path_b_file.name, path_b)

            processed_pages.append(
                ProcessedPage(
                    page_index=page_idx,
                    path_a_image_path=path_a_file.name,
                    path_b_image_path=path_b_file.name,
                )
            )

        # --- Step 5: OCR (from Path A of first page for text context) ---
        ocr_text = ""
        ocr_confidence = 0.0
        if processed_pages:
            first_page_a = cv2.imread(processed_pages[0].path_a_image_path)
            ocr_text, ocr_confidence = extract_text(first_page_a)

        # --- Step 6: Gemini VLM Semantic Extraction (from Path B) ---
        extraction_start = time.time()
        # Use first page for VLM (multi-page stitching handled by passing all pages)
        first_page_b = cv2.imread(processed_pages[0].path_b_image_path)

        # Mask PII before sending to LLM
        raw_ocr_masked = mask_invoice_for_llm({"ocr_text": ocr_text})

        extracted_json = extract_invoice_json(
            rgb_image=first_page_b,
            ocr_text=raw_ocr_masked.get("ocr_text", ocr_text),
            filename=filename,
        )
        extraction_ms = int((time.time() - extraction_start) * 1000)

        # --- Step 7: Pydantic Schema Validation ---
        try:
            invoice_payload = ExtractedInvoicePayload(**extracted_json)
        except Exception as e:
            raise_guardrail_error(
                Layer1ErrorCode.VLM_FAILED,
                f"VLM output failed schema validation: {e}",
                {"validation_error": str(e)},
            )

        # --- Step 8: Mathematical Checksum ---
        checksum_errors = run_checksum(extracted_json)
        if checksum_errors:
            # Still insert but mark as EXCEPTION_FLAGGED
            processing_status = "EXCEPTION_FLAGGED"
            validation_errors = checksum_errors
        else:
            processing_status = "VALIDATED"
            validation_errors = []

        # --- Step 9: Compute paise fields for searchable columns ---
        grand_total_paise = invoice_payload.financial_summary.grand_total_paise
        tds_deduction_paise = invoice_payload.financial_summary.tds_deduction_paise

        # --- Step 10: DB Insert (extracted_invoices + outbox_events) in single TX ---
        event_id = f"evt_{uuid.uuid4()}"

        # Insert extracted_invoices
        insert_invoice = text("""
            INSERT INTO extracted_invoices (
                document_id, irn, vendor_code, invoice_number,
                document_type_code, po_number, document_date, due_date,
                supplier_legal_name, supplier_gstin, supplier_pan,
                buyer_legal_name, buyer_gstin,
                currency_code, grand_total_paise, tds_deduction_paise,
                processing_status, parsed_payload, validation_errors
            ) VALUES (
                :document_id, :irn, :vendor_code, :invoice_number,
                :document_type_code, :po_number, :document_date, :due_date,
                :supplier_legal_name, :supplier_gstin, :supplier_pan,
                :buyer_legal_name, :buyer_gstin,
                'INR', :grand_total_paise, :tds_deduction_paise,
                :processing_status, :parsed_payload, :validation_errors
            )
        """)

        # Build outbox payload (CloudEvents 1.0 shape)
        outbox_payload = {
            "specversion": "1.0",
            "type": "invoice.extracted",
            "source": "/layer1/ingestion",
            "id": event_id,
            "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "data": {
                "document_id": str(document_id),
                "vendor_code": vendor_code,
                "invoice_number": invoice_payload.reference_data.invoice_number,
                "processing_status": processing_status,
            },
        }

        insert_outbox = text("""
            INSERT INTO outbox_events (
                event_id, aggregate_type, aggregate_id,
                topic, partition_key, event_type,
                payload, status
            ) VALUES (
                :event_id, 'ExtractedInvoice', :aggregate_id,
                :topic, :partition_key, 'InvoiceExtracted',
                :payload, 'PENDING'
            )
        """)

        try:
            db.execute(insert_invoice, {
                "document_id": str(document_id),
                "irn": invoice_payload.reference_data.irn,
                "vendor_code": vendor_code,
                "invoice_number": invoice_payload.reference_data.invoice_number,
                "document_type_code": invoice_payload.reference_data.document_type_code,
                "po_number": invoice_payload.reference_data.po_number,
                "document_date": invoice_payload.reference_data.document_date,
                "due_date": invoice_payload.reference_data.due_date,
                "supplier_legal_name": invoice_payload.supplier_details.legal_name,
                "supplier_gstin": invoice_payload.supplier_details.gstin,
                "supplier_pan": invoice_payload.supplier_details.pan,
                "buyer_legal_name": invoice_payload.buyer_details.legal_name,
                "buyer_gstin": invoice_payload.buyer_details.gstin,
                "grand_total_paise": grand_total_paise,
                "tds_deduction_paise": tds_deduction_paise,
                "processing_status": processing_status,
                "parsed_payload": invoice_payload.model_dump_json(),
                "validation_errors": json.dumps(validation_errors),
            })

            db.execute(insert_outbox, {
                "event_id": event_id,
                "aggregate_id": str(document_id),
                "topic": settings.invoice_extracted_topic,
                "partition_key": vendor_code,
                "payload": json.dumps(outbox_payload),
            })

            db.commit()

        except Exception as e:
            db.rollback()
            if "unique constraint" in str(e).lower() and "invoice_number" in str(e).lower():
                raise_guardrail_error(
                    Layer1ErrorCode.DUPLICATE_INVOICE,
                    f"Duplicate invoice: {invoice_payload.reference_data.invoice_number} for vendor {vendor_code}",
                    {"invoice_number": invoice_payload.reference_data.invoice_number},
                )
            raise_guardrail_error(
                Layer1ErrorCode.DATABASE_ERROR,
                f"Database insert failed: {e}",
                {"error": str(e)},
            )

        processing_time_ms = int((time.time() - start_time) * 1000)
        logger.info(
            "Invoice processed successfully",
            extra={
                "document_id": str(document_id),
                "vendor_code": vendor_code,
                "status": processing_status,
                "processing_time_ms": processing_time_ms,
            },
        )

        return UploadResponse(
            document_id=document_id,
            status=processing_status,
            message=f"Invoice processed in {processing_time_ms}ms. Status: {processing_status}",
            invoice_number=invoice_payload.reference_data.invoice_number,
        )

    finally:
        # Cleanup temp files
        tmp_path.unlink(missing_ok=True)
        for page in processed_pages:
            Path(page.path_a_image_path).unlink(missing_ok=True)
            Path(page.path_b_image_path).unlink(missing_ok=True)
