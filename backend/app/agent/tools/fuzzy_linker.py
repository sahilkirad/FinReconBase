"""
Tool 1: run_fuzzy_text_linker_tool — Deterministic Entity Resolution

Links supplier legal names (extracted_invoices) to narrations from Razorpay
settlements or bank statements using:

1. Token Set Ratio on corporate-noise-stripped, order-independent tokens
   (plain Levenshtein on full strings fails when word order changes).
2. Phonetic candidate blocking (Soundex + NYSIIS) for near-miss spellings
   (LOGISTX ~ LOGISTICS) and noise destruction of bank narration artifacts
   (IMPS/90123/HDFC/MUMBAI/... -> nexus logistics).
3. False-Positive Trap: non-overlapping tokens MUST resolve phonetically or
   by stem/prefix synonymy. "Tata Motors" vs "Tata Steel" scores 0.0 —
   a naive 50% match would cause a catastrophic ledger collision.

Legs: the same tool verifies Leg A (invoice -> Razorpay narration) and
Leg B (Razorpay -> bank narration) by passing the correct narration field.
"""

import logging

from app.schemas.layer2_tools import (
    FuzzyDiagnosticTrace,
    FuzzyLinkerInput,
    FuzzyLinkerResult,
    FuzzyLinkStatus,
)

logger = logging.getLogger(__name__)

# =============================================================================
# Deterministic normalization tables
# =============================================================================

CORPORATE_NOISE = {
    "pvt", "private", "limited", "ltd", "llp", "inc", "incorporated",
    "corp", "corporation", "co", "company", "group", "holdings",
    "and", "the", "of", "llc",
}

# Bank narration artifacts: transfer modes, payment apps, bank/branch names,
# cities and routing markers. Destroyed before the ratio is computed.
BANK_NOISE = {
    "imps", "neft", "rtgs", "upi", "nach", "ach", "transfer", "trf",
    "paytm", "phonepe", "googlepay", "gpay", "bhim",
    "hdfc", "icici", "sbi", "axis", "kotak", "yesbank", "idfc",
    "indusind", "canara", "pnb", "boi", "union", "citi", "hsbc", "rbl",
    "mumbai", "delhi", "bangalore", "bengaluru", "chennai", "kolkata",
    "pune", "hyderabad", "ahmedabad", "jaipur", "gurgaon", "noida",
    "india", "inr",
}


def tokenize(text: str) -> list[str]:
    """Lowercase, split on ANY non-alphanumeric run (incl. slashes).

    Bank narrations are dense: 'IMPS/90123/HDFC/MUMBAI/NEXUS LOGISTICS' has no
    spaces around '/', so '/' (and '-', '_') must act as token separators, not
    be silently joined.
    """
    import re

    return [tok for tok in re.split(r"[^a-z0-9]+", text.lower()) if tok]


def _strip_noise(tokens: list[str], *, narration: bool) -> list[str]:
    """Remove corporate suffixes always; bank noise + numerals for narrations."""
    out: list[str] = []
    for tok in tokens:
        if tok in CORPORATE_NOISE:
            continue
        if narration:
            if tok in BANK_NOISE:
                continue
            if tok.isdigit():
                continue
        out.append(tok)
    return out


# =============================================================================
# Phonetic encoders (pure Python, deterministic)
# =============================================================================


def soundex(word: str) -> str:
    """Classic Soundex — 1 letter + 3 digits."""
    word = word.lower()
    if not word:
        return ""
    first = word[0]
    mapping = {
        "b": "1", "f": "1", "p": "1", "v": "1",
        "c": "2", "g": "2", "j": "2", "k": "2", "q": "2",
        "s": "2", "x": "2", "z": "2",
        "d": "3", "t": "3",
        "l": "4",
        "m": "5", "n": "5",
        "r": "6",
    }
    code = first.upper()
    prev = mapping.get(first, "")
    for ch in word[1:]:
        if ch in "hw":
            continue
        digit = mapping.get(ch, "")
        if digit and digit != prev:
            code += digit
            if len(code) == 4:
                break
        prev = digit if digit else prev
    return (code + "000")[:4]


def nysiis(word: str) -> str:
    """Simplified NYSIIS encoder (deterministic, sufficient for blocking)."""
    word = word.lower()
    if not word:
        return ""

    def _replace(s: str, old: str, new: str) -> str:
        return s.replace(old, new)

    # Basic rule set
    w = word
    if w.startswith("mac"):
        w = "mc" + w[3:]
    elif w.startswith("pf"):
        w = w[2:]
    if w.endswith("ey"):
        w = w[:-2] + "y"
    elif w.endswith("e"):
        w = w[:-1]

    w = _replace(w, "ph", "f")
    w = _replace(w, "kn", "n")
    w = _replace(w, "sch", "s")
    w = _replace(w, "ee", "y")
    w = _replace(w, "ie", "y")
    w = _replace(w, "dt", "d")
    w = _replace(w, "rt", "d")
    w = _replace(w, "rd", "d")
    w = _replace(w, "nt", "d")
    w = _replace(w, "nd", "d")

    out = [w[0]]
    for ch in w[1:]:
        if ch in "aeiou":
            out.append("a")
        elif ch in "q":
            out.append("g")
        elif ch in "z":
            out.append("s")
        elif ch in "m":
            out.append("n")
        elif ch in "k":
            out.append("c")
        elif ch in "f":
            out.append("f")
        elif ch in "p":
            out.append("p")
        elif ch in "y":
            if out and out[-1] != "y":
                out.append("y")
        elif ch in "w":
            out.append("w")
        elif ch in "h":
            # keep silent unless surrounded by vowels — simplified: drop
            continue
        elif ch == "v":
            out.append("v")
        else:
            out.append(ch)

    # Collapse adjacent duplicates
    collapsed: list[str] = []
    for ch in out:
        if not collapsed or collapsed[-1] != ch:
            collapsed.append(ch)
    # NYSIIS trims a trailing 's'
    if len(collapsed) > 1 and collapsed[-1] == "s":
        collapsed.pop()
    result = "".join(collapsed)
    # Truncate/pad to 6 for stable comparison
    return (result + "aaaaaa")[:6]


