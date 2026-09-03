"""
Layer 2 E2E Test Feed Generator — Stream 2 (Razorpay) + Stream 3 (Bank)

Why this reads Postgres instead of re-running generate_test_batch.py:
the 50-invoice PDF generator is seedless-random, so re-running it can never
reproduce the numbers inside YOUR test_batch_50.pdf. The reconciliation
engine (subset-sum / anchor / ledger) matches against what Layer 1 actually
extracted and stored in `extracted_invoices` (integer paise). This script
therefore derives the feeds FROM THE DATABASE, so amounts always reconcile.

Per extracted invoice it emits exactly one pair of records:
  * one Razorpay settlement: status='processed', reference_id = INV number,
    utr = bank UTR, amount = invoice net (grand_total - tds). The Layer 2
    anchor_node uses reference_id == invoice_number to bind the payout, and
    its UTR narrows the subset search to one bank credit.
  * one bank CREDIT: utr = same UTR, amount = same net. Phase-1 unique
    subset-sum then matches 1 invoice : 1 credit deterministically.

Optional --anomalies N drops the LAST N invoices from BOTH feeds, so those
invoices can never match -> NO_MATCH -> fuzzy -> supervisor -> DLQ
(reconciliation.dlq.events). Use it to see the human-review path live.

Usage:
    python scripts/generate_layer2_feeds.py --vendor-code VEND_TEST_002
    python scripts/generate_layer2_feeds.py --vendor-code VEND_TEST_002 --anomalies 3
    python scripts/generate_layer2_feeds.py --vendor-code VEND_TEST_002 --push --token <JWT>

Outputs (in --out-dir, default ./):
    razorpay_webhooks.json   -> array of POST /webhooks/razorpay bodies
    bank_transactions.json   -> array for POST /ingestion/bank (one call)
"""

import argparse
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

DEFAULT_DSN = "postgresql+psycopg://postgres:postgres@localhost:5457/finrecon"

# SQL mirrors the Layer 2 boundary/fallback joins: batch_invoice_items ->
# extracted_invoices, only reconciled/consumable rows.
_LATEST_COMPLETED_BATCH_SQL = """
    SELECT batch_id::text
    FROM batch_jobs
    WHERE vendor_code = :vc AND status = 'COMPLETED'
    ORDER BY completed_at DESC NULLS LAST, created_at DESC
    LIMIT 1
"""

_INVOICES_FOR_BATCH_SQL = """
    SELECT e.document_id::text,
           e.invoice_number,
           e.vendor_code,
           e.document_date,
           e.supplier_legal_name,
           (e.grand_total_paise - e.tds_deduction_paise) AS net_paise,
           e.grand_total_paise,
           e.tds_deduction_paise
    FROM batch_invoice_items i
    JOIN extracted_invoices e ON e.document_id = i.document_id
    WHERE i.batch_id = :bid
      AND e.processing_status = 'VALIDATED'
      AND NOT EXISTS (
          SELECT 1 FROM invoice_reconciliations r
          WHERE r.document_id = e.document_id
      )
    ORDER BY e.invoice_number
"""

_ALL_OPEN_INVOICES_SQL = """
    SELECT e.document_id::text,
           e.invoice_number,
           e.vendor_code,
           e.document_date,
           e.supplier_legal_name,
           (e.grand_total_paise - e.tds_deduction_paise) AS net_paise,
           e.grand_total_paise,
           e.tds_deduction_paise
    FROM extracted_invoices e
    WHERE e.vendor_code = :vc
      AND e.processing_status = 'VALIDATED'
      AND NOT EXISTS (
          SELECT 1 FROM invoice_reconciliations r
          WHERE r.document_id = e.document_id
      )
    ORDER BY e.invoice_number
"""


def _connect(dsn: str):
    from sqlalchemy import create_engine

    engine = create_engine(dsn)
    return engine.connect()


def _fetch_invoices(conn, vendor_code: str, batch_id: str | None) -> list[dict]:
    from sqlalchemy import text

    if batch_id:
        rows = conn.execute(
            text(_INVOICES_FOR_BATCH_SQL), {"bid": batch_id}
        ).all()
    else:
        rows = conn.execute(
            text(_ALL_OPEN_INVOICES_SQL), {"vc": vendor_code}
        ).all()

    invoices = []
    for r in rows:
        net_paise = int(r[5])
        if net_paise <= 0:
            continue  # non-reconcilable net would never match a credit
        invoices.append({
            "document_id": str(r[0]),
            "invoice_number": str(r[1]),
            "vendor_code": str(r[2]),
            "document_date": r[3],
            "supplier_legal_name": str(r[4]),
            "net_paise": net_paise,
            "grand_total_paise": int(r[6]),
            "tds_deduction_paise": int(r[7]),
        })
    return invoices


def _bank_date(inv: dict) -> date:
    """Credit lands a couple of days after the invoice date (inside the
    ±7-day phase-3 tolerance window; phase 1 already wins, so this only
    matters if a collision forces chronology)."""
    base = inv.get("document_date") or date(2026, 8, 1)
    return base + timedelta(days=2)


