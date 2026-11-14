import unittest
from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.admin_api import router
from app.api_errors import api_exception_handler, request_validation_exception_handler
from app.database import get_db
from app.models import User
from app.security import get_current_user


class _FakeSession:
    def __init__(self) -> None:
        self.inserted_count = 2
        self.statement = None
        self.commits = 0
        self.rollbacks = 0

    async def execute(self, statement):
        self.statement = statement
        return SimpleNamespace(rowcount=self.inserted_count)

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


class DictionaryAdminEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.application = FastAPI(
            exception_handlers={
                StarletteHTTPException: api_exception_handler,
                RequestValidationError: request_validation_exception_handler,
            }
        )
        self.application.include_router(router)
        self.session = _FakeSession()
        self.user = self.make_user("admin")
        self.application.dependency_overrides[get_current_user] = self.current_user
        self.application.dependency_overrides[get_db] = self.database_session
        self.client = TestClient(self.application)

    @staticmethod
    def make_user(username: str) -> User:
        return User(
            id=uuid4(),
            username=username,
            email=f"{username}@example.com",
            hashed_password="must-not-be-returned",
            is_active=True,
            score=0,
        )

    async def current_user(self) -> User:
        return self.user

    async def database_session(self):
        yield self.session

    def tearDown(self) -> None:
        self.client.close()
        self.application.dependency_overrides.clear()

    def test_admin_can_add_words(self) -> None:
        response = self.client.post(
            "/admin/dictionary/",
            json={
                "dictionary": [
                    {"word": " лес ", "description": " Лесное растение "},
                    {"word": "река", "description": "Водный поток"},
                ]
            },
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json(), {"added": 2})
        self.assertEqual(self.session.commits, 1)
        self.assertIn(
            "ON CONFLICT (language_code, word_text) DO NOTHING",
            str(self.session.statement.compile()),
        )
        values = self.session.statement.compile().params
        self.assertEqual(
            [
                (
                    values[f"word_text_m{index}"],
                    values[f"definition_m{index}"],
                    values[f"language_code_m{index}"],
                    values[f"letter_count_m{index}"],
                )
                for index in range(2)
            ],
            [
                ("лес", "Лесное растение", "ru", 3),
                ("река", "Водный поток", "ru", 4),
            ],
        )

    def test_non_admin_cannot_add_words(self) -> None:
        self.user = self.make_user("not-admin")

        response = self.client.post(
            "/admin/dictionary/",
            json={"dictionary": [{"word": "лес", "description": "Лесное растение"}]},
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "admin_required")
        self.assertEqual(self.session.commits, 0)
        self.assertIsNone(self.session.statement)

    def test_empty_words_are_rejected_before_database_write(self) -> None:
        response = self.client.post(
            "/admin/dictionary/",
            json={"dictionary": [{"word": "  ", "description": "Описание"}]},
        )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.session.commits, 0)
        self.assertIsNone(self.session.statement)

    def test_duplicate_words_are_skipped_and_not_counted_as_added(self) -> None:
        self.session.inserted_count = 1
        response = self.client.post(
            "/admin/dictionary/",
            json={
                "dictionary": [
                    {"word": "лес", "description": "Лесное растение"},
                    {"word": "река", "description": "Водный поток"},
                ]
            },
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json(), {"added": 1})
        self.assertEqual(self.session.commits, 1)

    def test_all_duplicate_words_return_zero_added(self) -> None:
        self.session.inserted_count = 0
        response = self.client.post(
            "/admin/dictionary/",
            json={"dictionary": [{"word": "лес", "description": "Лесное растение"}]},
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json(), {"added": 0})
        self.assertEqual(self.session.commits, 1)

    def test_dictionary_write_requires_authentication(self) -> None:
        self.application.dependency_overrides.pop(get_current_user)

        response = self.client.post(
            "/admin/dictionary/",
            json={"dictionary": [{"word": "лес", "description": "Лесное растение"}]},
        )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(
            response.json()["error"]["code"],
            "authentication_required",
        )
        self.assertEqual(self.session.commits, 0)


if __name__ == "__main__":
    unittest.main()
