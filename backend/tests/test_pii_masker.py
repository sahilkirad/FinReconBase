"""
Tests for PII Masking Middleware (Microsoft Presidio)

Tests cover:
- PAN masking
- Aadhaar masking
- IFSC masking
- GSTIN masking
- Bank account masking (context-gated)
- Non-PII preservation
- Invoice payload masking (structured fields + raw OCR text)

NOTE: These tests require the Presidio packages and the spaCy model:
    pip install -r requirements.txt
    python -m spacy download en_core_web_sm
"""

import pytest

from app.tools.pii_masker import (
    mask_pii,
    mask_invoice_for_llm,
    PAN_PLACEHOLDER,
    AADHAAR_PLACEHOLDER,
    IFSC_PLACEHOLDER,
    GSTIN_PLACEHOLDER,
    ACCOUNT_PLACEHOLDER,
)


class TestMaskPII:
    """Test PII masking in strings via Presidio."""

    def test_masks_pan(self):
        """Should mask PAN numbers in text."""
        text = "PAN: AABCU9603R is registered"
        masked, vault = mask_pii(text)
        assert "AABCU9603R" not in masked
        assert PAN_PLACEHOLDER in masked
        assert vault.get("IN_PAN_1") == "AABCU9603R"

    def test_masks_aadhaar(self):
        """Should mask Aadhaar numbers (spaced or unspaced)."""
        text = "Aadhaar: 2341 2341 2341"
        masked, vault = mask_pii(text)
        assert "2341 2341 2341" not in masked
        assert AADHAAR_PLACEHOLDER in masked
        assert vault.get("IN_AADHAAR_1") == "2341 2341 2341"

    def test_masks_ifsc(self):
        """Should mask IFSC codes."""
        text = "IFSC: HDFC0001234"
        masked, vault = mask_pii(text)
        assert "HDFC0001234" not in masked
        assert IFSC_PLACEHOLDER in masked
        assert vault.get("IN_IFSC_1") == "HDFC0001234"

    def test_masks_gstin(self):
        """Should mask GSTIN (Indian tax identifier)."""
        text = "GSTIN: 27AABCU9603R1ZM"
        masked, vault = mask_pii(text)
        assert "27AABCU9603R1ZM" not in masked
        assert GSTIN_PLACEHOLDER in masked
        assert vault.get("IN_GSTIN_1") == "27AABCU9603R1ZM"

    def test_masks_account_number_with_context(self):
        """Should mask bank account numbers when context words are present."""
        text = "Account Number: 50100012345678"
        masked, vault = mask_pii(text)
        assert "50100012345678" not in masked
        assert ACCOUNT_PLACEHOLDER in masked
        assert vault.get("BANK_ACCOUNT_1") == "50100012345678"

    def test_does_not_mask_bare_digit_run(self):
        """A bare 9-18 digit run without account context is NOT an account."""
        text = "Grand Total: 215637100 paise"
        masked, vault = mask_pii(text)
        assert "215637100" in masked  # Amount preserved
        assert ACCOUNT_PLACEHOLDER not in masked
        assert "BANK_ACCOUNT" not in vault

    def test_preserves_non_pii(self):
        """Should not mask non-PII text."""
        text = "Invoice number: INV-2026-441"
        masked, vault = mask_pii(text)
        assert masked == text
        assert len(vault) == 0

    def test_vault_enables_rehydration(self):
        """Vault should map indexed tokens back to original values."""
        text = "PAN: AABCU9603R"
        masked, vault = mask_pii(text)
        assert masked != text
        assert vault.get("IN_PAN_1") == "AABCU9603R"


class TestMaskInvoiceForLLM:
    """Test invoice payload masking for LLM context."""

    def test_masks_banking_details(self):
        """Should mask bank account and IFSC in banking details."""
        payload = {
            "banking_details": {
                "bank_name": "HDFC Bank",
                "account_number": "50100012345678",
                "ifsc": "HDFC0001234",
                "upi_id": None,
                "account_number_masked": None,
            }
        }
        masked = mask_invoice_for_llm(payload)
        assert masked["banking_details"]["account_number"] == ACCOUNT_PLACEHOLDER
        assert masked["banking_details"]["ifsc"] == IFSC_PLACEHOLDER
        assert masked["banking_details"]["bank_name"] == "HDFC Bank"  # Not PII

    def test_masks_supplier_pan_and_gstin(self):
        """Should mask PAN and GSTIN in supplier details."""
        payload = {
            "supplier_details": {
                "legal_name": "Test Corp",
                "pan": "AABCU9603R",
                "gstin": "27AABCU9603R1ZM",
            },
            "buyer_details": {},
            "banking_details": {},
        }
        masked = mask_invoice_for_llm(payload)
        assert masked["supplier_details"]["pan"] == PAN_PLACEHOLDER
        assert masked["supplier_details"]["gstin"] == GSTIN_PLACEHOLDER
        assert masked["supplier_details"]["legal_name"] == "Test Corp"  # Not PII

    def test_masks_buyer_pan(self):
        """Should mask PAN in buyer details."""
        payload = {
            "supplier_details": {},
            "buyer_details": {
                "legal_name": "Buyer Corp",
                "pan": "AACCA1234F",
            },
            "banking_details": {},
        }
        masked = mask_invoice_for_llm(payload)
        assert masked["buyer_details"]["pan"] == PAN_PLACEHOLDER

    def test_masks_raw_ocr_text(self):
        """Should run raw OCR text through the Presidio pipeline."""
        payload = {"ocr_text": "PAN: AABCU9603R, IFSC: HDFC0001234"}
        masked = mask_invoice_for_llm(payload)
        assert "AABCU9603R" not in masked["ocr_text"]
        assert "HDFC0001234" not in masked["ocr_text"]
        assert PAN_PLACEHOLDER in masked["ocr_text"]
        assert IFSC_PLACEHOLDER in masked["ocr_text"]

    def test_preserves_original_payload(self):
        """Should not modify the original payload."""
        payload = {
            "banking_details": {"account_number": "50100012345678"},
            "supplier_details": {"pan": "AABCU9603R"},
        }
        masked = mask_invoice_for_llm(payload)
        # Original should be unchanged
        assert payload["banking_details"]["account_number"] == "50100012345678"
        assert payload["supplier_details"]["pan"] == "AABCU9603R"
        # Masked should be different
        assert masked["banking_details"]["account_number"] == ACCOUNT_PLACEHOLDER
        assert masked["supplier_details"]["pan"] == PAN_PLACEHOLDER