def build_feeds(invoices: list[dict], anomalies: int) -> tuple[list[dict], list[dict]]:
    """Return (razorpay_payloads, bank_payloads)."""
    usable = invoices[: len(invoices) - anomalies] if anomalies else invoices

    razorpay: list[dict] = []
    bank: list[dict] = []
    running_balance_paise = 0

    for idx, inv in enumerate(usable, start=1):
        net_paise = inv["net_paise"]
        utr = f"{300000000001 + idx:012d}"
        payout_id = f"pout_e2e_{idx:04d}"
        fund_account_id = f"fa_e2e_{idx:04d}"
        tx_date = _bank_date(inv)
        supplier = inv["supplier_legal_name"]
        invoice_number = inv["invoice_number"]
        epoch = int(
            datetime(tx_date.year, tx_date.month, tx_date.day, tzinfo=timezone.utc).timestamp()
        )

        razorpay.append({
            "payout_id": payout_id,
            "fund_account_id": fund_account_id,
            "amount_paise": net_paise,
            "currency": "INR",
            "status": "processed",  # anchor_node only binds status='processed'
            "utr": utr,
            "reference_id": invoice_number,  # anchor_node binds ref == invoice number
            "narration": f"{supplier.upper()} - PAYOUT {invoice_number}",
            "fees_paise": 0,
            "tax_paise": 0,
            "mode": "IMPS",
            "purpose": "payout",
            "event_created_at_epoch": epoch,
        })

        running_balance_paise += net_paise
        bank.append({
            "transaction_date": tx_date.isoformat(),
            "narration": f"CREDIT/IMPS/{utr}/{supplier.upper()}/{invoice_number}",
            "utr_number": utr,
            "transaction_type": "CREDIT",
            "amount_paise": net_paise,
            "closing_balance_paise": running_balance_paise,
        })

    return razorpay, bank


def _push(base_url: str, token: str, razorpay: list[dict], bank: list[dict]) -> None:
    import requests

    headers = {"Authorization": f"Bearer {token}"}
    ok = 0
    for idx, payload in enumerate(razorpay, start=1):
        resp = requests.post(
            f"{base_url}/webhooks/razorpay", json=payload, headers=headers, timeout=30
        )
        if resp.status_code == 202:
            ok += 1
        else:
            print(f"  razorpay[{idx}] {payload['payout_id']} -> HTTP {resp.status_code}: {resp.text[:200]}")
    print(f"Razorpay webhooks pushed: {ok}/{len(razorpay)} accepted (202)")

    resp = requests.post(
        f"{base_url}/ingestion/bank", json=bank, headers=headers, timeout=60
    )
    print(f"Bank feed push -> HTTP {resp.status_code}")
    print(resp.text[:400])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vendor-code", required=True, help="Vendor code owning the invoices (JWT vendor_code)")
    parser.add_argument("--batch-id", default=None, help="Explicit Layer 1 batch UUID; defaults to the latest COMPLETED batch of the vendor")
    parser.add_argument("--dsn", default=DEFAULT_DSN, help="Postgres DSN (host port 5457)")
    parser.add_argument("--out-dir", default=".", help="Directory for the generated JSON files")
    parser.add_argument("--anomalies", type=int, default=0, help="Drop last N invoices from BOTH feeds to demo the DLQ path")
    parser.add_argument("--push", action="store_true", help="POST the feeds to the live API after generating")
    parser.add_argument("--token", default=None, help="JWT bearer token (required with --push)")
    parser.add_argument("--base-url", default="http://localhost:8000", help="API base URL (with --push)")
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    conn = _connect(args.dsn)
    try:
        from sqlalchemy import text

        batch_id = args.batch_id
        if not batch_id:
            row = conn.execute(
                text(_LATEST_COMPLETED_BATCH_SQL), {"vc": args.vendor_code}
            ).first()
            if row is None:
                print(
                    f"ERROR: no COMPLETED batch found for vendor '{args.vendor_code}'. "
                    "Finish Layer 1 first (batch_jobs.status = COMPLETED), then re-run.",
                    file=sys.stderr,
                )
                return 1
            batch_id = str(row[0])

        invoices = _fetch_invoices(conn, args.vendor_code, batch_id)
    finally:
        conn.close()

    if not invoices:
        print(
            f"ERROR: no open VALIDATED invoices for vendor '{args.vendor_code}' "
            f"(batch {batch_id}). Nothing to reconcile.",
            file=sys.stderr,
        )
        return 1

    razorpay, bank = build_feeds(invoices, args.anomalies)

    rp_path = out_dir / "razorpay_webhooks.json"
    bk_path = out_dir / "bank_transactions.json"
    rp_path.write_text(json.dumps(razorpay, indent=2), encoding="utf-8")
    bk_path.write_text(json.dumps(bank, indent=2), encoding="utf-8")

    print("=" * 72)
    print(f"Vendor code        : {args.vendor_code}")
    print(f"Batch ID           : {batch_id}")
    print(f"Open VALIDATED inv : {len(invoices)}")
    print(f"Feeds generated    : {len(razorpay)} razorpay + {len(bank)} bank (anomalies dropped: {args.anomalies})")
    print(f"  -> {rp_path}")
    print(f"  -> {bk_path}")
    print("Expected outcome:")
    print(f"  - matched (LEDGER_COMMITTED)      : {len(invoices) - args.anomalies}")
    if args.anomalies:
        print(f"  - exceptions (reconciliation.dlq) : {args.anomalies} (NO_MATCH path)")
    print("=" * 72)
    print("Next:")
    print("  1. POST /ingestion/bank with the full bank_transactions.json body (single call)")
    print("  2. POST /webhooks/razorpay once PER object in razorpay_webhooks.json (or re-run with --push)")
    print("  3. Watch finrecon-recon-supervisor logs; verify invoice_reconciliations + outbox_events.")

    if args.push:
        if not args.token:
            print("ERROR: --push requires --token <JWT>", file=sys.stderr)
            return 1
        _push(args.base_url, args.token, razorpay, bank)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
