"""
Gemini VLM Semantic Extraction

Receives Path B (clean RGB) image + OCR text from Tesseract.
Returns the locked invoice JSON schema via Gemini 1.5 Flash.
Gemini is used ONLY for semantic mapping, never for financial correctness.


"""

import json
import base64
import logging

import cv2
import numpy as np

from app.core.config import get_settings

logger = logging.getLogger(__name__)

EXTRACTION_PROMPT = """You are a financial document extraction engine.
Analyze this invoice image and extract structured data.

The OCR text from the document is provided below for reference:
---
{ocr_text}
---

Return a JSON object matching this EXACT schema (no extra fields):
{{
  "metadata": {{
    "source_file": "filename",
    "page_count": 1,
    "processing_time_ms": 0
  }},
  "supplier_details": {{
    "legal_name": "string",
    "gstin": "string or null",
    "pan": "string or null",
    "address": "string or null",
    "state_code": "string or null",
    "state_name": "string or null",
    "phone": "string or null",
    "email": "string or null"
  }},
  "buyer_details": {{
    "legal_name": "string or null",
    "gstin": "string or null",
    "pan": "string or null",
    "address": "string or null",
    "state_code": "string or null",
    "state_name": "string or null",
    "phone": "string or null",
    "email": "string or null"
  }},
  "reference_data": {{
    "invoice_number": "string",
    "document_type_code": "INV",
    "po_number": "string or null",
    "grn_number": "string or null",
    "document_date": "YYYY-MM-DD",
    "due_date": "YYYY-MM-DD or null",
    "irn": "string or null"
  }},
  "banking_details": {{
    "bank_name": "string or null",
    "account_number": "string or null",
    "ifsc": "string or null",
    "upi_id": "string or null",
    "account_number_masked": "XXXX-XXXX-1234 or null"
  }},
  "line_items": [
    {{
      "line_number": 1,
      "description": "string",
      "hsn_sac_code": "string or null",
      "quantity": "number as string",
      "unit": "string",
      "unit_price_paise": 0,
      "taxable_value_paise": 0,
      "gst_rate": "number as string",
      "igst_paise": 0,
      "cgst_paise": 0,
      "sgst_paise": 0,
      "total_paise": 0
    }}
  ],
  "financial_summary": {{
    "subtotal_paise": 0,
    "total_tax_paise": 0,
    "total_igst_paise": 0,
    "total_cgst_paise": 0,
    "total_sgst_paise": 0,
    "tds_deduction_paise": 0,
    "other_charges_paise": 0,
    "discount_paise": 0,
    "rounding_adjustment_paise": 0,
    "grand_total_paise": 0
  }}
}}

IMPORTANT:
- "Grand Total" = Subtotal + Tax (gross invoice total BEFORE any TDS/deductions) -> financial_summary.grand_total_paise
- "Amount Due" / "Total Payable" / "Balance Due" = Grand Total minus TDS (post-deduction payable). Do NOT map these to grand_total_paise.
- Map "GST @ 18%", "Tax", "CGST", "SGST", "IGST" -> corresponding tax fields
- All money amounts must be in integer PAISE (rupees x 100)
- Return ONLY the JSON object, no markdown fences, no explanation.
"""


def _image_to_base64(image: np.ndarray) -> str:
    """Convert OpenCV numpy array to base64-encoded JPEG for Gemini API."""
    # P1: cap the longest edge and drop JPEG quality so the base64 payload
    # (and TPM cost per call) shrinks before upload.
    h, w = image.shape[:2]
    longest = max(h, w)
    if longest > 1500:
        scale = 1500 / longest
        image = cv2.resize(
            image, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA
        )
    _, buffer = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 80])
    return base64.b64encode(buffer).decode("utf-8")


def extract_invoice_json(
    ocr_text: str,
    rgb_image: np.ndarray | None = None,
    filename: str = "unknown",
    send_image: bool = True,
    model_override: str | None = None,
) -> dict:
    """Send OCR text (and optionally image) to Gemini VLM for semantic extraction.

    IMPORTANT: Gemini is ONLY used for semantic field extraction.
    Mathematical validation (line items, tax, grand total) is ALWAYS
    performed by our local deterministic Python checksum layer.

    Model Selection (enforced by invoice_worker):
        - gemini-3.5-flash-lite: Standard digital PDFs and clear scans
        - gemini-3.7-flash: Heavily degraded invoice photos

    Args:
        ocr_text: Extracted text from Tesseract OCR
        rgb_image: Optional RGB image for visual context (used for images, skipped for PDFs)
        filename: Source filename for logging
        send_image: If True, send image to VLM. If False, send text only (faster for PDFs).
        model_override: Override the default model selection (e.g., 'gemini-3.5-flash-lite')

    Returns the raw JSON dict matching ExtractedInvoicePayload schema.
    Raises RuntimeError if Gemini API is unavailable or extraction fails.
    """
    settings = get_settings()

    # HARD FAIL if API key not configured - no mock fallback
    if not settings.gemini_api_key or settings.gemini_api_key == "replace-with-gemini-api-key":
        raise RuntimeError(
            "Gemini API key not configured. Set GEMINI_API_KEY in .env. "
            "Production system requires a valid Gemini API key for VLM extraction."
        )

    # Determine model to use
    model_name = model_override or settings.gemini_model_fast

    try:
        import google.generativeai as genai

        genai.configure(api_key=settings.gemini_api_key)
        model = genai.GenerativeModel(model_name)

        prompt = EXTRACTION_PROMPT.format(ocr_text=ocr_text)

        if send_image and rgb_image is not None:
            # Send image + text (for image uploads where visual context matters)
            image_b64 = _image_to_base64(rgb_image)
            image_part = {"mime_type": "image/jpeg", "data": image_b64}
            response = model.generate_content([prompt, image_part], request_options={"timeout": settings.gemini_request_timeout_s})
        else:
            # Text only (for PDFs where OCR is accurate)
            response = model.generate_content(prompt, request_options={"timeout": settings.gemini_request_timeout_s})

        raw_text = response.text.strip()
        # Strip markdown code fences if present
        if raw_text.startswith("```"):
            raw_text = raw_text.split("\n", 1)[1]
            if raw_text.endswith("```"):
                raw_text = raw_text[:-3]
            raw_text = raw_text.strip()

        logger.info(
            "Gemini VLM extraction complete",
            extra={"source_file": filename, "model": model_name, "response_length": len(raw_text)},
        )

        return json.loads(raw_text)

    except Exception as e:
        logger.error("Gemini VLM extraction failed", extra={"error": str(e)})
        raise RuntimeError(f"Gemini VLM extraction failed: {e}") from e
