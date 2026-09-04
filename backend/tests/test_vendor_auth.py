"""
TDD tests — Milestone 1: Native Vendor Auth (Track 4 frontend).

Covers:
- hash_api_secret / verify_api_secret: PBKDF2 round-trip, wrong secret,
  malformed/tampered rows, cap on iterations
- normalize_vendor_code: trim + uppercase canonical form
- VendorRegisterRequest / VendorLoginRequest schema validation (422 paths)
- POST /auth/vendor/register: 201 + JWT + profile; 409 on duplicate vendor;
  atomic vendor_users + vendor_credentials inserts; IntegrityError -> 409
- POST /auth/vendor/token: 200 + JWT (120-min exp); 401 wrong secret;
  401 unknown vendor (no enumeration); 403 vendor with no user row

DB access is faked with a substring-dispatching session (mirrors the
test_ledger_writer.py convention) so tests never need a live Postgres.
"""

import time

import jwt as pyjwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from app.core.config import Settings, get_settings
from app.core.security import (
    hash_api_secret,
    normalize_vendor_code,
    verify_api_secret,
)
from app.db.session import get_db
from app.main import create_app


# =============================================================================
# Fakes
# =============================================================================


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def first(self):
        return self._rows[0] if self._rows else None


class _AuthFakeSession:
    """Captures SQL + params; returns canned rows per statement family."""

    def __init__(
        self,
        *,
        credential_row=None,
        user_row=None,
        credential_exists=False,
        user_exists=False,
        fail_on_insert=False,
    ):
        self.credential_row = credential_row
        self.user_row = user_row
        self.credential_exists = credential_exists
        self.user_exists = user_exists
        self.fail_on_insert = fail_on_insert
        self.executed: list[tuple[str, dict]] = []
        self.commits = 0
        self.rollbacks = 0

    def execute(self, sql, params=None):
        sql_text = str(sql)
        params = dict(params or {})
        self.executed.append((sql_text, params))

        if "SELECT vendor_name, api_secret_hash" in sql_text:
            return _Result([self.credential_row] if self.credential_row else [])
        if "SELECT user_id, role" in sql_text:
            return _Result([self.user_row] if self.user_row else [])
        if "SELECT 1 FROM vendor_credentials" in sql_text:
            return _Result([(1,)] if self.credential_exists else [])
        if "SELECT 1 FROM vendor_users" in sql_text:
            return _Result([(1,)] if self.user_exists else [])
        if "INSERT INTO" in sql_text:
            if self.fail_on_insert:
                raise IntegrityError(
                    "stmt",
                    params,
                    Exception('duplicate key value violates unique constraint "vendor_users_email_key"'),
                )
            return _Result([])
        return _Result([])

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    @property
    def vendor_user_inserts(self):
        return [p for s, p in self.executed if "INSERT INTO vendor_users" in s]

    @property
    def credential_inserts(self):
        return [p for s, p in self.executed if "INSERT INTO vendor_credentials" in s]


def build_test_client(
    db: _AuthFakeSession,
    settings: Settings | None = None,
) -> TestClient:
    app = create_app()

    def _override_db():
        yield db

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_settings] = lambda: settings or Settings(
        jwt_secret_key="test-secret",
        jwt_expire_minutes=120,
    )
    return TestClient(app)


def _register_payload(**overrides):
    base = {
        "vendor_code": "vend_demo_001",
        "vendor_name": "Demo Vendor One",
        "email": "demo@example.com",
        "api_secret": "SuperSecret123!",
    }
    base.update(overrides)
    return base


def _login_payload(**overrides):
    base = {
        "vendor_code": "VEND_TEST_002",
        "api_secret": "SuperSecret123!",
    }
    base.update(overrides)
    return base


# =============================================================================
# 1. Pure secret hashing
# =============================================================================


class TestSecretHashing:
    def test_round_trip(self):
        encoded = hash_api_secret("SuperSecret123!")
        assert encoded.startswith("pbkdf2_sha256$260000$")
        assert verify_api_secret("SuperSecret123!", encoded) is True

    def test_wrong_secret_rejected(self):
        encoded = hash_api_secret("SuperSecret123!")
        assert verify_api_secret("wrong-secret", encoded) is False

    def test_malformed_encoded_string_rejected(self):
        assert verify_api_secret("secret", "") is False
        assert verify_api_secret("secret", "not-a-hash") is False
        assert verify_api_secret("secret", "md5$100$abcd$ef") is False

    def test_tampered_hash_rejected(self):
        encoded = hash_api_secret("SuperSecret123!")
        tampered = encoded[:-2] + ("00" if encoded[-2:] != "00" else "ff")
        assert verify_api_secret("SuperSecret123!", tampered) is False

    def test_hostile_iterations_capped(self):
        hostile = "pbkdf2_sha256$999999999999$abcd$abcd"
        assert verify_api_secret("secret", hostile) is False

    def test_salts_are_unique_per_hash(self):
        a = hash_api_secret("same-secret")
        b = hash_api_secret("same-secret")
        assert a != b


class TestNormalizeVendorCode:
    def test_trims_and_uppercases(self):
        assert normalize_vendor_code("  vend_demo_001  ") == "VEND_DEMO_001"


# =============================================================================
# 2. Schema validation (pure — no DB)
# =============================================================================


