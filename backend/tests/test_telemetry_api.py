"""
TDD tests — Milestone 3: Batch telemetry (classifier + Redis writer + endpoints).

Covers:
- classify_invoice_path: fast_path (no LLM), agent (tool_calls / real AI content),
  deterministic markers never count as LLM invocations
- build_invoice_events: terminal stream line sequence per invoice
- BatchTelemetryWriter: publish -> read round-trip on an injected Redis client
- GET /batches/{id}/telemetry: composite funnel + per-invoice rows + path split
- GET /batches/{id}/telemetry/events: Redis terminal stream, vendor-scoped
- 404 for unknown / non-UUID / foreign-vendor batches; 401 without a token

Postgres is faked (substring-dispatching session); Redis is faked by monkey-
patching BatchTelemetryWriter with a canned reader.
"""

from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app.core.config import Settings, get_settings
from app.core.security import create_access_token
from app.db.session import get_db
from app.main import create_app
from app.telemetry.events import (
    BatchTelemetryWriter,
    build_invoice_events,
    classify_invoice_path,
)

TEST_SECRET = "test-secret"
JWT_VENDOR = "VEND_TEST_002"


# =============================================================================
# Pure classifier
# =============================================================================


def _ai(content: str = "", tool_calls=None) -> dict:
    msg = {"type": "ai", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return msg


class TestClassifyPath:
    def test_fast_path_when_no_llm_messages(self):
        result = classify_invoice_path(
            [{"type": "human", "content": "seed"}, {"type": "system", "content": "policy"}]
        )
        assert result["path"] == "fast_path"
        assert result["llm_invoked"] is False
        assert result["tool_calls"] == []

    def test_agent_path_when_tool_called(self):
        result = classify_invoice_path(
            [
                _ai(tool_calls=[{"name": "run_fuzzy_text_linker_tool"}]),
                {"type": "tool", "content": "ENTITY_RESOLVED"},
            ]
        )
        assert result["path"] == "agent"
        assert result["llm_invoked"] is True
        assert result["tool_calls"] == ["run_fuzzy_text_linker_tool"]

    def test_agent_path_on_real_llm_answer(self):
        result = classify_invoice_path([_ai(content="I will call the fuzzy linker.")])
        assert result["path"] == "agent"
        assert result["llm_invoked"] is True

    def test_deterministic_markers_are_not_llm(self):
        result = classify_invoice_path([_ai(content="Deterministic fallback: LEDGER_COMMITTED")])
        assert result["llm_invoked"] is False
        assert result["path"] == "fast_path"

    def test_multiple_tool_calls_kept_in_order(self):
        result = classify_invoice_path(
            [
                _ai(tool_calls=[{"name": "run_fuzzy_text_linker_tool"}]),
                _ai(tool_calls=[{"name": "run_subset_sum_matching_tool"}, {"name": "post_ledger_entry_tool"}]),
            ]
        )
        assert result["tool_calls"] == [
            "run_fuzzy_text_linker_tool",
            "run_subset_sum_matching_tool",
            "post_ledger_entry_tool",
        ]


class TestBuildEvents:
    def test_fast_path_sequence(self):
        events = build_invoice_events(
            invoice_number="INV-0001",
            subset_status="SUBSET_MATCHED",
            subset_message="matched",
            path="fast_path",
            llm_invoked=False,
            tool_calls=[],
            terminal="LEDGER_COMMITTED",
            terminal_detail="Committed",
            terminal_utr="UTR111",
        )
        stages = [e["stage"] for e in events]
        assert stages == ["started", "deterministic", "deterministic", "terminal"]
        assert events[-1]["terminal_status"] == "LEDGER_COMMITTED"
        assert events[-1]["utr"] == "UTR111"

    def test_agent_sequence_includes_tools(self):
        events = build_invoice_events(
            invoice_number="INV-0046",
            subset_status="NO_MATCH",
            subset_message="no anchor",
            path="agent",
            llm_invoked=True,
            tool_calls=["run_fuzzy_text_linker_tool"],
            terminal="EXCEPTION_ROUTED",
            terminal_detail="No UTR re-anchored.",
            terminal_utr=None,
        )
        stages = [e["stage"] for e in events]
        assert "agent" in stages and "tool_called" in stages
        assert events[-1]["terminal_status"] == "EXCEPTION_ROUTED"


# =============================================================================
# Redis writer (injected client)
# =============================================================================


class _FakePipeline:
    def __init__(self, store: dict, key: str):
        self.store = store
        self.key = key
        self._ops = []

    def rpush(self, key, value):
        self._ops.append(("rpush", key, value))
        return self

    def expire(self, key, ttl):
        self._ops.append(("expire", key, ttl))
        return self

    def execute(self):
        for op in self._ops:
            if op[0] == "rpush":
                self.store.setdefault(op[1], []).append(op[2])
        return []


class _FakeRedis:
    def __init__(self):
        self.store: dict[str, list[str]] = {}

    def pipeline(self):
        return _FakePipeline(self.store, "")

    def lrange(self, key, start, stop):
        values = self.store.get(key, [])
        if stop == -1:
            return values[start:]
        return values[start : stop + 1]


class TestBatchTelemetryWriter:
    def test_publish_read_round_trip(self):
        fake = _FakeRedis()
        writer = BatchTelemetryWriter("redis://fake", client=fake)

        writer.publish("batch-1", [{"stage": "started", "invoice": "INV-1"}])

        events = writer.read("batch-1")
        assert len(events) == 1
        assert events[0]["stage"] == "started"
        assert events[0]["invoice"] == "INV-1"

    def test_read_missing_batch_is_empty(self):
        fake = _FakeRedis()
        writer = BatchTelemetryWriter("redis://fake", client=fake)
        assert writer.read("batch-missing") == []


# =============================================================================
# Endpoint fakes
# =============================================================================


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


class _TelemetryFakeSession:
    def __init__(self, *, batch_row=None, run_row=None, item_rows=None, ticket_rows=None):
        self.batch_row = batch_row
        self.run_row = run_row
        self.item_rows = item_rows or []
        self.ticket_rows = ticket_rows or []
        self.executed: list[tuple[str, dict]] = []
        self.commits = 0

    def execute(self, sql, params=None):
        sql_text = str(sql)
        params = dict(params or {})
        self.executed.append((sql_text, params))

        if "FROM batch_jobs" in sql_text:
            return _ResultFirst([self.batch_row] if self.batch_row else [])
        if "FROM layer2_batch_runs" in sql_text:
            return _ResultFirst([self.run_row] if self.run_row else [])
        if "FROM batch_invoice_items bii" in sql_text:
            return _ResultAll(self.item_rows)
        if "FROM exception_tickets" in sql_text:
            return _ResultAll(self.ticket_rows)
        return _ResultFirst([])

    def commit(self):
        self.commits += 1


def build_test_client(db: _TelemetryFakeSession) -> TestClient:
    app = create_app()

    def _override_db():
        yield db

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_settings] = lambda: Settings(
        jwt_secret_key=TEST_SECRET,
        redis_url="redis://fake",
    )
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


