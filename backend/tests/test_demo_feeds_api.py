"""
TDD tests — POST /demo/auto-generate-feeds (in-API Streams 2 & 3 materialization).

Covers:
- shared builder parity: N razorpay + N bank, amounts == net, anomalies drop
  the LAST N invoices, deterministic UTR/payout ids, CREDIT rows w/ running
  balance
- 200 PUSHED for a terminal COMPLETED batch with open invoices (single commit,
  vendor_code JWT-scoped on every insert)
- 202 WAITING when the batch is still extracting (background task scheduled)
- 404 unknown / foreign-vendor / non-UUID batch
- 409 when the batch has no open VALIDATED invoices (already reconciled)
- 422 when anomalies >= number of open invoices

DB access is faked (substring-dispatching session, mirrors the
test_ingestion_api.py / test_vendor_auth.py convention). Routes require a real
JWT, so each request carries a token signed with the override secret.
"""

import datetime

from fastapi.testclient import TestClient

from app.core.config import Settings, get_settings
from app.core.security import create_access_token
from app.db.session import get_db
from app.main import create_app

TEST_SECRET = "test-secret"
JWT_VENDOR = "VEND_TEST_002"
BATCH_ID = "11111111-2222-3333-4444-555555555555"


class _ResultFirst:
    def __init__(self, rows):
        self._rows = rows

    def first(self):
        return self._rows[0] if self._rows else None


class _ResultAll:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


def _invoice_row(invoice_number: str, net_paise: int) -> tuple:
    return (
        f"doc-{invoice_number}",
        invoice_number,
        JWT_VENDOR,
        datetime.date(2026, 8, 1),
        "Nexus Logistics Pvt Ltd",
        net_paise,
        net_paise + 4000,
        4000,
    )


class _DemoFakeSession:
    def __init__(
        self,
        *,
        batch_status: str | None = "COMPLETED",
        invoices: list[tuple] | None = None,
        insert_returns_row: bool = True,
    ):
        self.batch_status = batch_status
        self.invoices = invoices if invoices is not None else [
            _invoice_row("INV-0001", 10000000),
            _invoice_row("INV-0002", 20000000),
            _invoice_row("INV-0003", 30000000),
        ]
        self.insert_returns_row = insert_returns_row
        self.executed: list[tuple[str, dict]] = []
        self.commits = 0
        self.rollbacks = 0

    def execute(self, sql, params=None):
        sql_text = str(sql)
        params = dict(params or {})
        self.executed.append((sql_text, params))

        if "FROM batch_jobs b" in sql_text:
            if self.batch_status is None:
                return _ResultFirst([])
            return _ResultFirst(
                [(BATCH_ID, JWT_VENDOR, self.batch_status, len(self.invoices))]
            )
        if "FROM batch_invoice_items i" in sql_text:
            return _ResultAll(self.invoices)
        if "INSERT INTO razorpay_settlements" in sql_text:
            return _ResultFirst([("settle-x",)] if self.insert_returns_row else [])
        if "INSERT INTO bank_transactions" in sql_text:
            return _ResultFirst([("txn-x",)] if self.insert_returns_row else [])
        return _ResultFirst([])

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    @property
    def razorpay_inserts(self):
        return [p for s, p in self.executed if "INSERT INTO razorpay_settlements" in s]

    @property
    def bank_inserts(self):
        return [p for s, p in self.executed if "INSERT INTO bank_transactions" in s]


def build_test_client(db: _DemoFakeSession) -> TestClient:
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


# =============================================================================
# Shared builder (app/demo/feeds.py) — parity with the offline CLI generator
# =============================================================================


class TestBuildFeeds:
    def _invoices(self, n: int = 3):
        return [
            {
                "document_id": f"doc-{i}",
                "invoice_number": f"INV-{i:04d}",
                "vendor_code": JWT_VENDOR,
                "document_date": datetime.date(2026, 8, 1),
                "supplier_legal_name": "Nexus Logistics Pvt Ltd",
                "net_paise": i * 10_000_00,
                "grand_total_paise": i * 10_000_00 + 4000,
                "tds_deduction_paise": 4000,
            }
            for i in range(1, n + 1)
        ]

    def test_emits_one_pair_per_invoice_with_deterministic_ids(self):
        from app.demo.feeds import build_feeds

        razorpay, bank = build_feeds(self._invoices(3), anomalies=0)

        assert len(razorpay) == 3
        assert len(bank) == 3
        # amounts mirror the invoice nets (integer paise)
        assert [r["amount_paise"] for r in razorpay] == [10_000_00, 20_000_00, 30_000_00]
        assert [b["amount_paise"] for b in bank] == [10_000_00, 20_000_00, 30_000_00]
        # razorpay settlement anchors the invoice number + carries the UTR
        assert razorpay[0]["reference_id"] == "INV-0001"
        assert razorpay[0]["status"] == "processed"
        # same UTR on both sides, deterministic sequence
        assert razorpay[0]["utr"] == bank[0]["utr_number"] == "300000000002"
        assert razorpay[2]["utr"] == bank[2]["utr_number"] == "300000000004"
        assert razorpay[0]["payout_id"] == "pout_e2e_0001"
        # bank credits keep a running closing balance
        assert bank[2]["closing_balance_paise"] == 60_000_00
        assert all(b["transaction_type"] == "CREDIT" for b in bank)

    def test_anomalies_drop_last_n_invoices_from_both_feeds(self):
        from app.demo.feeds import build_feeds

        razorpay, bank = build_feeds(self._invoices(5), anomalies=2)
        # LAST 2 invoices dropped -> first 3 remain, nets preserved
        assert len(razorpay) == len(bank) == 3
        assert [r["amount_paise"] for r in razorpay] == [10_000_00, 20_000_00, 30_000_00]
        assert razorpay[-1]["utr"] == "300000000004"

    def test_rejects_unknown_scenario(self):
        from app.demo.feeds import build_feeds

        try:
            build_feeds(self._invoices(1), anomalies=0, scenario="nope")
        except ValueError as exc:
            assert "Unsupported scenario" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected ValueError")


