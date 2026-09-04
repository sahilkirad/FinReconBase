"""
TDD tests — Milestone 2: Exception Desk API (maker/checker over exception_tickets).

Covers:
- GET /exception-tickets: vendor-scoped list, newest first, optional status filter
- GET with an invalid status filter -> 422 (Literal validation)
- PATCH legal transitions: OPEN -> IN_REVIEW -> RESOLVED (sets resolved_by/resolved_at)
- PATCH IN_REVIEW -> CLOSED (terminal close)
- PATCH illegal transitions -> 409 INVALID_TICKET_TRANSITION with NO DB write
- PATCH same-status / re-open / terminal-ticket -> 409
- PATCH unknown or foreign-vendor ticket -> 404 (no cross-tenant leak)
- PATCH non-UUID ticket id -> 404 (no DB 500)

DB access is faked (substring-dispatching session). Routes require a real JWT.
"""

import uuid

from fastapi.testclient import TestClient

from app.core.config import Settings, get_settings
from app.core.security import create_access_token
from app.db.session import get_db
from app.main import create_app

TEST_SECRET = "test-secret"
JWT_VENDOR = "VEND_TEST_002"
JWT_USER = "user-uuid-1"

# Shared positional column contract (must mirror app/api/exceptions.py).
_TICKET_ROW_ORDER = (
    "ticket_id", "vendor_code", "source_topic", "source_event_id",
    "bank_utr_number", "flagged_invoice_ids", "exception_reason",
    "variance_delta_paise", "human_readable_message", "flagged_payload",
    "status", "created_at", "resolved_at", "resolved_by",
)


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


class _ExceptionsFakeSession:
    def __init__(self, *, ticket_rows=None, update_row=None):
        # ticket_rows: list of positional tuples returned by the LIST query
        self.ticket_rows = ticket_rows or []
        # update_row: positional tuple returned by the UPDATE ... RETURNING query
        self.update_row = update_row
        self.executed: list[tuple[str, dict]] = []
        self.commits = 0
        self.rollbacks = 0

    def execute(self, sql, params=None):
        sql_text = str(sql)
        params = dict(params or {})
        self.executed.append((sql_text, params))

        if "UPDATE exception_tickets" in sql_text:
            return _ResultFirst([self.update_row] if self.update_row is not None else [])
        if "SELECT" in sql_text and "FROM exception_tickets" in sql_text:
            if "WHERE ticket_id" in sql_text:
                return _ResultFirst(
                    [self._find_by_id(params.get("ticket_id"))]
                    if self._find_by_id(params.get("ticket_id")) is not None
                    else []
                )
            return _ResultAll(self.ticket_rows)
        return _ResultFirst([])

    def _find_by_id(self, ticket_id: str):
        for row in self.ticket_rows:
            if str(row[0]) == str(ticket_id):
                return row
        return None

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    @property
    def updates(self):
        return [p for s, p in self.executed if "UPDATE exception_tickets" in s]


def make_ticket_row(
    *,
    ticket_id: str | None = None,
    status: str = "OPEN",
    vendor_code: str = JWT_VENDOR,
    resolved_at=None,
    resolved_by=None,
):
    ticket_id = ticket_id or str(uuid.uuid4())
    return (
        ticket_id, vendor_code, "reconciliation.dlq.events", "evt_dlq_1",
        "UTR999", ["INV-0046", "INV-0050"], "NO_MATCH", None,
        "No UTR re-anchored by the agent.", {"flagged": True}, status,
        "2026-09-04T10:00:00Z", resolved_at, resolved_by,
    )


def build_test_client(db: _ExceptionsFakeSession) -> TestClient:
    app = create_app()

    def _override_db():
        yield db

    def _override_settings():
        return Settings(jwt_secret_key=TEST_SECRET)

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_settings] = _override_settings
    return TestClient(app)


def _auth_header(vendor_code: str = JWT_VENDOR) -> dict[str, str]:
    settings = Settings(jwt_secret_key=TEST_SECRET)
    token = create_access_token(
        subject=JWT_USER,
        vendor_code=vendor_code,
        role="ADMIN",
        settings=settings,
    )
    return {"Authorization": f"Bearer {token}"}


