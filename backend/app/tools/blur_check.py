"""
Laplacian Blur Gate - Step 2 of Layer 1 Pipeline

Evaluates image sharpness using Laplacian variance.
If variance < threshold, attempts auto-sharpening.
If sharpening fails to resolve, returns BLUR_FAILED.
"""

import cv2
import numpy as np
import logging

from app.core.config import get_settings

logger = logging.getLogger(__name__)


def compute_laplacian_variance(image: np.ndarray) -> float:
    """Compute Laplacian variance as a sharpness metric.

    A blurred image has low variance (flat gradient).
    A sharp image has high variance (strong edges).
    """
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if len(image.shape) == 3 else image
    laplacian = cv2.Laplacian(gray, cv2.CV_64F)
    return float(laplacian.var())


def sharpen_image(image: np.ndarray) -> np.ndarray:
    """Attempt to recover a blurred image using Gaussian unsharp masking.

    Process: blur original -> subtract from original -> scale difference.
    This amplifies high-frequency edge content.
    """
    blurred = cv2.GaussianBlur(image, (0, 0), sigmaX=3)
    sharpened = cv2.addWeighted(image, 1.5, blurred, -0.5, 0)
    return sharpened


def check_blur(image: np.ndarray, source_label: str = "") -> tuple[float, bool, np.ndarray]:
    """Run blur detection + optional sharpening on a single page image.

    Returns:
        (blur_score, passed, processed_image)
        - blur_score: float Laplacian variance
        - passed: True if image passes blur gate
        - processed_image: original or sharpened image
    """
    settings = get_settings()
    threshold = settings.blur_threshold
    sharpen_enabled = settings.blur_sharpen_enabled

    score = compute_laplacian_variance(image)
    logger.info(
        "Blur score computed",
        extra={"score": score, "threshold": threshold, "source": source_label},
    )

    if score >= threshold:
        return score, True, image

    # Below threshold - attempt sharpening
    if sharpen_enabled:
        sharpened = sharpen_image(image)
        sharpened_score = compute_laplacian_variance(sharpened)
        logger.info(
            "Sharpening attempted",
            extra={
                "original_score": score,
                "sharpened_score": sharpened_score,
                "source": source_label,
            },
        )
        if sharpened_score >= threshold:
            return sharpened_score, True, sharpened

    # Still below threshold after sharpening (or sharpening disabled)
    return score, False, image
