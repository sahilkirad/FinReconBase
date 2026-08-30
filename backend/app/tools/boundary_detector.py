"""
Invoice Boundary Detection

Detects where one invoice ends and another begins in multi-page PDFs.
Uses lightweight OCR scan + heuristic signals (no VLM calls).

Signals used:
1. Invoice-start keywords: "INVOICE", "TAX INVOICE", "Bill To", "Invoice No"
2. Invoice-end cues: Grand total blocks, bank details, signature areas
3. Vendor identity: GSTIN patterns, vendor name consistency
4. Page-type classification: header, body, continuation, cover, unrelated
"""

import logging
import re
from dataclasses import dataclass, field
from enum import Enum

import cv2
import numpy as np

logger = logging.getLogger(__name__)


class PageType(str, Enum):
    """Classification of a page's role within an invoice."""
    HEADER = "header"           # First page of an invoice (has invoice number, date)
    BODY = "body"               # Middle page with line items
    CONTINUATION = "continuation"  # Continuation of previous invoice (no new header)
    COVER = "cover"             # Cover sheet, summary, or email printout
    UNRELATED = "unrelated"     # Page that doesn't belong to any invoice


@dataclass
class PageSignals:
    """Extracted signals from a single page."""
    page_index: int
    has_invoice_keyword: bool = False
    has_total_block: bool = False
    has_bank_details: bool = False
    has_gstin: bool = False
    invoice_number: str | None = None
    vendor_name: str | None = None
    page_type: PageType = PageType.UNRELATED
    confidence: float = 0.0
    ocr_text: str = ""


@dataclass
class InvoiceGroup:
    """A group of pages belonging to one invoice."""
    invoice_index: int
    page_indices: list[int] = field(default_factory=list)
    invoice_number: str | None = None
    confidence: float = 0.0
    needs_review: bool = False


# --- Regex patterns for boundary detection ---

INVOICE_KEYWORDS = re.compile(
    r'\b(invoice|tax\s+invoice|bill\s+to|invoice\s+no|invoice\s+number|'
    r'invoice\s+date| Invoice\b| GST\s+Invoice)\b',
    re.IGNORECASE,
)

INVOICE_NUMBER_PATTERN = re.compile(
    r'(?:invoice|inv|bill)\s+(?:no|number|#|num)\s*[:.\-]?\s*'
    r'([A-Z0-9][\w\-/]{2,30})',
    re.IGNORECASE,
)

TOTAL_BLOCK_KEYWORDS = re.compile(
    r'\b(grand\s+total|total\s+amount|amount\s+due|balance\s+due|'
    r'net\s+payable|total\s+payable|invoice\s+total)\b',
    re.IGNORECASE,
)

BANK_DETAIL_KEYWORDS = re.compile(
    r'\b(bank\s+name|account\s+no|account\s+number|ifsc|swift|'
    r'upi|neft|rtgs|micr|bank\s+details|payment\s+details)\b',
    re.IGNORECASE,
)

GSTIN_PATTERN = re.compile(
    r'\b\d{2}[A-Z]{5}\d{4}[A-Z]\d[Z][A-Z\d]\b'
)

# Signature / stamp detection (heuristic: bottom 20% of page with low text density)
SIGNATURE_KEYWORDS = re.compile(
    r'\b(authorized\s+signatory|signature|sign\s+here|stamp|seal|'
    r'date\s*:\s*|for\s+and\s+on\s+behalf)\b',
    re.IGNORECASE,
)


def _lightweight_ocr(image: np.ndarray) -> str:
    """Fast OCR scan for boundary detection (lower accuracy OK).
    
    Uses Tesseract with PSM 6 (uniform block) for speed over accuracy.
    We only need keyword matching, not perfect text extraction.
    """
    try:
        import pytesseract
        # Use faster settings for boundary detection
        config = '--psm 6 --oem 3'
        text = pytesseract.image_to_string(image, config=config)
        return text
    except (ImportError, Exception):
        # If OCR fails, return empty string (graceful degradation)
        logger.warning("OCR failed for boundary detection, using visual signals only")
        return ""


