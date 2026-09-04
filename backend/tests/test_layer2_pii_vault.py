"""
TDD tests — Layer 2 in-RAM PII vault.

- Structured masking of supplier/buyer pan/gstin/account/ifsc/aadhaar
- Deterministic token scheme ([PAN_TOKEN_1] ...) — same plaintext -> same token
- Rehydration round-trip at the tool boundary
- release_run() wipes the vault from RAM
- The vault map is never part of any state/config payload by construction
"""

import os

os.environ["GOOGLE_OAUTH_CLIENT_ID"] = "test"
os.environ["JWT_SECRET_KEY"] = "test-secret"
os.environ["GROQ_API_KEY"] = "test"
os.environ["GROQ_MODEL"] = "test"

from app.agent.pii.vault import (
    get_vault,
    new_run_token,
    register_run,
    release_run,
)

PAN = "AABCU9603R"
GSTIN = "27AABCU9603R1Z5"
IFSC = "HDFC0001234"
ACCOUNT = "50100234567890"
AADHAAR = "234512349012"


def _invoice_payload(**overrides) -> dict:
    payload = {
        "reference_data": {"invoice_number": "INV-1", "document_date": "2026-08-05"},
        "supplier_details": {
            "legal_name": "Nexus Logistics Pvt Ltd",
            "gstin": GSTIN,
            "pan": PAN,
            "address": "Mumbai",
        },
        "buyer_details": {"legal_name": "Acme Corp", "gstin": "27AABBCC2211H1Z7", "pan": None},
        "banking_details": {
            "bank_name": "HDFC",
            "account_number": ACCOUNT,
            "ifsc": IFSC,
        },
        "financial_summary": {"grand_total_paise": 24500000, "tds_deduction_paise": 0},
    }
    payload.update(overrides)
    return payload


class TestStructuredMasking:
    def test_pan_gstin_account_ifsc_tokenized(self):
        run_token = new_run_token()
        vault = register_run(run_token)
        try:
            masked = vault.mask_invoice_payload(_invoice_payload())
            supplier = masked["supplier_details"]
            assert supplier["pan"] == "[PAN_TOKEN_1]"
            assert supplier["gstin"] == "[GSTIN_TOKEN_1]"
            assert masked["banking_details"]["account_number"] == "[ACCOUNT_TOKEN_1]"
            assert masked["banking_details"]["ifsc"] == "[IFSC_TOKEN_1]"
            # Non-PII fields untouched
            assert supplier["legal_name"] == "Nexus Logistics Pvt Ltd"
            assert masked["financial_summary"]["grand_total_paise"] == 24500000
        finally:
            release_run(run_token)

    def test_same_plaintext_maps_to_same_token_deterministic(self):
        run_token = new_run_token()
        vault = register_run(run_token)
        try:
            a = vault.mask_invoice_payload(_invoice_payload())
            b = vault.mask_invoice_payload(_invoice_payload())
            assert a["supplier_details"]["pan"] == b["supplier_details"]["pan"]
            assert a["supplier_details"]["gstin"] == b["supplier_details"]["gstin"]
        finally:
            release_run(run_token)

    def test_distinct_values_get_distinct_tokens(self):
        run_token = new_run_token()
        vault = register_run(run_token)
        try:
            masked = vault.mask_invoice_payload(
                _invoice_payload(
                    supplier_details={
                        "legal_name": "A",
                        "pan": PAN,
                        "gstin": GSTIN,
                    },
                    buyer_details={
                        "legal_name": "B",
                        "pan": "AAACS1234F",
                        "gstin": "27AAACS1234F1Z9",
                    },
                )
            )
            assert masked["supplier_details"]["pan"] != masked["buyer_details"]["pan"]
            assert masked["supplier_details"]["gstin"] != masked["buyer_details"]["gstin"]
        finally:
            release_run(run_token)

    def test_missing_fields_untouched(self):
        run_token = new_run_token()
        vault = register_run(run_token)
        try:
            masked = vault.mask_invoice_payload(
                _invoice_payload(buyer_details={"legal_name": "Buyer"})
            )
            assert masked["buyer_details"].get("pan") is None
        finally:
            release_run(run_token)

    def test_original_payload_never_mutated(self):
        run_token = new_run_token()
        vault = register_run(run_token)
        try:
            original = _invoice_payload()
            vault.mask_invoice_payload(original)
            assert original["supplier_details"]["pan"] == PAN  # unchanged
        finally:
            release_run(run_token)


class TestRehydration:
    def test_rehydrate_token_returns_plaintext(self):
        run_token = new_run_token()
        vault = register_run(run_token)
        try:
            masked = vault.mask_invoice_payload(_invoice_payload())
            token = masked["supplier_details"]["pan"]
            assert vault.rehydrate(token) == PAN
            assert vault.rehydrate("[PAN_TOKEN_999]") is None
        finally:
            release_run(run_token)

    def test_rehydrate_text_swaps_all_tokens(self):
        run_token = new_run_token()
        vault = register_run(run_token)
        try:
            masked = vault.mask_invoice_payload(_invoice_payload())
            rendered = (
                f"PAN {masked['supplier_details']['pan']} "
                f"GSTIN {masked['supplier_details']['gstin']}"
            )
            restored = vault.rehydrate_text(rendered)
            assert PAN in restored
            assert GSTIN in restored
            assert "[PAN_TOKEN" not in restored
        finally:
            release_run(run_token)

    def test_release_run_wipes_map(self):
        run_token = new_run_token()
        vault = register_run(run_token)
        masked = vault.mask_invoice_payload(_invoice_payload())
        token = masked["supplier_details"]["pan"]
        release_run(run_token)
        # get_vault returns None and token map is cleared
        assert get_vault(run_token) is None
        assert vault.rehydrate(token) is None

    def test_run_token_never_serialized_into_config(self):
        """The vault map must never be in the graph config — only the token."""
        from app.agent.graph.supervisor import make_thread_config

        run_token = new_run_token()
        config = make_thread_config("batch-1", "doc-1", run_token=run_token)
        assert config["configurable"]["run_token"] == run_token
        serialized = repr(config)
        assert PAN not in serialized and ACCOUNT not in serialized
        release_run(run_token)
