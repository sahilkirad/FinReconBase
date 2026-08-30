"""
OCR Text Extraction Engine

Extracts raw text strings + word-level confidence from Path A binarized images.
Production: Tesseract OCR (pytesseract + tesseract-ocr system package).
"""

import logging

import numpy as np

from app.core.config import get_settings

logger = logging.getLogger(__name__)


def extract_text_tesseract(image: np.ndarray) -> tuple[str, float]:
    """Run Tesseract OCR on a binarized image.

    Returns (raw_text, average_confidence).
    Raises RuntimeError if pytesseract is not installed.
    """
    try:
        import pytesseract
    except ImportError:
        raise RuntimeError(
            "pytesseract not installed. "
            "Install via: pip install pytesseract && apt-get install tesseract-ocr"
        )

    try:
        data = pytesseract.image_to_data(image, output_type=pytesseract.Output.DICT)
        texts = []
        confidences = []
        for i, conf in enumerate(data["conf"]):
            if int(conf) > 0:  # Tesseract uses -1 for non-text blocks
                texts.append(data["text"][i])
                confidences.append(float(conf))
        raw_text = " ".join(texts)
        avg_confidence = sum(confidences) / len(confidences) if confidences else 0.0
        logger.info(
            "OCR extraction complete",
            extra={
                "text_length": len(raw_text),
                "confidence": avg_confidence,
                "word_count": len(texts),
            },
        )
        return raw_text, avg_confidence
    except Exception as e:
        logger.error("Tesseract OCR failed", extra={"error": str(e)})
        raise RuntimeError(f"Tesseract OCR failed: {e}") from e


def extract_text(image: np.ndarray) -> tuple[str, float]:
    """Main entry point - routes to the configured OCR engine."""
    settings = get_settings()
    if settings.ocr_engine == "tesseract":
        return extract_text_tesseract(image)
    else:
        raise RuntimeError(
            f"Unsupported OCR engine: {settings.ocr_engine}. "
            f"Only 'tesseract' is supported."
        )
