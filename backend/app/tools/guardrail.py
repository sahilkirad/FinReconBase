"""
Document Guardrail - Pre-flight Classification Gate

Fast-fail validation before expensive OCR/VLM processing:
1. MIME/Extension/Size validation
2. DocRex ONNX structural classification (production ML model)
3. PyMuPDF anchor keyword scan for digital PDFs
"""

import magic
import fitz  # PyMuPDF
from pathlib import Path
from typing import Optional
import re
import logging

from app.core.config import get_settings
from app.core.errors import Layer1ErrorCode, raise_guardrail_error

logger = logging.getLogger(__name__)

# Financial anchor keywords regex (case-insensitive)
FINANCIAL_ANCHOR_PATTERN = re.compile(r'(?i)(invoice|tax|amount due|total|balance)')

# Supported MIME types and extensions
ALLOWED_MIME_TYPES = {
    'application/pdf': ['.pdf'],
    'image/jpeg': ['.jpg', '.jpeg'],
    'image/png': ['.png'],
}

ALLOWED_EXTENSIONS = {'.pdf', '.jpg', '.jpeg', '.png'}


class DocumentGuardrail:
    def __init__(self):
        self.settings = get_settings()

    def validate_mime_and_size(self, file_path: Path, file_size: int, content_type: str) -> None:
        """Step 1: MIME & Size Gate - Instant rejection for invalid files."""
        max_size_bytes = self.settings.max_upload_size_mb * 1024 * 1024
        if file_size > max_size_bytes:
            raise_guardrail_error(
                Layer1ErrorCode.FILE_TOO_LARGE,
                f'File size {file_size} bytes exceeds maximum {max_size_bytes} bytes',
                {'file_size': file_size, 'max_size_mb': self.settings.max_upload_size_mb}
            )

        actual_mime = magic.from_file(str(file_path), mime=True)
        if actual_mime not in ALLOWED_MIME_TYPES:
            raise_guardrail_error(
                Layer1ErrorCode.UNSUPPORTED_FILE_TYPE,
                f'Unsupported file type: {actual_mime}',
                {'actual_mime': actual_mime, 'allowed_mimes': list(ALLOWED_MIME_TYPES.keys())}
            )

        ext = file_path.suffix.lower()
        if ext not in ALLOWED_EXTENSIONS:
            raise_guardrail_error(
                Layer1ErrorCode.UNSUPPORTED_FILE_TYPE,
                f'Unsupported file extension: {ext}',
                {'extension': ext, 'allowed_extensions': list(ALLOWED_EXTENSIONS)}
            )

        expected_exts = ALLOWED_MIME_TYPES.get(actual_mime, [])
        if ext not in expected_exts:
            raise_guardrail_error(
                Layer1ErrorCode.UNSUPPORTED_FILE_TYPE,
                f'File extension {ext} does not match MIME type {actual_mime}',
                {'extension': ext, 'mime_type': actual_mime, 'expected_extensions': expected_exts}
            )

        logger.info('MIME/Size validation passed', extra={
            'file_size': file_size,
            'mime_type': actual_mime,
            'extension': ext
        })

    def scan_anchor_keywords(self, file_path: Path) -> bool:
        """Step 3: Fast Anchor Keyword Extraction for digital PDFs.

        Uses PyMuPDF to extract text layer in ~10ms and scans for
        mandatory financial anchor keywords.
        Returns True if at least one anchor keyword is found.
        """
        if file_path.suffix.lower() != '.pdf':
            return True  # Non-PDFs skip anchor scan

        try:
            doc = fitz.open(str(file_path))
            for page_num in range(min(3, len(doc))):
                page = doc[page_num]
                text = page.get_text()
                if FINANCIAL_ANCHOR_PATTERN.search(text):
                    logger.info('Financial anchor keyword found', extra={
                        'page': page_num,
                        'file': str(file_path)
                    })
                    doc.close()
                    return True
            doc.close()
        except Exception as e:
            logger.warning('Anchor scan failed, treating as no anchors', extra={
                'error': str(e),
                'file': str(file_path)
            })

        return False

    def classify_document_structure(self, file_path: Path) -> tuple[str, float]:
        """Step 2: DocRex ONNX Structural Classification.

        Uses the production DocRex model (MobileNetV3-Small, 98.35% accuracy)
        to classify the document as invoice, bank_statement, or other.

        Returns:
            Tuple of (label, confidence_score).
            label: "invoice", "bank_statement", or "other"
            confidence: 0.0 to 1.0

        Raises:
            Layer1Exception if model is unavailable.
        """
        from app.tools.doc_classifier import get_classifier

        try:
            classifier = get_classifier()
            label, confidence = classifier.classify(file_path)
            return label, confidence
        except FileNotFoundError as e:
            logger.error('DocRex model files missing', extra={'error': str(e)})
            raise_guardrail_error(
                Layer1ErrorCode.INVALID_DOCUMENT_CLASSIFICATION,
                f'Document classifier model not available: {e}',
                {'model_error': str(e)}
            )
        except Exception as e:
            logger.error('DocRex classification failed', extra={'error': str(e)})
            raise_guardrail_error(
                Layer1ErrorCode.INVALID_DOCUMENT_CLASSIFICATION,
                f'Document classification failed: {e}',
                {'classification_error': str(e)}
            )

    def run_guardrail(self, file_path: Path, file_size: int, content_type: str) -> tuple[str, float, bool]:
        """Run complete pre-flight guardrail.

        Returns (classification_label, classification_score, anchor_keywords_found).
        Raises Layer1Exception on failure.
        """
        # Step 1: MIME & Size
        self.validate_mime_and_size(file_path, file_size, content_type)

        # Step 2: DocRex Structural Classification
        label, confidence = self.classify_document_structure(file_path)

        if label != 'invoice' or confidence < self.settings.classification_threshold:
            raise_guardrail_error(
                Layer1ErrorCode.INVALID_DOCUMENT_CLASSIFICATION,
                f'Document classified as "{label}" with confidence {confidence:.2f} '
                f'(threshold: {self.settings.classification_threshold}). '
                f'Expected "invoice" with confidence >= threshold.',
                {
                    'classification_label': label,
                    'classification_score': confidence,
                    'threshold': self.settings.classification_threshold,
                    'expected_label': 'invoice',
                }
            )

        # Step 3: Anchor Keyword Scan (for digital PDFs)
        anchor_found = self.scan_anchor_keywords(file_path)

        if file_path.suffix.lower() == '.pdf' and not anchor_found:
            raise_guardrail_error(
                Layer1ErrorCode.INVALID_DOCUMENT_CLASSIFICATION,
                'No financial anchor keywords found in digital PDF',
                {'anchor_keywords_found': False}
            )

        logger.info('Document guardrail passed', extra={
            'classification_label': label,
            'classification_score': confidence,
            'anchor_keywords_found': anchor_found,
            'file': str(file_path)
        })

        return label, confidence, anchor_found


def create_guardrail() -> DocumentGuardrail:
    return DocumentGuardrail()
