"""
Tool 3: run_subset_sum_matching_tool — Strict 3-Phase Deterministic Waterfall

The LLM never decides how to resolve a match. This tool owns the decision:

    Phase 1 — Amount Matching (Primary):
        Query unmatched bank CREDIT rows and open (VALIDATED, unreconciled)
        extracted invoices; run a subset-sum search of invoice net amounts
        against each credit. Exactly one credit with exactly one subset
        match  =>  return that UTR.

    Phase 2 — Entity Matching (Collision Resolution):
        If multiple identical amounts collide (e.g. two INR 500 invoices),
        fuzzy-match the supplier legal name against the bank narration
        (Token Set Ratio engine). A unique winner >= 0.85 resolves it.

    Phase 3 — Chronological Tie-Breaker:
        If amounts AND names are identical (recurring monthly fees), pick the
        candidate whose invoice date is closest to the bank settlement date,
        strictly within the ±N-day tolerance window (default 7).

    Hard Exception:
        A perfect tie after all three phases  =>  AMBIGUOUS_COLLISION.
        The tool deliberately fails; the agent routes to human review.
        It NEVER guesses.

Invoice net contribution used by the solver: grand_total_paise - tds_deduction_paise
(gateway/MDR netting is applied via the razorpay leg in the Milestone 2 graph).
"""

import logging
from dataclasses import dataclass
from datetime import date

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.agent.tools.fuzzy_linker import (
    _strip_noise,
    token_set_ratio_tokens,
    tokenize,
)

logger = logging.getLogger(__name__)
from app.schemas.layer2_tools import SubsetSumInput, SubsetSumResult, SubsetSumStatus

# Cap on enumerated subsets — deterministic bound; beyond it the result is
# treated as a collision (the solver refuses to pick among >LIMIT options).
_MAX_SUBSETS = 250


@dataclass(frozen=True)
class CreditRow:
    """An unmatched bank CREDIT (Stream 3) candidate."""

    utr_number: str
    transaction_date: date
    narration: str
    amount_paise: int


@dataclass(frozen=True)
class InvoiceRow:
    """An open extracted invoice candidate (Stream 1)."""

    document_id: str
    invoice_number: str
    supplier_legal_name: str
    document_date: date | None
    net_paise: int  # grand_total_paise - tds_deduction_paise


# =============================================================================
# Pure solver
# =============================================================================


def find_subsets(target_paise: int, candidates: list[InvoiceRow]) -> list[list[int]]:
    """Enumerate candidate index subsets whose net amounts sum to target_paise.

    Deterministic ordering: candidates sorted ascending by (net, invoice_number);
    enumeration capped at _MAX_SUBSETS.
    """
    indexed = sorted(
        enumerate(candidates),
        key=lambda pair: (pair[1].net_paise, pair[1].invoice_number),
    )
    nets = [row.net_paise for _, row in indexed]
    n = len(nets)

    # memo[i][s] reachable bool over suffix for pruning
    from functools import lru_cache

    @lru_cache(maxsize=None)
    def reachable(pos: int, remaining: int) -> bool:
        if remaining == 0:
            return True
        if pos >= n or remaining < 0:
            return False
        if reachable(pos + 1, remaining):
            return True
        return reachable(pos + 1, remaining - nets[pos])

    solutions: list[list[int]] = []

    def _search(pos: int, remaining: int, chosen: list[int]) -> None:
        if len(solutions) >= _MAX_SUBSETS:
            return
        if remaining == 0:
            solutions.append(list(chosen))
            return
        if pos >= n or remaining < 0 or not reachable(pos, remaining):
            return
        # include
        chosen.append(indexed[pos][0])
        _search(pos + 1, remaining - nets[pos], chosen)
        chosen.pop()
        # exclude
        _search(pos + 1, remaining, chosen)

    _search(0, target_paise, [])
    return solutions


# =============================================================================
# 3-phase waterfall (pure, deterministic — fully unit-testable)
# =============================================================================