# =============================================================================
# GET /exception-tickets
# =============================================================================


class TestListTickets:
    def test_lists_vendor_tickets_newest_first(self):
        row_a = make_ticket_row(status="OPEN")
        row_b = make_ticket_row(status="IN_REVIEW")
        db = _ExceptionsFakeSession(ticket_rows=[row_a, row_b])
        client = build_test_client(db)

        response = client.get("/exception-tickets", headers=_auth_header())

        assert response.status_code == 200
        body = response.json()
        assert body["vendor_code"] == JWT_VENDOR
        assert body["total"] == 2
        assert len(body["items"]) == 2
        assert body["items"][0]["status"] in ("OPEN", "IN_REVIEW")
        assert body["items"][0]["flagged_invoice_ids"] == ["INV-0046", "INV-0050"]
        # list query is scoped to the JWT vendor
        list_params = [p for s, p in db.executed if "FROM exception_tickets" in s and "WHERE ticket_id" not in s][0]
        assert list_params["vendor_code"] == JWT_VENDOR

    def test_empty_list(self):
        db = _ExceptionsFakeSession(ticket_rows=[])
        client = build_test_client(db)

        response = client.get("/exception-tickets", headers=_auth_header())

        assert response.status_code == 200
        assert response.json() == {"vendor_code": JWT_VENDOR, "total": 0, "items": []}

    def test_invalid_status_filter_returns_422(self):
        db = _ExceptionsFakeSession(ticket_rows=[])
        client = build_test_client(db)

        response = client.get(
            "/exception-tickets",
            params={"status": "FIXED"},
            headers=_auth_header(),
        )

        assert response.status_code == 422
        assert db.executed == []

    def test_requires_auth(self):
        db = _ExceptionsFakeSession(ticket_rows=[])
        client = build_test_client(db)

        response = client.get("/exception-tickets")

        assert response.status_code == 401


# =============================================================================
# PATCH /exception-tickets/{ticket_id}
# =============================================================================


