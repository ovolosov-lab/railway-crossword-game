import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api_errors import api_exception_handler, request_validation_exception_handler
from app.config import Settings
from app.database import get_db
from app.game_api import router
from app.game_state import ActiveGame, WordPlacement
from app.models import User
from app.security import get_authenticated_user, get_current_user


class _FakeSession:
    def __init__(self) -> None:
        self.fail_queries = False
        self.leaderboard = [("winner", 42)]
        self.catalog_results = [
            [
                SimpleNamespace(
                    id=1,
                    name="London",
                    country_code="GB",
                    top=Decimal("1405.00"),
                    left=Decimal("779.00"),
                    is_capital=True,
                )
            ],
            [
                SimpleNamespace(
                    id=1,
                    source_city_id=1,
                    target_city_id=2,
                    length_in_letters=5,
                )
            ],
        ]
        self.catalog_index = 0
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        if self.fail_queries:
            raise SQLAlchemyError("database unavailable")
        return SimpleNamespace(all=lambda: self.leaderboard)

    async def scalars(self, statement):
        self.statements.append(statement)
        if self.fail_queries:
            raise SQLAlchemyError("database unavailable")
        result = self.catalog_results[self.catalog_index % len(self.catalog_results)]
        self.catalog_index += 1
        return SimpleNamespace(all=lambda: result)


class GameReadEndpointTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.user_id = uuid4()
        self.user = User(
            id=self.user_id,
            username="current-user",
            email="current@example.com",
            hashed_password="must-not-be-returned",
            is_active=True,
            score=0,
        )
        self.started_at = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)
        self.settings = Settings(
            jwt_secret=SecretStr("test-secret-for-game-read-endpoints-32-bytes"),
            game_duration_hours=1,
        )
        self.game = ActiveGame.create(
            settings=self.settings,
            started_at=self.started_at,
        )
        self.session = _FakeSession()
        self.application = FastAPI(
            exception_handlers={
                StarletteHTTPException: api_exception_handler,
                RequestValidationError: request_validation_exception_handler,
            }
        )
        self.application.state.activeGame = self.game
        self.application.state.game_lock = asyncio.Lock()
        self.application.include_router(router)
        self.application.dependency_overrides[get_current_user] = self.current_user
        self.application.dependency_overrides[get_db] = self.database_session
        self.client = TestClient(self.application)

    async def current_user(self) -> User:
        return self.user

    async def database_session(self):
        yield self.session

    def tearDown(self) -> None:
        self.client.close()
        self.application.dependency_overrides.clear()

    async def test_stats_returns_round_version_count_and_public_winner_ranking(self) -> None:
        await self.game.add_player(
            self.user_id,
            1,
            2,
            now=self.started_at,
        )
        before = self.game.model_dump(mode="json")

        response = self.client.get("/game/stats/")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["round"]["id"], str(self.game.game_id))
        self.assertEqual(body["round"]["status"], "active")
        self.assertEqual(body["round"]["state_version"], self.game.state_version)
        self.assertEqual(body["player"]["state"], "selecting_route")
        self.assertEqual(body["participant_count"], 1)
        self.assertEqual(body["leaderboard"], [{"username": "winner", "score": 42}])
        self.assertNotIn("email", response.text)
        self.assertNotIn("hashed_password", response.text)
        self.assertNotIn("must-not-be-returned", response.text)
        self.assertIn("game_results", str(self.session.statements[0]))
        self.assertEqual(self.game.model_dump(mode="json"), before)

    async def test_routes_returns_map_and_each_personal_state_without_joining(self) -> None:
        response = self.client.get("/game/routes/")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["player"]["state"], "watching_stats")
        self.assertIsNone(body["player"]["source_city_id"])
        self.assertEqual(body["cities"][0]["id"], 1)
        self.assertEqual(body["routes"][0]["id"], 1)
        self.assertEqual(self.game.players, {})
        self.assertEqual(body["round"]["state_version"], 0)

        await self.game.add_player(self.user_id, 1, 2, now=self.started_at)
        await self.game.add_shared_words(
            1,
            [
                WordPlacement(
                    dictionary_word_id=10,
                    route_id=1,
                    position_index=0,
                    letter_count=5,
                )
            ],
            now=self.started_at,
        )
        await self.game.set_player_route(
            self.user_id,
            [1],
            {1: (1, 2)},
            now=self.started_at,
        )
        response = self.client.get("/game/routes/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["player"]["state"], "playing")
        self.assertEqual(response.json()["player"]["route_ids"], [1])

    async def test_finished_round_projects_won_and_lost_states(self) -> None:
        await self.game.add_player(self.user_id, 1, 2, now=self.started_at)
        self.game._finish(
            finished_at=self.started_at + timedelta(minutes=1),
            reason="winner",
            winner_user_id=self.user_id,
        )

        won_response = self.client.get("/game/stats/")
        self.assertEqual(won_response.json()["player"]["state"], "won")
        self.assertEqual(won_response.json()["round"]["status"], "finished")
        self.assertEqual(won_response.json()["round"]["finish_reason"], "winner")

        self.game.winner_user_id = uuid4()
        lost_response = self.client.get("/game/routes/")
        self.assertEqual(lost_response.json()["player"]["state"], "lost")

    async def test_missing_round_is_reported_as_being_created(self) -> None:
        self.application.state.activeGame = None

        response = self.client.get("/game/stats/")

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["round"]["id"])
        self.assertEqual(response.json()["round"]["status"], "being_created")
        self.assertEqual(response.json()["round"]["state_version"], 0)
        self.assertEqual(response.json()["player"]["state"], "watching_stats")

    async def test_database_failures_return_service_unavailable(self) -> None:
        self.session.fail_queries = True

        stats_response = self.client.get("/game/stats/")
        routes_response = self.client.get("/game/routes/")

        self.assertEqual(stats_response.status_code, 503)
        self.assertEqual(
            stats_response.json()["error"]["code"],
            "service_unavailable",
        )
        self.assertEqual(routes_response.status_code, 503)
        self.assertEqual(
            routes_response.json()["error"]["code"],
            "service_unavailable",
        )

    async def test_unauthenticated_stats_request_is_rejected(self) -> None:
        async def no_user():
            return None

        self.application.dependency_overrides.pop(get_current_user)
        self.application.dependency_overrides[get_authenticated_user] = no_user

        response = self.client.get("/game/stats/")

        self.assertEqual(response.status_code, 401)
        self.assertEqual(
            response.json()["error"]["code"],
            "authentication_required",
        )


if __name__ == "__main__":
    unittest.main()
