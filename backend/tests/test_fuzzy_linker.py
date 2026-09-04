"""
TDD tests — run_fuzzy_text_linker_tool (Token Set Ratio entity resolution).

Doc coverage:
- Happy path (clean match): 'Acme Corp' vs 'ACME CORP' => 1.0
- Out-of-order shorthand: 'Technologies Nexus' vs 'NEXUS TECH' => resolve
- Heavy banking noise: 'IMPS/90123/HDFC/MUMBAI/NEXUS LOGISTICS' => 'nexus logistics'
- Near-phonetic OCR variance: LOGISTX ~ LOGISTICS with phonetic_match
- False-Positive Trap: 'Tata Motors' vs 'Tata Steel' MUST score 0.0
"""

import pytest

from app.agent.tools.fuzzy_linker import run_fuzzy_text_linker
from app.schemas.layer2_tools import (
    FuzzyLinkStatus,
    FuzzyLinkerInput,
)


def _link(source: str, target: str, threshold: float = 0.85) -> FuzzyLinkerInput:
    return FuzzyLinkerInput(
        source_entity_name=source,
        target_bank_narration=target,
        context_vendor_code="VEND_NEXUS_001",
        match_threshold=threshold,
    )


class TestHappyPaths:
    def test_exact_clean_match(self):
        result = run_fuzzy_text_linker(_link("Acme Corp", "ACME CORP"))
        assert result.status == FuzzyLinkStatus.ENTITY_RESOLVED
        assert result.confidence_score == 1.0
        assert result.resolved_vendor_code == "VEND_NEXUS_001"

    def test_corporate_suffix_noise_stripped(self):
        """'Nexus Logistics Private Limited' == 'PRIVATE LIMITED NEXUS LOGISTICS' => 1.0."""
        result = run_fuzzy_text_linker(
            _link("Nexus Logistics Private Limited", "PRIVATE LIMITED NEXUS LOGISTICS")
        )
        assert result.status == FuzzyLinkStatus.ENTITY_RESOLVED
        assert result.confidence_score == 1.0


class TestOutOfOrderShorthand:
    def test_technologies_nexus_vs_nexus_tech(self):
        """'Technologies Nexus' vs 'NEXUS TECH' — token overlap + stem synonymy."""
        result = run_fuzzy_text_linker(_link("Technologies Nexus", "NEXUS TECH"))
        assert result.status == FuzzyLinkStatus.ENTITY_RESOLVED
        assert result.confidence_score >= 0.85
        assert result.diagnostic_trace.phonetic_match or result.confidence_score >= 1.0


class TestBankingNoise:
    def test_heavy_bank_narration_noise(self):
        """'IMPS/90123/HDFC/MUMBAI/NEXUS LOGISTICS' isolates 'nexus logistics'."""
        result = run_fuzzy_text_linker(
            _link("Nexus Logistics Private Limited", "IMPS/90123/HDFC/MUMBAI/NEXUS LOGISTICS")
        )
        assert result.status == FuzzyLinkStatus.ENTITY_RESOLVED
        assert result.confidence_score == 1.0
        assert result.diagnostic_trace.normalized_target == "nexus logistics"

    def test_ocr_variant_logistx_matches_logistics(self):
        """Bank descriptor 'NEXUS LOGISTX' matches 'Nexus Logistics' phonetically."""
        result = run_fuzzy_text_linker(_link("Nexus Logistics Pvt Ltd", "NEXUS LOGISTX"))
        assert result.status == FuzzyLinkStatus.ENTITY_RESOLVED
        assert result.confidence_score >= 0.85
        assert result.diagnostic_trace.phonetic_match is True


class TestFalsePositiveTrap:
    def test_tata_motors_vs_tata_steel_scores_zero(self):
        """Non-overlapping tokens must resolve phonetically; 'Motors' vs 'Steel'
        sound different => score 0.0 — a naive 50% would clear the WRONG vendor."""
        result = run_fuzzy_text_linker(_link("Tata Motors", "Tata Steel"))
        assert result.status == FuzzyLinkStatus.ENTITY_MISMATCH
        assert result.confidence_score == 0.0
        assert result.resolved_vendor_code is None

    def test_below_threshold_is_mismatch(self):
        result = run_fuzzy_text_linker(_link("Acme Corp", "Acme Enterprises Inc"))
        assert result.status == FuzzyLinkStatus.ENTITY_MISMATCH
        assert result.confidence_score < 0.85

    def test_truncated_narration_degrades_below_threshold(self):
        """Bank narration truncates the legal name ('Nexus Logistics' vs
        'Nexus Logistics Services') => containment score 2/3 = 0.6667,
        safely below 0.85 — routed to human review, never auto-matched."""
        result = run_fuzzy_text_linker(
            _link("Nexus Logistics Services", "NEXUS LOGISTICS", threshold=0.85)
        )
        assert result.status == FuzzyLinkStatus.ENTITY_MISMATCH
        assert result.confidence_score == pytest.approx(2 / 3, abs=1e-4)

    def test_high_threshold_rejects_partial(self):
        """threshold 0.95: containment 0.6667 < 0.95 => mismatch."""
        result = run_fuzzy_text_linker(
            _link("Nexus Logistics Services", "NEXUS LOGISTICS", threshold=0.95)
        )
        assert result.status == FuzzyLinkStatus.ENTITY_MISMATCH
