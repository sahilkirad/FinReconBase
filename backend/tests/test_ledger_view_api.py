"""
TDD tests — Milestone 3: GET /ledger/entries (Immutable Ledger page).

Covers:
- returns vendor-scoped double-entry batches (DR/CR lines + imbalance proof)
- utr_number filter is passed to the query
- per-batch imbalance_paise is computed as DR - CR
- non-UUID batch_id filter -> 400 (no DB 500)
- 401 without a token

Postgres is faked with a substring-dispatching session (same convention as
the other API test modules).
"""

from datetime import datetime, timezone

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


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _batch_row(batch_id: str = BATCH_ID, utr: str = "UTR111"):
    return (
        batch_id, "evt_completed_1", JWT_VENDOR, utr, "pout_1",
        31540000, ["INV-0001"], _now(),
    )


def _entry_row(entry_type: str, amount: int):
    return (
        entry_type,
        "LIABILITY" if entry_type == "DEBIT" else "ASSET",
        "Accounts Payable - VEND_TEST_002" if entry_type == "DEBIT" else "HDFC Corporate Current Account",
        amount,
        ["INV-0001"],
        _now(),
    )


class _LedgerFakeSession:
    def __init__(self, *, batch_rows=None, entry_rows=None):
        self.batch_rows = batch_rows or []
        self.entry_rows = entry_rows or []  # returned for ANY batch lookup
        self.executed: list[tuple[str, dict]] = []
        self.commits = 0

    def execute(self, sql, params=None):
        sql_text = str(sql)
        params = dict(params or {})
        self.executed.append((sql_text, params))

        if "FROM reconciliation_batches" in sql_text:
            return _ResultAll(self.batch_rows)
        if "FROM ledger_entries" in sql_text:
            return _ResultAll(self.entry_rows)
        return _ResultFirst([])

    def commit(self):
        self.commits += 1

    @property
    def batch_params(self):
        return [p for s, p in self.executed if "FROM reconciliation_batches" in s]


def build_test_client(db: _LedgerFakeSession) -> TestClient:
    app = create_app()

    def _override_db():
        yield db

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_settings] = lambda: Settings(jwt_secret_key=TEST_SECRET)
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


class TestLedgerEntriesEndpoint:
    def test_returns_balanced_double_entry(self):
        db = _LedgerFakeSession(
            batch_rows=[_batch_row()],
            entry_rows=[_entry_row("DEBIT", 31540000), _entry_row("CREDIT", 31540000)],
        )
        client = build_test_client(db)

        response = client.get("/ledger/entries", headers=_auth_header())

        assert response.status_code == 200
        body = response.json()
        assert body["vendor_code"] == JWT_VENDOR
        assert body["total"] == 1
        item = body["items"][0]
        assert item["utr_number"] == "UTR111"
        assert item["total_reconciled_amount_paise"] == 31540000
        assert item["matched_invoice_ids"] == ["INV-0001"]
        assert len(item["entries"]) == 2
        assert item["entries"][0]["entry_type"] == "DEBIT"
        assert item["entries"][1]["entry_type"] == "CREDIT"
        assert item["imbalance_paise"] == 0

    def test_imbalance_proof_is_dr_minus_cr(self):
        db = _LedgerFakeSession(
            batch_rows=[_batch_row()],
            entry_rows=[_entry_row("DEBIT", 10000000), _entry_row("CREDIT", 9800000)],
        )
        client = build_test_client(db)

        response = client.get("/ledger/entries", headers=_auth_header())

        assert response.status_code == 200
        assert response.json()["items"][0]["imbalance_paise"] == 200000

    def test_utr_filter_reaches_query(self):
        db = _LedgerFakeSession(batch_rows=[_batch_row(utr="UTR999")])
        client = build_test_client(db)

        response = client.get(
            "/ledger/entries",
            params={"utr_number": "UTR999"},
            headers=_auth_header(),
        )

        assert response.status_code == 200
        params = db.batch_params[0]
        assert params["vendor_code"] == JWT_VENDOR  # multi-tenant scope
        assert params["utr"] == "UTR999"

    def test_empty_ledger(self):
        db = _LedgerFakeSession()
        client = build_test_client(db)

        response = client.get("/ledger/entries", headers=_auth_header())

        assert response.status_code == 200
        assert response.json() == {"vendor_code": JWT_VENDOR, "total": 0, "items": []}

    def test_non_uuid_batch_filter_is_400(self):
        db = _LedgerFakeSession(batch_rows=[_batch_row()])
        client = build_test_client(db)

        response = client.get(
            "/ledger/entries",
            params={"batch_id": "not-a-uuid"},
            headers=_auth_header(),
        )

        assert response.status_code == 400

    def test_requires_auth(self):
        db = _LedgerFakeSession(batch_rows=[_batch_row()])
        client = build_test_client(db)

        response = client.get("/ledger/entries")

        assert response.status_code == 401
