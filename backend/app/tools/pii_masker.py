"""
PII Masking Middleware — Microsoft Presidio

Masks sensitive Indian financial data (PAN, Aadhaar, IFSC, GSTIN, bank
account numbers, phone numbers) from raw OCR text before it reaches the
Gemini VLM context window.

Pipeline:
1. Presidio AnalyzerEngine detects entities (custom PatternRecognizers for
   Indian financial identifiers + predefined PHONE_NUMBER recognizer).
2. Presidio AnonymizerEngine replaces each finding with a standard
   placeholder such as [PAN_REDACTED].

RULE: Tools use real data locally in RAM only. Any text sent to an LLM
context window must be masked first.
"""

import copy
import logging
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Placeholders (standard redaction tokens)
# ---------------------------------------------------------------------------
PAN_PLACEHOLDER = "[PAN_REDACTED]"
AADHAAR_PLACEHOLDER = "[AADHAAR_REDACTED]"
IFSC_PLACEHOLDER = "[IFSC_REDACTED]"
GSTIN_PLACEHOLDER = "[GSTIN_REDACTED]"
ACCOUNT_PLACEHOLDER = "[ACCOUNT_REDACTED]"
PHONE_PLACEHOLDER = "[PHONE_REDACTED]"

# entity_type -> placeholder
ENTITY_PLACEHOLDERS: dict[str, str] = {
    "IN_PAN": PAN_PLACEHOLDER,
    "IN_AADHAAR": AADHAAR_PLACEHOLDER,
    "IN_IFSC": IFSC_PLACEHOLDER,
    "IN_GSTIN": GSTIN_PLACEHOLDER,
    "BANK_ACCOUNT": ACCOUNT_PLACEHOLDER,
    "PHONE_NUMBER": PHONE_PLACEHOLDER,
}

# Only these entity types are analyzed (explicit allow-list)
ENTITIES: list[str] = list(ENTITY_PLACEHOLDERS.keys())

# Findings below this score are dropped. Bank account numbers (9-18 digit
# runs) are additionally gated on nearby context words so invoice amounts,
# dates and totals are never mistaken for accounts.
SCORE_THRESHOLD = 0.5

# Context keywords that qualify a 9-18 digit run as a bank account number
ACCOUNT_CONTEXT_KEYWORDS = (
    "account", "acc", "bank", "savings", "beneficiary", "ifsc",
)

# Look-back / look-ahead window around a candidate account number
ACCOUNT_CONTEXT_LOOKBACK = 80
ACCOUNT_CONTEXT_LOOKAHEAD = 30


def _has_account_context(text: str, start: int, end: int) -> bool:
    """Return True if account-related keywords appear near a digit run."""
    window = text[max(0, start - ACCOUNT_CONTEXT_LOOKBACK):end + ACCOUNT_CONTEXT_LOOKAHEAD].lower()
    return any(kw in window for kw in ACCOUNT_CONTEXT_KEYWORDS)

_analyzer = None
_anonymizer = None


# ---------------------------------------------------------------------------
# Presidio engine construction (lazy singletons)
# ---------------------------------------------------------------------------


def _build_analyzer():
    """Build the Presidio AnalyzerEngine with Indian financial recognizers.

    Uses spaCy `en_core_web_sm` for NLP context — installed at Docker
    build time (`python -m spacy download en_core_web_sm`).
    """
    from presidio_analyzer import AnalyzerEngine, Pattern, PatternRecognizer
    from presidio_analyzer.nlp_engine import NlpEngineProvider
    from presidio_analyzer.recognizer_registry import RecognizerRegistry

    # Deterministic custom recognizers for Indian financial identifiers
    # (version-independent — do not rely on presidio predefined IN_* set)
    patterns: dict[str, list[Pattern]] = {
        # PAN: 5 letters + 4 digits + 1 letter
        "IN_PAN": [Pattern("in_pan", r"\b[A-Z]{5}[0-9]{4}[A-Z]\b", 0.6)],
        # Aadhaar: 4-4-4 digits with optional single spaces
        "IN_AADHAAR": [Pattern("in_aadhaar", r"\b[0-9]{4}[ ]?[0-9]{4}[ ]?[0-9]{4}\b", 0.65)],
        # IFSC: 4 letters + 0 + 6 alphanumerics
        "IN_IFSC": [Pattern("in_ifsc", r"\b[A-Z]{4}0[A-Z0-9]{6}\b", 0.6)],
        # GSTIN: 2-digit state + 10-char PAN + entity code + Z + check digit
        "IN_GSTIN": [Pattern("in_gstin", r"\b[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]\b", 0.85)],
        # Bank account: 9-18 digit runs. Score above threshold so Presidio
        # reports them; mask_pii() post-filters on context keywords so plain
        # amounts/dates/totals are never masked as accounts.
        "BANK_ACCOUNT": [
            Pattern("bank_account", r"\b[0-9]{9,18}\b", 0.55)
        ],
    }

    nlp_configuration = {
        "nlp_engine_name": "spacy",
        "models": [{"lang_code": "en", "model_name": "en_core_web_sm"}],
    }
    provider = NlpEngineProvider(nlp_configuration=nlp_configuration)
    nlp_engine = provider.create_engine()

    registry = RecognizerRegistry()
    registry.load_predefined_recognizers(nlp_engine=nlp_engine)

    for entity_type, entity_patterns in patterns.items():
        registry.add_recognizer(
            PatternRecognizer(
                supported_entity=entity_type,
                name=f"{entity_type}Recognizer",
                patterns=entity_patterns,
            )
        )

    return AnalyzerEngine(
        nlp_engine=nlp_engine,
        registry=registry,
        supported_languages=["en"],
    )


