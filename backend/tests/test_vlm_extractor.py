"""
Tests for Gemini VLM Semantic Extraction

Tests cover:
- API key validation
- Response parsing
- Error handling
"""

import pytest
import numpy as np
from unittest.mock import patch, MagicMock

from app.tools.vlm_extractor import extract_invoice_json, _image_to_base64


class TestVLMExtraction:
    """Test Gemini VLM extraction."""

    def test_raises_without_api_key(self):
        """Should raise RuntimeError if Gemini API key is not configured."""
        img = np.zeros((100, 100, 3), dtype=np.uint8)

        with patch('app.tools.vlm_extractor.get_settings') as mock_settings:
            mock_settings.return_value.gemini_api_key = "replace-with-gemini-api-key"

            with pytest.raises(RuntimeError) as exc_info:
                extract_invoice_json(img, "test OCR text", "test.pdf")

            assert "Gemini API key not configured" in str(exc_info.value)

    def test_image_to_base64(self):
        """Should convert image to base64 string."""
        img = np.zeros((100, 100, 3), dtype=np.uint8)
        img[50, 50] = [255, 0, 0]  # Red pixel

        b64 = _image_to_base64(img)
        assert isinstance(b64, str)
        assert len(b64) > 0
        # Should be valid base64
        import base64
        decoded = base64.b64decode(b64)
        assert len(decoded) > 0

    def test_extracts_with_mock_gemini(self):
        """Test extraction with mocked Gemini response."""
        img = np.zeros((100, 100, 3), dtype=np.uint8)

        mock_response = MagicMock()
        mock_response.text = '''{
            "metadata": {"source_file": "test.pdf", "page_count": 1, "processing_time_ms": 0},
            "supplier_details": {"legal_name": "Test Corp", "gstin": None, "pan": None, "address": None, "state_code": None, "state_name": None, "phone": None, "email": None},
            "buyer_details": {"legal_name": "Test Buyer", "gstin": None, "pan": None, "address": None, "state_code": None, "state_name": None, "phone": None, "email": None},
            "reference_data": {"invoice_number": "INV-001", "document_type_code": "INV", "po_number": None, "grn_number": None, "document_date": "2026-08-29", "due_date": None, "irn": None},
            "banking_details": {"bank_name": None, "account_number": None, "ifsc": None, "upi_id": None, "account_number_masked": None},
            "line_items": [{"line_number": 1, "description": "Test item", "hsn_sac_code": None, "quantity": "1.0", "unit": "NOS", "unit_price_paise": 100000, "taxable_value_paise": 100000, "gst_rate": "18.0", "igst_paise": 18000, "cgst_paise": 0, "sgst_paise": 0, "total_paise": 118000}],
            "financial_summary": {"subtotal_paise": 100000, "total_tax_paise": 18000, "total_igst_paise": 18000, "total_cgst_paise": 0, "total_sgst_paise": 0, "tds_deduction_paise": 0, "other_charges_paise": 0, "discount_paise": 0, "rounding_adjustment_paise": 0, "grand_total_paise": 118000}
        }'''

        with patch('app.tools.vlm_extractor.get_settings') as mock_settings:
            mock_settings.return_value.gemini_api_key = "real-api-key"
            mock_settings.return_value.gemini_model = "gemini-1.5-flash"

            with patch('google.generativeai.GenerativeModel') as mock_model_class:
                mock_model = MagicMock()
                mock_model.generate_content.return_value = mock_response
                mock_model_class.return_value = mock_model

                result = extract_invoice_json(img, "OCR text", "test.pdf")

                assert result["reference_data"]["invoice_number"] == "INV-001"
                assert result["financial_summary"]["grand_total_paise"] == 118000
