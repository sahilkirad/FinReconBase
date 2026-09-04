"""
Tool 2: calculate_tds_mdr_tool — Deterministic Gross-to-Net Waterfall

Enforces the statutory netting rule before an invoice enters the
reconciliation pool:

    Expected Payer Transfer = Grand Total (paise) - TDS (paise)
    Gateway Deductions      = Razorpay Fees (paise) + Razorpay Tax (paise)
    Net Expected Settlement = Expected Payer Transfer - Gateway Deductions

Outcomes:
- WATERFALL_CALCULATED          — math completed (may carry TDS_RATE_ANOMALY flag)
- NEGATIVE_NET_SETTLEMENT       — deductions exceed the gross total (blocked)
- INVALID_PAISE_CASTING         — corrupted decimal input (blocked before crash)
"""

import logging
from decimal import Decimal

from app.agent.tools.common import InvalidPaiseStringError, paise_to_rupees, rupees_to_paise

logger = logging.getLogger(__name__)
from app.schemas.layer2_tools import (
    TdsMdrInput,
    TdsMdrResult,
    WaterfallDeductionBreakdown,
    WaterfallStatus,
)

# Standard Indian TDS rates per Section (used only for anomaly detection —
# the deduction amount itself is always the extracted, authoritative value).
TDS_CATEGORY_RATES: dict[str, Decimal] = {
    "194C": Decimal("0.02"),
    "194J": Decimal("0.10"),
    "194I": Decimal("0.10"),
    "194H": Decimal("0.10"),
    "194A": Decimal("0.10"),
    "194B": Decimal("0.30"),
    "194D": Decimal("0.20"),
    "194DA": Decimal("0.20"),
    "194E": Decimal("0.30"),
    "194G": Decimal("0.05"),
}

# If the deducted rate exceeds 20% of the gross (or 1.25x the statutory rate)
# the record is flagged for compliance review.
TDS_ANOMALY_ABS_CAP = Decimal("0.20")
TDS_ANOMALY_MULTIPLE = Decimal("1.25")


def _tds_rate_anomaly(
    tds_paise: int,
    gross_paise: int,
    tds_category_code: str,
) -> bool:
    """Cross-check deducted amount against the statutory slab.

    math completes either way; only a warning flag is appended.
    """
    if gross_paise <= 0:
        return False
    actual_rate = Decimal(tds_paise) / Decimal(gross_paise)
    if actual_rate > TDS_ANOMALY_ABS_CAP:
        return True
    standard = TDS_CATEGORY_RATES.get(tds_category_code.upper())
    if standard is not None and actual_rate > standard * TDS_ANOMALY_MULTIPLE:
        return True
    return False


def calculate_tds_mdr(inp: TdsMdrInput) -> TdsMdrResult:
    """Run the strict gross-to-net waterfall on integer paise."""
    try:
        gross_paise = rupees_to_paise(inp.grand_total_rupees)
        tds_paise = rupees_to_paise(inp.tds_deducted_rupees)
    except InvalidPaiseStringError as exc:
        logger.error(
            "TDS_WATERFALL_INVALID_PAISE", extra={"invoice_id": inp.invoice_id, "error": str(exc)}
        )
        return TdsMdrResult(
            status=WaterfallStatus.INVALID_PAISE_CASTING,
            invoice_id=inp.invoice_id,
            message=str(exc),
            flags=["INVALID_PAISE_CASTING"],
        )

    gateway_paise = inp.gateway_fees_paise + inp.gateway_tax_paise
    net_paise = gross_paise - tds_paise - gateway_paise

    if net_paise < 0:
        logger.error(
            "TDS_WATERFALL_NEGATIVE_NET",
            extra={"invoice_id": inp.invoice_id, "gross_paise": gross_paise, "net_paise": net_paise},
        )
        return TdsMdrResult(
            status=WaterfallStatus.NEGATIVE_NET_SETTLEMENT,
            invoice_id=inp.invoice_id,
            message=(
                "Extracted TDS/gateway deductions exceed the grand total; "
                "record blocked from the reconciliation pool."
            ),
            flags=["NEGATIVE_NET_SETTLEMENT"],
        )

    flags: list[str] = []
    if _tds_rate_anomaly(tds_paise, gross_paise, inp.tds_category_code):
        flags.append("TDS_RATE_ANOMALY")

    logger.info(
        "TDS_WATERFALL_CALCULATED",
        extra={
            "invoice_id": inp.invoice_id,
            "gross_paise": gross_paise,
            "net_paise": net_paise,
            "flags": flags,
        },
    )
    return TdsMdrResult(
        status=WaterfallStatus.WATERFALL_CALCULATED,
        invoice_id=inp.invoice_id,
        net_expected_settlement=paise_to_rupees(net_paise),
        deduction_breakdown=WaterfallDeductionBreakdown(
            total_tds_rupees=paise_to_rupees(tds_paise),
            total_gateway_deductions_rupees=paise_to_rupees(gateway_paise),
        ),
        flags=flags,
        message="Waterfall calculated." if not flags else "Waterfall calculated with flags.",
    )
