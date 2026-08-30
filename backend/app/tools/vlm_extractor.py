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
import signal

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
    _, buffer = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 95])
    return base64.b64encode(buffer).decode("utf-8")


class VLMTimeoutError(Exception):
    pass

def _vlm_timeout_handler(signum, frame):
    raise VLMTimeoutError("VLM extraction timed out after 45 seconds")


def extract_invoice_json(
    rgb_image: np.ndarray,
    ocr_text: str,
    filename: str = "unknown",
) -> dict:
    """Send Path B image + OCR text to Gemini VLM for semantic extraction.

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

    # Overall timeout: kill function after 45 seconds (Linux only)
    import sys
    if sys.platform != "win32":
        signal.signal(signal.SIGALRM, _vlm_timeout_handler)
        signal.alarm(45)
    try:
        import google.generativeai as genai

        genai.configure(api_key=settings.gemini_api_key)
        model = genai.GenerativeModel(settings.gemini_model)

        image_b64 = _image_to_base64(rgb_image)
        image_part = {"mime_type": "image/jpeg", "data": image_b64}

        prompt = EXTRACTION_PROMPT.format(ocr_text=ocr_text)
        response = model.generate_content([prompt, image_part], request_options={"timeout": 30})

        raw_text = response.text.strip()
        # Strip markdown code fences if present
        if raw_text.startswith("```"):
            raw_text = raw_text.split("\n", 1)[1]
            if raw_text.endswith("```"):
                raw_text = raw_text[:-3]
            raw_text = raw_text.strip()

        logger.info(
            "Gemini VLM extraction complete",
            extra={"filename": filename, "response_length": len(raw_text)},
        )

        return json.loads(raw_text)

    except VLMTimeoutError:
        if sys.platform != "win32":
            signal.alarm(0)
        raise RuntimeError("VLM extraction timed out after 45 seconds") from None
    except Exception as e:
        if sys.platform != "win32":
            signal.alarm(0)
        logger.error("Gemini VLM extraction failed", extra={"error": str(e)})
        raise RuntimeError(f"Gemini VLM extraction failed: {e}") from e