def levenshtein(a: str, b: str) -> int:
    """Classic DP Levenshtein distance (used on short tokens only)."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        cur = [i]
        for j, cb in enumerate(b, start=1):
            cur.append(min(
                prev[j] + 1,
                cur[j - 1] + 1,
                prev[j - 1] + (0 if ca == cb else 1),
            ))
        prev = cur
    return prev[-1]


def _token_synonym(a: str, b: str) -> tuple[bool, str]:
    """Return (matched, method) for a pair of differing tokens."""
    if a == b:
        return True, "exact"
    if soundex(a) == soundex(b) and soundex(a) not in ("", "0000"):
        return True, "soundex"
    if nysiis(a) == nysiis(b) and nysiis(a) not in ("", "aaaaaa"):
        return True, "nysiis"
    # Stem/prefix synonymy: "tech" ~ "technologies"
    if len(a) >= 4 and len(b) >= 4 and (a.startswith(b) or b.startswith(a)):
        return True, "prefix"
    # OCR/near-phonetic variance: "logistx" ~ "logistics"
    if len(a) >= 5 and len(b) >= 5 and levenshtein(a, b) <= 2:
        return True, "edit"
    return False, ""


# =============================================================================
# Token Set Ratio core
# =============================================================================


def token_set_ratio_tokens(tokens_a: list[str], tokens_b: list[str]) -> tuple[float, bool]:
    """Order-independent similarity with the false-positive trap.

    Returns (score in [0,1], phonetic_match).

    - Equal token sets (after noise stripping) => 1.0.
    - Containment (one set a subset of the other, e.g. bank narration is a
      truncated supplier name) => |smaller| / |larger| — degraded, not zeroed,
      so truncated narrations score < 0.85 and route to human review instead
      of silently passing (false-negative is safe; false-positive is not).
    - Partial overlap where a non-overlapping token cannot be synonym-resolved
      => 0.0 (the Tata Motors vs Tata Steel ledger-collision trap).
    """
    if not tokens_a and not tokens_b:
        return 1.0, False
    if not tokens_a or not tokens_b:
        return 0.0, False

    set_a = list(dict.fromkeys(tokens_a))
    set_b = list(dict.fromkeys(tokens_b))
    inter = [t for t in set_a if t in set_b]
    rem_a = [t for t in set_a if t not in set_b]
    rem_b = [t for t in set_b if t not in set_a]

    phonetic_match = False
    matched_pairs = 0

    # Containment: no contradictory remainder exists on either side.
    if not rem_a or not rem_b:
        matched = len(inter)
        larger = max(len(set_a), len(set_b))
        score = matched / larger if larger else 0.0
        return round(min(score, 1.0), 4), False

    # Best-effort synonym pairing of the non-overlapping remainder.
    remaining_b = list(rem_b)
    for token in list(rem_a):
        best_idx: int | None = None
        best_method = ""
        for j, other in enumerate(remaining_b):
            ok, method = _token_synonym(token, other)
            if ok:
                if best_idx is None or method == "exact":
                    best_idx = j
                    best_method = method
        if best_idx is not None:
            matched_pairs += 1
            phonetic_match = phonetic_match or best_method in ("soundex", "nysiis", "edit")
            remaining_b.pop(best_idx)

    # False-positive trap: any unresolvable remainder on BOTH sides kills the match.
    if (len(rem_a) - matched_pairs) > 0 or remaining_b:
        return 0.0, phonetic_match

    aligned = len(inter) + matched_pairs
    score = (2.0 * aligned) / (len(set_a) + len(set_b))
    return round(min(score, 1.0), 4), phonetic_match


# =============================================================================
# Tool entry point
# =============================================================================


def run_fuzzy_text_linker(inp: FuzzyLinkerInput) -> FuzzyLinkerResult:
    """Resolve source_entity_name against target_bank_narration."""
    source_tokens = _strip_noise(tokenize(inp.source_entity_name), narration=False)
    target_tokens = _strip_noise(tokenize(inp.target_bank_narration), narration=True)

    score, phonetic_match = token_set_ratio_tokens(source_tokens, target_tokens)

    resolved = score >= inp.match_threshold
    logger.info(
        "FUZZY_VERDICT",
        extra={
            "status": FuzzyLinkStatus.ENTITY_RESOLVED if resolved else FuzzyLinkStatus.ENTITY_MISMATCH,
            "score": round(score, 4),
            "phonetic_match": phonetic_match,
        },
    )
    return FuzzyLinkerResult(
        status=FuzzyLinkStatus.ENTITY_RESOLVED if resolved else FuzzyLinkStatus.ENTITY_MISMATCH,
        confidence_score=score,
        resolved_vendor_code=inp.context_vendor_code if resolved else None,
        diagnostic_trace=FuzzyDiagnosticTrace(
            normalized_source=" ".join(source_tokens),
            normalized_target=" ".join(target_tokens),
            phonetic_match=phonetic_match,
        ),
    )
