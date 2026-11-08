from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import Annotated
from uuid import UUID

import jwt
from fastapi import Cookie, Depends, Header, status
from loguru import logger
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api_errors import ApiException
from app.config import get_settings
from app.database import get_db
from app.models import User


def create_csrf_token(secret_key: str | None = None) -> str:
    secret = (
        secret_key or get_settings().jwt_secret.get_secret_value()
    ).encode("utf-8")
    nonce = secrets.token_urlsafe(32)
    signature = hmac.new(secret, nonce.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{nonce}.{signature}"


def is_valid_csrf_token(token: str, secret_key: str | None = None) -> bool:
    nonce, separator, supplied_signature = token.rpartition(".")
    if not separator or not nonce or not supplied_signature:
        return False
    try:
        nonce_bytes = nonce.encode("ascii")
    except UnicodeEncodeError:
        return False
    secret = (
        secret_key or get_settings().jwt_secret.get_secret_value()
    ).encode("utf-8")
    expected_signature = hmac.new(
        secret,
        nonce_bytes,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(supplied_signature, expected_signature)


async def get_authenticated_user(
    session: Annotated[AsyncSession, Depends(get_db)],
    token: Annotated[
        str | None,
        Cookie(alias=get_settings().auth_cookie_name),
    ] = None,
) -> User | None:
    if token is None:
        return None

    settings = get_settings()
    try:
        claims = jwt.decode(
            token,
            settings.jwt_secret.get_secret_value(),
            algorithms=[settings.jwt_algorithm],
            options={"require": ["exp", "iat", "sub"]},
        )
        subject = claims.get("sub")
        if not isinstance(subject, str):
            return None
        user_id = UUID(subject)
    except (jwt.InvalidTokenError, ValueError):
        return None

    try:
        user = await session.scalar(select(User).where(User.id == user_id))
    except SQLAlchemyError as exc:
        logger.exception("Database error while validating an authenticated user")
        raise ApiException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            code="service_unavailable",
            message="Authentication service is temporarily unavailable.",
        ) from exc

    if user is None or not user.is_active:
        return None
    return user


async def get_current_user(
    user: Annotated[User | None, Depends(get_authenticated_user)],
) -> User:
    if user is None:
        raise ApiException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            code="authentication_required",
            message="A valid authentication cookie is required.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


async def require_csrf_token(
    csrf_cookie: Annotated[
        str | None,
        Cookie(alias=get_settings().csrf_cookie_name),
    ] = None,
    csrf_header: Annotated[
        str | None,
        Header(alias="X-CSRF-Token"),
    ] = None,
) -> None:
    if csrf_cookie is None or csrf_header is None:
        raise ApiException(
            status_code=status.HTTP_403_FORBIDDEN,
            code="csrf_validation_failed",
            message="A valid CSRF token is required.",
        )
    try:
        cookie_bytes = csrf_cookie.encode("ascii")
        header_bytes = csrf_header.encode("ascii")
    except UnicodeEncodeError:
        cookie_bytes = b""
        header_bytes = b""

    if not hmac.compare_digest(cookie_bytes, header_bytes) or not is_valid_csrf_token(
        csrf_header
    ):
        raise ApiException(
            status_code=status.HTTP_403_FORBIDDEN,
            code="csrf_validation_failed",
            message="A valid CSRF token is required.",
        )
