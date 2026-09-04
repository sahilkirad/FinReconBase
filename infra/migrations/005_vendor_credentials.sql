-- Migration 005: Native Vendor Credentials (Track 4 Frontend Auth)
-- ADDITIVE ONLY. Creates one brand-new table; no existing table, column,
-- type, index, or constraint is modified (Core-logic schema stays frozen).
--
-- Rationale:
--   * vendor_users (email, google_subject_id NOT NULL UNIQUE, vendor_code, role)
--     is part of the frozen Core-logic schema and keeps its Google-linked shape.
--   * Native sign-ups still insert a vendor_users row using a synthetic
--     google_subject_id ('native_<user_uuid>') so the JWT 'sub' continues to
--     reference vendor_users.user_id and existing auth code is untouched.
--   * vendor_name + api_secret_hash live HERE (new table only), because the
--     vendor_users table cannot be altered.
--
-- api_secret_hash format: pbkdf2_sha256$<iterations>$<salt_hex>$<dk_hex>

CREATE TABLE vendor_credentials (
    vendor_code TEXT PRIMARY KEY,
    vendor_name TEXT NOT NULL,
    api_secret_hash TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT vendor_credentials_vendor_code_format
        CHECK (vendor_code ~ '^[A-Z0-9_\-]{3,64}$')
);

CREATE INDEX vendor_credentials_created_at_idx
    ON vendor_credentials (created_at);
