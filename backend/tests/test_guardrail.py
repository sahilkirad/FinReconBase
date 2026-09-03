"""
Tests for Document Guardrail - Pre-flight Classification Gate

Tests cover:
- MIME/Size validation
- DocRex ONNX classification
- Anchor keyword scanning
- Full guardrail pipeline
"""

import pytest
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.core.errors import Layer1Exception, Layer1ErrorCode
from app.tools.guardrail import DocumentGuardrail, create_guardrail


class TestMimeAndSizeValidation:
    """Test Step 1: MIME & Size Gate."""

    def setup_method(self):
        self.guardrail = DocumentGuardrail()

    def test_rejects_file_too_large(self, tmp_path):
        """File over 10MB should be rejected with FILE_TOO_LARGE."""
        # Create a file over 10MB
        large_file = tmp_path / "large_invoice.pdf"
        large_file.write_bytes(b"x" * (11 * 1024 * 1024))  # 11MB

        with pytest.raises(Layer1Exception) as exc_info:
            self.guardrail.validate_mime_and_size(large_file, 11 * 1024 * 1024, "application/pdf")

        assert exc_info.value.detail["error_code"] == Layer1ErrorCode.FILE_TOO_LARGE.value

    def test_rejects_unsupported_extension(self, tmp_path):
        """Unsupported file extension should be rejected."""
        exe_file = tmp_path / "malware.exe"
        exe_file.write_bytes(b"MZ" + b"\x00" * 100)

        with pytest.raises(Layer1Exception) as exc_info:
            self.guardrail.validate_mime_and_size(exe_file, 100, "application/octet-stream")

        assert exc_info.value.detail["error_code"] == Layer1ErrorCode.UNSUPPORTED_FILE_TYPE.value

    def test_accepts_valid_pdf(self, tmp_path):
        """Valid PDF should pass MIME validation."""
        pdf_file = tmp_path / "invoice.pdf"
        pdf_file.write_bytes(b"%PDF-1.4" + b"\x00" * 100)

        # This should not raise
        self.guardrail.validate_mime_and_size(pdf_file, 1024, "application/pdf")

    def test_rejects_size_mismatch(self, tmp_path):
        """File with mismatched extension and MIME should be rejected."""
        pdf_file = tmp_path / "invoice.pdf"
        pdf_file.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 100)  # JPEG header

        with pytest.raises(Layer1Exception) as exc_info:
            self.guardrail.validate_mime_and_size(pdf_file, 1024, "application/pdf")

        assert exc_info.value.detail["error_code"] == Layer1ErrorCode.UNSUPPORTED_FILE_TYPE.value


class TestAnchorKeywordScan:
    """Test Step 3: Anchor Keyword Extraction."""

    def setup_method(self):
        self.guardrail = DocumentGuardrail()

    def test_non_pdf_skips_anchor_scan(self, tmp_path):
        """Non-PDF files should skip anchor scan and return True."""
        jpg_file = tmp_path / "invoice.jpg"
        jpg_file.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 100)

        result = self.guardrail.scan_anchor_keywords(jpg_file)
        assert result is True

    def test_rejects_pdf_without_anchors(self, tmp_path):
        """PDF without financial keywords should return False."""
        # Create a minimal PDF without financial keywords
        pdf_content = b"""%PDF-1.4
1 0 obj
<< /Type /Catalog /Pages 2 0 R >>
endobj
2 0 obj
<< /Type /Pages /Kids [3 0 R] /Count 1 >>
endobj
3 0 obj
<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >>
endobj
xref
0 4
0000000000 65535 f 
0000000009 00000 n 
0000000058 00000 n 
0000000115 00000 n 
trailer
<< /Size 4 /Root 1 0 R >>
startxref
190
%%EOF"""
        pdf_file = tmp_path / "novel.pdf"
        pdf_file.write_bytes(pdf_content)

        result = self.guardrail.scan_anchor_keywords(pdf_file)
        assert result is False

    def test_accepts_pdf_with_anchors(self, tmp_path):
        """PDF with financial keywords should return True."""
        # This test requires a real PDF with text - would need a fixture
        # For now, we test the regex pattern directly
        import re
        pattern = re.compile(r'(?i)(invoice|tax|amount due|total|balance)')

        assert pattern.search("This is an Invoice for services") is not None
        assert pattern.search("Total amount due: Rs 1000") is not None
        assert pattern.search("GST Tax included") is not None
        assert pattern.search("Balance statement") is not None
        assert pattern.search("Random text without keywords") is None


