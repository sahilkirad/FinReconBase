"""
TDD tests — Milestone 2: POST /webhooks/razorpay/batch (one-shot feed upload).

Covers:
- 200 with accepted/duplicates/total accounting for a fresh batch
- 200 absorbing an all-duplicate replay (ON CONFLICT DO NOTHING -> duplicates)
- per-record vendor_code is ALWAYS the JWT vendor, never the payload
- 403 when the JWT vendor is not onboarded
- 422 when a payload record fails schema validation (no DB touch)

DB access is faked (substring-dispatching session, mirrors the
test_vendor_auth.py / test_ledger_writer.py convention). Routes require a real
JWT, so each request carries a token signed with the override secret.
"""

from fastapi.testclient import TestClient

from app.core.config import Settings, get_settings
from app.core.security import create_access_token
from app.db.session import get_db
from app.main import create_app

TEST_SECRET = "test-secret"
JWT_VENDOR = "VEND_TEST_002"


class _ResultFirst:
    def __init__(self, rows):
        self._rows = rows

    def first(self):
        return self._rows[0] if self._rows else None


class _IngestionFakeSession:
    def __init__(self, *, onboarded=True, insert_returns_row=True):
        self.onboarded = onboarded
        self.insert_returns_row = insert_returns_row
        self.executed: list[tuple[str, dict]] = []
        self.commits = 0
        self.rollbacks = 0

    def execute(self, sql, params=None):
        sql_text = str(sql)
        params = dict(params or {})
        self.executed.append((sql_text, params))

        if "SELECT 1 FROM vendor_users" in sql_text:
            return _ResultFirst([(1,)] if self.onboarded else [])
        if "INSERT INTO razorpay_settlements" in sql_text:
            return _ResultFirst([("settle-x",)] if self.insert_returns_row else [])
        return _ResultFirst([])

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    @property
    def razorpay_inserts(self):
        return [p for s, p in self.executed if "INSERT INTO razorpay_settlements" in s]


def build_test_client(db: _IngestionFakeSession) -> TestClient:
    app = create_app()

    def _override_db():
        yield db

    def _override_settings():
        return Settings(jwt_secret_key=TEST_SECRET)

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_settings] = _override_settings
    return TestClient(app)


def _auth_header() -> dict[str, str]:
    settings = Settings(jwt_secret_key=TEST_SECRET)
    token = create_access_token(
        subject="user-uuid-1",
        vendor_code=JWT_VENDOR,
        role="ADMIN",
        settings=settings,
    )
    return {"Authorization": f"Bearer {token}"}


def _settlement(payout_id: str, **overrides) -> dict:
    base = {
        "payout_id": payout_id,
        "fund_account_id": "fa_123",
        "amount_paise": 204390500,
        "currency": "INR",
        "status": "processed",
        "utr": "300000000001",
        "reference_id": "INV-0001",
        "narration": "Payout",
        "fees_paise": 0,
        "tax_paise": 0,
        "mode": "bank_transfer",
        "purpose": "payout",
        "event_created_at_epoch": 1756900000,
    }
    base.update(overrides)
    return base


class TestRazorpayBatchEndpoint:
    def test_batch_records_all_new_settlements(self):
        db = _IngestionFakeSession()
        client = build_test_client(db)

        response = client.post(
            "/webhooks/razorpay/batch",
            json=[_settlement("pout_1"), _settlement("pout_2")],
            headers=_auth_header(),
        )

        assert response.status_code == 200
        body = response.json()
        assert body["accepted"] == 2
        assert body["duplicates"] == 0
        assert body["total"] == 2
        assert db.commits == 1
        assert len(db.razorpay_inserts) == 2
        # vendor_code is JWT-scoped on every insert — payloads never carry it
        assert all(p["vendor_code"] == JWT_VENDOR for p in db.razorpay_inserts)

    def test_batch_absorbs_all_duplicate_replay(self):
        """Same file re-pushed after a webhook retry -> absorbed, never fails."""
        db = _IngestionFakeSession(insert_returns_row=False)
        client = build_test_client(db)

        response = client.post(
            "/webhooks/razorpay/batch",
            json=[_settlement("pout_1"), _settlement("pout_2")],
            headers=_auth_header(),
        )

        assert response.status_code == 200
        body = response.json()
        assert body["accepted"] == 0
        assert body["duplicates"] == 2
        assert body["total"] == 2
        assert len(db.razorpay_inserts) == 2  # still attempted idempotently

    def test_batch_rejects_unonboarded_vendor(self):
        db = _IngestionFakeSession(onboarded=False)
        client = build_test_client(db)

        response = client.post(
            "/webhooks/razorpay/batch",
            json=[_settlement("pout_1")],
            headers=_auth_header(),
        )

        assert response.status_code == 403
        assert db.razorpay_inserts == []  # vendor check ran before any insert

    def test_batch_invalid_record_returns_422_before_db(self):
        db = _IngestionFakeSession()
        client = build_test_client(db)

        response = client.post(
            "/webhooks/razorpay/batch",
            json=[_settlement("pout_1", amount_paise=-5)],
            headers=_auth_header(),
        )

        assert response.status_code == 422
        assert db.executed == []
