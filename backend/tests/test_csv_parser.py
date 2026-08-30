"""
Tests for CSV Parser

Tests cover:
- CSV parsing
- Row validation
- Conversion to ExtractedInvoicePayload
- Duplicate detection
"""

import pytest
from datetime import date

from app.tools.csv_parser import (
    parse_csv,
    validate_row,
    to_extracted_payload,
    validate_csv,
)


VALID_CSV_HEADER = "invoice_number,supplier_name,supplier_gstin,buyer_name,invoice_date,due_date,description,quantity,unit_price,total_amount,cgst,sgst,igst,tds_amount,po_number"

VALID_CSV_ROW = "INV-001,Nexus Logistics Pvt Ltd,27AABCU9603R1ZM,Acme Corp,2026-08-15,2026-09-15,Freight Services,1,90000.00,106200.00,8100.00,8100.00,0.00,1800.00,PO-2026-001"


class TestCSVParsing:
    """Test CSV file parsing."""

    def test_parse_valid_csv(self):
        """Valid CSV should parse correctly."""
        csv_content = f"{VALID_CSV_HEADER}\n{VALID_CSV_ROW}"
        rows = parse_csv(csv_content)
        
        assert len(rows) == 1
        assert rows[0]["invoice_number"] == "INV-001"
        assert rows[0]["supplier_name"] == "Nexus Logistics Pvt Ltd"
        assert rows[0]["total_amount"] == "106200.00"

    def test_parse_multiple_rows(self):
        """Multiple rows should all be parsed."""
        row2 = "INV-002,Transport Co,09AADCB2230M1ZT,Acme Corp,2026-08-16,,Local Transport,2,25000.00,59000.00,4500.00,4500.00,0.00,0.00,"
        csv_content = f"{VALID_CSV_HEADER}\n{VALID_CSV_ROW}\n{row2}"
        rows = parse_csv(csv_content)
        
        assert len(rows) == 2
        assert rows[1]["invoice_number"] == "INV-002"

    def test_parse_empty_csv(self):
        """Empty CSV should return empty list."""
        csv_content = VALID_CSV_HEADER
        rows = parse_csv(csv_content)
        assert rows == []

    def test_parse_bytes_input(self):
        """Bytes input should be handled correctly."""
        csv_content = f"{VALID_CSV_HEADER}\n{VALID_CSV_ROW}".encode("utf-8")
        rows = parse_csv(csv_content)
        assert len(rows) == 1


class TestRowValidation:
    """Test individual row validation."""

    def test_valid_row(self):
        """Valid row should have no errors."""
        row = {
            "invoice_number": "INV-001",
            "supplier_name": "Nexus Logistics",
            "buyer_name": "Acme Corp",
            "invoice_date": "2026-08-15",
            "total_amount": "106200.00",
        }
        errors = validate_row(row, 2)
        assert errors == []

    def test_missing_invoice_number(self):
        """Missing invoice number should fail."""
        row = {
            "invoice_number": "",
            "supplier_name": "Nexus Logistics",
            "buyer_name": "Acme Corp",
            "invoice_date": "2026-08-15",
            "total_amount": "106200.00",
        }
        errors = validate_row(row, 2)
        assert len(errors) == 1
        assert "invoice_number" in errors[0]

    def test_missing_supplier_name(self):
        """Missing supplier name should fail."""
        row = {
            "invoice_number": "INV-001",
            "supplier_name": "",
            "buyer_name": "Acme Corp",
            "invoice_date": "2026-08-15",
            "total_amount": "106200.00",
        }
        errors = validate_row(row, 2)
        assert len(errors) == 1
        assert "supplier_name" in errors[0]

    def test_invalid_total_amount(self):
        """Non-numeric total amount should fail."""
        row = {
            "invoice_number": "INV-001",
            "supplier_name": "Nexus Logistics",
            "buyer_name": "Acme Corp",
            "invoice_date": "2026-08-15",
            "total_amount": "abc",
        }
        errors = validate_row(row, 2)
        assert len(errors) == 1
        assert "total_amount" in errors[0]

    def test_negative_total_amount(self):
        """Negative total amount should fail."""
        row = {
            "invoice_number": "INV-001",
            "supplier_name": "Nexus Logistics",
            "buyer_name": "Acme Corp",
            "invoice_date": "2026-08-15",
            "total_amount": "-100",
        }
        errors = validate_row(row, 2)
        assert len(errors) == 1
        assert "negative" in errors[0].lower()

    def test_invalid_date_format(self):
        """Invalid date format should fail."""
        row = {
            "invoice_number": "INV-001",
            "supplier_name": "Nexus Logistics",
            "buyer_name": "Acme Corp",
            "invoice_date": "15-08-2026",
            "total_amount": "106200.00",
        }
        errors = validate_row(row, 2)
        assert len(errors) == 1
        assert "date" in errors[0].lower()

    def test_invalid_gstin_length(self):
        """GSTIN with wrong length should fail."""
        row = {
            "invoice_number": "INV-001",
            "supplier_name": "Nexus Logistics",
            "buyer_name": "Acme Corp",
            "invoice_date": "2026-08-15",
            "total_amount": "106200.00",
            "supplier_gstin": "12345",
        }
        errors = validate_row(row, 2)
        assert len(errors) == 1
        assert "GSTIN" in errors[0]

    def test_multiple_errors(self):
        """Multiple validation errors should all be reported."""
        row = {
            "invoice_number": "",
            "supplier_name": "",
            "buyer_name": "",
            "invoice_date": "",
            "total_amount": "abc",
        }
        errors = validate_row(row, 2)
        assert len(errors) >= 3


