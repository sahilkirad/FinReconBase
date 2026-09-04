"""
TDD tests — run_subset_sum_matching_tool (strict 3-phase waterfall).

Phase 1 — amount/subset-sum: one credit, unique subset  => matched (UTR)
Phase 2 — entity: duplicate amounts resolved by supplier fuzzy match
Phase 3 — chronology: identical amounts+names resolved by nearest date (<= ±7d)
Hard exception — perfect tie after all 3 phases => AMBIGUOUS_COLLISION
"""

from datetime import date

from app.agent.tools.subset_sum import (
    CreditRow,
    InvoiceRow,
    resolve_three_phase,
    run_subset_sum_matching,
    find_subsets,
)
from app.schemas.layer2_tools import SubsetSumInput, SubsetSumStatus


def _credit(utr: str, amount_paise: int, narration: str = "NEXUS LOGISTICS", d: date = date(2026, 8, 10)) -> CreditRow:
    return CreditRow(utr_number=utr, transaction_date=d, narration=narration, amount_paise=amount_paise)


def _invoice(
    number: str,
    net_paise: int,
    supplier: str = "Nexus Logistics Pvt Ltd",
    d: date = date(2026, 8, 5),
) -> InvoiceRow:
    return InvoiceRow(
        document_id=f"doc-{number}",
        invoice_number=number,
        supplier_legal_name=supplier,
        document_date=d,
        net_paise=net_paise,
    )


class TestPhase1AmountMatching:
    def test_single_invoice_exact_match(self):
        """1 credit ₹2,45,000, 1 invoice ₹2,45,000 => UTR returned, phase 1."""
        result = resolve_three_phase(
            credits=[_credit("UTR1", 24500000)],
            invoices=[_invoice("INV-1", 24500000)],
            tolerance_days=7,
        )
        assert result.status == SubsetSumStatus.SUBSET_MATCHED
        assert result.matched_bank_utr == "UTR1"
        assert result.matched_invoice_ids == ["INV-1"]
        assert result.phase_applied == 1
        assert result.net_total_paise == 24500000

    def test_multi_invoice_subset_match(self):
        """Doc scenario: bank credit ₹2,45,000 matched by 3 invoice NET amounts
        (A ₹98,000 + B ₹98,000 + C ₹49,000 — post-TDS nets that total exactly
        the credited amount after deductions)."""
        invoices = [
            _invoice("INV-A", 9800000),
            _invoice("INV-B", 9800000),
            _invoice("INV-C", 4900000),
        ]
        result = resolve_three_phase(
            credits=[_credit("UTR1", 24500000)],
            invoices=invoices,
            tolerance_days=7,
        )
        assert result.status == SubsetSumStatus.SUBSET_MATCHED
        assert sorted(result.matched_invoice_ids) == ["INV-A", "INV-B", "INV-C"]
        assert result.phase_applied == 1

    def test_no_match_when_no_subset(self):
        result = resolve_three_phase(
            credits=[_credit("UTR1", 24500000)],
            invoices=[_invoice("INV-1", 12300000)],
            tolerance_days=7,
        )
        assert result.status == SubsetSumStatus.NO_MATCH


class TestPhase2EntityCollision:
    def test_identical_amounts_resolved_by_supplier_name(self):
        """Two ₹500 invoices (Tata Motors vs Tata Steel); bank credit 500 with
        narration TATA MOTORS => phase 2 picks the correct invoice."""
        invoices = [
            _invoice("INV-MOTORS", 50000, supplier="Tata Motors Pvt Ltd", d=date(2026, 8, 1)),
            _invoice("INV-STEEL", 50000, supplier="Tata Steel Ltd", d=date(2026, 8, 1)),
        ]
        result = resolve_three_phase(
            credits=[_credit("UTR500", 50000, narration="TATA MOTORS LTD", d=date(2026, 8, 1))],
            invoices=invoices,
            tolerance_days=7,
        )
        assert result.status == SubsetSumStatus.SUBSET_MATCHED
        assert result.matched_invoice_ids == ["INV-MOTORS"]
        assert result.phase_applied == 2


