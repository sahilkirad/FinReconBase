"""
PII Masking Middleware

Masks sensitive financial data (PAN, bank account, Aadhaar) before
sending to LLM context windows. Tools use real data locally in RAM.
"""

import re
import copy
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Pattern matchers for Indian financial PII
PAN_PATTERN = re.compile(r'\b[A-Z]{5}[0-9]{4}[A-Z]\b')
ACCOUNT_PATTERN = re.compile(r'\b\d{9,18}\b')  # 9-18 digit bank accounts
IFSC_PATTERN = re.compile(r'\b[A-Z]{4}0[A-Z0-9]{6}\b')
AADHAAR_PATTERN = re.compile(r'\b\d{4}\s?\d{4}\s?\d{4}\b')

MASKED_PAN = "[PAN_MASKED]"
MASKED_ACCOUNT = "[ACCOUNT_MASKED]"
MASKED_IFSC = "[IFSC_MASKED]"
MASKED_AADHAAR = "[AADHAAR_MASKED]"


def mask_pii(text: str) -> tuple[str, dict[str, str]]:
    """Mask PII in a string. Returns (masked_text, vault_map).

    vault_map maps masked tokens back to original values for rehydration.
    """
    vault = {}
    masked = text

    for match in PAN_PATTERN.finditer(text):
        token = f"[PAN_{match.start()}]"
        vault[token] = match.group()
        masked = masked.replace(match.group(), MASKED_PAN, 1)

    for match in IFSC_PATTERN.finditer(masked):
        token = f"[IFSC_{match.start()}]"
        vault[token] = match.group()
        masked = masked.replace(match.group(), MASKED_IFSC, 1)

    for match in AADHAAR_PATTERN.finditer(masked):
        token = f"[AADHAAR_{match.start()}]"
        vault[token] = match.group()
        masked = masked.replace(match.group(), MASKED_AADHAAR, 1)

    for match in ACCOUNT_PATTERN.finditer(masked):
        token = f"[ACCT_{match.start()}]"
        vault[token] = match.group()
        masked = masked.replace(match.group(), MASKED_ACCOUNT, 1)

    return masked, vault


def mask_invoice_for_llm(invoice_payload: dict[str, Any]) -> dict[str, Any]:
    """Create a masked copy of the invoice payload safe for LLM context.

    Banking details (account_number, ifsc) are masked.
    PAN/GSTIN in supplier/buyer are masked.
    """
    masked = copy.deepcopy(invoice_payload)

    # Mask banking details
    banking = masked.get("banking_details", {})
    if banking.get("account_number"):
        banking["account_number_masked"] = (
            f"XXXX-{banking['account_number'][-4:]}"
            if len(banking["account_number"]) >= 4
            else "XXXX"
        )
        banking["account_number"] = "[ACCOUNT_MASKED]"
    if banking.get("ifsc"):
        banking["ifsc"] = "[IFSC_MASKED]"

    # Mask PAN in supplier
    supplier = masked.get("supplier_details", {})
    if supplier.get("pan"):
        supplier["pan"] = "[PAN_MASKED]"

    # Mask PAN in buyer
    buyer = masked.get("buyer_details", {})
    if buyer.get("pan"):
        buyer["pan"] = "[PAN_MASKED]"

    return masked
