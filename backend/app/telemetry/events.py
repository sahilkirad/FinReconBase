"""
Layer 2 live telemetry — per-invoice stage events (ephemeral, Redis only).

WHY Redis and not a table: the Core-logic schema is frozen, and the live
monitoring page does not need durable history — it needs the *current* run's
funnel + agent terminal stream. Events live under ``layer2:telemetry:{batch_id}``
(LIST) with a 2-hour TTL and are written ONLY by the recon supervisor worker;
the FastAPI dashboard reads them through GET /batches/{id}/telemetry/events.

Purity contract:
    classify_invoice_path / build_invoice_events take plain data (message
    objects are introspected via duck-typing: ``.tool_calls`` / ``.type`` /
    dict access) so they are unit-testable without langchain installed.
"""

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

BATCH_TELEMETRY_PREFIX = "layer2:telemetry"  # LIST layer2:telemetry:{batch_id}
TELEMETRY_TTL_S = 60 * 60 * 2  # 2-hour live window per batch

# Content markers of AIMessages written by deterministic code paths (never
# from the Groq model) — used to tell "LLM really invoked" from "LLM-free".
_DETERMINISTIC_MARKERS = (
    "Deterministic fallback:",
    "Groq invocation failed",
)

FAST_PATH_TERMINALS = {"LEDGER_COMMITTED", "ALREADY_COMMITTED"}


def _message_type(message: Any) -> str:
    if hasattr(message, "type"):
        return str(message.type)
    if isinstance(message, dict):
        return str(message.get("type") or "")
    return ""


def _message_tool_calls(message: Any) -> list:
    calls = getattr(message, "tool_calls", None)
    if calls is None and isinstance(message, dict):
        calls = message.get("tool_calls")
    return list(calls or [])


def _message_content(message: Any) -> str:
    content = getattr(message, "content", None)
    if content is None and isinstance(message, dict):
        content = message.get("content")
    return str(content or "")


def classify_invoice_path(messages: list) -> dict:
    """Classify how ONE invoice's sub-graph reached its terminal state.

    Returns:
        {
            "path": "fast_path" | "agent" | "deterministic_fallback" | "unknown",
            "llm_invoked": bool,   # Groq produced at least one real answer
            "tool_calls": [names]  # ordered tool executions (ReAct observe)
        }

    Rules (derived from the graph's state messages — no execution changes):
    - An AIMessage carrying tool_calls, or any AI content NOT written by the
      deterministic markers, proves the Groq model ran  -> llm_invoked.
    - tool_calls are collected in message order for the ReAct terminal log.
    """
    tool_calls: list[str] = []
    llm_invoked = False
    for message in messages:
        if _message_type(message) != "ai":
            continue
        calls = _message_tool_calls(message)
        for call in calls:
            name = call.get("name") if isinstance(call, dict) else getattr(call, "name", None)
            if name:
                tool_calls.append(str(name))
        content = _message_content(message)
        if calls or not content.startswith(_DETERMINISTIC_MARKERS):
            llm_invoked = True

    if not tool_calls and not llm_invoked:
        path = "fast_path"
    elif llm_invoked or tool_calls:
        path = "agent"
    else:  # pragma: no cover — defensive
        path = "deterministic_fallback"
    return {"path": path, "llm_invoked": llm_invoked, "tool_calls": tool_calls}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def build_invoice_events(
    *,
    invoice_number: str,
    subset_status: str | None,
    subset_message: str | None,
    path: str,
    llm_invoked: bool,
    tool_calls: list,
    terminal: str,
    terminal_detail: str,
    terminal_utr: str | None,
) -> list[dict]:
    """The ReAct terminal-log line sequence for one invoice (pure)."""
    ts = _now_iso()
    events: list[dict] = [
        {
            "ts": ts,
            "invoice": invoice_number,
            "stage": "started",
            "detail": "Sub-graph dispatched (Map leaf)",
        }
    ]
    if subset_status:
        events.append(
            {
                "ts": ts,
                "invoice": invoice_number,
                "stage": "deterministic",
                "detail": (
                    f"run_subset_sum_matching_tool -> {subset_status}"
                    + (f" :: {(subset_message or '')[:200]}" if subset_message else "")
                ),
            }
        )
    if path == "fast_path":
        events.append(
            {
                "ts": ts,
                "invoice": invoice_number,
                "stage": "deterministic",
                "detail": "Deterministic fast-path — Groq LLM never invoked",
            }
        )
    elif llm_invoked:
        events.append(
            {
                "ts": ts,
                "invoice": invoice_number,
                "stage": "agent",
                "detail": "ReAct agent invoked (tool-bound Groq)",
            }
        )
    for tool in tool_calls:
        events.append(
            {
                "ts": ts,
                "invoice": invoice_number,
                "stage": "tool_called",
                "detail": str(tool),
            }
        )
    detail = (terminal_detail or "")[:240]
    events.append(
        {
            "ts": ts,
            "invoice": invoice_number,
            "stage": "terminal",
            "terminal_status": terminal,
            "utr": terminal_utr,
            "detail": detail or terminal,
        }
    )
    return events


def build_batch_event(batch_id: str, stage: str, detail: str, **extra) -> dict:
    return {
        "ts": _now_iso(),
        "batch_id": str(batch_id),
        "stage": stage,
        "detail": detail,
        **extra,
    }


class BatchTelemetryWriter:
    """Redis LIST writer/reader for one batch's telemetry stream.

    The same class serves both sides: the worker publishes (RPUSH + refresh
    TTL), the FastAPI dashboard reads (LRANGE). decode_responses=True so
    payloads are str. Failures never propagate: telemetry is best-effort and
    must NEVER break the reconciliation pipeline.
    """

    def __init__(self, redis_url: str, client=None):
        if client is not None:
            self.redis = client  # injected (tests / fakes)
        else:
            import redis as redis_lib  # lazy: local shells may lack the package

            self.redis = redis_lib.Redis.from_url(redis_url, decode_responses=True)

    @staticmethod
    def key(batch_id: str) -> str:
        return f"{BATCH_TELEMETRY_PREFIX}:{batch_id}"

    def publish(self, batch_id: str, events: list[dict]) -> None:
        """Append stage events to the batch stream (RPUSH + extend TTL)."""
        if not events or not batch_id:
            return
        try:
            pipe = self.redis.pipeline()
            for event in events:
                pipe.rpush(self.key(batch_id), json.dumps(event))
            pipe.expire(self.key(batch_id), TELEMETRY_TTL_S)
            pipe.execute()
        except Exception as exc:  # pragma: no cover — best-effort contract
            logger.warning(
                "Telemetry publish failed (ignored)",
                extra={"batch_id": batch_id, "error": str(exc)},
            )

    def read(self, batch_id: str) -> list[dict]:
        try:
            raw = self.redis.lrange(self.key(batch_id), 0, -1)
        except Exception as exc:  # pragma: no cover
            logger.warning("Telemetry read failed", extra={"error": str(exc)})
            return []
        events: list[dict] = []
        for payload in raw or []:
            try:
                events.append(json.loads(payload))
            except (TypeError, ValueError):
                continue
        return events