def resolve_three_phase(
    *,
    credits: list[CreditRow],
    invoices: list[InvoiceRow],
    tolerance_days: int,
) -> SubsetSumResult:
    """Run the strict waterfall over candidate credits + open invoices."""

    def _unit_key(combo: list[int]) -> list[InvoiceRow]:
        return [invoices[i] for i in combo]

    # Phase 1 groundwork: every credit with >=1 subset match becomes a candidate
    candidates: list[tuple[CreditRow, list[list[int]]]] = []
    for credit in credits:
        combos = find_subsets(credit.amount_paise, invoices)
        if combos:
            candidates.append((credit, combos))

    if not candidates:
        return SubsetSumResult(status=SubsetSumStatus.NO_MATCH, message="No subset sum matches any unmatched bank credit.")

    # ---- Phase 1: unique winner ----
    if len(candidates) == 1 and len(candidates[0][1]) == 1:
        return _build_matched(candidates[0][0], candidates[0][1][0], invoices, phase=1)

    # ---- Collision: Phase 2 (entity) then Phase 3 (chronology) ----
    # Candidate units: every (credit, subset) pair with a single-invoice subset
    # can be entity-resolved; multi-invoice subsets are only amount-identity.
    units: list[tuple[CreditRow, list[int]]] = [
        (credit, combo)
        for credit, combos in candidates
        for combo in combos
    ]

    if len(units) == 1:
        return _build_matched(units[0][0], units[0][1], invoices, phase=1)

    # Phase 2 — entity resolution against narration
    phase2_survivors: list[tuple[CreditRow, list[int], float]] = []
    for credit, combo in units:
        if len(combo) == 1:
            invoice = invoices[combo[0]]
            source_tokens = _strip_noise(
                tokenize(invoice.supplier_legal_name), narration=False
            )
            target_tokens = _strip_noise(tokenize(credit.narration), narration=True)
            score, _ = token_set_ratio_tokens(source_tokens, target_tokens)
            phase2_survivors.append((credit, combo, score))
        else:
            # Multi-invoice subset: cannot entity-resolve; survives only if
            # it is the sole remaining option.
            phase2_survivors.append((credit, combo, 0.0))

    best = max(phase2_survivors, key=lambda u: u[2])
    if best[2] >= 0.85:
        winners = [u for u in phase2_survivors if u[2] == best[2]]
        if len(winners) == 1:
            credit, combo, _ = winners[0]
            return _build_matched(credit, combo, invoices, phase=2)

    # Phase 3 — chronological tie-breaker within the tolerance window
    dated_units: list[tuple[CreditRow, list[int], int]] = []
    for credit, combo, _score in phase2_survivors:
        invoice_dates = [
            invoices[i].document_date for i in combo if invoices[i].document_date
        ]
        if not invoice_dates:
            continue
        best_delta = min(abs((credit.transaction_date - d).days) for d in invoice_dates)
        if best_delta <= tolerance_days:
            dated_units.append((credit, combo, best_delta))

    if dated_units:
        min_delta = min(u[2] for u in dated_units)
        closest = [u for u in dated_units if u[2] == min_delta]
        if len(closest) == 1:
            credit, combo, _ = closest[0]
            return _build_matched(credit, combo, invoices, phase=3)

    return SubsetSumResult(
        status=SubsetSumStatus.AMBIGUOUS_COLLISION,
        message=(
            "Perfect tie after amount/entity/chronology phases — "
            "refusing to guess (AMBIGUOUS_COLLISION)."
        ),
    )


def _build_matched(
    credit: CreditRow,
    combo: list[int],
    invoices: list[InvoiceRow],
    phase: int,
) -> SubsetSumResult:
    matched = sorted(invoices[i].invoice_number for i in combo)
    return SubsetSumResult(
        status=SubsetSumStatus.SUBSET_MATCHED,
        matched_invoice_ids=matched,
        matched_bank_utr=credit.utr_number,
        bank_transaction_date=credit.transaction_date.isoformat(),
        phase_applied=phase,
        net_total_paise=credit.amount_paise,
        message=f"Matched via phase {phase} (amount/subset-sum).",
    )


