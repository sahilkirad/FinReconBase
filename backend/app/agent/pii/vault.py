"""
Layer 2 — In-RAM PII Vault (Masking & Rehydration)

Deterministic tokenization for PII that must never reach the Groq/Llama
context window:

    [PAN_TOKEN_1]          supplier/buyer PAN
    [GSTIN_TOKEN_1]        supplier/buyer GSTIN
    [AADHAAR_TOKEN_1]      Aadhaar number
    [ACCOUNT_TOKEN_1]      bank account number
    [IFSC_TOKEN_1]         IFSC code

Design guarantees:

1. RAM-only registry — the vault lives in a process-local dict keyed by an
   opaque run_token. The run_token rides in RunnableConfig.configurable so
   the LangGraph checkpointer (PostgresSaver) NEVER serializes the plaintext
   map. `release_run()` wipes it when the batch run finishes.
2. Deterministic — the same plaintext value always maps to the same token
   within a vault (idempotent), so double tool calls rehydrate identically.
3. Structured masking — known JSON keys (supplier/buyer pan, gstin, banking
   details) are masked by path with the registered label.
4. Free-text scanning — OCR/narration free text is analyzed with the same
   Microsoft Presidio recognizers used in Layer 1 (read-only import — no L1
   modification), and each finding is replaced with its own token.
5. Rehydration happens strictly inside the local Python tool boundary, never
   in the LLM-visible payload.

The deterministic reconciliation tools do not need the plaintext values for
the 3-phase waterfall (they match on vendor_code, invoice numbers, amounts,
dates and DB-side legal names); rehydration is provided for the terminal
tool boundary so any future enrichment leg has exact plaintext access.
"""

import copy
import logging
import re
import threading
import uuid
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Token labels (deterministic token scheme per entity class)
# ---------------------------------------------------------------------------

TOKEN_LABELS: dict[str, str] = {
    "IN_PAN": "PAN",
    "IN_GSTIN": "GSTIN",
    "IN_AADHAAR": "AADHAAR",
    "BANK_ACCOUNT": "ACCOUNT",
    "IN_IFSC": "IFSC",
}

# Entity labels that appear inside structured extracted_invoices JSON
STRUCTURED_FIELD_ENTITY = {
    "pan": "IN_PAN",
    "gstin": "IN_GSTIN",
    "account_number": "BANK_ACCOUNT",
    "ifsc": "IN_IFSC",
    "aadhaar": "IN_AADHAAR",
}

_LOCK = threading.RLock()
_REGISTRY: dict[str, "PIIVault"] = {}


