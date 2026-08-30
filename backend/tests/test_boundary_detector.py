"""
Tests for Invoice Boundary Detection

Tests cover:
- Single page documents
- Multi-page single invoice
- Multiple invoices in one PDF
- Cover page handling
- Low confidence / manual review flagging
"""

import numpy as np
import pytest
from unittest.mock import patch, MagicMock

from app.tools.boundary_detector import (
    PageType,
    PageSignals,
    InvoiceGroup,
    detect_boundaries,
    _extract_page_signals,
    _is_cover_page,
    INVOICE_KEYWORDS,
    TOTAL_BLOCK_KEYWORDS,
    BANK_DETAIL_KEYWORDS,
    GSTIN_PATTERN,
)


class TestRegexPatterns:
    """Test regex pattern matching."""

    def test_invoice_keyword_matches(self):
        assert INVOICE_KEYWORDS.search("TAX INVOICE") is not None
        assert INVOICE_KEYWORDS.search("Invoice No: 12345") is not None
        assert INVOICE_KEYWORDS.search("Bill To: Acme Corp") is not None
        assert INVOICE_KEYWORDS.search("Invoice Date: 2026-01-15") is not None

    def test_invoice_keyword_rejects(self):
        assert INVOICE_KEYWORDS.search("Hello World") is None
        assert INVOICE_KEYWORDS.search("Purchase Order") is None

    def test_total_block_matches(self):
        assert TOTAL_BLOCK_KEYWORDS.search("Grand Total: 106200.00") is not None
        assert TOTAL_BLOCK_KEYWORDS.search("Amount Due: 104400.00") is not None
        assert TOTAL_BLOCK_KEYWORDS.search("Net Payable: 50000") is not None

    def test_bank_detail_matches(self):
        assert BANK_DETAIL_KEYWORDS.search("Bank Name: SBI") is not None
        assert BANK_DETAIL_KEYWORDS.search("IFSC: SBIN0001234") is not None
        assert BANK_DETAIL_KEYWORDS.search("Account No: 1234567890") is not None

    def test_gstin_matches(self):
        # Valid GSTIN format: 2 digits + 5 letters + 4 digits + 1 letter + Z + 1 alphanumeric
        assert GSTIN_PATTERN.search("27AABCU9603R1ZM") is not None
        assert GSTIN_PATTERN.search("09AADCB2230M1ZT") is not None

    def test_gstin_rejects_invalid(self):
        assert GSTIN_PATTERN.search("12345") is None
        assert GSTIN_PATTERN.search("INVALID") is None


class TestPageSignals:
    """Test signal extraction from page images."""

    @patch('app.tools.boundary_detector._lightweight_ocr')
    def test_header_page_detected(self, mock_ocr):
        """Page with invoice keyword + number should be classified as HEADER."""
        mock_ocr.return_value = "TAX INVOICE\nInvoice No: INV-001\nDate: 2026-01-15"
        img = np.zeros((100, 100, 3), dtype=np.uint8)
        
        signals = _extract_page_signals(img, 0)
        
        assert signals.has_invoice_keyword is True
        assert signals.invoice_number == "INV-001"
        assert signals.page_type == PageType.HEADER
        assert signals.confidence >= 0.7

    @patch('app.tools.boundary_detector._lightweight_ocr')
    def test_total_block_page(self, mock_ocr):
        """Page with grand total should be detected."""
        mock_ocr.return_value = "Subtotal: 90000\nTax: 16200\nGrand Total: 106200"
        img = np.zeros((100, 100, 3), dtype=np.uint8)
        
        signals = _extract_page_signals(img, 0)
        
        assert signals.has_total_block is True
        assert signals.page_type == PageType.BODY

    @patch('app.tools.boundary_detector._lightweight_ocr')
    def test_bank_details_page(self, mock_ocr):
        """Page with bank details should be detected."""
        mock_ocr.return_value = "Bank Name: HDFC\nIFSC: HDFC0001234\nAccount: 1234567890"
        img = np.zeros((100, 100, 3), dtype=np.uint8)
        
        signals = _extract_page_signals(img, 0)
        
        assert signals.has_bank_details is True
        assert signals.page_type == PageType.BODY

    @patch('app.tools.boundary_detector._lightweight_ocr')
    def test_cover_page_detected(self, mock_ocr):
        """Cover sheet should be classified as COVER."""
        mock_ocr.return_value = "Cover Sheet\nFrom: accounts@company.com\nSubject: Monthly Invoices"
        img = np.zeros((100, 100, 3), dtype=np.uint8)
        
        signals = _extract_page_signals(img, 0)
        is_cover = _is_cover_page(signals)
        
        assert is_cover is True

    @patch('app.tools.boundary_detector._lightweight_ocr')
    def test_empty_page(self, mock_ocr):
        """Blank page should be classified as UNRELATED."""
        mock_ocr.return_value = ""
        img = np.zeros((100, 100, 3), dtype=np.uint8)
        
        signals = _extract_page_signals(img, 0)
        
        assert signals.page_type == PageType.UNRELATED
        assert signals.confidence == 0.9


