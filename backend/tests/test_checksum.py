"""
Tests for Mathematical Checksum Validation

Tests cover:
- Line item validation
- Financial summary validation
- Float rejection
- Edge cases
"""

import pytest
from decimal import Decimal

from app.tools.checksum import (
    _to_decimal,
    validate_line_items,
    validate_financial_summary,
    run_checksum,
)


class TestDecimalConversion:
    """Test Decimal conversion safety."""

    def test_int_to_decimal(self):
        """Integer should convert to Decimal correctly."""
        result = _to_decimal(100)
        assert result == Decimal("100")

    def test_string_to_decimal(self):
        """String should convert to Decimal correctly."""
        result = _to_decimal("100.50")
        assert result == Decimal("100.50")

    def test_decimal_passthrough(self):
        """Decimal should pass through unchanged."""
        d = Decimal("100.50")
        result = _to_decimal(d)
        assert result == d

    def test_float_raises_error(self):
        """Float should raise ValueError to prevent precision loss."""
        with pytest.raises(ValueError) as exc_info:
            _to_decimal(100.50)
        assert "Float detected" in str(exc_info.value)

    def test_invalid_type_raises_error(self):
        """Invalid type should raise ValueError."""
        with pytest.raises(ValueError):
            _to_decimal([1, 2, 3])


class TestLineItemValidation:
    """Test line item math validation."""

    def test_valid_line_item(self):
        """Valid line item should pass validation."""
        items = [{
            "line_number": 1,
            "quantity": "10",
            "unit_price_paise": 10000,
            "taxable_value_paise": 100000,
            "igst_paise": 18000,
            "cgst_paise": 0,
            "sgst_paise": 0,
            "total_paise": 118000,
        }]
        errors = validate_line_items(items)
        assert errors == []

    def test_invalid_taxable_value(self):
        """Line item with wrong taxable value should fail."""
        items = [{
            "line_number": 1,
            "quantity": "10",
            "unit_price_paise": 10000,
            "taxable_value_paise": 99999,  # Wrong! Should be 100000
            "igst_paise": 18000,
            "cgst_paise": 0,
            "sgst_paise": 0,
            "total_paise": 118000,
        }]
        errors = validate_line_items(items)
        assert len(errors) == 2
        assert "taxable_value_paise" in errors[0]

    def test_invalid_total(self):
        """Line item with wrong total should fail."""
        items = [{
            "line_number": 1,
            "quantity": "10",
            "unit_price_paise": 10000,
            "taxable_value_paise": 100000,
            "igst_paise": 18000,
            "cgst_paise": 0,
            "sgst_paise": 0,
            "total_paise": 99999,  # Wrong! Should be 118000
        }]
        errors = validate_line_items(items)
        assert len(errors) == 1
        assert "total_paise" in errors[0]


class TestFinancialSummaryValidation:
    """Test financial summary validation."""

    def test_valid_summary(self):
        """Valid financial summary should pass."""
        summary = {
            "subtotal_paise": 100000,
            "total_tax_paise": 18000,
            "total_igst_paise": 18000,
            "total_cgst_paise": 0,
            "total_sgst_paise": 0,
            "tds_deduction_paise": 0,
            "other_charges_paise": 0,
            "discount_paise": 0,
            "rounding_adjustment_paise": 0,
            "grand_total_paise": 118000,
        }
        items = [{
            "line_number": 1,
            "taxable_value_paise": 100000,
            "igst_paise": 18000,
            "cgst_paise": 0,
            "sgst_paise": 0,
        }]
        errors = validate_financial_summary(summary, items)
        assert errors == []

    def test_invalid_grand_total(self):
        """Wrong grand total should fail."""
        summary = {
            "subtotal_paise": 100000,
            "total_tax_paise": 18000,
            "total_igst_paise": 18000,
            "total_cgst_paise": 0,
            "total_sgst_paise": 0,
            "tds_deduction_paise": 0,
            "other_charges_paise": 0,
            "discount_paise": 0,
            "rounding_adjustment_paise": 0,
            "grand_total_paise": 99999,  # Wrong!
        }
        items = [{
            "line_number": 1,
            "taxable_value_paise": 100000,
            "igst_paise": 18000,
            "cgst_paise": 0,
            "sgst_paise": 0,
        }]
        errors = validate_financial_summary(summary, items)
        assert len(errors) >= 1
        assert any("grand_total_paise" in e for e in errors)

    def test_negative_tds_rejected(self):
        """Negative TDS should be rejected."""
        summary = {
            "subtotal_paise": 100000,
            "total_tax_paise": 18000,
            "total_igst_paise": 18000,
            "total_cgst_paise": 0,
            "total_sgst_paise": 0,
            "tds_deduction_paise": -5000,  # Negative!
            "other_charges_paise": 0,
            "discount_paise": 0,
            "rounding_adjustment_paise": 0,
            "grand_total_paise": 113000,
        }
        items = [{
            "line_number": 1,
            "taxable_value_paise": 100000,
            "igst_paise": 18000,
            "cgst_paise": 0,
            "sgst_paise": 0,
        }]
        errors = validate_financial_summary(summary, items)
        assert any("negative" in e.lower() for e in errors)


class TestRunChecksum:
    """Test full checksum pipeline."""

    def test_empty_line_items_fails(self):
        """Invoice with no line items should fail."""
        payload = {"line_items": [], "financial_summary": {}}
        errors = run_checksum(payload)
        assert len(errors) == 1
        assert "No line items" in errors[0]

    def test_valid_invoice_passes(self):
        """Valid invoice should pass checksum."""
        payload = {
            "line_items": [{
                "line_number": 1,
                "quantity": "10",
                "unit_price_paise": 10000,
                "taxable_value_paise": 100000,
                "igst_paise": 18000,
                "cgst_paise": 0,
                "sgst_paise": 0,
                "total_paise": 118000,
            }],
            "financial_summary": {
                "subtotal_paise": 100000,
                "total_tax_paise": 18000,
                "total_igst_paise": 18000,
                "total_cgst_paise": 0,
                "total_sgst_paise": 0,
                "tds_deduction_paise": 0,
                "other_charges_paise": 0,
                "discount_paise": 0,
                "rounding_adjustment_paise": 0,
                "grand_total_paise": 118000,
            }
        }
        errors = run_checksum(payload)
        assert errors == []
