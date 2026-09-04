"""
TDD tests — calculate_tds_mdr_tool (gross-to-net statutory waterfall).

Covers the doc's testing paths:
- Happy path (direct NEFT): net == grand_total - TDS exactly
- Gateway MDR edge: fees + 18% GST on fee deducted
- NEGATIVE_NET_SETTLEMENT when deductions exceed gross
- INVALID_PAISE_CASTING for corrupted decimal input
- TDS_RATE_ANOMALY flag appended while math completes
"""

import pytest
from pydantic import ValidationError

from app.agent.tools.tds_mdr import calculate_tds_mdr
from app.schemas.layer2_tools import TdsMdrInput, WaterfallStatus


def _waterfall(**overrides) -> TdsMdrInput:
    base = {
        "invoice_id": "INV-1",
        "grand_total_rupees": "104400.00",
        "tds_deducted_rupees": "1800.00",
        "tds_category_code": "194C",
        "gateway_fees_paise": 0,
        "gateway_tax_paise": 0,
    }
    base.update(overrides)
    return TdsMdrInput(**base)


class TestWaterfallHappyPath:
    def test_direct_neft_no_gateway(self):
        """₹1,00,000 invoice, 2% TDS (194C = ₹2,000), no gateway => ₹98,000.00."""
        result = calculate_tds_mdr(
            _waterfall(
                invoice_id="INV-441",
                grand_total_rupees="100000.00",
                tds_deducted_rupees="2000.00",
                tds_category_code="194C",
            )
        )
        assert result.status == WaterfallStatus.WATERFALL_CALCULATED
        assert result.net_expected_settlement == "98000.00"
        assert result.deduction_breakdown.total_tds_rupees == "2000.00"
        assert result.deduction_breakdown.total_gateway_deductions_rupees == "0.00"
        assert result.flags == []

    def test_doc_example_104400_minus_1800(self):
        """Doc example: grand 104400.00, TDS 1800.00 => net 102600.00."""
        result = calculate_tds_mdr(_waterfall())
        assert result.status == WaterfallStatus.WATERFALL_CALCULATED
        assert result.net_expected_settlement == "102600.00"


class TestGatewayFeeEdge:
    def test_mdr_fee_plus_gst_deduction(self):
        """₹10,000 invoice, TDS 0, Razorpay fee ₹200 (20000 paise) + 18% GST ₹36 (3600 paise)
        => net ₹9,764.00 (deduction ₹236.00)."""
        result = calculate_tds_mdr(
            _waterfall(
                invoice_id="INV-500",
                grand_total_rupees="10000.00",
                tds_deducted_rupees="0.00",
                gateway_fees_paise=20000,
                gateway_tax_paise=3600,
            )
        )
        assert result.status == WaterfallStatus.WATERFALL_CALCULATED
        assert result.net_expected_settlement == "9764.00"
        assert result.deduction_breakdown.total_gateway_deductions_rupees == "236.00"


class TestExceptionRouting:
    def test_negative_net_settlement(self):
        """TDS/gateway exceed the gross total — record must NOT enter the pool."""
        result = calculate_tds_mdr(
            _waterfall(grand_total_rupees="1000.00", tds_deducted_rupees="1500.00")
        )
        assert result.status == WaterfallStatus.NEGATIVE_NET_SETTLEMENT
        assert "NEGATIVE_NET_SETTLEMENT" in result.flags
        assert result.net_expected_settlement is None

    def test_gateway_pushes_net_negative(self):
        result = calculate_tds_mdr(
            _waterfall(
                grand_total_rupees="100.00",
                tds_deducted_rupees="0.00",
                gateway_fees_paise=15000,  # ₹150 on ₹100
            )
        )
        assert result.status == WaterfallStatus.NEGATIVE_NET_SETTLEMENT

    def test_tds_rate_anomaly_flagged_but_math_completes(self):
        """Invoice claims 194J (standard 10%) but deducted 35% => anomaly flag,
        math still completes with the extracted amount."""
        result = calculate_tds_mdr(
            _waterfall(
                grand_total_rupees="100000.00",
                tds_deducted_rupees="35000.00",
                tds_category_code="194J",
            )
        )
        assert result.status == WaterfallStatus.WATERFALL_CALCULATED
        assert "TDS_RATE_ANOMALY" in result.flags
        assert result.net_expected_settlement == "65000.00"


class TestInvalidPaiseCasting:
    def test_comma_thousands_rejected_at_schema(self):
        """'104,400.00' is not strict decimal encoding — rejected before the tool runs."""
        with pytest.raises(ValidationError):
            _waterfall(grand_total_rupees="104,400.00")

    def test_three_decimals_rejected_at_schema(self):
        with pytest.raises(ValidationError):
            _waterfall(grand_total_rupees="100.000")

    def test_negative_gateway_fee_rejected(self):
        with pytest.raises(ValidationError):
            _waterfall(gateway_fees_paise=-1)