class PIIVault:
    """One vault per reconciliation run (per batch_id / single document).

    Maps token -> plaintext for rehydration and plaintext -> token for
    deterministic idempotent masking.
    """

    def __init__(self, run_token: str):
        self.run_token = run_token
        self._token_to_plain: dict[str, str] = {}
        self._plain_to_token: dict[str, str] = {}
        self._counters: dict[str, int] = {}

    # ---- token allocation ------------------------------------------------

    def _token_for(self, entity_type: str, plaintext: str) -> str:
        """Deterministic per-vault token for a plaintext value."""
        if not plaintext:
            return plaintext
        existing = self._plain_to_token.get(f"{entity_type}:{plaintext}")
        if existing is not None:
            return existing
        label = TOKEN_LABELS.get(entity_type, "PII")
        self._counters[label] = self._counters.get(label, 0) + 1
        token = f"[{label}_TOKEN_{self._counters[label]}]"
        self._token_to_plain[token] = plaintext
        self._plain_to_token[f"{entity_type}:{plaintext}"] = token
        return token

    def rehydrate(self, token: str) -> str | None:
        """Return the plaintext for a token (tool boundary only)."""
        return self._token_to_plain.get(token)

    def mask_token(self, token: str) -> bool:
        """Drop a single token mapping (post-tool-call hygiene)."""
        plain = self._token_to_plain.pop(token, None)
        if plain is not None:
            self._plain_to_token.pop(plain, None)
            return True
        return False

    def rehydrate_text(self, text: str) -> str:
        """Swap every token back to plaintext (tool boundary only)."""
        if not text:
            return text

        def _swap(match: re.Match) -> str:
            return self._token_to_plain.get(match.group(0), match.group(0))

        return re.sub(r"\[[A-Z]+_TOKEN_\d+\]", _swap, text)

    # ---- structured payload masking --------------------------------------

    def mask_invoice_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Return a deep copy with all sensitive structured fields tokenized."""
        masked = copy.deepcopy(payload)

        for section in ("supplier_details", "buyer_details"):
            block = masked.get(section)
            if isinstance(block, dict):
                for field, entity_type in STRUCTURED_FIELD_ENTITY.items():
                    value = block.get(field)
                    if isinstance(value, str) and value.strip():
                        block[field] = self._token_for(entity_type, value)

        banking = masked.get("banking_details")
        if isinstance(banking, dict):
            for field, entity_type in STRUCTURED_FIELD_ENTITY.items():
                value = banking.get(field)
                if isinstance(value, str) and value.strip():
                    banking[field] = self._token_for(entity_type, value)

        return masked


# ---------------------------------------------------------------------------
# Registry helpers
# ---------------------------------------------------------------------------


def new_run_token() -> str:
    return f"run_{uuid.uuid4()}"


def register_run(run_token: str) -> PIIVault:
    """Register a vault and return it. Must be released via release_run()."""
    with _LOCK:
        vault = _REGISTRY.get(run_token)
        if vault is None:
            vault = PIIVault(run_token)
            _REGISTRY[run_token] = vault
        return vault


def get_vault(run_token: str | None) -> PIIVault | None:
    if not run_token:
        return None
    with _LOCK:
        return _REGISTRY.get(run_token)


def release_run(run_token: str | None) -> None:
    """Wipe the vault from RAM when the batch run finishes."""
    if not run_token:
        return
    with _LOCK:
        vault = _REGISTRY.pop(run_token, None)
        if vault is not None:
            vault._token_to_plain.clear()
            vault._plain_to_token.clear()
            logger.info("PII vault released", extra={"run_token": run_token})


# ---------------------------------------------------------------------------
# Free-text Presidio scanning (shared recognizers with Layer 1, read-only)
# ---------------------------------------------------------------------------

_text_analyzer = None
_text_analyzer_lock = threading.Lock()


def _get_analyzer():
    """Lazily build the Presidio analyzer (same recognizers as L1 masker)."""
    global _text_analyzer
    if _text_analyzer is None:
        with _text_analyzer_lock:
            if _text_analyzer is None:
                from app.tools.pii_masker import get_analyzer

                _text_analyzer = get_analyzer()
                logger.info("Layer 2 PII text analyzer initialized")
    return _text_analyzer


def mask_free_text(vault: PIIVault, text: str) -> str:
    """Tokenize PII in arbitrary free text using the shared Presidio engine.

    Bank-account gating mirrors Layer 1 (only digit runs near account
    context words are masked) so invoice amounts/dates are never tokenized.
    Each finding span becomes its own deterministic token in `vault`.
    """
    if not text or not text.strip():
        return text

    analyzer = _get_analyzer()
    from app.tools.pii_masker import ENTITIES, SCORE_THRESHOLD, _has_account_context

    findings = analyzer.analyze(
        text=text,
        language="en",
        entities=ENTITIES,
        score_threshold=SCORE_THRESHOLD,
    )
    findings = [
        r
        for r in findings
        if not (r.entity_type == "BANK_ACCOUNT" and not _has_account_context(text, r.start, r.end))
    ]
    if not findings:
        return text

    out: list[str] = []
    cursor = 0
    for r in sorted(findings, key=lambda r: (r.start, r.end)):
        if r.start < cursor:
            continue  # overlapping spans (defensive)
        out.append(text[cursor:r.start])
        plaintext = text[r.start:r.end]
        out.append(vault._token_for(r.entity_type, plaintext))
        cursor = r.end
    out.append(text[cursor:])
    return "".join(out)
