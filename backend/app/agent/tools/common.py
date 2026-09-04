"""
Shared deterministic helpers for Layer 2 tools.

Monetary rule : floating point is forbidden. Rupee decimal strings
enter tools, are converted to integer paise for every computation, and exit
back as rupee decimal strings.
"""

import re
import uuid
from datetime import datetime, timezone
from decimal import Decimal

RUPEES_STRING_RE = re.compile(r"^\d+(\.\d{1,2})?$")

# Kafka topic names (must mirror app.kafka.config)
RECONCILIATION_COMPLETED_TOPIC = "reconciliation.completed.events"
RECONCILIATION_DLQ_TOPIC = "reconciliation.dlq.events"


class InvalidPaiseStringError(ValueError):
    """Raised when a rupee string is not strict decimal encoding."""


def rupees_to_paise(value: str) -> int:
    """Convert a strict rupee decimal string to integer paise.

    Raises InvalidPaiseStringError on malformed input (commas, >2 decimals,
    negative signs, non-numeric) — the deterministic caller converts this into
    an INVALID_PAISE_CASTING outcome rather than crashing the pipeline.
    """
    if not isinstance(value, str) or not RUPEES_STRING_RE.fullmatch(value):
        raise InvalidPaiseStringError(f"Invalid decimal rupees string: {value!r}")
    rupees, _, fraction = value.partition(".")
    paise = int(rupees) * 100
    if fraction:
        paise += int(fraction.ljust(2, "0"))
    return paise


def paise_to_rupees(paise: int) -> str:
    """Format integer paise back into a strict 2-decimal rupee string."""
    if paise < 0:
        raise InvalidPaiseStringError(f"Negative paise cannot be formatted: {paise}")
    whole, rem = divmod(paise, 100)
    return f"{whole}.{rem:02d}"


def new_event_id() -> str:
    """Idempotency-ready event id (outbox event_id / CloudEvents id)."""
    return f"evt_{uuid.uuid4()}"


def build_cloud_event(event_type: str, source: str, data: dict) -> dict:
    """CNCF CloudEvents 1.0 envelope (mirrors Layer 1 conventions)."""
    return {
        "specversion": "1.0",
        "type": event_type,
        "source": source,
        "id": new_event_id(),
        "time": datetime.now(timezone.utc).isoformat(),
        "data": data,
    }


# =============================================================================
# Outbox insert (transactional outbox pattern — shared by the terminal tools)
# =============================================================================

OUTBOX_INSERT_SQL = """
    INSERT INTO outbox_events (
        event_id, aggregate_type, aggregate_id,
        topic, partition_key, event_type,
        payload, status
    ) VALUES (
        :event_id, :aggregate_type, :aggregate_id,
        :topic, :partition_key, :event_type,
        :payload, 'PENDING'
    )
"""
