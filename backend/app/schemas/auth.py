import re

from pydantic import BaseModel, Field, field_validator

from app.core.security import normalize_vendor_code

_VENDOR_CODE_RE = re.compile(r"^[A-Za-z0-9_-]{3,64}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class GoogleLoginRequest(BaseModel):
    id_token: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class AuthenticatedUserResponse(BaseModel):
    user_id: str
    email: str | None = None
    vendor_code: str
    role: str


# =============================================================================
# Native vendor auth (Track 4 frontend) — zero Google OAuth
# =============================================================================


class VendorRegisterRequest(BaseModel):
    """Self-registration payload for a new vendor tenant.

    On success the backend persists the vendor (vendor_users + the additive
    vendor_credentials table) and returns a JWT that logs the vendor in
    immediately.
    """

    vendor_code: str = Field(min_length=3, max_length=64)
    vendor_name: str = Field(min_length=2, max_length=120)
    email: str = Field(min_length=3, max_length=254)
    api_secret: str = Field(min_length=8, max_length=128)

    @field_validator("vendor_code")
    @classmethod
    def _validate_vendor_code(cls, v: str) -> str:
        normalized = normalize_vendor_code(v)
        if not _VENDOR_CODE_RE.fullmatch(normalized):
            raise ValueError(
                "Vendor code may contain only letters, digits, '_' or '-' (3-64 chars)."
            )
        return normalized

    @field_validator("vendor_name")
    @classmethod
    def _clean_vendor_name(cls, v: str) -> str:
        cleaned = " ".join(v.strip().split())
        if len(cleaned) < 2:
            raise ValueError("Vendor name must be at least 2 characters.")
        return cleaned

    @field_validator("email")
    @classmethod
    def _validate_email(cls, v: str) -> str:
        normalized = v.strip().lower()
        if not _EMAIL_RE.fullmatch(normalized):
            raise ValueError("A valid email address is required.")
        return normalized


class VendorLoginRequest(BaseModel):
    """Native login for both self-registered and pre-seeded demo vendors."""

    vendor_code: str = Field(min_length=1, max_length=64)
    api_secret: str = Field(min_length=1, max_length=128)

    @field_validator("vendor_code")
    @classmethod
    def _validate_vendor_code(cls, v: str) -> str:
        return normalize_vendor_code(v)


class VendorTokenResponse(TokenResponse):
    """JWT + vendor profile so the frontend can hydrate the nav bar immediately."""

    vendor_code: str
    vendor_name: str
    role: str