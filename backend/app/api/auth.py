import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.security import (
    create_access_token,
    get_current_user_context,
    hash_api_secret,
    verify_api_secret,
    verify_google_id_token,
)
from app.db.session import get_db
from app.schemas.auth import (
    AuthenticatedUserResponse,
    GoogleLoginRequest,
    TokenResponse,
    VendorLoginRequest,
    VendorRegisterRequest,
    VendorTokenResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/google", response_model=TokenResponse)
def login_with_google(
    payload: GoogleLoginRequest,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> TokenResponse:
    google_claims = verify_google_id_token(payload.id_token, settings)
    google_subject_id = google_claims.get("sub")
    email = google_claims.get("email")

    if not google_subject_id or not email:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Google token is missing required identity claims.",
        )

    result = db.execute(
        text(
            """
            SELECT user_id, email, vendor_code, role
            FROM vendor_users
            WHERE google_subject_id = :google_subject_id
              AND email = :email
            """
        ),
        {
            "google_subject_id": google_subject_id,
            "email": email,
        },
    ).mappings().first()

    if result is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User is not mapped to a vendor account.",
        )

    access_token = create_access_token(
        subject=str(result["user_id"]),
        vendor_code=str(result["vendor_code"]),
        role=str(result["role"]),
        settings=settings,
    )

    return TokenResponse(access_token=access_token)


# =============================================================================
# Native vendor auth (Track 4 frontend)
# =============================================================================

# SQL for vendor_credentials (additive table, migration 005).
_INSERT_VENDOR_USER_SQL = text(
    """
    INSERT INTO vendor_users (
        user_id, email, google_subject_id, vendor_code, role
    ) VALUES (
        :user_id, :email, :google_subject_id, :vendor_code, :role
    )
    """
)

_INSERT_CREDENTIAL_SQL = text(
    """
    INSERT INTO vendor_credentials (
        vendor_code, vendor_name, api_secret_hash
    ) VALUES (
        :vendor_code, :vendor_name, :api_secret_hash
    )
    """
)

_SELECT_CREDENTIAL_SQL = text(
    """
    SELECT vendor_name, api_secret_hash
    FROM vendor_credentials
    WHERE vendor_code = :vendor_code
    """
)

# A vendor_code may carry multiple vendor_users rows (Google sign-ins); the
# native token binds to the ADMIN row when one exists, else the oldest user.
_SELECT_NATIVE_USER_SQL = text(
    """
    SELECT user_id, role
    FROM vendor_users
    WHERE vendor_code = :vendor_code
    ORDER BY (role = 'ADMIN') DESC, created_at ASC
    LIMIT 1
    """
)

_SELECT_CREDENTIAL_EXISTS_SQL = text(
    "SELECT 1 FROM vendor_credentials WHERE vendor_code = :vendor_code"
)

_SELECT_USER_EXISTS_SQL = text(
    "SELECT 1 FROM vendor_users WHERE vendor_code = :vendor_code"
)


@router.post(
    "/vendor/register",
    response_model=VendorTokenResponse,
    status_code=status.HTTP_201_CREATED,
)
def register_vendor(
    payload: VendorRegisterRequest,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> VendorTokenResponse:
    """Self-register a vendor tenant and log it in with a 120-minute JWT.

    Persists two rows in ONE transaction: a vendor_users row (synthetic
    google_subject_id = 'native_<uuid>', role ADMIN) so the token 'sub' keeps
    referencing vendor_users.user_id, and a vendor_credentials row (additive
    table, migration 005) holding the vendor_name + PBKDF2 secret hash.
    """
    vendor_code = payload.vendor_code  # normalized by the schema validator

    existing = db.execute(
        _SELECT_CREDENTIAL_EXISTS_SQL, {"vendor_code": vendor_code}
    ).first()
    user_exists = db.execute(
        _SELECT_USER_EXISTS_SQL, {"vendor_code": vendor_code}
    ).first()
    if existing is not None or user_exists is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error_code": "VENDOR_CODE_ALREADY_ONBOARDED",
                "message": f"Vendor '{vendor_code}' is already onboarded.",
            },
        )

    user_id = str(uuid.uuid4())
    secret_hash = hash_api_secret(payload.api_secret)

    try:
        db.execute(
            _INSERT_VENDOR_USER_SQL,
            {
                "user_id": user_id,
                "email": payload.email,
                "google_subject_id": f"native_{user_id}",
                "vendor_code": vendor_code,
                "role": "ADMIN",
            },
        )
        db.execute(
            _INSERT_CREDENTIAL_SQL,
            {
                "vendor_code": vendor_code,
                "vendor_name": payload.vendor_name,
                "api_secret_hash": secret_hash,
            },
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error_code": "VENDOR_REGISTRATION_CONFLICT",
                "message": "Vendor registration conflicted with an existing record "
                           f"(duplicate email or vendor code). {exc.orig}",
            },
        )

    access_token = create_access_token(
        subject=user_id,
        vendor_code=vendor_code,
        role="ADMIN",
        settings=settings,
    )

    logger.info(
        "VENDOR_REGISTERED", extra={"vendor_code": vendor_code, "role": "ADMIN"}
    )
    return VendorTokenResponse(
        access_token=access_token,
        vendor_code=vendor_code,
        vendor_name=payload.vendor_name,
        role="ADMIN",
    )


@router.post("/vendor/token", response_model=VendorTokenResponse)
def login_vendor(
    payload: VendorLoginRequest,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> VendorTokenResponse:
    """Native login: vendor_code + api_secret -> 120-minute JWT.

    Used by both self-registered tenants and pre-seeded demo vendors
    (e.g. VEND_TEST_002). Always 401 on bad credentials; never reveals
    whether a vendor_code exists.
    """
    vendor_code = payload.vendor_code  # normalized by the schema validator

    credential = db.execute(
        _SELECT_CREDENTIAL_SQL, {"vendor_code": vendor_code}
    ).first()

    if credential is None:
        # Equalize timing against the hashing cost of a real lookup so the
        # endpoint cannot be used to enumerate registered vendor codes.
        hash_api_secret(payload.api_secret)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error_code": "INVALID_VENDOR_CREDENTIALS",
                "message": "Invalid vendor code or API secret.",
            },
        )

    if not verify_api_secret(payload.api_secret, str(credential[1])):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error_code": "INVALID_VENDOR_CREDENTIALS",
                "message": "Invalid vendor code or API secret.",
            },
        )

    user = db.execute(
        _SELECT_NATIVE_USER_SQL, {"vendor_code": vendor_code}
    ).first()
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error_code": "VENDOR_HAS_NO_USER_ACCOUNT",
                "message": f"Vendor '{vendor_code}' has no user account to authenticate.",
            },
        )

    access_token = create_access_token(
        subject=str(user[0]),
        vendor_code=vendor_code,
        role=str(user[1]),
        settings=settings,
    )

    logger.info("VENDOR_LOGIN_OK", extra={"vendor_code": vendor_code})
    return VendorTokenResponse(
        access_token=access_token,
        vendor_code=vendor_code,
        vendor_name=str(credential[0]),
        role=str(user[1]),
    )


@router.get("/me", response_model=AuthenticatedUserResponse)
def read_current_user(
    user_context: dict = Depends(get_current_user_context),
) -> AuthenticatedUserResponse:
    return AuthenticatedUserResponse(
        user_id=str(user_context["sub"]),
        email=user_context.get("email"),
        vendor_code=str(user_context["vendor_code"]),
        role=str(user_context["role"]),
    )