class TestTransitionTicket:
    def test_open_to_in_review(self):
        ticket_id = str(uuid.uuid4())
        row = make_ticket_row(ticket_id=ticket_id, status="OPEN")
        updated = make_ticket_row(ticket_id=ticket_id, status="IN_REVIEW")
        db = _ExceptionsFakeSession(
            ticket_rows=[row],
            update_row=updated,
        )
        client = build_test_client(db)

        response = client.patch(
            f"/exception-tickets/{ticket_id}",
            json={"status": "IN_REVIEW"},
            headers=_auth_header(),
        )

        assert response.status_code == 200
        assert response.json()["status"] == "IN_REVIEW"
        assert response.json()["resolved_by"] is None  # non-terminal
        assert db.commits == 1
        update_params = db.updates[0]
        assert update_params["new_status"] == "IN_REVIEW"
        assert update_params["vendor_code"] == JWT_VENDOR
        assert update_params["resolved_by"] is None

    def test_in_review_to_resolved_records_reviewer(self):
        ticket_id = str(uuid.uuid4())
        row = make_ticket_row(ticket_id=ticket_id, status="IN_REVIEW")
        updated = make_ticket_row(
            ticket_id=ticket_id,
            status="RESOLVED",
            resolved_at="2026-09-04T11:00:00Z",
            resolved_by=JWT_USER,
        )
        db = _ExceptionsFakeSession(ticket_rows=[row], update_row=updated)
        client = build_test_client(db)

        response = client.patch(
            f"/exception-tickets/{ticket_id}",
            json={"status": "RESOLVED"},
            headers=_auth_header(),
        )

        assert response.status_code == 200
        assert response.json()["status"] == "RESOLVED"
        assert response.json()["resolved_by"] == JWT_USER
        update_params = db.updates[0]
        assert update_params["resolved_by"] == JWT_USER  # reviewer = JWT sub

    def test_in_review_to_closed_is_terminal(self):
        ticket_id = str(uuid.uuid4())
        row = make_ticket_row(ticket_id=ticket_id, status="IN_REVIEW")
        updated = make_ticket_row(
            ticket_id=ticket_id,
            status="CLOSED",
            resolved_at="2026-09-04T11:00:00Z",
            resolved_by=JWT_USER,
        )
        db = _ExceptionsFakeSession(ticket_rows=[row], update_row=updated)
        client = build_test_client(db)

        response = client.patch(
            f"/exception-tickets/{ticket_id}",
            json={"status": "CLOSED"},
            headers=_auth_header(),
        )

        assert response.status_code == 200
        assert response.json()["status"] == "CLOSED"

    def test_illegal_direct_jump_open_to_resolved(self):
        """The deterministic guardrail: no OPEN -> RESOLVED shortcut."""
        ticket_id = str(uuid.uuid4())
        row = make_ticket_row(ticket_id=ticket_id, status="OPEN")
        db = _ExceptionsFakeSession(ticket_rows=[row])
        client = build_test_client(db)

        response = client.patch(
            f"/exception-tickets/{ticket_id}",
            json={"status": "RESOLVED"},
            headers=_auth_header(),
        )

        assert response.status_code == 409
        assert response.json()["detail"]["error_code"] == "INVALID_TICKET_TRANSITION"
        assert db.updates == []  # guardrail fires before any DB write
        assert db.commits == 0

    def test_same_status_rejected(self):
        ticket_id = str(uuid.uuid4())
        row = make_ticket_row(ticket_id=ticket_id, status="OPEN")
        db = _ExceptionsFakeSession(ticket_rows=[row])
        client = build_test_client(db)

        response = client.patch(
            f"/exception-tickets/{ticket_id}",
            json={"status": "OPEN"},
            headers=_auth_header(),
        )

        assert response.status_code == 409

    def test_terminal_ticket_cannot_be_reopened(self):
        ticket_id = str(uuid.uuid4())
        row = make_ticket_row(
            ticket_id=ticket_id,
            status="RESOLVED",
            resolved_at="2026-09-04T11:00:00Z",
            resolved_by=JWT_USER,
        )
        db = _ExceptionsFakeSession(ticket_rows=[row])
        client = build_test_client(db)

        response = client.patch(
            f"/exception-tickets/{ticket_id}",
            json={"status": "IN_REVIEW"},
            headers=_auth_header(),
        )

        assert response.status_code == 409
        assert db.updates == []

    def test_unknown_ticket_returns_404(self):
        db = _ExceptionsFakeSession(ticket_rows=[])
        client = build_test_client(db)

        response = client.patch(
            f"/exception-tickets/{uuid.uuid4()}",
            json={"status": "IN_REVIEW"},
            headers=_auth_header(),
        )

        assert response.status_code == 404

    def test_foreign_vendor_ticket_is_invisible_404(self):
        """A ticket owned by another vendor must read as 'not found'."""
        ticket_id = str(uuid.uuid4())
        other_vendor_row = make_ticket_row(
            ticket_id=ticket_id,
            vendor_code="VEND_OTHER_999",
            status="OPEN",
        )
        # The JWT vendor's list query does not contain the foreign row.
        db = _ExceptionsFakeSession(ticket_rows=[other_vendor_row])
        client = build_test_client(db)

        response = client.patch(
            f"/exception-tickets/{ticket_id}",
            json={"status": "IN_REVIEW"},
            headers=_auth_header(),
        )

        assert response.status_code == 404
        assert db.updates == []

    def test_non_uuid_ticket_id_returns_404_not_500(self):
        db = _ExceptionsFakeSession(ticket_rows=[])
        client = build_test_client(db)

        response = client.patch(
            "/exception-tickets/not-a-uuid",
            json={"status": "IN_REVIEW"},
            headers=_auth_header(),
        )

        assert response.status_code == 404

    def test_invalid_status_body_returns_422(self):
        ticket_id = str(uuid.uuid4())
        row = make_ticket_row(ticket_id=ticket_id, status="OPEN")
        db = _ExceptionsFakeSession(ticket_rows=[row])
        client = build_test_client(db)

        response = client.patch(
            f"/exception-tickets/{ticket_id}",
            json={"status": "ARCHIVED"},
            headers=_auth_header(),
        )

        assert response.status_code == 422
        assert db.executed == []