# =============================================================================
# Endpoint — POST /demo/auto-generate-feeds
# =============================================================================


class TestAutoGenerateFeedsEndpoint:
    def test_terminal_batch_pushes_feeds_in_one_commit(self):
        db = _DemoFakeSession(batch_status="COMPLETED")
        client = build_test_client(db)

        response = client.post(
            "/demo/auto-generate-feeds",
            json={"batch_id": BATCH_ID, "anomalies": 1},
            headers=_auth_header(),
        )

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "PUSHED"
        assert body["invoices_generated"] == 2  # 3 open - 1 anomaly
        assert body["anomalies"] == 1
        assert body["razorpay_accepted"] == 2
        assert body["razorpay_duplicates"] == 0
        assert body["bank_accepted"] == 2
        assert body["bank_duplicates"] == 0
        assert db.commits == 1
        assert len(db.razorpay_inserts) == 2
        assert len(db.bank_inserts) == 2
        # vendor_code is JWT-scoped on every insert, never payload-driven
        assert all(p["vendor_code"] == JWT_VENDOR for p in db.razorpay_inserts)
        assert all(p["vendor_code"] == JWT_VENDOR for p in db.bank_inserts)
        # deterministic idempotency keys
        assert db.razorpay_inserts[0]["payout_id"] == "pout_e2e_0001"
        assert db.bank_inserts[0]["utr_number"] == db.razorpay_inserts[0]["utr"]

    def test_duplicate_rows_absorbed_and_reported(self):
        db = _DemoFakeSession(batch_status="COMPLETED", insert_returns_row=False)
        client = build_test_client(db)

        response = client.post(
            "/demo/auto-generate-feeds",
            json={"batch_id": BATCH_ID, "anomalies": 0},
            headers=_auth_header(),
        )

        assert response.status_code == 200
        body = response.json()
        assert body["razorpay_accepted"] == 0
        assert body["razorpay_duplicates"] == 3
        assert body["bank_duplicates"] == 3

    def test_pending_batch_schedules_background_wait(self, monkeypatch):
        db = _DemoFakeSession(batch_status="PENDING")
        client = build_test_client(db)
        scheduled = []

        def fake_background(batch_id, vendor_code, anomalies):
            scheduled.append((batch_id, vendor_code, anomalies))

        monkeypatch.setattr("app.api.demo._background_wait_and_push", fake_background)

        response = client.post(
            "/demo/auto-generate-feeds",
            json={"batch_id": BATCH_ID, "anomalies": 4},
            headers=_auth_header(),
        )

        assert response.status_code == 202
        body = response.json()
        assert body["status"] == "WAITING"
        assert "PENDING" in body["message"]
        assert scheduled == [(BATCH_ID, JWT_VENDOR, 4)]
        # no inserts attempted while waiting
        assert db.razorpay_inserts == []
        assert db.bank_inserts == []

    def test_unknown_batch_returns_404(self):
        db = _DemoFakeSession(batch_status=None)
        client = build_test_client(db)

        response = client.post(
            "/demo/auto-generate-feeds",
            json={"batch_id": BATCH_ID, "anomalies": 0},
            headers=_auth_header(),
        )

        assert response.status_code == 404
        assert response.json()["detail"]["error_code"] == "BATCH_NOT_FOUND"
        assert db.razorpay_inserts == []

    def test_non_uuid_batch_returns_404_before_db(self):
        db = _DemoFakeSession(batch_status="COMPLETED")
        client = build_test_client(db)

        response = client.post(
            "/demo/auto-generate-feeds",
            json={"batch_id": "not-a-uuid", "anomalies": 0},
            headers=_auth_header(),
        )

        assert response.status_code == 404
        assert db.executed == []  # UUID gate ran before any SQL

    def test_no_open_invoices_returns_409(self):
        db = _DemoFakeSession(batch_status="COMPLETED", invoices=[])
        client = build_test_client(db)

        response = client.post(
            "/demo/auto-generate-feeds",
            json={"batch_id": BATCH_ID, "anomalies": 0},
            headers=_auth_header(),
        )

        assert response.status_code == 409
        assert response.json()["detail"]["error_code"] == "NO_RECONCILABLE_INVOICES"

    def test_anomalies_not_less_than_open_invoices_returns_422(self):
        db = _DemoFakeSession(batch_status="COMPLETED")
        client = build_test_client(db)

        response = client.post(
            "/demo/auto-generate-feeds",
            json={"batch_id": BATCH_ID, "anomalies": 3},  # 3 open invoices
            headers=_auth_header(),
        )

        assert response.status_code == 422
        assert response.json()["detail"]["error_code"] == "INVALID_ANOMALIES"
        assert db.razorpay_inserts == []

    def test_unauthenticated_request_rejected(self):
        db = _DemoFakeSession(batch_status="COMPLETED")
        client = build_test_client(db)

        response = client.post(
            "/demo/auto-generate-feeds",
            json={"batch_id": BATCH_ID, "anomalies": 0},
        )

        assert response.status_code in (401, 403)
        assert db.razorpay_inserts == []
