"""
Tests for PII Masking Middleware

Tests cover:
- PAN masking
- Bank account masking
- IFSC masking
- Invoice payload masking
"""

import pytest

from app.tools.pii_masker import (
    mask_pii,
    mask_invoice_for_llm,
    PAN_PATTERN,
    ACCOUNT_PATTERN,
    IFSC_PATTERN,
)


class TestPIIPatterns:
    """Test PII pattern matching."""

    def test_pan_pattern_matches(self):
        """Should match valid PAN numbers."""
        assert PAN_PATTERN.search("AABCU9603R") is not None
        assert PAN_PATTERN.search("ABCDE1234F") is not None

    def test_pan_pattern_rejects_invalid(self):
        """Should not match invalid PAN numbers."""
        assert PAN_PATTERN.search("AABCU9603") is None  # Too short
        assert PAN_PATTERN.search("AABCU960345") is None  # Too long
        assert PAN_PATTERN.search("1234567890") is None  # All digits

    def test_account_pattern_matches(self):
        """Should match bank account numbers."""
        assert ACCOUNT_PATTERN.search("50100012345678") is not None
        assert ACCOUNT_PATTERN.search("123456789") is not None

    def test_ifsc_pattern_matches(self):
        """Should match IFSC codes."""
        assert IFSC_PATTERN.search("HDFC0001234") is not None
        assert IFSC_PATTERN.search("ICIC0001234") is not None


class TestMaskPII:
    """Test PII masking in strings."""

    def test_masks_pan(self):
        """Should mask PAN numbers in text."""
        text = "PAN: AABCU9603R is registered"
        masked, vault = mask_pii(text)
        assert "AABCU9603R" not in masked
        assert "[PAN_MASKED]" in masked

    def test_masks_account_number(self):
        """Should mask bank account numbers."""
        text = "Account: 50100012345678"
        masked, vault = mask_pii(text)
        assert "50100012345678" not in masked
        assert "[ACCOUNT_MASKED]" in masked

    def test_masks_ifsc(self):
        """Should mask IFSC codes."""
        text = "IFSC: HDFC0001234"
        masked, vault = mask_pii(text)
        assert "HDFC0001234" not in masked
        assert "[IFSC_MASKED]" in masked

    def test_preserves_non_pii(self):
        """Should not mask non-PII text."""
        text = "Invoice number: INV-2026-441"
        masked, vault = mask_pii(text)
        assert masked == text
        assert len(vault) == 0

    def test_vault_enables_rehydration(self):
        """Vault should map masked tokens back to original values."""
        text = "PAN: AABCU9603R"
        masked, vault = mask_pii(text)
        # Find the PAN token
        pan_tokens = [k for k in vault.keys() if "PAN" in k]
        assert len(pan_tokens) == 1
        assert vault[pan_tokens[0]] == "AABCU9603R"


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
        assert masked["banking_details"]["account_number"] == "[ACCOUNT_MASKED]"
        assert masked["banking_details"]["ifsc"] == "[IFSC_MASKED]"
        assert masked["banking_details"]["bank_name"] == "HDFC Bank"  # Not PII

    def test_masks_supplier_pan(self):
        """Should mask PAN in supplier details."""
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
        assert masked["supplier_details"]["pan"] == "[PAN_MASKED]"
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
        assert masked["buyer_details"]["pan"] == "[PAN_MASKED]"

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
        assert masked["banking_details"]["account_number"] == "[ACCOUNT_MASKED]"
        assert masked["supplier_details"]["pan"] == "[PAN_MASKED]"
