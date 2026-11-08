import unittest
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.auth import password_context
from app.database import get_db
from app.models import User
from main import app


class UniqueConstraintError(Exception):
    def __init__(self, constraint_name: str) -> None:
        super().__init__("duplicate value")
        self.constraint_name = constraint_name


class RegistrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.session = MagicMock()
        self.session.scalar = AsyncMock(return_value=None)
        self.session.flush = AsyncMock()
        self.session.commit = AsyncMock()
        self.session.rollback = AsyncMock()
        self.session.add = MagicMock()

        async def override_get_db():
            yield self.session

        self.override_get_db = override_get_db
        app.dependency_overrides[get_db] = self.override_get_db

    def tearDown(self) -> None:
        app.dependency_overrides.pop(get_db, None)

    def register(self, payload: dict[str, str]):
        with TestClient(app) as client:
            return client.post("/auth/register/", json=payload)

    def valid_payload(self) -> dict[str, str]:
        return {
            "username": "  railway-player  ",
            "email": "PLAYER@example.com",
            "password": "correct horse battery staple",
        }

    def test_registration_creates_user_and_returns_public_data(self) -> None:
        response = self.register(self.valid_payload())

        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body["username"], "railway-player")
        self.assertEqual(body["email"], "player@example.com")
        self.assertEqual(body["score"], 0)
        self.assertTrue(body["is_active"])
        self.assertNotIn("password", body)
        self.assertNotIn("hashed_password", body)
        self.assertNotIn("set-cookie", response.headers)

        user = self.session.add.call_args.args[0]
        self.assertIsInstance(user, User)
        self.assertTrue(
            password_context.verify(
                "correct horse battery staple",
                user.hashed_password,
            )
        )
        self.assertNotEqual(user.hashed_password, "correct horse battery staple")
        self.session.flush.assert_awaited_once()
        self.session.commit.assert_awaited_once()
        self.session.rollback.assert_not_awaited()

    def test_registration_rejects_existing_username_or_email(self) -> None:
        self.session.scalar.return_value = User(
            id=uuid4(),
            username="RAILWAY-PLAYER",
            email="someone@example.com",
            hashed_password="hash",
        )

        response = self.register(self.valid_payload())

        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json()["error"]["message"],
            "Username or email is already registered",
        )
        self.assertEqual(response.json()["error"]["code"], "conflict")
        self.session.add.assert_not_called()
        self.session.commit.assert_not_awaited()

    def test_registration_rejects_invalid_input_before_database_access(self) -> None:
        payload = self.valid_payload()
        payload["password"] = "short"

        response = self.register(payload)

        self.assertEqual(response.status_code, 422)
        self.session.scalar.assert_not_awaited()
        self.session.add.assert_not_called()

    def test_registration_maps_unique_constraint_race_to_conflict(self) -> None:
        self.session.flush.side_effect = IntegrityError(
            "insert",
            {},
            UniqueConstraintError("uq_users_username_lower"),
        )

        response = self.register(self.valid_payload())

        self.assertEqual(response.status_code, 409)
        self.session.rollback.assert_awaited_once()
        self.session.commit.assert_not_awaited()

    def test_registration_reports_database_unavailability(self) -> None:
        self.session.scalar.side_effect = SQLAlchemyError("database unavailable")

        response = self.register(self.valid_payload())

        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json()["error"]["message"],
            "Registration service is temporarily unavailable",
        )
        self.assertEqual(response.json()["error"]["code"], "service_unavailable")
        self.session.add.assert_not_called()

    def test_registration_route_is_registered(self) -> None:
        self.assertIn("/auth/register/", app.openapi()["paths"])


if __name__ == "__main__":
    unittest.main()
