"""
Tests for Laplacian Blur Gate

Tests cover:
- Blur score computation
- Auto-sharpening
- Blur gate pass/fail
"""

import pytest
import numpy as np

from app.tools.blur_check import (
    compute_laplacian_variance,
    sharpen_image,
    check_blur,
)


class TestLaplacianVariance:
    """Test blur score computation."""

    def test_sharp_image_has_high_variance(self):
        """A sharp image should have high Laplacian variance."""
        # Create a sharp image with strong edges
        sharp = np.zeros((100, 100), dtype=np.uint8)
        sharp[40:60, 40:60] = 255  # White square on black

        score = compute_laplacian_variance(sharp)
        assert score > 100.0, f"Sharp image score {score} should be > 100"

    def test_blurry_image_has_low_variance(self):
        """A blurry image should have low Laplacian variance."""
        # Create a blurry image (uniform gray)
        blurry = np.full((100, 100), 128, dtype=np.uint8)

        score = compute_laplacian_variance(blurry)
        assert score < 10.0, f"Blurry image score {score} should be < 10"

    def test_handles_grayscale_input(self):
        """Should handle grayscale input correctly."""
        gray = np.random.randint(0, 255, (100, 100), dtype=np.uint8)
        score = compute_laplacian_variance(gray)
        assert isinstance(score, float)
        assert score >= 0.0

    def test_handles_color_input(self):
        """Should handle color (BGR) input correctly."""
        color = np.random.randint(0, 255, (100, 100, 3), dtype=np.uint8)
        score = compute_laplacian_variance(color)
        assert isinstance(score, float)
        assert score >= 0.0


class TestSharpening:
    """Test image sharpening."""

    def test_sharpening_increases_sharpness(self):
        """Sharpening should increase the Laplacian variance."""
        # Create a slightly blurry image
        blurry = np.zeros((100, 100, 3), dtype=np.uint8)
        blurry[40:60, 40:60] = 255
        # Apply Gaussian blur to make it blurry
        import cv2
        blurry = cv2.GaussianBlur(blurry, (15, 15), 5)

        original_score = compute_laplacian_variance(blurry)
        sharpened = sharpen_image(blurry)
        sharpened_score = compute_laplacian_variance(sharpened)

        assert sharpened_score > original_score, "Sharpening should increase sharpness"


class TestBlurCheck:
    """Test the complete blur check pipeline."""

    def test_sharp_image_passes(self):
        """A sharp image should pass the blur gate."""
        sharp = np.zeros((100, 100, 3), dtype=np.uint8)
        sharp[40:60, 40:60] = 255

        score, passed, _ = check_blur(sharp, "test_sharp")
        assert passed is True
        assert score > 0

    def test_blurry_image_fails(self):
        """A very blurry image should fail the blur gate."""
        blurry = np.full((100, 100, 3), 128, dtype=np.uint8)

        score, passed, _ = check_blur(blurry, "test_blurry")
        assert passed is False
        assert score < 100

    def test_returns_processed_image(self):
        """Should return the processed (possibly sharpened) image."""
        sharp = np.zeros((100, 100, 3), dtype=np.uint8)
        sharp[40:60, 40:60] = 255

        _, _, processed = check_blur(sharp, "test")
        assert processed.shape == sharp.shape
        assert processed.dtype == sharp.dtype
