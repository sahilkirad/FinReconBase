"""
Tests for Invoice Upload API Endpoint

Tests cover:
- API endpoint structure
- Authentication requirements
- Error responses
"""

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings, get_settings
from app.core.security import create_access_token
from app.main import create_app


def build_test_client(settings: Settings) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    return TestClient(app)


class TestInvoiceAPI:
    """Test invoice upload API."""

    def test_upload_requires_auth(self):
        """Upload endpoint should require authentication."""
        settings = Settings(jwt_secret_key="test-secret")
        client = build_test_client(settings)

        response = client.post("/invoices")
        assert response.status_code == 401

    def test_upload_requires_file(self):
        """Upload endpoint should require a file."""
        settings = Settings(jwt_secret_key="test-secret")
        client = build_test_client(settings)

        token = create_access_token(
            subject="user_123",
            vendor_code="VEND_TEST_001",
            role="ADMIN",
            settings=settings,
        )

        response = client.post(
            "/invoices",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 422  # Missing required fields

    def test_upload_rejects_large_file(self):
        """Upload should reject files over 10MB."""
        settings = Settings(jwt_secret_key="test-secret")
        client = build_test_client(settings)

        token = create_access_token(
            subject="user_123",
            vendor_code="VEND_TEST_001",
            role="ADMIN",
            settings=settings,
        )

        # Create a large file (11MB)
        large_content = b"x" * (11 * 1024 * 1024)

        response = client.post(
            "/invoices",
            headers={"Authorization": f"Bearer {token}"},
            files={"file": ("large.pdf", large_content, "application/pdf")},
            data={"vendor_code": "VEND_TEST_001"},
        )
        # Should fail with FILE_TOO_LARGE or similar
        assert response.status_code in [413, 422]

    def test_upload_rejects_invalid_file_type(self):
        """Upload should reject non-invoice file types."""
        settings = Settings(jwt_secret_key="test-secret")
        client = build_test_client(settings)

        token = create_access_token(
            subject="user_123",
            vendor_code="VEND_TEST_001",
            role="ADMIN",
            settings=settings,
        )

        # Create a text file disguised as PDF
        fake_pdf = b"This is not a PDF file"

        response = client.post(
            "/invoices",
            headers={"Authorization": f"Bearer {token}"},
            files={"file": ("document.txt", fake_pdf, "text/plain")},
            data={"vendor_code": "VEND_TEST_001"},
        )
        # Should fail with UNSUPPORTED_FILE_TYPE
        assert response.status_code == 422

    def test_health_endpoint_still_works(self):
        """Health endpoint should continue working."""
        settings = Settings(jwt_secret_key="test-secret")
        client = build_test_client(settings)

        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    def test_auth_endpoint_still_works(self):
        """Auth endpoint should continue working."""
        settings = Settings(jwt_secret_key="test-secret")
        client = build_test_client(settings)

        response = client.get("/auth/me")
        assert response.status_code == 401  # No token provided