class TestBoundaryDetection:
    """Test full boundary detection pipeline."""

    def test_single_page_returns_one_invoice(self):
        """Single page should always return one invoice."""
        img = np.zeros((100, 100, 3), dtype=np.uint8)
        
        with patch('app.tools.boundary_detector._lightweight_ocr') as mock_ocr:
            mock_ocr.return_value = "TAX INVOICE\nInvoice No: INV-001"
            invoices = detect_boundaries([img])
        
        assert len(invoices) == 1
        assert invoices[0].page_indices == [0]
        assert invoices[0].confidence == 1.0

    def test_empty_pages_returns_empty(self):
        """No pages should return empty list."""
        invoices = detect_boundaries([])
        assert invoices == []

    @patch('app.tools.boundary_detector._lightweight_ocr')
    def test_two_separate_invoices(self, mock_ocr):
        """Two pages with different invoice numbers should split into 2 invoices."""
        # Page 0: First invoice header
        # Page 1: Second invoice header
        mock_ocr.side_effect = [
            "TAX INVOICE\nInvoice No: INV-001\nGrand Total: 50000",
            "TAX INVOICE\nInvoice No: INV-002\nGrand Total: 75000",
        ]
        
        page0 = np.zeros((100, 100, 3), dtype=np.uint8)
        page1 = np.zeros((100, 100, 3), dtype=np.uint8)
        
        invoices = detect_boundaries([page0, page1])
        
        assert len(invoices) == 2
        assert invoices[0].invoice_number == "INV-001"
        assert invoices[1].invoice_number == "INV-002"

    @patch('app.tools.boundary_detector._lightweight_ocr')
    def test_multi_page_single_invoice(self, mock_ocr):
        """Multi-page invoice (header + body) should stay as one invoice."""
        # Page 0: Invoice header
        # Page 1: Continuation page (line items, no new header)
        mock_ocr.side_effect = [
            "TAX INVOICE\nInvoice No: INV-001\nLine items continue on next page",
            "Item 3: 25000\nItem 4: 30000\nGrand Total: 106200",
        ]
        
        page0 = np.zeros((100, 100, 3), dtype=np.uint8)
        page1 = np.zeros((100, 100, 3), dtype=np.uint8)
        
        invoices = detect_boundaries([page0, page1])
        
        # Should be 1 invoice (continuation page merges with header)
        assert len(invoices) == 1
        assert invoices[0].page_indices == [0, 1]
        assert invoices[0].invoice_number == "INV-001"

    @patch('app.tools.boundary_detector._lightweight_ocr')
    def test_cover_page_excluded(self, mock_ocr):
        """Cover page should be separated from invoice pages."""
        mock_ocr.side_effect = [
            "Cover Sheet\nFrom: vendor@company.com\nSubject: August Invoices",
            "TAX INVOICE\nInvoice No: INV-001\nGrand Total: 50000",
        ]
        
        page0 = np.zeros((100, 100, 3), dtype=np.uint8)
        page1 = np.zeros((100, 100, 3), dtype=np.uint8)
        
        invoices = detect_boundaries([page0, page1])
        
        assert len(invoices) == 2
        # First group is cover page
        assert invoices[0].page_indices == [0]
        # Second group is actual invoice
        assert invoices[1].page_indices == [1]
        assert invoices[1].invoice_number == "INV-001"

    @patch('app.tools.boundary_detector._lightweight_ocr')
    def test_single_page_with_no_signals(self, mock_ocr):
        """Single page with no signals should still be accepted (no review needed)."""
        # Single page is always accepted regardless of content
        mock_ocr.return_value = "Some random text without invoice keywords"
        img = np.zeros((100, 100, 3), dtype=np.uint8)
        
        invoices = detect_boundaries([img])
        
        assert len(invoices) == 1
        assert invoices[0].confidence == 1.0
        assert invoices[0].needs_review is False

    @patch('app.tools.boundary_detector._lightweight_ocr')
    def test_gstin_change_triggers_boundary(self, mock_ocr):
        """Different GSTINs should trigger invoice boundary."""
        mock_ocr.side_effect = [
            "TAX INVOICE\nGSTIN: 27AABCU9603R1ZM\nGrand Total: 50000",
            "TAX INVOICE\nGSTIN: 09AADCB2230M1ZT\nGrand Total: 75000",
        ]
        
        page0 = np.zeros((100, 100, 3), dtype=np.uint8)
        page1 = np.zeros((100, 100, 3), dtype=np.uint8)
        
        invoices = detect_boundaries([page0, page1])
        
        assert len(invoices) == 2


class TestEdgeCases:
    """Test edge cases and error handling."""

    @patch('app.tools.boundary_detector._lightweight_ocr')
    def test_ocr_failure_graceful_degradation(self, mock_ocr):
        """OCR failure should not crash boundary detection."""
        mock_ocr.return_value = ""  # Simulate OCR failure
        img = np.zeros((100, 100, 3), dtype=np.uint8)
        
        invoices = detect_boundaries([img, img, img])
        
        # Should still return groups (low confidence)
        assert len(invoices) >= 1
        for inv in invoices:
            assert inv.needs_review is True

    @patch('app.tools.boundary_detector._lightweight_ocr')
    def test_many_pages_performance(self, mock_ocr):
        """50 pages should complete in reasonable time."""
        import time
        
        # Simulate 50 pages with alternating invoice headers
        mock_ocr.side_effect = [
            f"TAX INVOICE\nInvoice No: INV-{i:03d}\nGrand Total: {i * 1000}"
            for i in range(50)
        ]
        
        pages = [np.zeros((100, 100, 3), dtype=np.uint8) for _ in range(50)]
        
        start = time.time()
        invoices = detect_boundaries(pages)
        elapsed = time.time() - start
        
        # Should find ~50 invoices (each page is a new invoice)
        assert len(invoices) == 50
        # Should complete in under 30 seconds
        assert elapsed < 30