class TestRegisterSchema:
    def test_normalizes_vendor_code(self):
        from app.schemas.auth import VendorRegisterRequest

        req = VendorRegisterRequest(**_register_payload())
        assert req.vendor_code == "VEND_DEMO_001"

    def test_rejects_invalid_vendor_code_chars(self):
        from app.schemas.auth import VendorRegisterRequest

        with pytest.raises(Exception):
            VendorRegisterRequest(**_register_payload(vendor_code="bad code!!"))

    def test_rejects_short_api_secret(self):
        from app.schemas.auth import VendorRegisterRequest

        with pytest.raises(Exception):
            VendorRegisterRequest(**_register_payload(api_secret="short"))

    def test_rejects_invalid_email(self):
        from app.schemas.auth import VendorRegisterRequest

        with pytest.raises(Exception):
            VendorRegisterRequest(**_register_payload(email="not-an-email"))

    def test_normalizes_email_to_lower(self):
        from app.schemas.auth import VendorRegisterRequest

        req = VendorRegisterRequest(**_register_payload(email=" Demo@Example.COM "))
        assert req.email == "demo@example.com"


# =============================================================================
# 3. POST /auth/vendor/register
# =============================================================================


class TestRegisterEndpoint:
    def test_register_returns_201_with_jwt_and_profile(self):
        db = _AuthFakeSession()
        client = build_test_client(db)

        response = client.post("/auth/vendor/register", json=_register_payload())

        assert response.status_code == 201
        body = response.json()
        assert body["token_type"] == "bearer"
        assert body["vendor_code"] == "VEND_DEMO_001"
        assert body["vendor_name"] == "Demo Vendor One"
        assert body["role"] == "ADMIN"

        claims = pyjwt.decode(body["access_token"], "test-secret", algorithms=["HS256"])
        assert claims["vendor_code"] == "VEND_DEMO_001"
        assert claims["role"] == "ADMIN"
        assert claims["sub"]
        assert claims["exp"] > time.time() + 100 * 60  # ~120-minute lifetime

        assert db.commits == 1
        assert len(db.vendor_user_inserts) == 1
        assert len(db.credential_inserts) == 1
        user_params = db.vendor_user_inserts[0]
        assert user_params["email"] == "demo@example.com"
        assert user_params["google_subject_id"] == f"native_{user_params['user_id']}"
        cred_params = db.credential_inserts[0]
        assert cred_params["vendor_code"] == "VEND_DEMO_001"
        assert verify_api_secret("SuperSecret123!", cred_params["api_secret_hash"]) is True

    def test_register_duplicate_credential_returns_409(self):
        db = _AuthFakeSession(credential_exists=True)
        client = build_test_client(db)

        response = client.post("/auth/vendor/register", json=_register_payload())

        assert response.status_code == 409
        assert response.json()["detail"]["error_code"] == "VENDOR_CODE_ALREADY_ONBOARDED"
        assert db.commits == 0

    def test_register_duplicate_vendor_user_returns_409(self):
        """Google-mapped vendor already exists -> vendor_code is taken."""
        db = _AuthFakeSession(user_exists=True)
        client = build_test_client(db)

        response = client.post("/auth/vendor/register", json=_register_payload())

        assert response.status_code == 409

    def test_register_integrity_error_rolls_back_to_409(self):
        db = _AuthFakeSession(fail_on_insert=True)
        client = build_test_client(db)

        response = client.post("/auth/vendor/register", json=_register_payload())

        assert response.status_code == 409
        assert response.json()["detail"]["error_code"] == "VENDOR_REGISTRATION_CONFLICT"
        assert db.rollbacks == 1
        assert db.commits == 0

    def test_register_invalid_body_returns_422(self):
        db = _AuthFakeSession()
        client = build_test_client(db)

        response = client.post(
            "/auth/vendor/register",
            json=_register_payload(api_secret="x", email="nope"),
        )

        assert response.status_code == 422
        assert db.executed == []  # validation rejects before any DB touch


# =============================================================================
# 4. POST /auth/vendor/token
# =============================================================================


class TestLoginEndpoint:
    def _seeded_credential(self):
        return ("Demo Vendor One", hash_api_secret("SuperSecret123!"))

    def test_login_success_returns_200_with_jwt(self):
        db = _AuthFakeSession(
            credential_row=self._seeded_credential(),
            user_row=("user-uuid-1", "ADMIN"),
        )
        client = build_test_client(db)

        response = client.post("/auth/vendor/token", json=_login_payload())

        assert response.status_code == 200
        body = response.json()
        assert body["vendor_code"] == "VEND_TEST_002"
        assert body["vendor_name"] == "Demo Vendor One"
        assert body["role"] == "ADMIN"

        claims = pyjwt.decode(body["access_token"], "test-secret", algorithms=["HS256"])
        assert claims["sub"] == "user-uuid-1"
        assert claims["vendor_code"] == "VEND_TEST_002"
        assert claims["role"] == "ADMIN"

    def test_login_wrong_secret_returns_401(self):
        db = _AuthFakeSession(
            credential_row=self._seeded_credential(),
            user_row=("user-uuid-1", "ADMIN"),
        )
        client = build_test_client(db)

        response = client.post(
            "/auth/vendor/token",
            json=_login_payload(api_secret="wrong-secret"),
        )

        assert response.status_code == 401
        assert response.json()["detail"]["error_code"] == "INVALID_VENDOR_CREDENTIALS"

    def test_login_unknown_vendor_returns_401_not_403(self):
        """No credential row -> same 401 as a wrong secret (no enumeration)."""
        db = _AuthFakeSession()
        client = build_test_client(db)

        response = client.post("/auth/vendor/token", json=_login_payload())

        assert response.status_code == 401
        assert response.json()["detail"]["error_code"] == "INVALID_VENDOR_CREDENTIALS"

    def test_login_vendor_without_user_row_returns_403(self):
        db = _AuthFakeSession(credential_row=self._seeded_credential())
        client = build_test_client(db)

        response = client.post("/auth/vendor/token", json=_login_payload())

        assert response.status_code == 403
        assert response.json()["detail"]["error_code"] == "VENDOR_HAS_NO_USER_ACCOUNT"