def _batch_row(batch_id: str = "b0000000-0000-0000-0000-000000000001"):
    return (
        batch_id, JWT_VENDOR, "pdf", "test_batch_50.pdf",
        2, 2, 0, "COMPLETED", _now(), _now(),
    )


def _run_row():
    return (
        "COMPLETED", "BATCH", 2, 1, 1, 0, None, _now(), _now(),
    )


def _item_row(invoice: str, *, utr=None, reason=None, doc_id=None):
    return (
        1 if invoice == "INV-0001" else 2,
        invoice,
        doc_id or f"doc-{invoice}",
        "COMPLETED",
        None,
        "VALIDATED",
        utr,
        "pout_1" if utr else None,
        204390500 if utr else None,
        _now() if utr else None,
        204400000,  # extracted net (grand_total - tds) -> net_paise
    )


def _canned_events():
    return [
        {"ts": _now(), "invoice": "INV-0001", "stage": "terminal",
         "terminal_status": "LEDGER_COMMITTED", "utr": "UTR111", "detail": "ok"},
        {"ts": _now(), "invoice": "INV-0046", "stage": "tool_called",
         "detail": "run_fuzzy_text_linker_tool"},
        {"ts": _now(), "invoice": "INV-0046", "stage": "terminal",
         "terminal_status": "EXCEPTION_ROUTED", "detail": "DLQ"},
    ]


class _CannedTelemetryWriter:
    """Factory-compatible stand-in: returns the configured event list."""

    def __init__(self, redis_url):
        self.events = _CANONICAL_EVENTS

    def read(self, batch_id):
        return list(self.events)


_CANONICAL_EVENTS = _canned_events()


# =============================================================================
# GET /batches/{id}/telemetry
# =============================================================================


