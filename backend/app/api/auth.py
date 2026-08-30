from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.security import (
    create_access_token,
    get_current_user_context,
    verify_google_id_token,
)
from app.db.session import get_db
from app.schemas.auth import (
    AuthenticatedUserResponse,
    GoogleLoginRequest,
    TokenResponse,
)

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