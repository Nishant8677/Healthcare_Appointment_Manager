"""Registration, login and identity endpoints."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_app_settings, get_current_user, get_session
from app.core.config import Settings
from app.core.exceptions import EmailAlreadyRegistered, InactiveAccount, InvalidCredentials
from app.core.security import create_access_token
from app.models.user import User
from app.schemas.auth import LoginRequest, RegisterRequest, TokenResponse, UserResponse
from app.services import auth_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post(
    "/register",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a patient account",
)
async def register(
    payload: RegisterRequest,
    session: AsyncSession = Depends(get_session),
) -> User:
    """Create a patient account.

    The role is fixed to `patient` here and is never read from the request body: allowing a
    caller to choose their own role would let anyone register as an admin. Doctor and admin
    accounts are created by an existing admin.
    """
    try:
        return await auth_service.register_patient(
            session,
            email=payload.email,
            password=payload.password.get_secret_value(),
            full_name=payload.full_name,
        )
    except EmailAlreadyRegistered:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with this email already exists.",
        ) from None


@router.post("/login", response_model=TokenResponse, summary="Exchange credentials for a token")
async def login(
    payload: LoginRequest,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_app_settings),
) -> TokenResponse:
    try:
        user = await auth_service.authenticate(
            session,
            email=payload.email,
            password=payload.password.get_secret_value(),
        )
    except InvalidCredentials:
        # One message for unknown address and wrong password alike, so the response cannot
        # be used to discover which people are patients of this clinic.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password.",
            headers={"WWW-Authenticate": "Bearer"},
        ) from None
    except InactiveAccount:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This account has been deactivated. Contact the clinic.",
        ) from None

    token = create_access_token(
        user_id=user.id,
        role=user.role,
        secret=settings.jwt_secret.get_secret_value(),
        algorithm=settings.jwt_algorithm,
        expires_in_minutes=settings.access_token_ttl_minutes,
    )
    logger.info("login succeeded", extra={"user_id": str(user.id), "role": user.role.value})

    return TokenResponse(
        access_token=token,
        expires_in=settings.access_token_ttl_minutes * 60,
    )


@router.get("/me", response_model=UserResponse, summary="The signed-in user")
async def read_current_user(user: User = Depends(get_current_user)) -> User:
    return user