def _extract_page_signals(image: np.ndarray, page_index: int) -> PageSignals:
    """Extract all boundary signals from a single page image."""
    signals = PageSignals(page_index=page_index)
    
    # Run lightweight OCR
    ocr_text = _lightweight_ocr(image)
    signals.ocr_text = ocr_text
    
    if not ocr_text.strip():
        # No text detected - likely a blank page or image-only page
        signals.page_type = PageType.UNRELATED
        signals.confidence = 0.9
        return signals
    
    # Signal 1: Invoice keyword detection
    if INVOICE_KEYWORDS.search(ocr_text):
        signals.has_invoice_keyword = True
    
    # Signal 2: Invoice number extraction
    inv_match = INVOICE_NUMBER_PATTERN.search(ocr_text)
    if inv_match:
        signals.invoice_number = inv_match.group(1)
    
    # Signal 3: Total block detection
    if TOTAL_BLOCK_KEYWORDS.search(ocr_text):
        signals.has_total_block = True
    
    # Signal 4: Bank details detection
    if BANK_DETAIL_KEYWORDS.search(ocr_text):
        signals.has_bank_details = True
    
    # Signal 5: GSTIN detection (Indian tax ID)
    gstin_match = GSTIN_PATTERN.search(ocr_text)
    if gstin_match:
        signals.has_gstin = True
        signals.vendor_name = gstin_match.group(0)  # Use GSTIN as vendor identifier
    
    # Signal 6: Signature/stamp detection
    has_signature = bool(SIGNATURE_KEYWORDS.search(ocr_text))
    
    # --- Page type classification ---
    if signals.has_invoice_keyword and signals.invoice_number:
        # Strong start signal: has both keyword and invoice number
        signals.page_type = PageType.HEADER
        signals.confidence = 0.95
    elif signals.has_invoice_keyword:
        # Has keyword but no clear invoice number
        signals.page_type = PageType.HEADER
        signals.confidence = 0.7
    elif signals.has_total_block and has_signature:
        # End-of-invoice signal: total + signature
        signals.page_type = PageType.BODY  # Last body page
        signals.confidence = 0.8
    elif signals.has_total_block:
        # Has total but no signature - likely last page of invoice
        signals.page_type = PageType.BODY
        signals.confidence = 0.6
    elif signals.has_bank_details:
        # Bank details usually appear at end of invoice
        signals.page_type = PageType.BODY
        signals.confidence = 0.5
    else:
        # No clear signals - classify as continuation
        signals.page_type = PageType.CONTINUATION
        signals.confidence = 0.4
    
    return signals


def _is_cover_page(signals: PageSignals) -> bool:
    """Detect cover sheets, summary pages, or email printouts."""
    text = signals.ocr_text.lower()
    cover_indicators = [
        'cover sheet', 'summary', 'email', 'from:', 'to:', 'subject:',
        're:', 'forwarded', 'printed from', 'page 1 of', 'confidential',
    ]
    return any(indicator in text for indicator in cover_indicators)


def detect_boundaries(page_images: list[np.ndarray]) -> list[InvoiceGroup]:
    """
    Detect invoice boundaries in a multi-page document.
    
    Args:
        page_images: List of page images (BGR format from OpenCV)
    
    Returns:
        List of InvoiceGroup, each containing pages belonging to one invoice.
        Low-confidence groups have needs_review=True.
    """
    if not page_images:
        return []
    
    if len(page_images) == 1:
        # Single page - definitely one invoice
        return [InvoiceGroup(
            invoice_index=0,
            page_indices=[0],
            confidence=1.0,
            needs_review=False,
        )]
    
    # Step 1: Extract signals from all pages
    all_signals = []
    for idx, img in enumerate(page_images):
        signals = _extract_page_signals(img, idx)
        
        # Check for cover pages
        if _is_cover_page(signals):
            signals.page_type = PageType.COVER
            signals.confidence = 0.85
        
        all_signals.append(signals)
        
        logger.debug(
            "Page %d signals: type=%s, invoice_kw=%s, total=%s, bank=%s, gstin=%s, inv_no=%s",
            idx, signals.page_type.value, signals.has_invoice_keyword,
            signals.has_total_block, signals.has_bank_details,
            signals.has_gstin, signals.invoice_number,
        )
    
    # Step 2: Group pages into invoices
    invoices = []
    current_group = InvoiceGroup(invoice_index=0)
    current_group.page_indices.append(0)
    
    for idx in range(1, len(all_signals)):
        signals = all_signals[idx]
        prev_signals = all_signals[idx - 1]
        
        # Decision: Does this page start a new invoice?
        starts_new_invoice = False
        
        # Rule 1: Header page always starts a new invoice
        if signals.page_type == PageType.HEADER:
            starts_new_invoice = True
        
        # Rule 2: Cover page starts a new "group" (but marked as cover)
        elif signals.page_type == PageType.COVER:
            starts_new_invoice = True
        
        # Rule 3: GSTIN change indicates new vendor = new invoice
        elif (signals.has_gstin and prev_signals.has_gstin and
              signals.vendor_name != prev_signals.vendor_name):
            starts_new_invoice = True
        
        # Rule 4: Previous page had total block + this page has header signals
        elif prev_signals.has_total_block and signals.has_invoice_keyword:
            starts_new_invoice = True
        
        if starts_new_invoice:
            # Finalize current group
            invoices.append(current_group)
            # Start new group
            current_group = InvoiceGroup(invoice_index=len(invoices))
        
        current_group.page_indices.append(idx)
    
    # Don't forget the last group
    invoices.append(current_group)
    
    # Step 3: Assign invoice numbers and compute confidence
    for inv in invoices:
        # Find the best invoice number from any page in the group
        for page_idx in inv.page_indices:
            if all_signals[page_idx].invoice_number:
                inv.invoice_number = all_signals[page_idx].invoice_number
                break
        
        # Compute average confidence
        confidences = [all_signals[pi].confidence for pi in inv.page_indices]
        inv.confidence = sum(confidences) / len(confidences) if confidences else 0.0
        
        # Flag low-confidence groups for manual review
        if inv.confidence < 0.5 or not inv.invoice_number:
            inv.needs_review = True
    
    logger.info(
        "Boundary detection complete",
        extra={
            "total_pages": len(page_images),
            "invoices_found": len(invoices),
            "low_confidence_count": sum(1 for i in invoices if i.needs_review),
        },
    )
    
    return invoices
