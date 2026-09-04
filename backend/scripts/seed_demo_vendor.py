"""
Seed demo vendor credentials (Track 4 native auth demo).

Idempotent: safe to run any number of times. For each demo vendor it ensures
- a vendor_users row exists (synthetic google_subject_id = 'native_seed_<code>',
  role ADMIN) so the native login can bind a JWT 'sub', and
- a vendor_credentials row (migration 005) holding vendor_name + the PBKDF2
  hash of the shared demo secret.

Usage:
    docker compose exec backend-api python scripts/seed_demo_vendor.py
    # or on the host from backend/ with your .env reachable:
    python scripts/seed_demo_vendor.py

Env:
    DEMO_VENDOR_SECRET   shared API secret for every seeded demo vendor
                         (default: FinReconDemo@2026)
"""

import os
import sys

from sqlalchemy import text

from app.core.security import hash_api_secret, normalize_vendor_code

# (vendor_code, vendor_name) pairs provisioned for the judge demo.
DEMO_VENDORS = [
    ("VEND_TEST_002", "Test Vendor (Batch 50)"),
    ("VEND_DEMO_001", "Acme Finance Demo"),
]

_INSERT_USER_SQL = text(
    """
    INSERT INTO vendor_users (email, google_subject_id, vendor_code, role)
    VALUES (:email, :google_subject_id, :vendor_code, 'ADMIN')
    ON CONFLICT DO NOTHING
    """
)

_UPSERT_CREDENTIAL_SQL = text(
    """
    INSERT INTO vendor_credentials (vendor_code, vendor_name, api_secret_hash)
    VALUES (:vendor_code, :vendor_name, :api_secret_hash)
    ON CONFLICT (vendor_code) DO UPDATE
        SET vendor_name = EXCLUDED.vendor_name,
            api_secret_hash = EXCLUDED.api_secret_hash
    """
)


def main() -> int:
    from app.db.session import SessionLocal

    secret = os.getenv("DEMO_VENDOR_SECRET", "FinReconDemo@2026")
    if len(secret) < 8:
        print("DEMO_VENDOR_SECRET must be at least 8 characters.", file=sys.stderr)
        return 1

    db = SessionLocal()
    try:
        for raw_code, vendor_name in DEMO_VENDORS:
            vendor_code = normalize_vendor_code(raw_code)
            db.execute(
                _INSERT_USER_SQL,
                {
                    "email": f"{vendor_code.lower()}@finrecon-demo.local",
                    "google_subject_id": f"native_seed_{vendor_code}",
                    "vendor_code": vendor_code,
                },
            )
            db.execute(
                _UPSERT_CREDENTIAL_SQL,
                {
                    "vendor_code": vendor_code,
                    "vendor_name": vendor_name,
                    "api_secret_hash": hash_api_secret(secret),
                },
            )
            print(f"seeded {vendor_code}: '{vendor_name}'")
        db.commit()
    finally:
        db.close()

    print("\nDemo logins (sessionStorage JWT, 120-minute TTL):")
    for raw_code, vendor_name in DEMO_VENDORS:
        vendor_code = normalize_vendor_code(raw_code)
        print(f"  vendor_code : {vendor_code}")
        print(f"  vendor_name : {vendor_name}")
        print(f"  api_secret  : {secret}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
