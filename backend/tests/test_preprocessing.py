"""
Tests for Dual-Path OpenCV Preprocessing Pipeline

Tests cover:
- Skew detection and correction
- Denoising
- Path A (binarized for OCR)
- Path B (RGB for VLM)
"""

import pytest
import numpy as np

from app.tools.preprocessing import (
    compute_skew_angle,
    deskew,
    denoise,
    rescale_to_300_dpi,
    binarize,
    preprocess_path_a_ocr,
    preprocess_path_b_vlm,
)


class TestSkewDetection:
    """Test document skew angle detection."""

    def test_straight_image_has_zero_angle(self):
        """A perfectly straight image should have near-zero skew angle."""
        # Create a straight horizontal line
        straight = np.zeros((100, 200), dtype=np.uint8)
        straight[49:51, :] = 255  # Horizontal line

        angle = compute_skew_angle(straight)
        assert abs(angle) < 5.0, f"Straight image angle {angle} should be near 0"

    def test_handles_empty_image(self):
        """Should handle image with no detectable lines."""
        empty = np.zeros((100, 100), dtype=np.uint8)
        angle = compute_skew_angle(empty)
        assert angle == 0.0


class TestDenoising:
    """Test median filtering denoising."""

    def test_denoising_preserves_shape(self):
        """Denoising should preserve image shape."""
        img = np.random.randint(0, 255, (100, 100, 3), dtype=np.uint8)
        denoised = denoise(img)
        assert denoised.shape == img.shape

    def test_denoising_reduces_noise(self):
        """Denoising should reduce salt-and-pepper noise."""
        # Create image with noise
        clean = np.full((100, 100, 3), 128, dtype=np.uint8)
        noisy = clean.copy()
        # Add salt-and-pepper noise
        noisy[::10, ::10] = 255  # Salt
        noisy[5::10, 5::10] = 0  # Pepper

        denoised = denoise(noisy)
        # Denoised should be closer to clean
        clean_diff = np.mean(np.abs(denoised.astype(float) - clean.astype(float)))
        noisy_diff = np.mean(np.abs(noisy.astype(float) - clean.astype(float)))
        assert clean_diff < noisy_diff, "Denoising should reduce noise"


class TestPathA:
    """Test Path A: Binarized for OCR."""

    def test_path_a_returns_single_channel(self):
        """Path A should return a single-channel (grayscale) image."""
        img = np.random.randint(0, 255, (100, 100, 3), dtype=np.uint8)
        result = preprocess_path_a_ocr(img)
        # Binarized output should be single channel
        assert len(result.shape) == 2 or result.shape[2] == 1

    def test_path_a_is_binary(self):
        """Path A output should be binary (0 or 255)."""
        img = np.random.randint(0, 255, (100, 100, 3), dtype=np.uint8)
        result = preprocess_path_a_ocr(img)
        unique_values = np.unique(result)
        assert all(v in [0, 255] for v in unique_values), \
            f"Path A output should be binary, got unique values: {unique_values}"


class TestPathB:
    """Test Path B: Clean RGB for VLM."""

    def test_path_b_preserves_color(self):
        """Path B should preserve color information."""
        img = np.random.randint(0, 255, (100, 100, 3), dtype=np.uint8)
        result = preprocess_path_b_vlm(img)
        # Should still have 3 channels
        assert len(result.shape) == 3
        assert result.shape[2] == 3

    def test_path_b_preserves_shape(self):
        """Path B should preserve image dimensions."""
        img = np.random.randint(0, 255, (200, 300, 3), dtype=np.uint8)
        result = preprocess_path_b_vlm(img)
        assert result.shape[:2] == img.shape[:2]