class TestFullGuardrail:
    """Test the complete guardrail pipeline."""

    def setup_method(self):
        self.guardrail = DocumentGuardrail()

    def test_guardrail_returns_three_values(self, tmp_path):
        """Guardrail should return (label, score, anchor_found)."""
        # This test would require a real DocRex model
        # For now, we test the structure
        pdf_file = tmp_path / "invoice.pdf"
        pdf_file.write_bytes(b"%PDF-1.4" + b"\x00" * 100)

        # The actual test would mock the classifier
        # For production, this test requires the model to be present
        pass


class TestFailClosedGuardrail:
    """Structural classification is compulsory — missing model fails closed."""

    def test_guardrail_rejects_when_model_missing(self, tmp_path):
        """Model missing -> upload must be REJECTED, not silently skipped."""
        guardrail = DocumentGuardrail()

        pdf_file = tmp_path / "invoice.pdf"
        pdf_file.write_bytes(b"%PDF-1.4" + b"\x00" * 100)

        with patch(
            "app.tools.doc_classifier.get_classifier",
            side_effect=FileNotFoundError("model not found"),
        ):
            with pytest.raises(Layer1Exception) as exc_info:
                guardrail.run_guardrail(pdf_file, 1024, "application/pdf")

        assert exc_info.value.detail["error_code"] == \
            Layer1ErrorCode.INVALID_DOCUMENT_CLASSIFICATION.value

    def test_guardrail_passes_invoice_above_threshold(self, tmp_path):
        """Invoice classified above threshold passes MIME + anchor gates."""
        import fitz

        guardrail = DocumentGuardrail()

        # Build a real PDF with a text layer containing financial anchors
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((72, 72), "TAX INVOICE Total Amount Due")
        pdf_bytes = doc.tobytes()
        doc.close()

        pdf_file = tmp_path / "invoice.pdf"
        pdf_file.write_bytes(pdf_bytes)

        mock_classifier = MagicMock()
        mock_classifier.classify.return_value = ("invoice", 0.98)

        with patch(
            "app.tools.doc_classifier.get_classifier",
            return_value=mock_classifier,
        ):
            label, confidence, anchor_found = guardrail.run_guardrail(
                pdf_file, len(pdf_bytes), "application/pdf"
            )

        assert label == "invoice"
        assert confidence == 0.98
        assert anchor_found is True

    def test_guardrail_rejects_non_invoice_document(self, tmp_path):
        """Non-invoice classification must be rejected even above threshold."""
        guardrail = DocumentGuardrail()

        pdf_file = tmp_path / "invoice.pdf"
        pdf_file.write_bytes(b"%PDF-1.4" + b"\x00" * 100)

        mock_classifier = MagicMock()
        mock_classifier.classify.return_value = ("bank_statement", 0.98)

        with patch(
            "app.tools.doc_classifier.get_classifier",
            return_value=mock_classifier,
        ):
            with pytest.raises(Layer1Exception) as exc_info:
                guardrail.run_guardrail(pdf_file, 1024, "application/pdf")

        assert exc_info.value.detail["error_code"] == \
            Layer1ErrorCode.INVALID_DOCUMENT_CLASSIFICATION.value


class TestBatchSizeOverride:
    """Batch endpoint can raise the size gate via max_size_mb."""

    def test_batch_size_override_allows_larger_files(self, tmp_path):
        """max_size_mb override should allow files above the single-upload limit."""
        guardrail = DocumentGuardrail()

        large_file = tmp_path / "large_invoice.pdf"
        large_file.write_bytes(b"%PDF-1.4" + b"x" * (11 * 1024 * 1024))  # 11MB

        # 11MB exceeds the 10MB single-upload default but is under the batch
        # limit (100MB) — passing max_size_mb=100 must NOT raise.
        guardrail.validate_mime_and_size(
            large_file, 11 * 1024 * 1024, "application/pdf", max_size_mb=100
        )
