import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token

from app.core.config import Settings, get_settings

bearer_scheme = HTTPBearer(auto_error=False)


def create_access_token(
    *,
    subject: str,
    vendor_code: str,
    role: str,
    settings: Settings,
) -> str:
    expires_at = datetime.now(timezone.utc) + timedelta(
        minutes=settings.jwt_expire_minutes
    )

    payload = {
        "sub": subject,
        "vendor_code": vendor_code,
        "role": role,
        "exp": expires_at,
    }

    return jwt.encode(
        payload,
        settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
    )


def decode_access_token(token: str, settings: Settings) -> dict[str, Any]:
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret_key,
            algorithms=[settings.jwt_algorithm],
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired authentication token.",
        ) from exc

    required_claims = ("sub", "vendor_code", "role")
    if any(claim not in payload for claim in required_claims):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication token is missing required claims.",
        )

    return payload


# =============================================================================
# Native vendor API-secret hashing (Track 4 frontend auth)
# =============================================================================

_API_SECRET_PBKDF2_ITERATIONS = 260_000
# Iterations are read back from the stored string, so this cap only guards
# against a corrupted/hostile row forcing an absurd CPU burn on verify.
_MAX_PBKDF2_ITERATIONS = 1_000_000


def hash_api_secret(
    secret: str,
    *,
    iterations: int = _API_SECRET_PBKDF2_ITERATIONS,
) -> str:
    """Hash an API secret into a self-describing PBKDF2-HMAC-SHA256 string.

    Format: ``pbkdf2_sha256$<iterations>$<salt_hex>$<dk_hex>``
    """
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac(
        "sha256",
        secret.encode("utf-8"),
        bytes.fromhex(salt),
        iterations,
    )
    return f"pbkdf2_sha256${iterations}${salt}${dk.hex()}"


def verify_api_secret(secret: str, encoded: str) -> bool:
    """Constant-time verification of a secret against a stored hash string."""
    try:
        algorithm, iterations_raw, salt_hex, expected_hex = encoded.split("$", 3)
        iterations = int(iterations_raw)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(expected_hex)
    except (ValueError, TypeError):
        return False

    if algorithm != "pbkdf2_sha256" or not 1 <= iterations <= _MAX_PBKDF2_ITERATIONS:
        return False

    dk = hashlib.pbkdf2_hmac(
        "sha256",
        secret.encode("utf-8"),
        salt,
        iterations,
    )
    return hmac.compare_digest(dk, expected)


def normalize_vendor_code(raw: str) -> str:
    """Canonical vendor-code form: trimmed + upper-cased.

    Mirrors the CHECK constraint on vendor_credentials.vendor_code
    (uppercase [A-Z0-9_-]) so VEND_test_002 and VEND_TEST_002 are the same
    tenant.
    """
    return raw.strip().upper()


def verify_google_id_token(token: str, settings: Settings) -> dict[str, Any]:
    try:
        return id_token.verify_oauth2_token(
            token,
            google_requests.Request(),
            settings.google_oauth_client_id,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Google ID token.",
        ) from exc


def get_current_user_context(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer authentication token.",
        )

    return decode_access_token(credentials.credentials, settings)