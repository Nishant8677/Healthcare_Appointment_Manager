"""Request and response contracts for authentication."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, SecretStr, field_validator

from app.models.enums import UserRole

# Long enough to resist guessing, capped so an enormous body cannot be used to burn CPU in
# the hashing function.
PASSWORD_MIN_LENGTH = 10
PASSWORD_MAX_LENGTH = 128


class RegisterRequest(BaseModel):
    """Self-registration. Always creates a patient — see the route for why."""

    email: EmailStr
    password: SecretStr = Field(
        min_length=PASSWORD_MIN_LENGTH,
        max_length=PASSWORD_MAX_LENGTH,
        description=f"At least {PASSWORD_MIN_LENGTH} characters.",
    )
    full_name: str = Field(min_length=1, max_length=200)

    @field_validator("full_name")
    @classmethod
    def _strip_name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("full_name cannot be blank")
        return stripped


class LoginRequest(BaseModel):
    email: EmailStr
    password: SecretStr


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"  # noqa: S105 - the OAuth scheme name, not a credential
    expires_in: int = Field(description="Token lifetime in seconds.")


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: EmailStr
    full_name: str
    role: UserRole
    is_active: bool
    created_at: datetime
