"""
CSV Parser for Bulk Invoice Ingestion

Parses CSV files containing invoice data and validates each row.
Converts valid rows to ExtractedInvoicePayload schema.
"""

import csv
import io
import logging
from datetime import date, datetime
from typing import Optional

from app.schemas.invoice import ExtractedInvoicePayload, LineItem, FinancialSummary

logger = logging.getLogger(__name__)


# Required columns for invoice CSV
REQUIRED_COLUMNS = [
    "invoice_number",
    "supplier_name",
    "buyer_name",
    "invoice_date",
    "total_amount",
]

# Optional columns with defaults
OPTIONAL_COLUMNS = {
    "supplier_gstin": None,
    "buyer_gstin": None,
    "due_date": None,
    "description": "Services",
    "quantity": "1",
    "unit_price": None,
    "hsn_sac_code": None,
    "cgst": "0",
    "sgst": "0",
    "igst": "0",
    "tds_amount": "0",
    "po_number": None,
    "currency": "INR",
}


def parse_csv(file_content: str | bytes) -> list[dict]:
    """
    Parse CSV content into a list of row dicts.
    
    Args:
        file_content: CSV file content as string or bytes
    
    Returns:
        List of dicts, one per row
    """
    if isinstance(file_content, bytes):
        file_content = file_content.decode("utf-8-sig")  # Handle BOM
    
    reader = csv.DictReader(io.StringIO(file_content))
    rows = []
    
    for row_num, row in enumerate(reader, start=2):  # Start at 2 (header is row 1)
        # Strip whitespace from all values
        cleaned = {k.strip(): v.strip() if v else "" for k, v in row.items()}
        cleaned["_row_number"] = row_num
        rows.append(cleaned)
    
    logger.info(f"Parsed {len(rows)} rows from CSV")
    return rows


def validate_row(row: dict, row_num: int) -> list[str]:
    """
    Validate a single CSV row.
    
    Args:
        row: Row data dict
        row_num: Row number (for error messages)
    
    Returns:
        List of error messages (empty if valid)
    """
    errors = []
    
    # Check required columns
    for col in REQUIRED_COLUMNS:
        if not row.get(col):
            errors.append(f"Row {row_num}: Missing required field '{col}'")
    
    # Validate invoice_number format
    invoice_num = row.get("invoice_number", "")
    if invoice_num and len(invoice_num) > 50:
        errors.append(f"Row {row_num}: Invoice number too long (max 50 chars)")
    
    # Validate total_amount is numeric
    total_amount = row.get("total_amount", "")
    if total_amount:
        try:
            amount = float(total_amount)
            if amount < 0:
                errors.append(f"Row {row_num}: Total amount cannot be negative")
        except ValueError:
            errors.append(f"Row {row_num}: Invalid total_amount '{total_amount}'")
    
    # Validate invoice_date format
    invoice_date = row.get("invoice_date", "")
    if invoice_date:
        try:
            if len(invoice_date) == 10:  # YYYY-MM-DD
                datetime.strptime(invoice_date, "%Y-%m-%d")
            elif len(invoice_date) == 10:  # DD/MM/YYYY
                datetime.strptime(invoice_date, "%d/%m/%Y")
            else:
                errors.append(f"Row {row_num}: Invalid date format '{invoice_date}' (use YYYY-MM-DD)")
        except ValueError:
            errors.append(f"Row {row_num}: Invalid date '{invoice_date}'")
    
    # Validate GSTIN format if provided
    gstin = row.get("supplier_gstin", "")
    if gstin and len(gstin) != 15:
        errors.append(f"Row {row_num}: Invalid GSTIN length (should be 15 chars)")
    
    return errors