class TestPhase3ChronologicalTieBreaker:
    def _recurring_fixture(self, invoice_dates: tuple[date, date], credit_date: date) -> list[InvoiceRow]:
        return [
            _invoice("INV-1", 5000, supplier="Nexus Hosting Pvt Ltd", d=invoice_dates[0]),
            _invoice("INV-2", 5000, supplier="Nexus Hosting Pvt Ltd", d=invoice_dates[1]),
        ]

    def test_nearest_date_within_tolerance_wins(self):
        """Same vendor, same ₹50 amount; credit on 08-10. INV-2 (08-15, 5d) beats
        INV-1 (08-03, 7d) => phase 3 selects INV-2."""
        invoices = self._recurring_fixture(
            (date(2026, 8, 3), date(2026, 8, 15)), credit_date=date(2026, 8, 10)
        )
        result = resolve_three_phase(
            credits=[_credit("UTR50", 5000, narration="NEXUS HOSTING", d=date(2026, 8, 10))],
            invoices=invoices,
            tolerance_days=7,
        )
        assert result.status == SubsetSumStatus.SUBSET_MATCHED
        assert result.matched_invoice_ids == ["INV-2"]
        assert result.phase_applied == 3

    def test_outside_tolerance_window_never_matches(self):
        """Both invoices 20+ days from the credit => no chronological winner."""
        invoices = self._recurring_fixture(
            (date(2026, 7, 1), date(2026, 9, 1)), credit_date=date(2026, 8, 10)
        )
        result = resolve_three_phase(
            credits=[_credit("UTR50", 5000, narration="NEXUS HOSTING", d=date(2026, 8, 10))],
            invoices=invoices,
            tolerance_days=7,
        )
        assert result.status == SubsetSumStatus.AMBIGUOUS_COLLISION


class TestHardException:
    def test_perfect_tie_returns_ambiguous_collision(self):
        """Amounts + names identical + equidistant dates => deliberate failure."""
        invoices = [
            _invoice("INV-1", 5000, supplier="Nexus Hosting Pvt Ltd", d=date(2026, 8, 3)),
            _invoice("INV-2", 5000, supplier="Nexus Hosting Pvt Ltd", d=date(2026, 8, 17)),
        ]
        result = resolve_three_phase(
            credits=[_credit("UTR50", 5000, narration="NEXUS HOSTING", d=date(2026, 8, 10))],
            invoices=invoices,
            tolerance_days=7,
        )
        assert result.status == SubsetSumStatus.AMBIGUOUS_COLLISION
        assert result.matched_bank_utr is None

    def test_multiple_credit_collision_is_never_a_guess(self):
        """Two identical credits, same vendor, dates equidistant => collision."""
        invoices = [
            _invoice("INV-1", 5000, supplier="Nexus Hosting Pvt Ltd", d=date(2026, 8, 10)),
        ]
        credits = [
            _credit("UTR-A", 5000, narration="NEXUS HOSTING", d=date(2026, 8, 5)),
            _credit("UTR-B", 5000, narration="NEXUS HOSTING", d=date(2026, 8, 15)),
        ]
        result = resolve_three_phase(credits=credits, invoices=invoices, tolerance_days=7)
        # delta 5d for both credits — equidistant => hard exception
        assert result.status == SubsetSumStatus.AMBIGUOUS_COLLISION


class TestPureSolver:
    def test_find_subsets_unique_combo(self):
        invoices = [_invoice("A", 9800000), _invoice("B", 9800000), _invoice("C", 4900000)]
        combos = find_subsets(24500000, invoices)
        assert len(combos) == 1
        assert sorted(invoices[i].invoice_number for i in combos[0]) == ["A", "B", "C"]

    def test_find_subsets_duplicate_amounts_multiple_combos(self):
        invoices = [_invoice("A", 5000), _invoice("B", 5000)]
        combos = find_subsets(5000, invoices)
        assert len(combos) == 2  # {A} and {B}


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeSession:
    """Feeds canned DB rows to run_subset_sum_matching."""

    def __init__(self, credit_rows, invoice_rows):
        self._credits = credit_rows
        self._invoices = invoice_rows

    def execute(self, sql, params):
        sql_text = str(sql)
        if "bank_transactions" in sql_text:
            return _Result(self._credits)
        return _Result(self._invoices)


class TestDbOrchestration:
    def test_sql_materialization_maps_to_waterfall(self):
        credit_rows = [
            ("UTR1", date(2026, 8, 10), "NEXUS LOGISTICS", 24500000),
        ]
        invoice_rows = [
            ("doc-1", "INV-1", "Nexus Logistics Pvt Ltd", date(2026, 8, 5), 24500000),
        ]
        result = run_subset_sum_matching(
            SubsetSumInput(vendor_code="VEND_TEST", target_amount_paise=24500000),
            _FakeSession(credit_rows, invoice_rows),
        )
        assert result.status == SubsetSumStatus.SUBSET_MATCHED
        assert result.matched_bank_utr == "UTR1"
        assert result.matched_invoice_ids == ["INV-1"]

    def test_target_utr_filter_narrows_credits(self):
        credit_rows = [
            ("UTR1", date(2026, 8, 10), "NEXUS LOGISTICS", 24500000),
        ]
        invoice_rows = [
            ("doc-1", "INV-1", "Nexus Logistics Pvt Ltd", date(2026, 8, 5), 24500000),
        ]
        result = run_subset_sum_matching(
            SubsetSumInput(
                vendor_code="VEND_TEST",
                target_amount_paise=24500000,
                bank_utr_number="UTR1",
            ),
            _FakeSession(credit_rows, invoice_rows),
        )
        assert result.status == SubsetSumStatus.SUBSET_MATCHED