# =============================================================================
# DB-driven orchestration
# =============================================================================

_FETCH_UNMATCHED_CREDITS_SQL = """
    SELECT b.utr_number, b.transaction_date, b.narration, b.amount_paise
    FROM bank_transactions b
    WHERE b.vendor_code = :vendor_code
      AND b.transaction_type = 'CREDIT'
      AND NOT EXISTS (
          SELECT 1 FROM invoice_reconciliations r
          WHERE r.vendor_code = b.vendor_code
            AND r.utr_number = b.utr_number
      )
      {utr_filter}
    ORDER BY b.transaction_date, b.utr_number
"""

_FETCH_OPEN_INVOICES_SQL = """
    SELECT e.document_id::text, e.invoice_number, e.supplier_legal_name,
           e.document_date,
           (e.grand_total_paise - e.tds_deduction_paise) AS net_paise
    FROM extracted_invoices e
    WHERE e.vendor_code = :vendor_code
      AND e.processing_status = 'VALIDATED'
      AND NOT EXISTS (
          SELECT 1 FROM invoice_reconciliations r
          WHERE r.document_id = e.document_id
      )
    ORDER BY e.invoice_number
"""


def run_subset_sum_matching(inp: SubsetSumInput, db: Session) -> SubsetSumResult:
    """Materialize candidates from the DB, then run the 3-phase waterfall."""
    utr_filter = "AND b.utr_number = :utr_number" if inp.bank_utr_number else ""
    params: dict = {"vendor_code": inp.vendor_code}
    if inp.bank_utr_number:
        params["utr_number"] = inp.bank_utr_number

    credit_rows = db.execute(
        text(_FETCH_UNMATCHED_CREDITS_SQL.format(utr_filter=utr_filter)),
        params,
    ).all()

    credits = [
        CreditRow(
            utr_number=str(row[0]),
            transaction_date=row[1],
            narration=str(row[2] or ""),
            amount_paise=int(row[3]),
        )
        for row in credit_rows
    ]

    # If a target UTR is given, restrict the pool to that single credit.
    if inp.bank_utr_number:
        credits = [c for c in credits if c.utr_number == inp.bank_utr_number]

    invoice_rows = db.execute(text(_FETCH_OPEN_INVOICES_SQL), {"vendor_code": inp.vendor_code}).all()

    invoices = [
        InvoiceRow(
            document_id=str(row[0]),
            invoice_number=str(row[1]),
            supplier_legal_name=str(row[2] or ""),
            document_date=row[3],
            net_paise=int(row[4]),
        )
        for row in invoice_rows
    ]

    # Narrow to credits matching the caller's requested target amount when
    # provided (deterministic subset of the waterfall input).
    if inp.bank_utr_number is None:
        credits = [c for c in credits if c.amount_paise == inp.target_amount_paise]

    if not credits or not invoices:
        logger.info(
            "SUBSET_NO_CANDIDATES",
            extra={
                "vendor_code": inp.vendor_code,
                "bank_utr": inp.bank_utr_number,
                "credits": len(credits),
                "invoices": len(invoices),
            },
        )
        return SubsetSumResult(status=SubsetSumStatus.NO_MATCH, message="No candidate credits or open invoices.")

    result = resolve_three_phase(
        credits=credits,
        invoices=invoices,
        tolerance_days=inp.date_tolerance_days,
    )
    logger.info(
        "SUBSET_VERDICT",
        extra={
            "vendor_code": inp.vendor_code,
            "target_utr": inp.bank_utr_number,
            "status": result.status,
            "utr": result.matched_bank_utr,
            "phase": result.phase_applied,
            "matched_invoices": result.matched_invoice_ids,
        },
    )
    return result
