"""
Batch Processing Schemas

Pydantic models for batch invoice upload API requests and responses.
"""

from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, Field


class BatchUploadResponse(BaseModel):
    """Response after uploading a batch of invoices."""
    batch_id: UUID
    vendor_code: str
    source_type: str  # 'pdf' (both PDF and image batches; CSV support removed)
    filename: Optional[str] = None
    total_invoices: int
    valid_invoices: int
    invalid_invoices: int
    status: str  # PENDING, VALIDATING, PROCESSING
    validation_summary: Optional[dict] = None
    message: str


class BatchStatusResponse(BaseModel):
    """Response for batch status query."""
    batch_id: UUID
    vendor_code: str
    source_type: str
    filename: Optional[str] = None
    total_invoices: int
    processed_count: int
    failed_count: int
    status: str
    created_at: datetime
    updated_at: datetime
    completed_at: Optional[datetime] = None
    progress_percent: float = 0.0


class BatchInvoiceItemResponse(BaseModel):
    """Response for a single invoice item in a batch."""
    id: UUID
    document_id: Optional[UUID] = None
    row_number: Optional[int] = None
    invoice_number: Optional[str] = None
    status: str
    error_message: Optional[str] = None
    processing_time_ms: Optional[int] = None


class BatchInvoicesResponse(BaseModel):
    """Response listing all invoices in a batch."""
    batch_id: UUID
    total_items: int
    items: list[BatchInvoiceItemResponse]


class BatchErrorResponse(BaseModel):
    """Error response for batch operations."""
    error_code: str
    message: str
    detail: Optional[dict] = None


class CSVValidationRow(BaseModel):
    """Validation result for a single CSV row."""
    row_number: int
    is_valid: bool
    errors: list[str] = []
    invoice_number: Optional[str] = None


class CSVValidationSummary(BaseModel):
    """Summary of CSV validation results."""
    total_rows: int
    valid_rows: int
    invalid_rows: int
    duplicate_rows: int
    errors: list[CSVValidationRow] = []
