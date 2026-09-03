"""
TDD tests — Layer 2 consumer Redis buffer (dedupe + fan-in staging).

Uses an in-memory fake redis client so the tests run without a live Redis;
the Lua-dedupe contract (SADD + RPUSH atomically) is what we verify.
"""

import json

from app.kafka.layer2_buffer import Layer2RedisBuffer


class FakeRedis:
    """Minimal in-memory redis double implementing the buffer surface."""

    def __init__(self):
        self._sets: dict[str, set] = {}
        self._lists: dict[str, list] = {}

    def eval(self, script, num_keys, *args):
        # script: SADD seen; if ==1 then RPUSH list; return 1 else 0
        keys = args[:num_keys]
        rest = args[num_keys:]
        seen_key, list_key = keys
        event_id, payload = rest
        seen = self._sets.setdefault(seen_key, set())
        if event_id in seen:
            return 0
        seen.add(event_id)
        self._lists.setdefault(list_key, []).append(payload)
        return 1

    def llen(self, key):
        return len(self._lists.get(key, []))

    def lrange(self, key, start, end):
        items = self._lists.get(key, [])
        if end == -1:
            return items[start:]
        return items[start:end + 1]

    def ltrim(self, key, start, end):
        items = self._lists.get(key, [])
        if end == -1:
            kept = items[start:]
        else:
            kept = items[start:end + 1]
        self._lists[key] = kept

    def delete(self, *keys):
        for k in keys:
            self._sets.pop(k, None)
            self._lists.pop(k, None)
        return 1


def _event(event_id: str, batch_id: str | None = "b1") -> dict:
    return {
        "specversion": "1.0",
        "type": "invoice.extracted",
        "id": event_id,
        "data": {
            "document_id": event_id,
            "vendor_code": "VEND_TEST",
            "batch_id": batch_id,
        },
    }


class TestPushBatch:
    def test_first_push_appends(self):
        buf = Layer2RedisBuffer("redis://fake", client=FakeRedis())
        assert buf.push_batch("b1", _event("evt1", "b1")) is True
        assert buf.length("b1") == 1

    def test_duplicate_push_is_deduped(self):
        buf = Layer2RedisBuffer("redis://fake", client=FakeRedis())
        event = _event("evt1", "b1")
        assert buf.push_batch("b1", event) is True
        assert buf.push_batch("b1", event) is False
        assert buf.length("b1") == 1  # single copy

    def test_distinct_events_both_append(self):
        buf = Layer2RedisBuffer("redis://fake", client=FakeRedis())
        buf.push_batch("b1", _event("evt1", "b1"))
        buf.push_batch("b1", _event("evt2", "b1"))
        assert buf.length("b1") == 2

    def test_same_event_id_other_batch_not_confused(self):
        buf = Layer2RedisBuffer("redis://fake", client=FakeRedis())
        buf.push_batch("b1", _event("evt1", "b1"))
        buf.push_batch("b2", _event("evt1", "b2"))
        assert buf.length("b1") == 1
        assert buf.length("b2") == 1

    def test_missing_id_not_appended(self):
        buf = Layer2RedisBuffer("redis://fake", client=FakeRedis())
        assert buf.push_batch("b1", {"data": {}}) is False
        assert buf.length("b1") == 0


class TestDrain:
    def test_drain_returns_payloads_and_empties_list(self):
        buf = Layer2RedisBuffer("redis://fake", client=FakeRedis())
        buf.push_batch("b1", _event("evt1", "b1"))
        buf.push_batch("b1", _event("evt2", "b1"))
        drained = buf.drain_batch("b1")
        assert len(drained) == 2
        assert {d["id"] for d in drained} == {"evt1", "evt2"}
        assert buf.length("b1") == 0

    def test_drain_empty_batch(self):
        buf = Layer2RedisBuffer("redis://fake", client=FakeRedis())
        assert buf.drain_batch("nope") == []


class TestSingles:
    def test_push_single_deduped(self):
        buf = Layer2RedisBuffer("redis://fake", client=FakeRedis())
        event = _event("evt_single", None)
        assert buf.push_single(event) is True
        assert buf.push_single(event) is False
        drained = buf.drain_singles(limit=10)
        assert len(drained) == 1
        assert drained[0]["id"] == "evt_single"

    def test_drain_singles_limit(self):
        buf = Layer2RedisBuffer("redis://fake", client=FakeRedis())
        for i in range(5):
            buf.push_single(_event(f"evt_s{i}", None))
        first = buf.drain_singles(limit=3)
        second = buf.drain_singles(limit=10)
        assert len(first) == 3
        assert len(second) == 2

    def test_single_and_batch_namespaces_isolated(self):
        buf = Layer2RedisBuffer("redis://fake", client=FakeRedis())
        buf.push_batch("b1", _event("evt_shared", "b1"))
        buf.push_single(_event("evt_shared", None))
        assert buf.length("b1") == 1


class TestCleanup:
    def test_cleanup_batch_removes_buffer_and_seen(self):
        buf = Layer2RedisBuffer("redis://fake", client=FakeRedis())
        buf.push_batch("b1", _event("evt1", "b1"))
        buf.cleanup_batch("b1")
        assert buf.length("b1") == 0
        # After cleanup the same event can be buffered again (fresh run)
        assert buf.push_batch("b1", _event("evt1", "b1")) is True

    def test_payload_json_round_trip(self):
        buf = Layer2RedisBuffer("redis://fake", client=FakeRedis())
        event = _event("evt1", "b1")
        buf.push_batch("b1", event)
        drained = buf.drain_batch("b1")
        assert drained[0] == event
