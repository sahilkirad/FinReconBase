"""
Tests for Batch Invoice Upload API

Tests cover:
- PDF page splitting (Fan-Out)
- Single image saving
- API endpoint structure
- Authentication requirements
"""

import pytest
import numpy as np
from pathlib import Path
from unittest.mock import patch, MagicMock


class TestPDFPageSplitting:
    """Test PDF page splitting to individual JPEG files."""

    def test_split_pdf_creates_page_files(self, tmp_path):
        """Should create individual JPEG files for each page."""
        from app.api.batch import _split_pdf_to_pages
        import fitz

        doc = fitz.open()
        page1 = doc.new_page()
        page1.insert_text((72, 72), "Page 1 content")
        page2 = doc.new_page()
        page2.insert_text((72, 72), "Page 2 content")
        pdf_bytes = doc.tobytes()
        doc.close()

        pages = _split_pdf_to_pages(
            pdf_bytes=pdf_bytes,
            document_id="test-doc-001",
            storage_path=str(tmp_path),
        )

        assert len(pages) == 2
        for i, page in enumerate(pages):
            assert page["page_index"] == i
            assert "file_path" in page
            assert "file_size" in page
            assert page["file_size"] > 0
            assert Path(page["file_path"]).exists()

    def test_split_pdf_saves_as_jpeg(self, tmp_path):
        """Should save pages as JPEG format."""
        from app.api.batch import _split_pdf_to_pages
        import fitz

        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((72, 72), "Test content")
        pdf_bytes = doc.tobytes()
        doc.close()

        pages = _split_pdf_to_pages(
            pdf_bytes=pdf_bytes,
            document_id="test-doc-002",
            storage_path=str(tmp_path),
        )

        assert pages[0]["file_path"].endswith(".jpg")

    def test_split_pdf_creates_directory(self, tmp_path):
        """Should create batch directory if it doesn't exist."""
        from app.api.batch import _split_pdf_to_pages
        import fitz

        doc = fitz.open()
        page = doc.new_page()
        pdf_bytes = doc.tobytes()
        doc.close()

        batch_dir = tmp_path / "new-batch-id"
        assert not batch_dir.exists()

        pages = _split_pdf_to_pages(
            pdf_bytes=pdf_bytes,
            document_id="new-batch-id",
            storage_path=str(tmp_path),
        )

        assert batch_dir.exists()
        assert batch_dir.is_dir()


class TestSingleImageSaving:
    """Test single image (JPEG/PNG) saving."""

    def test_save_jpeg_image(self, tmp_path):
        """Should save JPEG image to volume."""
        from app.api.batch import _save_single_image
        import cv2

        img = np.zeros((100, 100, 3), dtype=np.uint8)
        img[50, 50] = [255, 0, 0]
        _, buffer = cv2.imencode(".jpg", img)
        image_bytes = buffer.tobytes()

        pages = _save_single_image(
            image_bytes=image_bytes,
            document_id="test-img-001",
            filename="test.jpg",
            storage_path=str(tmp_path),
        )

        assert len(pages) == 1
        assert pages[0]["page_index"] == 0
        assert pages[0]["file_size"] > 0
        assert Path(pages[0]["file_path"]).exists()

    def test_save_png_image(self, tmp_path):
        """Should save PNG image to volume."""
        from app.api.batch import _save_single_image
        import cv2

        img = np.zeros((100, 100, 3), dtype=np.uint8)
        img[50, 50] = [0, 255, 0]
        _, buffer = cv2.imencode(".png", img)
        image_bytes = buffer.tobytes()

        pages = _save_single_image(
            image_bytes=image_bytes,
            document_id="test-img-002",
            filename="test.png",
            storage_path=str(tmp_path),
        )

        assert len(pages) == 1
        assert pages[0]["file_size"] > 0

    def test_invalid_image_raises_error(self, tmp_path):
        """Should raise ValueError for invalid image data."""
        from app.api.batch import _save_single_image

        with pytest.raises(ValueError, match="Cannot decode image"):
            _save_single_image(
                image_bytes=b"not an image",
                document_id="test-img-003",
                filename="invalid.jpg",
                storage_path=str(tmp_path),
            )


class TestBatchAPIEndpoint:
    """Test batch upload API endpoint structure."""

    def test_upload_requires_auth(self):
        """Upload endpoint should require authentication."""
        import os
        os.environ["GOOGLE_OAUTH_CLIENT_ID"] = "test"
        os.environ["JWT_SECRET_KEY"] = "test-secret"
        os.environ["GROQ_API_KEY"] = "test"
        os.environ["GROQ_MODEL"] = "test"

        from fastapi.testclient import TestClient
        from app.core.config import get_settings
        from app.main import create_app

        settings = get_settings()
        app = create_app()
        client = TestClient(app)

        response = client.post("/invoices/batch")
        assert response.status_code == 401

    def test_upload_rejects_invalid_file_type(self):
        """Upload should reject non-PDF/CSV file types."""
        import os
        os.environ["GOOGLE_OAUTH_CLIENT_ID"] = "test"
        os.environ["JWT_SECRET_KEY"] = "test-secret"
        os.environ["GROQ_API_KEY"] = "test"
        os.environ["GROQ_MODEL"] = "test"

        from fastapi.testclient import TestClient
        from app.core.config import get_settings
        from app.core.security import create_access_token
        from app.main import create_app

        settings = get_settings()
        app = create_app()
        client = TestClient(app)

        token = create_access_token(
            subject="user_123",
            vendor_code="VEND_TEST_001",
            role="ADMIN",
            settings=settings,
        )

        response = client.post(
            "/invoices/batch",
            headers={"Authorization": f"Bearer {token}"},
            files={"file": ("test.txt", b"not a pdf", "text/plain")},
            data={"vendor_code": "VEND_TEST_001"},
        )
        # Should fail with UNSUPPORTED_FILE_TYPE
        assert response.status_code == 422

    def test_upload_rejects_csv(self):
        """CSV support is removed - .csv must be rejected with 422."""
        import os
        os.environ["GOOGLE_OAUTH_CLIENT_ID"] = "test"
        os.environ["JWT_SECRET_KEY"] = "test-secret"
        os.environ["GROQ_API_KEY"] = "test"
        os.environ["GROQ_MODEL"] = "test"

        from fastapi.testclient import TestClient
        from app.core.config import get_settings
        from app.core.security import create_access_token
        from app.main import create_app

        settings = get_settings()
        app = create_app()
        client = TestClient(app)

        token = create_access_token(
            subject="user_123",
            vendor_code="VEND_TEST_001",
            role="ADMIN",
            settings=settings,
        )

        csv_content = b"invoice_number,supplier_name,total_amount\nINV-1,Test Supplier,1000"
        response = client.post(
            "/invoices/batch",
            headers={"Authorization": f"Bearer {token}"},
            files={"file": ("invoices.csv", csv_content, "text/csv")},
            data={"vendor_code": "VEND_TEST_001"},
        )
        # CSV must be rejected with UNSUPPORTED_FILE_TYPE
        assert response.status_code == 422
        assert "UNSUPPORTED_FILE_TYPE" in response.text