def get_analyzer():
    """Get the lazily-initialized Presidio AnalyzerEngine singleton."""
    global _analyzer
    if _analyzer is None:
        try:
            _analyzer = _build_analyzer()
            logger.info("Presidio PII analyzer initialized")
        except Exception as e:
            logger.error(f"Failed to initialize Presidio analyzer: {e}")
            raise RuntimeError(
                "Presidio PII analyzer unavailable. Install presidio-analyzer "
                f"and the spaCy en_core_web_sm model. Error: {e}"
            ) from e
    return _analyzer


def get_anonymizer():
    """Get the lazily-initialized Presidio AnonymizerEngine singleton."""
    global _anonymizer
    if _anonymizer is None:
        from presidio_anonymizer import AnonymizerEngine

        _anonymizer = AnonymizerEngine()
    return _anonymizer


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def mask_pii(text: str) -> tuple[str, dict[str, str]]:
    """Mask PII in a string using Presidio. Returns (masked_text, vault_map).

    Each detected entity instance is replaced with its standard placeholder,
    e.g. ``[PAN_REDACTED]``.

    vault_map is an index-keyed reference of what was masked, e.g.
    ``{"IN_PAN_1": "AABCU9603R", "BANK_ACCOUNT_1": "50100012345678"}``.
    Since all instances of an entity share the same placeholder token in the
    masked text, the vault is ordered by span position for rehydration.
    """
    analyzer = get_analyzer()
    anonymizer = get_anonymizer()

    if not text:
        return text, {}

    results = analyzer.analyze(
        text=text,
        language="en",
        entities=ENTITIES,
        score_threshold=SCORE_THRESHOLD,
    )

    # Gate bank-account findings on nearby context so amounts/dates/totals
    # (9-18 digit runs) are not masked as accounts.
    results = [
        r
        for r in results
        if not (r.entity_type == "BANK_ACCOUNT" and not _has_account_context(text, r.start, r.end))
    ]

    if not results:
        return text, {}

    from presidio_anonymizer.entities import OperatorConfig

    operators = {
        entity: OperatorConfig("replace", {"new_value": placeholder})
        for entity, placeholder in ENTITY_PLACEHOLDERS.items()
    }
    anonymized = anonymizer.anonymize(
        text=text,
        analyzer_results=results,
        operators=operators,
    )

    # Build index-keyed vault of originals ordered by span position
    vault: dict[str, str] = {}
    counts: dict[str, int] = {}
    for r in sorted(results, key=lambda r: (r.start, r.end)):
        entity = r.entity_type
        counts[entity] = counts.get(entity, 0) + 1
        vault[f"{entity}_{counts[entity]}"] = text[r.start:r.end]

    return anonymized.text, vault


def mask_invoice_for_llm(invoice_payload: dict[str, Any]) -> dict[str, Any]:
    """Create a masked copy of the invoice payload safe for LLM context.

    - Raw OCR text (if present) is run through the Presidio pipeline.
    - Banking details (account_number, ifsc) are masked.
    - PAN/GSTIN in supplier/buyer are masked.
    """
    masked = copy.deepcopy(invoice_payload)

    # Mask raw OCR text via Presidio
    ocr_text = masked.get("ocr_text")
    if isinstance(ocr_text, str) and ocr_text.strip():
        masked["ocr_text"], _ = mask_pii(ocr_text)

    # Mask banking details
    banking = masked.get("banking_details", {})
    if banking.get("account_number"):
        banking["account_number_masked"] = (
            f"XXXX-{banking['account_number'][-4:]}"
            if len(str(banking["account_number"])) >= 4
            else "XXXX"
        )
        banking["account_number"] = ACCOUNT_PLACEHOLDER
    if banking.get("ifsc"):
        banking["ifsc"] = IFSC_PLACEHOLDER

    # Mask PAN/GSTIN in supplier
    supplier = masked.get("supplier_details", {})
    if supplier.get("pan"):
        supplier["pan"] = PAN_PLACEHOLDER
    if supplier.get("gstin"):
        supplier["gstin"] = GSTIN_PLACEHOLDER

    # Mask PAN/GSTIN in buyer
    buyer = masked.get("buyer_details", {})
    if buyer.get("pan"):
        buyer["pan"] = PAN_PLACEHOLDER
    if buyer.get("gstin"):
        buyer["gstin"] = GSTIN_PLACEHOLDER

    return masked