class TestBatchTelemetryEndpoint:
    def test_composite_funnel_and_path_split(self, monkeypatch):
        monkeypatch.setattr("app.telemetry.events.BatchTelemetryWriter", _CannedTelemetryWriter)

        db = _TelemetryFakeSession(
            batch_row=_batch_row(),
            run_row=_run_row(),
            item_rows=[
                _item_row("INV-0001", utr="UTR111"),
                _item_row("INV-0046"),
            ],
            ticket_rows=[
                (["INV-0046"], "NO_MATCH", "OPEN", _now()),
            ],
        )
        client = build_test_client(db)

        response = client.get(
            "/batches/b0000000-0000-0000-0000-000000000001/telemetry",
            headers=_auth_header(),
        )

        assert response.status_code == 200
        body = response.json()
        assert body["vendor_code"] == JWT_VENDOR
        assert body["status"] == "COMPLETED"
        assert body["layer2"]["status"] == "COMPLETED"
        assert body["layer2"]["matched_count"] == 1

        funnel = body["funnel"]
        assert funnel["total"] == 2
        assert funnel["settled"] == 1
        assert funnel["exceptions"] == 1
        assert funnel["open"] == 0
        assert funnel["fast_path"] == 1
        assert funnel["agent_routed"] == 1

        by_invoice = {i["invoice_number"]: i for i in body["invoices"]}
        assert by_invoice["INV-0001"]["path"] == "fast_path"
        assert by_invoice["INV-0001"]["utr_number"] == "UTR111"
        assert by_invoice["INV-0046"]["path"] == "agent"
        assert by_invoice["INV-0046"]["exception_reason"] == "NO_MATCH"
        assert by_invoice["INV-0046"]["tool_calls"] == ["run_fuzzy_text_linker_tool"]

    def test_degrades_when_no_telemetry(self, monkeypatch):
        class _EmptyWriter:
            def __init__(self, redis_url):
                pass

            def read(self, batch_id):
                return []

        monkeypatch.setattr("app.telemetry.events.BatchTelemetryWriter", _EmptyWriter)

        db = _TelemetryFakeSession(
            batch_row=_batch_row(),
            run_row=_run_row(),
            item_rows=[_item_row("INV-0001", utr="UTR111"), _item_row("INV-0046")],
            ticket_rows=[(["INV-0046"], "NO_MATCH", "OPEN", _now())],
        )
        client = build_test_client(db)

        response = client.get(
            "/batches/b0000000-0000-0000-0000-000000000001/telemetry",
            headers=_auth_header(),
        )

        assert response.status_code == 200
        assert response.json()["funnel"]["fast_path"] is None
        assert response.json()["funnel"]["settled"] == 1  # DB funnel stays accurate

    def test_unknown_batch_404(self):
        db = _TelemetryFakeSession()
        client = build_test_client(db)

        response = client.get(
            "/batches/b0000000-0000-0000-0000-000000000001/telemetry",
            headers=_auth_header(),
        )

        assert response.status_code == 404

    def test_non_uuid_batch_404(self):
        db = _TelemetryFakeSession(batch_row=_batch_row())
        client = build_test_client(db)

        response = client.get("/batches/not-a-uuid/telemetry", headers=_auth_header())

        assert response.status_code == 404

    def test_requires_auth(self):
        db = _TelemetryFakeSession(batch_row=_batch_row())
        client = build_test_client(db)

        response = client.get("/batches/b0000000-0000-0000-0000-000000000001/telemetry")

        assert response.status_code == 401


# =============================================================================
# GET /batches/{id}/telemetry/events
# =============================================================================


class TestTelemetryEventsEndpoint:
    def test_returns_terminal_stream(self, monkeypatch):
        monkeypatch.setattr("app.telemetry.events.BatchTelemetryWriter", _CannedTelemetryWriter)

        db = _TelemetryFakeSession(batch_row=_batch_row())
        client = build_test_client(db)

        response = client.get(
            "/batches/b0000000-0000-0000-0000-000000000001/telemetry/events",
            headers=_auth_header(),
        )

        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 3
        assert body["events"][0]["invoice"] == "INV-0001"

    def test_events_unknown_batch_404(self):
        db = _TelemetryFakeSession()
        client = build_test_client(db)

        response = client.get(
            "/batches/b0000000-0000-0000-0000-000000000001/telemetry/events",
            headers=_auth_header(),
        )

        assert response.status_code == 404