class TestPayloadConversion:
    """Test conversion to ExtractedInvoicePayload."""

    def test_convert_valid_row(self):
        """Valid row should convert to valid payload."""
        row = {
            "invoice_number": "INV-001",
            "supplier_name": "Nexus Logistics Pvt Ltd",
            "supplier_gstin": "27AABCU9603R1ZM",
            "buyer_name": "Acme Corp",
            "invoice_date": "2026-08-15",
            "due_date": "2026-09-15",
            "description": "Freight Services",
            "quantity": "1",
            "unit_price": "90000.00",
            "total_amount": "106200.00",
            "cgst": "8100.00",
            "sgst": "8100.00",
            "igst": "0.00",
            "tds_amount": "1800.00",
            "po_number": "PO-2026-001",
            "_row_number": 2,
        }
        
        payload = to_extracted_payload(row)
        
        assert payload.reference_data.invoice_number == "INV-001"
        assert payload.supplier_details.legal_name == "Nexus Logistics Pvt Ltd"
        assert payload.supplier_details.gstin == "27AABCU9603R1ZM"
        assert payload.buyer_details.legal_name == "Acme Corp"
        assert payload.financial_summary.grand_total_paise == 10620000
        assert payload.financial_summary.tds_deduction_paise == 180000
        assert payload.financial_summary.total_cgst_paise == 810000
        assert payload.financial_summary.total_sgst_paise == 810000
        assert len(payload.line_items) == 1

    def test_convert_minimal_row(self):
        """Minimal row with defaults should convert correctly."""
        row = {
            "invoice_number": "INV-002",
            "supplier_name": "Simple Vendor",
            "buyer_name": "Simple Buyer",
            "invoice_date": "2026-08-16",
            "total_amount": "50000.00",
            "_row_number": 3,
        }
        
        payload = to_extracted_payload(row)
        
        assert payload.reference_data.invoice_number == "INV-002"
        assert payload.financial_summary.grand_total_paise == 5000000
        assert payload.financial_summary.tds_deduction_paise == 0


class TestFullCSVValidation:
    """Test full CSV validation pipeline."""

    def test_validate_valid_csv(self):
        """Valid CSV should have no errors."""
        csv_content = f"{VALID_CSV_HEADER}\n{VALID_CSV_ROW}"
        valid_rows, errors, duplicates = validate_csv(csv_content)
        
        assert len(valid_rows) == 1
        assert errors == []
        assert duplicates == []

    def test_validate_csv_with_duplicates(self):
        """Duplicate invoice numbers should be detected."""
        csv_content = f"{VALID_CSV_HEADER}\n{VALID_CSV_ROW}\n{VALID_CSV_ROW}"
        valid_rows, errors, duplicates = validate_csv(csv_content)
        
        assert len(duplicates) == 1
        assert "INV-001" in duplicates
        # Only first occurrence is valid
        assert len(valid_rows) == 1

    def test_validate_csv_with_errors(self):
        """Invalid rows should be reported."""
        bad_row = ",,,,invalid-date,,,"
        csv_content = f"{VALID_CSV_HEADER}\n{bad_row}"
        valid_rows, errors, duplicates = validate_csv(csv_content)
        
        assert len(valid_rows) == 0
        assert len(errors) > 0
