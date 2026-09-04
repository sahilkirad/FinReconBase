"""
Layer 2 E2E Test Feed Generator — Stream 2 (Razorpay) + Stream 3 (Bank)

Why this reads Postgres instead of re-running generate_test_batch.py:
the 50-invoice PDF generator is seedless-random, so re-running it can never
reproduce the numbers inside YOUR test_batch_50.pdf. The reconciliation
engine (subset-sum / anchor / ledger) matches against what Layer 1 actually
extracted and stored in `extracted_invoices` (integer paise). This script
therefore derives the feeds FROM THE DATABASE, so amounts always reconcile.

NOTE: the pure feed builders now live in app/demo/feeds.py (single source of
truth). POST /demo/auto-generate-feeds uses the same builders in-API — the
UI path replaces this manual terminal flow.

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
from pathlib import Path

# Allow `python scripts/generate_layer2_feeds.py` from the backend dir to
# import the shared app package (same trick as generate_test_batch.py).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.demo.feeds import (  # noqa: E402
    build_feeds,
    fetch_all_open_invoices,
    fetch_batch_invoices,
    fetch_latest_completed_batch,
)

DEFAULT_DSN = "postgresql+psycopg://postgres:postgres@localhost:5457/finrecon"


def _connect(dsn: str):
    from sqlalchemy import create_engine

    engine = create_engine(dsn)
    return engine.connect()


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
    parser.add_argument(
        "--scenario",
        choices=["clean", "agent-fallback"],
        default="clean",
        help="Layer 2 test scenario",
    )
    parser.add_argument("--push", action="store_true", help="POST the feeds to the live API after generating")
    parser.add_argument("--token", default=None, help="JWT bearer token (required with --push)")
    parser.add_argument("--base-url", default="http://localhost:8000", help="API base URL (with --push)")
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    conn = _connect(args.dsn)
    try:
        batch_id = args.batch_id
        if not batch_id:
            batch_id = fetch_latest_completed_batch(conn, args.vendor_code)
            if batch_id is None:
                print(
                    f"ERROR: no COMPLETED batch found for vendor '{args.vendor_code}'. "
                    "Finish Layer 1 first (batch_jobs.status = COMPLETED), then re-run.",
                    file=sys.stderr,
                )
                return 1

        invoices = fetch_batch_invoices(conn, batch_id)
    finally:
        conn.close()

    if not invoices:
        print(
            f"ERROR: no open VALIDATED invoices for vendor '{args.vendor_code}' "
            f"(batch {batch_id}). Nothing to reconcile.",
            file=sys.stderr,
        )
        return 1

    razorpay, bank = build_feeds(invoices, args.anomalies, args.scenario)

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
    if args.scenario == "agent-fallback":
        print(f"  - deterministic matches            : {len(invoices) - args.anomalies}")
        print(f"  - forwarded to Groq agent         : {args.anomalies}")
        print(f"  - unresolved cases may reach DLQ  : {args.anomalies}")
    else:
        print(f"  - deterministic matches            : {len(invoices)}")
    print("=" * 72)
    print("Next:")
    print("  1. POST /ingestion/bank with the full bank_transactions.json body (single call)")
    print("  2. POST /webhooks/razorpay once PER object in razorpay_webhooks.json (or re-run with --push)")
    print("  3. Watch finrecon-recon-supervisor logs; verify invoice_reconciliations + outbox_events.")
    print("  (Alternatively, POST /demo/auto-generate-feeds does all of this automatically.)")

    if args.push:
        if not args.token:
            print("ERROR: --push requires --token <JWT>", file=sys.stderr)
            return 1
        _push(args.base_url, args.token, razorpay, bank)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