def to_extracted_payload(row: dict) -> ExtractedInvoicePayload:
    """
    Convert a validated CSV row to ExtractedInvoicePayload schema.
    
    Args:
        row: Validated row data
    
    Returns:
        ExtractedInvoicePayload instance
    """
    # Parse amounts (convert from rupees to paise)
    total_amount = float(row.get("total_amount", 0))
    total_paise = int(total_amount * 100)
    
    cgst = float(row.get("cgst", 0))
    sgst = float(row.get("sgst", 0))
    igst = float(row.get("igst", 0))
    tds = float(row.get("tds_amount", 0))
    
    cgst_paise = int(cgst * 100)
    sgst_paise = int(sgst * 100)
    igst_paise = int(igst * 100)
    tds_paise = int(tds * 100)
    
    # Calculate subtotal (total - tax)
    tax_paise = cgst_paise + sgst_paise + igst_paise
    subtotal_paise = total_paise - tax_paise
    
    # Parse date
    invoice_date_str = row.get("invoice_date", "")
    if len(invoice_date_str) == 10:
        try:
            invoice_date = datetime.strptime(invoice_date_str, "%Y-%m-%d").date()
        except ValueError:
            invoice_date = datetime.strptime(invoice_date_str, "%d/%m/%Y").date()
    else:
        invoice_date = date.today()
    
    # Parse due date if provided
    due_date_str = row.get("due_date", "")
    due_date = None
    if due_date_str:
        try:
            due_date = datetime.strptime(due_date_str, "%Y-%m-%d").date()
        except ValueError:
            try:
                due_date = datetime.strptime(due_date_str, "%d/%m/%Y").date()
            except ValueError:
                pass
    
    # Build line item
    quantity = row.get("quantity", "1")
    unit_price = float(row.get("unit_price", total_amount)) if row.get("unit_price") else total_amount
    unit_price_paise = int(unit_price * 100)
    
    line_item = LineItem(
        line_number=1,
        description=row.get("description", "Services"),
        hsn_sac_code=row.get("hsn_sac_code"),
        quantity=quantity,
        unit="NOS",
        unit_price_paise=unit_price_paise,
        taxable_value_paise=subtotal_paise,
        gst_rate="18.0",
        igst_paise=igst_paise,
        cgst_paise=cgst_paise,
        sgst_paise=sgst_paise,
        total_paise=total_paise,
    )
    
    # Build financial summary
    financial_summary = FinancialSummary(
        subtotal_paise=subtotal_paise,
        total_tax_paise=tax_paise,
        total_igst_paise=igst_paise,
        total_cgst_paise=cgst_paise,
        total_sgst_paise=sgst_paise,
        tds_deduction_paise=tds_paise,
        other_charges_paise=0,
        discount_paise=0,
        rounding_adjustment_paise=0,
        grand_total_paise=total_paise,
    )
    
    # Build payload
    payload = ExtractedInvoicePayload(
        metadata={"source": "csv_upload", "row_number": row.get("_row_number")},
        supplier_details={
            "legal_name": row.get("supplier_name", "Unknown"),
            "gstin": row.get("supplier_gstin"),
            "pan": None,
            "address": None,
            "state_code": None,
            "state_name": None,
            "phone": None,
            "email": None,
        },
        buyer_details={
            "legal_name": row.get("buyer_name"),
            "gstin": row.get("buyer_gstin"),
            "pan": None,
            "address": None,
            "state_code": None,
            "state_name": None,
            "phone": None,
            "email": None,
        },
        reference_data={
            "invoice_number": row.get("invoice_number"),
            "document_type_code": "INV",
            "po_number": row.get("po_number"),
            "grn_number": None,
            "document_date": invoice_date.isoformat(),
            "due_date": due_date.isoformat() if due_date else None,
            "irn": None,
        },
        banking_details={
            "bank_name": None,
            "account_number": None,
            "ifsc": None,
            "upi_id": None,
            "account_number_masked": None,
        },
        line_items=[line_item],
        financial_summary=financial_summary,
    )
    
    return payload


def validate_csv(file_content: str | bytes) -> tuple[list[dict], list[str], list[str]]:
    """
    Validate entire CSV file.
    
    Args:
        file_content: CSV file content
    
    Returns:
        Tuple of (valid_rows, errors, duplicate_invoice_numbers)
    """
    rows = parse_csv(file_content)
    
    all_errors = []
    valid_rows = []
    seen_invoices = set()
    duplicates = []
    
    for row in rows:
        row_num = row.get("_row_number", 0)
        errors = validate_row(row, row_num)
        
        # Check for duplicates
        invoice_num = row.get("invoice_number", "")
        if invoice_num in seen_invoices:
            duplicates.append(invoice_num)
            errors.append(f"Row {row_num}: Duplicate invoice number '{invoice_num}'")
        
        if errors:
            all_errors.extend(errors)
        else:
            valid_rows.append(row)
            seen_invoices.add(invoice_num)
    
    return valid_rows, all_errors, duplicates
