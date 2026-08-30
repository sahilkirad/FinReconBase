"""
Dual-Path OpenCV Preprocessing Pipeline

Path A (OCR): Deskew -> Denoise -> 300 DPI -> Adaptive Binarization
Path B (VLM): Deskew -> Denoise -> Preserve RGB (no binarization)
"""

import cv2
import numpy as np
import logging

logger = logging.getLogger(__name__)

TARGET_DPI = 300


def compute_skew_angle(image: np.ndarray) -> float:
    """Detect document skew angle using Hough Line Transform.

    Returns angle in degrees. Near-zero means already horizontal.
    """
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if len(image.shape) == 3 else image
    edges = cv2.Canny(gray, 50, 150, apertureSize=3)
    lines = cv2.HoughLinesP(edges, 1, np.pi / 180, threshold=100,
                            minLineLength=100, maxLineGap=10)
    if lines is None:
        return 0.0
    angles = []
    for line in lines:
        x1, y1, x2, y2 = line[0]
        angle = np.degrees(np.arctan2(y2 - y1, x2 - x1))
        if abs(angle) < 45:  # only near-horizontal lines
            angles.append(angle)
    if not angles:
        return 0.0
    return float(np.median(angles))


def deskew(image: np.ndarray) -> np.ndarray:
    """Rotate image to correct detected skew."""
    angle = compute_skew_angle(image)
    if abs(angle) < 0.5:
        return image
    h, w = image.shape[:2]
    center = (w // 2, h // 2)
    matrix = cv2.getRotationMatrix2D(center, angle, 1.0)
    rotated = cv2.warpAffine(image, matrix, (w, h),
                             flags=cv2.INTER_CUBIC,
                             borderMode=cv2.BORDER_REPLICATE)
    logger.info("Deskewed image", extra={"angle_degrees": angle})
    return rotated


def denoise(image: np.ndarray) -> np.ndarray:
    """Apply median filtering to remove scanner dust/speckles.

    Median filter preserves edges while removing salt-and-pepper noise.
    Critical: does NOT erase period/decimal characters.
    """
    return cv2.medianBlur(image, 3)


def rescale_to_300_dpi(image: np.ndarray, current_dpi: float = 150.0) -> np.ndarray:
    """Rescale image to target DPI for optimal OCR accuracy."""
    scale_factor = TARGET_DPI / current_dpi
    if abs(scale_factor - 1.0) < 0.05:
        return image
    h, w = image.shape[:2]
    new_w = int(w * scale_factor)
    new_h = int(h * scale_factor)
    return cv2.resize(image, (new_w, new_h), interpolation=cv2.INTER_CUBIC)


def binarize(image: np.ndarray) -> np.ndarray:
    """Adaptive Gaussian binarization for OCR-ready black-and-white output.

    Converts to high-contrast pure B/W pixels for character bounding-box extraction.
    """
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if len(image.shape) == 3 else image
    return cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 10
    )


def preprocess_path_a_ocr(image: np.ndarray) -> np.ndarray:
    """Path A: Full pipeline for deterministic OCR (Tesseract).

    Deskew -> Denoise -> Rescale 300 DPI -> Adaptive Binarization.
    Returns a binarized single-channel image ready for Tesseract.
    """
    processed = deskew(image)
    processed = denoise(processed)
    processed = rescale_to_300_dpi(processed)
    processed = binarize(processed)
    return processed


def preprocess_path_b_vlm(image: np.ndarray) -> np.ndarray:
    """Path B: Pipeline for Gemini VLM / Vision AI.

    Deskew -> Denoise only. Preserves RGB/grayscale anti-aliasing
    so the VLM retains full visual context.
    """
    processed = deskew(image)
    processed = denoise(processed)
    return processed
