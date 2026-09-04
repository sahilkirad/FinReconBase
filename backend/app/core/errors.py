from enum import Enum
from typing import Optional
from fastapi import HTTPException, status


class Layer1ErrorCode(str, Enum):
    # Guardrail errors (fail fast, before expensive processing)
    UNSUPPORTED_FILE_TYPE = 'UNSUPPORTED_FILE_TYPE'
    FILE_TOO_LARGE = 'FILE_TOO_LARGE'
    INVALID_DOCUMENT_CLASSIFICATION = 'INVALID_DOCUMENT_CLASSIFICATION'
    
    # Processing errors
    BLUR_FAILED = 'BLUR_FAILED'
    OCR_FAILED = 'OCR_FAILED'
    VLM_FAILED = 'VLM_FAILED'
    MATH_CHECKSUM_FAILED = 'MATH_CHECKSUM_FAILED'
    
    # Persistence errors
    DUPLICATE_INVOICE = 'DUPLICATE_INVOICE'
    DATABASE_ERROR = 'DATABASE_ERROR'


class Layer1Exception(HTTPException):
    def __init__(self, error_code: Layer1ErrorCode, message: str, detail: Optional[dict] = None):
        # Map error codes to HTTP status codes
        status_map = {
            Layer1ErrorCode.UNSUPPORTED_FILE_TYPE: status.HTTP_422_UNPROCESSABLE_ENTITY,
            Layer1ErrorCode.FILE_TOO_LARGE: status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            Layer1ErrorCode.INVALID_DOCUMENT_CLASSIFICATION: status.HTTP_422_UNPROCESSABLE_ENTITY,
            Layer1ErrorCode.BLUR_FAILED: status.HTTP_422_UNPROCESSABLE_ENTITY,
            Layer1ErrorCode.OCR_FAILED: status.HTTP_500_INTERNAL_SERVER_ERROR,
            Layer1ErrorCode.VLM_FAILED: status.HTTP_500_INTERNAL_SERVER_ERROR,
            Layer1ErrorCode.MATH_CHECKSUM_FAILED: status.HTTP_422_UNPROCESSABLE_ENTITY,
            Layer1ErrorCode.DUPLICATE_INVOICE: status.HTTP_409_CONFLICT,
            Layer1ErrorCode.DATABASE_ERROR: status.HTTP_500_INTERNAL_SERVER_ERROR,
        }
        
        http_status = status_map.get(error_code, status.HTTP_500_INTERNAL_SERVER_ERROR)
        super().__init__(
            status_code=http_status,
            detail={
                'error_code': error_code.value,
                'message': message,
                'detail': detail or {}
            }
        )


def raise_guardrail_error(error_code: Layer1ErrorCode, message: str, detail: Optional[dict] = None) -> None:
    raise Layer1Exception(error_code, message, detail)
