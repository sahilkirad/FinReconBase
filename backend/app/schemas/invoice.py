from datetime import date
from decimal import Decimal
from typing import Annotated, Optional
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


# ---- Request/Response Schemas ----

class UploadResponse(BaseModel):
    document_id: UUID
    status: str  # VALIDATED, EXCEPTION_FLAGGED
    message: str
    invoice_number: Optional[str] = None


class UploadErrorResponse(BaseModel):
    error_code: str
    message: str
    detail: Optional[dict] = None


# ---- Locked Invoice JSON Schema (Extracted from document) ----

class LineItem(BaseModel):
    line_number: int
    description: str
    hsn_sac_code: Optional[str] = None
    quantity: Decimal
    unit: str
    unit_price_paise: int
    taxable_value_paise: int
    gst_rate: Decimal
    igst_paise: int = 0
    cgst_paise: int = 0
    sgst_paise: int = 0
    total_paise: int

    @field_validator('unit_price_paise', 'taxable_value_paise', 'igst_paise', 'cgst_paise', 'sgst_paise', 'total_paise', mode='before')
    @classmethod
    def validate_paise(cls, v):
        if isinstance(v, (int, float, str)):
            return int(v)
        raise ValueError('Paise fields must be integers')


class SupplierDetails(BaseModel):
    legal_name: str
    gstin: Optional[str] = None
    pan: Optional[str] = None
    address: Optional[str] = None
    state_code: Optional[str] = None
    state_name: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None


class BuyerDetails(BaseModel):
    legal_name: Optional[str] = None
    gstin: Optional[str] = None
    pan: Optional[str] = None
    address: Optional[str] = None
    state_code: Optional[str] = None
    state_name: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None


class ReferenceData(BaseModel):
    invoice_number: str
    document_type_code: str  # INV, CRN, DBN, etc.
    po_number: Optional[str] = None
    grn_number: Optional[str] = None
    document_date: date
    due_date: Optional[date] = None
    irn: Optional[str] = None  # Invoice Reference Number


class BankingDetails(BaseModel):
    bank_name: Optional[str] = None
    account_number: Optional[str] = None
    ifsc: Optional[str] = None
    upi_id: Optional[str] = None
    # Sensitive fields masked for LLM context
    account_number_masked: Optional[str] = None


class FinancialSummary(BaseModel):
    subtotal_paise: int
    total_tax_paise: int
    total_igst_paise: int = 0
    total_cgst_paise: int = 0
    total_sgst_paise: int = 0
    tds_deduction_paise: int = 0
    other_charges_paise: int = 0
    discount_paise: int = 0
    rounding_adjustment_paise: int = 0
    grand_total_paise: int

    @field_validator('subtotal_paise', 'total_tax_paise', 'total_igst_paise', 'total_cgst_paise', 
                     'total_sgst_paise', 'tds_deduction_paise', 'other_charges_paise', 
                     'discount_paise', 'rounding_adjustment_paise', 'grand_total_paise', mode='before')
    @classmethod
    def validate_paise(cls, v):
        if isinstance(v, (int, float, str)):
            return int(v)
        raise ValueError('Paise fields must be integers')


class ExtractedInvoicePayload(BaseModel):
    metadata: dict = Field(default_factory=dict)  # source_file, page_count, processing_time_ms, etc.
    supplier_details: SupplierDetails
    buyer_details: BuyerDetails
    reference_data: ReferenceData
    banking_details: BankingDetails
    line_items: list[LineItem]
    financial_summary: FinancialSummary


# ---- Internal Processing Models ----

class ProcessedPage(BaseModel):
    page_index: int
    path_a_image_path: str  # binarized for OCR
    path_b_image_path: str  # RGB for VLM
    ocr_text: Optional[str] = None
    ocr_confidence: Optional[float] = None


class DocumentProcessingContext(BaseModel):
    document_id: UUID
    vendor_code: str
    original_filename: str
    mime_type: str
    file_size_bytes: int
    page_count: int
    pages: list[ProcessedPage]
    classification_label: str
    classification_score: float
    anchor_keywords_found: bool
    blur_score: float
    blur_check_passed: bool
