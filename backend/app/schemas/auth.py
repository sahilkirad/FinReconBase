from pydantic import BaseModel


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