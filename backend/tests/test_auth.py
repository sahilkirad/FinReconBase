from fastapi.testclient import TestClient

from app.core.config import Settings, get_settings
from app.core.security import create_access_token
from app.main import create_app


def build_test_client(settings: Settings) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    return TestClient(app)


def test_missing_jwt_returns_401() -> None:
    settings = Settings(jwt_secret_key="test-secret")
    client = build_test_client(settings)

    response = client.get("/auth/me")

    assert response.status_code == 401


def test_invalid_jwt_returns_401() -> None:
    settings = Settings(jwt_secret_key="test-secret")
    client = build_test_client(settings)

    response = client.get(
        "/auth/me",
        headers={"Authorization": "Bearer not-a-valid-token"},
    )

    assert response.status_code == 401


def test_valid_jwt_extracts_vendor_context() -> None:
    settings = Settings(
        jwt_secret_key="test-secret",
        jwt_expire_minutes=15,
    )
    client = build_test_client(settings)

    token = create_access_token(
        subject="user_123",
        vendor_code="VEND_NEXUS_001",
        role="ADMIN",
        settings=settings,
    )

    response = client.get(
        "/auth/me",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "user_id": "user_123",
        "email": None,
        "vendor_code": "VEND_NEXUS_001",
        "role": "ADMIN",
    }