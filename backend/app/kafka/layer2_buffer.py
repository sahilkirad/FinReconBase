"""Layer 2 — Kafka Redis buffer (consumer handoff staging)."""

import json
import logging

logger = logging.getLogger(__name__)

# Key prefixes (kept here so consumer + poller agree on layout)
BATCH_BUFFER_PREFIX = "layer2:batch_buffer"   # LIST  layer2:batch_buffer:{batch_id}
SEEN_PREFIX = "layer2:seen"                   # SET   layer2:seen:{batch_id}
SINGLE_BUFFER_KEY = "layer2:single_buffer"    # LIST  (non-batch events)

# Dedupe+push performed atomically in Lua: only push when the event id is new.
_PUSH_LUA = """
local seen_key = KEYS[1]
local list_key = KEYS[2]
local event_id = ARGV[1]
local payload = ARGV[2]
if redis.call('SADD', seen_key, event_id) == 1 then
    redis.call('RPUSH', list_key, payload)
    return 1
end
return 0
"""


class Layer2RedisBuffer:
    """Redis staging used by the Kafka consumer.

    - push_batch(batch_id, event): dedupes by CloudEvents id (SADD) then RPUSH.
    - push_single(event): non-batch events land in a shared list; the boundary
      poller dispatches each entry as an immediate single-item run.
    - length / drain / cleanup helpers.
    """

    def __init__(self, redis_url: str, client=None):
        if client is not None:
            self.redis = client  # injected (tests / fakes)
        else:
            import redis as redis_lib  # lazy: local shells may lack the package

            self.redis = redis_lib.Redis.from_url(redis_url, decode_responses=True)

    # ---- batch events ----------------------------------------------------

    @staticmethod
    def batch_key(batch_id: str) -> str:
        return f"{BATCH_BUFFER_PREFIX}:{batch_id}"

    @staticmethod
    def seen_key(batch_id: str) -> str:
        return f"{SEEN_PREFIX}:{batch_id}"

    def push_batch(self, batch_id: str, event: dict) -> bool:
        """Append a CloudEvents payload to the batch buffer (idempotent).

        Returns True if the event was newly appended, False on duplicate.
        """
        event_id = event.get("id") or event.get("data", {}).get("document_id", "")
        if not event_id:
            return False
        payload = json.dumps(event)
        added = self.redis.eval(
            _PUSH_LUA,
            2,
            self.seen_key(batch_id),
            self.batch_key(batch_id),
            event_id,
            payload,
        )
        return bool(added)

    def length(self, batch_id: str) -> int:
        return int(self.redis.llen(self.batch_key(batch_id)) or 0)

    def drain_batch(self, batch_id: str) -> list[dict]:
        """Atomically pop all payloads for a batch (fan-in materialization)."""
        raw = self.redis.lrange(self.batch_key(batch_id), 0, -1)
        events = [json.loads(p) for p in raw]
        self.redis.delete(self.batch_key(batch_id))
        return events

    # ---- single (non-batch) events --------------------------------------

    def push_single(self, event: dict) -> bool:
        event_id = event.get("id") or event.get("data", {}).get("document_id", "")
        if not event_id:
            return False
        payload = json.dumps(event)
        added = self.redis.eval(
            _PUSH_LUA,
            2,
            f"{SEEN_PREFIX}:single",
            SINGLE_BUFFER_KEY,
            event_id,
            payload,
        )
        return bool(added)

    def drain_singles(self, limit: int = 20) -> list[dict]:
        """Pop up to `limit` single events for immediate dispatch."""
        raw = self.redis.lrange(SINGLE_BUFFER_KEY, 0, limit - 1)
        if not raw:
            return []
        self.redis.ltrim(SINGLE_BUFFER_KEY, len(raw), -1)
        return [json.loads(p) for p in raw]

    # ---- cleanup ---------------------------------------------------------

    def cleanup_batch(self, batch_id: str) -> None:
        """Remove buffer + dedupe keys after a run closes."""
        self.redis.delete(self.batch_key(batch_id), self.seen_key(batch_id))

    def close(self) -> None:
        try:
            self.redis.close()
        except Exception:  # pragma: no cover
            pass
