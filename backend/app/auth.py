from __future__ import annotations

from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Annotated
from uuid import UUID, uuid4

import jwt
from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.concurrency import run_in_threadpool
from loguru import logger
from passlib.context import CryptContext
from passlib.exc import UnknownHashError
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.models import User
from app.schemas import UserLogin, UserRead, UserRegister
from app.security import create_csrf_token


router = APIRouter(prefix="/auth", tags=["auth"])
password_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


@lru_cache(maxsize=1)
def _dummy_password_hash() -> str:
    return password_context.hash("invalid-user-password-placeholder")


def create_access_token(user_id: UUID) -> str:
    settings = get_settings()
    issued_at = datetime.now(timezone.utc)
    expires_at = issued_at + timedelta(minutes=settings.access_token_expire_minutes)
    return jwt.encode(
        {
            "sub": str(user_id),
            "iat": issued_at,
            "exp": expires_at,
        },
        settings.jwt_secret.get_secret_value(),
        algorithm=settings.jwt_algorithm,
    )


async def verify_password(password: str, hashed_password: str) -> bool:
    try:
        return await run_in_threadpool(
            password_context.verify,
            password,
            hashed_password,
        )
    except UnknownHashError as exc:
        logger.exception("Stored user password hash has an unsupported format")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Authentication service is unavailable",
        ) from exc


def _integrity_constraint_name(exc: IntegrityError) -> str | None:
    original = exc.orig
    constraint_name = getattr(original, "constraint_name", None)
    if constraint_name is not None:
        return str(constraint_name)

    diagnostic = getattr(original, "diag", None)
    constraint_name = getattr(diagnostic, "constraint_name", None)
    if constraint_name is not None:
        return str(constraint_name)

    cause = getattr(original, "__cause__", None)
    constraint_name = getattr(cause, "constraint_name", None)
    if constraint_name is not None:
        return str(constraint_name)
    diagnostic = getattr(cause, "diag", None)
    constraint_name = getattr(diagnostic, "constraint_name", None)
    return str(constraint_name) if constraint_name is not None else None


@router.post(
    "/register/",
    response_model=UserRead,
    status_code=status.HTTP_201_CREATED,
)
async def register(
    credentials: UserRegister,
    session: Annotated[AsyncSession, Depends(get_db)],
) -> UserRead:
    username = credentials.username
    email = str(credentials.email).lower()

    try:
        existing_user = await session.scalar(
            select(User).where(
                or_(
                    func.lower(User.username) == username.lower(),
                    func.lower(User.email) == email,
                )
            )
        )
    except SQLAlchemyError as exc:
        logger.exception("Database error while checking registration uniqueness")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Registration service is temporarily unavailable",
        ) from exc

    if existing_user is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Username or email is already registered",
        )

    password = credentials.password.get_secret_value()
    hashed_password = await run_in_threadpool(password_context.hash, password)
    now = datetime.now(timezone.utc)
    user = User(
        id=uuid4(),
        username=username,
        email=email,
        hashed_password=hashed_password,
        score=0,
        is_active=True,
        created_at=now,
        updated_at=now,
    )

    try:
        session.add(user)
        await session.flush()
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        constraint_name = _integrity_constraint_name(exc)
        if constraint_name in {"uq_users_username_lower", "uq_users_email_lower"}:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Username or email is already registered",
            ) from exc

        logger.exception("Database integrity error while registering a user")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Registration service is temporarily unavailable",
        ) from exc
    except SQLAlchemyError as exc:
        await session.rollback()
        logger.exception("Database error while registering a user")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Registration service is temporarily unavailable",
        ) from exc

    logger.info("User registered: {}", user.id)
    return UserRead.model_validate(user)


@router.post("/")
async def login(
    credentials: UserLogin,
    response: Response,
    session: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, str]:
    settings = get_settings()
    try:
        result = await session.execute(
            select(User).where(func.lower(User.email) == str(credentials.email).casefold())
        )
        user = result.scalar_one_or_none()
    except SQLAlchemyError as exc:
        logger.exception("Database error while authenticating a user")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication service is temporarily unavailable",
        ) from exc

    if user is None:
        dummy_hash = await run_in_threadpool(_dummy_password_hash)
        await verify_password(
            credentials.password.get_secret_value(),
            dummy_hash,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    password_is_valid = await verify_password(
        credentials.password.get_secret_value(),
        user.hashed_password,
    )
    if not user.is_active or not password_is_valid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    response.set_cookie(
        key=settings.auth_cookie_name,
        value=create_access_token(user.id),
        max_age=settings.access_token_expire_minutes * 60,
        httponly=True,
        secure=settings.app_env.casefold() != "development",
        samesite=settings.auth_cookie_samesite,
        path="/",
    )
    response.set_cookie(
        key=settings.csrf_cookie_name,
        value=create_csrf_token(settings.jwt_secret.get_secret_value()),
        max_age=settings.access_token_expire_minutes * 60,
        httponly=False,
        secure=settings.app_env.casefold() != "development",
        samesite=settings.auth_cookie_samesite,
        path="/",
    )
    logger.info("User authenticated: {}", user.id)
    return {"status": "authenticated"}
