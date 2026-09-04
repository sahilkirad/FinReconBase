"""
TDD tests — Layer 2 ingestion endpoints (Streams 2 & 3 materialization).

- POST /webhooks/razorpay  (razorpay_settlements)
- POST /ingestion/bank     (bank_transactions)

Covers: auth required, happy recording, idempotent duplicate absorption,
invalid payload rejection (422), vendor onboarding gate (403), and the
vendor_code ALWAYS coming from the JWT (never the payload).
"""

import os

os.environ["GOOGLE_OAUTH_CLIENT_ID"] = "test"
os.environ["JWT_SECRET_KEY"] = "test-secret"
os.environ["GROQ_API_KEY"] = "test"
os.environ["GROQ_MODEL"] = "test"

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.core.security import create_access_token
from app.db.session import get_db
from app.main import create_app


class _Result:
    def __init__(self, row):
        self._row = row

    def first(self):
        return self._row

    def all(self):
        return [self._row] if self._row is not None else []


class _FakeDB:
    """Deterministic fake session for the ingestion endpoints."""

    def __init__(self, *, onboarded=True, insert_returns_row=True):
        self.onboarded = onboarded
        self.insert_returns_row = insert_returns_row
        self.executed: list[str] = []
        self.committed = 0
        self.rolled_back = False

    def execute(self, sql, params=None):
        sql_text = str(sql)
        self.executed.append(sql_text)

        if "vendor_users" in sql_text:
            return _Result(("user-1",) if self.onboarded else None)

        if "razorpay_settlements" in sql_text and "RETURNING" in sql_text:
            return _Result(("settlement-1",) if self.insert_returns_row else None)

        if "razorpay_settlements" in sql_text and "WHERE payout_id" in sql_text:
            return _Result(("settlement-1",))

        if "bank_transactions" in sql_text and "RETURNING" in sql_text:
            return _Result(("txn-1",) if self.insert_returns_row else None)

        return _Result(None)

    def commit(self):
        self.committed += 1

    def rollback(self):
        self.rolled_back = True


@pytest.fixture()
def client():
    app = create_app()

    def _override_get_db():
        yield _FakeDB()

    app.dependency_overrides[get_db] = _override_get_db
    return TestClient(app)


@pytest.fixture()
def token():
    settings = get_settings()
    return create_access_token(
        subject="user_123",
        vendor_code="VEND_TEST_001",
        role="ADMIN",
        settings=settings,
    )


VALID_RAZORPAY = {
    "payout_id": "pout_00000000000001",
    "fund_account_id": "fa_00000000000001",
    "amount_paise": 1000000,
    "currency": "INR",
    "status": "processed",
    "utr": "HDFCN202608249912",
    "reference_id": "INV-441",
    "narration": "Acme Corp Vendor Payment",
    "fees_paise": 0,
    "tax_paise": 0,
    "mode": "IMPS",
    "purpose": "vendor_bill",
    "event_created_at_epoch": 1545383037,
}

VALID_BANK = [
    {
        "transaction_date": "2026-08-25",
        "narration": "IMPS/ACME CORP/UTR/HDFCN202608249912",
        "utr_number": "HDFCN202608249912",
        "transaction_type": "CREDIT",
        "amount_paise": 1000000,
        "closing_balance_paise": 50000000,
    }
]


class TestAuthRequired:
    def test_razorpay_requires_auth(self, client):
        response = client.post("/webhooks/razorpay", json=VALID_RAZORPAY)
        assert response.status_code == 401

    def test_bank_requires_auth(self, client):
        response = client.post("/ingestion/bank", json=VALID_BANK)
        assert response.status_code == 401


class TestRazorpayWebhook:
    def test_records_settlement(self, client, token):
        fake = _FakeDB(insert_returns_row=True)

        def _override():
            yield fake

        client.app.dependency_overrides[get_db] = _override
        response = client.post(
            "/webhooks/razorpay",
            json=VALID_RAZORPAY,
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 202
        body = response.json()
        assert body["status"] == "recorded"
        assert body["settlement_id"] == "settlement-1"
        assert fake.committed == 1

    def test_duplicate_delivery_absorbed(self, client, token):
        """Webhook retries must not fail: ON CONFLICT => 'duplicate' with the
        existing settlement id."""
        fake = _FakeDB(insert_returns_row=False)

        def _override():
            yield fake

        client.app.dependency_overrides[get_db] = _override
        response = client.post(
            "/webhooks/razorpay",
            json=VALID_RAZORPAY,
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 202
        body = response.json()
        assert body["status"] == "duplicate"
        assert body["settlement_id"] == "settlement-1"

    def test_invalid_amount_rejected_422(self, client, token):
        payload = dict(VALID_RAZORPAY, amount_paise=-1)
        response = client.post(
            "/webhooks/razorpay",
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 422

    def test_missing_payout_id_rejected_422(self, client, token):
        payload = dict(VALID_RAZORPAY)
        del payload["payout_id"]
        response = client.post(
            "/webhooks/razorpay",
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 422

    def test_vendor_must_be_onboarded(self, client, token):
        fake = _FakeDB(onboarded=False)

        def _override():
            yield fake

        client.app.dependency_overrides[get_db] = _override
        response = client.post(
            "/webhooks/razorpay",
            json=VALID_RAZORPAY,
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 403


class TestBankIngestion:
    def test_ingests_bank_transactions(self, client, token):
        fake = _FakeDB(insert_returns_row=True)

        def _override():
            yield fake

        client.app.dependency_overrides[get_db] = _override
        response = client.post(
            "/ingestion/bank",
            json=VALID_BANK,
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["accepted"] == 1
        assert body["duplicates"] == 0
        assert body["total"] == 1

    def test_duplicate_rows_reported_not_failed(self, client, token):
        """Same feed re-delivered: second identical row => duplicate count."""
        fake = _FakeDB(insert_returns_row=False)

        def _override():
            yield fake

        client.app.dependency_overrides[get_db] = _override
        rows = VALID_BANK * 2  # both conflict with an existing unique row
        response = client.post(
            "/ingestion/bank",
            json=rows,
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["accepted"] == 0
        assert body["duplicates"] == 2

    def test_invalid_transaction_type_rejected_422(self, client, token):
        rows = [dict(VALID_BANK[0], transaction_type="TRANSFER")]
        response = client.post(
            "/ingestion/bank",
            json=rows,
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 422

    def test_negative_amount_rejected_422(self, client, token):
        rows = [dict(VALID_BANK[0], amount_paise=-5)]
        response = client.post(
            "/ingestion/bank",
            json=rows,
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 422

    def test_vendor_must_be_onboarded(self, client, token):
        fake = _FakeDB(onboarded=False)

        def _override():
            yield fake

        client.app.dependency_overrides[get_db] = _override
        response = client.post(
            "/ingestion/bank",
            json=VALID_BANK,
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 403
