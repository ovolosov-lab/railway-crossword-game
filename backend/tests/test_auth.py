import unittest
from http.cookies import SimpleCookie
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import jwt
from fastapi import HTTPException, Response
from pydantic import SecretStr
from sqlalchemy.exc import SQLAlchemyError

from app import auth
from app.config import Settings
from app.models import User
from app.security import is_valid_csrf_token
from app.schemas import UserLogin


class AuthTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.settings = Settings(
            jwt_secret=SecretStr("test-secret-for-auth-unit-tests-only-32-bytes"),
            app_env="production",
            access_token_expire_minutes=15,
        )
        self.password = "correct horse battery staple"
        self.user = User(
            id=uuid4(),
            username="railway-player",
            email="player@example.com",
            hashed_password=auth.password_context.hash(self.password),
            is_active=True,
        )
        self.session = AsyncMock()

    async def call_login(
        self,
        user: User | None,
        *,
        password: str | None = None,
        email: str = "PLAYER@example.com",
    ) -> tuple[Response, dict[str, str]]:
        result = MagicMock()
        result.scalar_one_or_none.return_value = user
        self.session.execute.return_value = result
        response = Response()
        credentials = UserLogin.model_validate(
            {"email": email, "password": password or self.password}
        )
        with patch("app.auth.get_settings", return_value=self.settings):
            body = await auth.login(credentials, response, self.session)
        return response, body

    async def test_success_sets_signed_http_only_cookie(self) -> None:
        response, body = await self.call_login(self.user)
        self.assertEqual(body, {"status": "authenticated"})
        self.assertNotIn("token", body)
        self.assertNotIn("csrf_token", body)

        cookies = SimpleCookie()
        for set_cookie in response.headers.getlist("set-cookie"):
            cookies.load(set_cookie)
        cookie = cookies[self.settings.auth_cookie_name]
        csrf_cookie = cookies[self.settings.csrf_cookie_name]
        self.assertTrue(cookie["httponly"])
        self.assertTrue(cookie["secure"])
        self.assertEqual(cookie["samesite"], "lax")
        self.assertEqual(cookie["path"], "/")
        self.assertEqual(cookie["max-age"], "900")
        self.assertFalse(csrf_cookie["httponly"])
        self.assertTrue(csrf_cookie["secure"])
        self.assertEqual(csrf_cookie["samesite"], "lax")
        self.assertEqual(csrf_cookie["max-age"], "900")
        self.assertTrue(
            is_valid_csrf_token(
                csrf_cookie.value,
                self.settings.jwt_secret.get_secret_value(),
            )
        )

        claims = jwt.decode(
            cookie.value,
            self.settings.jwt_secret.get_secret_value(),
            algorithms=[self.settings.jwt_algorithm],
        )
        self.assertEqual(claims["sub"], str(self.user.id))
        self.assertIn("iat", claims)
        self.assertIn("exp", claims)

    async def test_development_cookie_does_not_require_secure_flag(self) -> None:
        self.settings = Settings(
            jwt_secret=SecretStr("test-secret-for-auth-unit-tests-only-32-bytes"),
            app_env="development",
        )
        response, _ = await self.call_login(self.user)

        cookies = SimpleCookie()
        for set_cookie in response.headers.getlist("set-cookie"):
            cookies.load(set_cookie)
        self.assertTrue(cookies[self.settings.auth_cookie_name]["httponly"])
        self.assertFalse(cookies[self.settings.auth_cookie_name]["secure"])
        self.assertFalse(cookies[self.settings.csrf_cookie_name]["secure"])

    async def test_unknown_user_wrong_password_and_inactive_user_share_error(self) -> None:
        inactive_user = User(
            id=uuid4(),
            username="inactive",
            email="inactive@example.com",
            hashed_password=self.user.hashed_password,
            is_active=False,
        )
        cases = (
            (None, self.password),
            (self.user, "incorrect password"),
            (inactive_user, self.password),
        )
        errors: list[tuple[int, str]] = []

        for user, password in cases:
            result = MagicMock()
            result.scalar_one_or_none.return_value = user
            self.session.execute.return_value = result
            response = Response()
            credentials = UserLogin.model_validate(
                {"email": "player@example.com", "password": password}
            )
            with patch("app.auth.get_settings", return_value=self.settings):
                with self.assertRaises(HTTPException) as context:
                    await auth.login(credentials, response, self.session)
            errors.append((context.exception.status_code, context.exception.detail))
            self.assertNotIn("set-cookie", response.headers)

        self.assertEqual(errors, [(401, "Invalid email or password")] * 3)

    async def test_database_failure_returns_service_unavailable(self) -> None:
        self.session.execute.side_effect = SQLAlchemyError("database unavailable")
        response = Response()
        credentials = UserLogin.model_validate(
            {"email": "player@example.com", "password": self.password}
        )
        with patch("app.auth.get_settings", return_value=self.settings):
            with self.assertRaises(HTTPException) as context:
                await auth.login(credentials, response, self.session)

        self.assertEqual(context.exception.status_code, 503)
        self.assertNotIn("set-cookie", response.headers)

    async def test_auth_route_is_registered(self) -> None:
        from main import app as fastapi_app

        self.assertIn("/auth/", fastapi_app.openapi()["paths"])

    def test_weak_jwt_secret_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            Settings(jwt_secret=SecretStr("too-short"))


if __name__ == "__main__":
    unittest.main()
