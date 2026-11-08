import asyncio
import unittest
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from pydantic import SecretStr
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api_errors import api_exception_handler, request_validation_exception_handler
from app.config import Settings
from app.database import get_db
from app.game_api import router, write_router
from app.game_state import ActiveGame, PrevGame, WordPlacement
from app.models import GameResult, User
from app.security import get_current_user, require_csrf_token


class _FakeSession:
    """Provide deterministic dictionary and winner-persistence behavior."""

    def __init__(self) -> None:
        """Create empty persistence state and the game catalogs used by tests."""
        self.fail_queries = False
        self.fail_winner_persistence = False
        self.scores = {}
        self.game_results = {}
        self.pending_results = []
        self.city_ids = [1, 2, 3]
        self.routes = [
            SimpleNamespace(
                id=10,
                source_city_id=1,
                target_city_id=2,
                length_in_letters=5,
            ),
            SimpleNamespace(
                id=20,
                source_city_id=2,
                target_city_id=3,
                length_in_letters=5,
            ),
            SimpleNamespace(
                id=30,
                source_city_id=1,
                target_city_id=3,
                length_in_letters=5,
            ),
            SimpleNamespace(
                id=40,
                source_city_id=1,
                target_city_id=2,
                length_in_letters=1,
            ),
        ]
        self.words = [
            SimpleNamespace(
                id=101,
                word_text="secret_one",
                definition="First definition",
                letter_count=2,
                is_active=True,
            ),
            SimpleNamespace(
                id=102,
                word_text="secret_two",
                definition="Second definition",
                letter_count=2,
                is_active=True,
            ),
            SimpleNamespace(
                id=103,
                word_text="secret_three",
                definition="Third definition",
                letter_count=3,
                is_active=True,
            ),
            SimpleNamespace(
                id=106,
                word_text="secret_four",
                definition="Fourth definition",
                letter_count=3,
                is_active=True,
            ),
        ]

    async def scalars(self, statement):
        if self.fail_queries:
            from sqlalchemy.exc import SQLAlchemyError

            raise SQLAlchemyError("database unavailable")
        query = str(statement).lower()
        if "from cities" in query:
            values = self.city_ids
        elif "from routes" in query:
            values = self.routes
        elif "from dictionary_words" in query:
            values = self.words
        else:
            raise AssertionError(f"Unexpected query: {statement}")
        return SimpleNamespace(all=lambda: values)

    async def scalar(self, statement):
        """Resolve the single-row queries issued by guess and hint endpoints."""
        if self.fail_queries:
            from sqlalchemy.exc import SQLAlchemyError

            raise SQLAlchemyError("database unavailable")
        query = str(statement).lower()
        identifier = next(iter(statement.compile().params.values()))
        if "from dictionary_words" in query:
            return next((word for word in self.words if word.id == identifier), None)
        if "from game_results" in query:
            return self.game_results.get(identifier)
        raise AssertionError(f"Unexpected scalar query: {statement}")

    async def rollback(self) -> None:
        """Match the async session rollback interface."""
        return None

    def add(self, value) -> None:
        """Stage a terminal game result for the surrounding fake transaction."""
        if not isinstance(value, GameResult):
            raise AssertionError(f"Unexpected ORM object: {value!r}")
        self.pending_results.append(value)

    async def flush(self) -> None:
        """Simulate inserting staged results and injectable database failures."""
        if self.fail_winner_persistence:
            from sqlalchemy.exc import SQLAlchemyError

            raise SQLAlchemyError("winner persistence failed")
        for result in self.pending_results:
            self.game_results[result.game_id] = result

    async def execute(self, statement):
        """Simulate the atomic permanent-score update."""
        if self.fail_winner_persistence:
            from sqlalchemy.exc import SQLAlchemyError

            raise SQLAlchemyError("winner persistence failed")
        compiled = statement.compile()
        params = list(compiled.params.values())
        user_id = next(value for value in params if hasattr(value, "hex"))
        score_delta = next(value for value in params if isinstance(value, int))
        if user_id not in self.scores:
            updated_id = None
        else:
            self.scores[user_id] += score_delta
            updated_id = user_id
        return SimpleNamespace(scalar_one_or_none=lambda: updated_id)

    @asynccontextmanager
    async def begin(self):
        """Restore fake persistence state when a transaction raises."""
        original_scores = self.scores.copy()
        original_results = self.game_results.copy()
        try:
            yield
        except BaseException:
            self.scores = original_scores
            self.game_results = original_results
            self.pending_results.clear()
            raise


class GameWriteEndpointTests(unittest.IsolatedAsyncioTestCase):
    """Exercise CSRF-protected game writes through FastAPI's public contract."""

    def setUp(self) -> None:
        """Create an isolated app, active round, and overridable dependencies."""
        self.settings = Settings(
            jwt_secret=SecretStr("test-secret-for-game-write-endpoints-32-bytes"),
            game_duration_hours=1,
        )
        self.game = ActiveGame.create(settings=self.settings)
        self.application = FastAPI(
            exception_handlers={
                StarletteHTTPException: api_exception_handler,
                RequestValidationError: request_validation_exception_handler,
            }
        )
        self.application.state.activeGame = self.game
        self.application.state.game_lock = asyncio.Lock()
        self.application.state.game_settings = self.settings
        self.application.state.prevGame = PrevGame()
        self.application.state.guess_rate_limits = {}
        self.application.include_router(router)
        self.application.include_router(write_router)
        self.session = _FakeSession()
        self.user = self.make_user()
        self.application.dependency_overrides[get_current_user] = self.current_user
        self.application.dependency_overrides[require_csrf_token] = self.allow_csrf
        self.application.dependency_overrides[get_db] = self.database_session
        self.client = TestClient(self.application)

    @staticmethod
    def make_user() -> User:
        return User(
            id=uuid4(),
            username="game-player",
            email="game-player@example.com",
            hashed_password="must-not-be-returned",
            is_active=True,
            score=12,
        )

    async def current_user(self) -> User:
        self.session.scores.setdefault(self.user.id, self.user.score)
        return self.user

    async def allow_csrf(self) -> None:
        return None

    async def database_session(self):
        yield self.session

    def tearDown(self) -> None:
        self.client.close()
        self.application.dependency_overrides.clear()

    def join_as(self, user: User | None = None):
        if user is not None:
            self.user = user
        return self.client.post("/game/join/")

    def confirm_route(self, route_ids: list[int], round_id=None):
        return self.client.post(
            "/game/route/",
            json={
                "round_id": str(round_id or self.game.game_id),
                "route_ids": route_ids,
            },
        )

    async def prepare_playing_player(self):
        """Create a player with one shared crossword placement."""
        await self.game.add_player(
            self.user.id,
            1,
            2,
            starting_score=self.user.score,
        )
        await self.game.add_shared_words(
            10,
            [
                WordPlacement(
                    dictionary_word_id=101,
                    route_id=10,
                    position_index=0,
                    letter_count=10,
                )
            ],
        )
        await self.game.set_player_route(self.user.id, [10], {10: (1, 2)})
        return self.game.shared_words[10][0]

    async def test_join_assigns_unique_unordered_pairs_and_is_idempotent(self) -> None:
        assigned_pairs = set()
        joined_users = []
        for _ in range(3):
            user = self.make_user()
            response = self.join_as(user)
            self.assertEqual(response.status_code, 200)
            body = response.json()
            self.assertEqual(body["player"]["state"], "selecting_route")
            joined_users.append((user, body["player"]["source_city_id"], body["player"]["target_city_id"]))
            assigned_pairs.add(
                frozenset(
                    (
                        body["player"]["source_city_id"],
                        body["player"]["target_city_id"],
                    )
                )
            )
        self.assertEqual(len(assigned_pairs), 3)

        self.user = self.make_user()
        exhausted_response = self.client.post("/game/join/")
        self.assertEqual(exhausted_response.status_code, 409)
        self.assertEqual(
            exhausted_response.json()["error"]["code"],
            "city_pairs_exhausted",
        )
        self.assertEqual(
            exhausted_response.json()["player"]["state"],
            "watching_stats",
        )

        existing_user, original_start, original_end = joined_users[0]
        self.user = existing_user
        retry = self.client.post("/game/join/")
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(retry.json()["player"]["source_city_id"], original_start)
        self.assertEqual(retry.json()["player"]["target_city_id"], original_end)

    async def test_join_requires_csrf_and_respects_last_minute_window(self) -> None:
        self.application.dependency_overrides.pop(require_csrf_token)
        no_csrf = self.client.post("/game/join/")
        self.assertEqual(no_csrf.status_code, 403)
        self.assertEqual(no_csrf.json()["error"]["code"], "csrf_validation_failed")

        self.application.dependency_overrides[require_csrf_token] = self.allow_csrf
        self.game.expires_at = datetime.now(timezone.utc) + timedelta(seconds=60)
        response = self.client.post("/game/join/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["round"]["status"], "being_created")
        self.assertEqual(response.json()["player"]["state"], "watching_stats")
        self.assertEqual(self.game.players, {})

    async def test_route_confirmation_generates_public_idempotent_crossword(self) -> None:
        with patch(
            "app.game_api.secrets.choice",
            return_value=(1, 2),
        ), patch(
            "app.game_api.secrets.randbelow",
            return_value=0,
        ):
            joined = self.join_as()
        self.assertEqual(joined.status_code, 200)

        with patch(
            "app.game_api.secrets.choice",
            side_effect=lambda choices: choices[0],
        ):
            response = self.confirm_route([10])
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["player"]["state"], "playing")
        self.assertEqual(body["player"]["route_ids"], [10])
        self.assertEqual(len(body["crossword"]), 1)
        self.assertEqual(
            [placement["position_index"] for placement in body["crossword"]],
            [0],
        )
        self.assertGreaterEqual(body["crossword"][0]["letter_count"], 3)
        self.assertNotIn("secret_one", response.text)
        self.assertNotIn("word_text", response.text)
        placement_ids = [item["placement_id"] for item in body["crossword"]]
        state_version = body["state_version"]

        retry = self.confirm_route([10])
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(
            [item["placement_id"] for item in retry.json()["crossword"]],
            placement_ids,
        )
        self.assertEqual(retry.json()["state_version"], state_version)

        replaced = self.confirm_route([30])
        self.assertEqual(replaced.status_code, 409)
        self.assertEqual(
            replaced.json()["error"]["code"],
            "route_already_confirmed",
        )

    async def test_route_rejects_disconnected_segments_without_mutating_game(self) -> None:
        with patch(
            "app.game_api.secrets.choice",
            return_value=(1, 2),
        ), patch(
            "app.game_api.secrets.randbelow",
            return_value=0,
        ):
            self.join_as()

        response = self.confirm_route([20])

        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json()["error"]["code"],
            "route_not_connected",
        )
        self.assertEqual(self.game.players[self.user.id].chosen_route_ids, [])
        self.assertEqual(self.game.shared_words, {})

    async def test_shared_segments_reuse_the_same_word_placements(self) -> None:
        with patch(
            "app.game_api.secrets.choice",
            return_value=(1, 2),
        ), patch(
            "app.game_api.secrets.randbelow",
            return_value=0,
        ):
            self.join_as()
        first_response = self.confirm_route([10])
        self.assertEqual(first_response.status_code, 200)
        shared_ids = [
            item["placement_id"] for item in first_response.json()["crossword"]
        ]
        shared_placement = self.game.shared_words[10][0]
        shared_placement.guessed_by_user_id = self.user.id
        shared_placement.guessed_at = datetime.now(timezone.utc)
        expected_revealed_word = next(
            word.word_text
            for word in self.session.words
            if word.id == shared_placement.dictionary_word_id
        )

        second_user = self.make_user()
        await self.game.add_player(second_user.id, 1, 3, starting_score=0)
        self.user = second_user
        second_response = self.confirm_route([10, 20])

        self.assertEqual(second_response.status_code, 200)
        second_shared_ids = [
            item["placement_id"]
            for item in second_response.json()["crossword"]
            if item["route_id"] == 10
        ]
        self.assertEqual(second_shared_ids, shared_ids)
        solved_shared = next(
            item
            for item in second_response.json()["crossword"]
            if item["route_id"] == 10
        )
        self.assertTrue(solved_shared["is_guessed"])
        self.assertEqual(solved_shared["revealed_word"], expected_revealed_word)
        self.assertEqual(
            self.game.players[second_user.id].chosen_route_ids,
            [10, 20],
        )

    async def test_partial_empty_segments_are_allowed_but_empty_crossword_cleans_up(self) -> None:
        self.session.words = [
            SimpleNamespace(
                id=501,
                word_text="three",
                definition="A valid clue",
                letter_count=3,
                is_active=True,
            )
        ]
        with patch(
            "app.game_api.secrets.choice",
            return_value=(1, 3),
        ), patch(
            "app.game_api.secrets.randbelow",
            return_value=0,
        ):
            self.join_as()

        partial_response = self.confirm_route([40, 20])
        self.assertEqual(partial_response.status_code, 200)
        self.assertEqual(self.game.shared_words[40], [])
        self.assertTrue(self.game.shared_words[20])

        placement = self.game.shared_words[20][0]
        guess_result = await self.game.guess_word(
            self.user.id,
            20,
            placement.dictionary_word_id,
            "three",
            "three",
        )
        self.assertTrue(guess_result.game_finished)
        self.assertEqual(self.game.finish_reason, "winner")

        empty_game = ActiveGame.create(settings=self.settings)
        self.application.state.activeGame = empty_game
        self.game = empty_game
        self.session.words = []
        with patch(
            "app.game_api.secrets.choice",
            return_value=(1, 2),
        ), patch(
            "app.game_api.secrets.randbelow",
            return_value=0,
        ):
            self.join_as()
        failed_response = self.confirm_route([10])

        self.assertEqual(failed_response.status_code, 503)
        self.assertEqual(
            failed_response.json()["error"]["code"],
            "crossword_generation_failed",
        )
        self.assertEqual(failed_response.json()["player"]["state"], "watching_stats")
        self.assertEqual(empty_game.players, {})
        self.assertEqual(empty_game.shared_words, {})

        with patch(
            "app.game_api.secrets.choice",
            return_value=(1, 2),
        ), patch(
            "app.game_api.secrets.randbelow",
            return_value=0,
        ):
            rejoined = self.client.post("/game/join/")
        self.assertEqual(rejoined.status_code, 200)
        self.assertEqual(rejoined.json()["player"]["source_city_id"], 1)
        self.assertEqual(rejoined.json()["player"]["target_city_id"], 2)

    async def test_each_route_with_capacity_for_three_gets_a_three_character_word(self) -> None:
        self.session.words.extend(
            [
                SimpleNamespace(
                    id=104,
                    word_text="four",
                    definition="Fourth definition",
                    letter_count=4,
                    is_active=True,
                ),
                SimpleNamespace(
                    id=105,
                    word_text="five",
                    definition="Fifth definition",
                    letter_count=4,
                    is_active=True,
                ),
            ]
        )
        with patch(
            "app.game_api.secrets.choice",
            return_value=(1, 3),
        ), patch(
            "app.game_api.secrets.randbelow",
            return_value=0,
        ):
            self.join_as()
        with patch(
            "app.game_api.secrets.choice",
            side_effect=lambda choices: choices[0],
        ):
            response = self.confirm_route([10, 20])

        self.assertEqual(response.status_code, 200)
        for route_id in (10, 20):
            route_words = [
                placement
                for placement in response.json()["crossword"]
                if placement["route_id"] == route_id
            ]
            self.assertTrue(route_words)
            self.assertGreaterEqual(route_words[0]["letter_count"], 3)
            self.assertLessEqual(
                route_words[0]["position_index"] + route_words[0]["letter_count"],
                3,
            )

    async def test_route_with_capacity_for_three_fails_without_a_long_dictionary_word(self) -> None:
        self.session.words = [
            SimpleNamespace(
                id=501,
                word_text="two",
                definition="Only a short word",
                letter_count=2,
                is_active=True,
            )
        ]
        with patch(
            "app.game_api.secrets.choice",
            return_value=(1, 2),
        ), patch(
            "app.game_api.secrets.randbelow",
            return_value=0,
        ):
            self.join_as()

        response = self.confirm_route([10])

        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json()["error"]["code"],
            "crossword_generation_failed",
        )
        self.assertEqual(response.json()["player"]["state"], "selecting_route")
        self.assertEqual(self.game.shared_words, {})
        self.assertEqual(self.game.players[self.user.id].chosen_route_ids, [])

    async def test_route_requires_current_round_and_joined_player(self) -> None:
        wrong_round = self.confirm_route([10], round_id=uuid4())
        self.assertEqual(wrong_round.status_code, 409)
        self.assertEqual(
            wrong_round.json()["error"]["code"],
            "round_id_mismatch",
        )

        not_joined = self.confirm_route([10])
        self.assertEqual(not_joined.status_code, 409)
        self.assertEqual(not_joined.json()["error"]["code"], "player_not_joined")

    async def test_correct_guess_persists_winner_and_starts_next_round(self) -> None:
        """A normalized correct answer commits one result and advances the round."""
        placement = await self.prepare_playing_player()

        response = self.client.post(
            "/game/guess/",
            json={
                "round_id": str(self.game.game_id),
                "placement_id": str(placement.placement_id),
                "answer": " SECRET_ONE ",
            },
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["round"]["id"], str(self.game.game_id))
        self.assertEqual(body["round"]["status"], "finished")
        self.assertEqual(body["player"]["state"], "won")
        self.assertEqual(body["guess"]["result"], "correct")
        self.assertEqual(body["guess"]["points_awarded"], 90)
        self.assertEqual(body["guess"]["revealed_word"], "secret_one")
        self.assertEqual(self.session.scores[self.user.id], self.user.score + 90)
        self.assertEqual(len(self.session.game_results), 1)
        self.assertEqual(self.session.game_results[self.game.game_id].score_awarded, 90)
        self.assertNotEqual(self.application.state.activeGame.game_id, self.game.game_id)
        self.assertEqual(self.application.state.prevGame.round_id, self.game.game_id)

        retry = self.client.post(
            "/game/guess/",
            json={
                "round_id": str(self.game.game_id),
                "placement_id": str(placement.placement_id),
                "answer": "secret_one",
            },
        )
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(retry.json()["player"]["state"], "won")
        self.assertNotIn("guess", retry.json())
        self.assertNotIn("state_version", retry.json())
        self.assertNotIn("state_version", retry.json()["round"])
        self.assertNotIn("round_score", retry.json()["player"])
        self.assertEqual(self.session.scores[self.user.id], self.user.score + 90)
        self.assertEqual(len(self.session.game_results), 1)

    async def test_winner_database_failure_does_not_mutate_round(self) -> None:
        """A failed terminal transaction leaves the original game snapshot intact."""
        placement = await self.prepare_playing_player()
        self.session.fail_winner_persistence = True
        state_version = self.game.state_version

        response = self.client.post(
            "/game/guess/",
            json={
                "round_id": str(self.game.game_id),
                "placement_id": str(placement.placement_id),
                "answer": "secret_one",
            },
        )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json()["error"]["code"],
            "winner_persistence_failed",
        )
        self.assertEqual(self.game.status, "active")
        self.assertFalse(placement.is_guessed)
        self.assertEqual(self.game.state_version, state_version)
        self.assertEqual(self.session.game_results, {})

    async def test_already_solved_word_can_complete_and_persist_player_victory(self) -> None:
        """A route completed by shared progress can still establish its winner."""
        placement = await self.prepare_playing_player()
        placement.guessed_by_user_id = uuid4()
        placement.guessed_at = datetime.now(timezone.utc)

        response = self.client.post(
            "/game/guess/",
            json={
                "round_id": str(self.game.game_id),
                "placement_id": str(placement.placement_id),
                "answer": "not-the-answer",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["guess"]["result"], "already_solved")
        self.assertEqual(response.json()["guess"]["points_awarded"], 0)
        self.assertEqual(response.json()["player"]["state"], "won")
        self.assertEqual(self.session.game_results[self.game.game_id].score_awarded, 0)
        self.assertEqual(self.session.scores[self.user.id], self.user.score)

    async def test_incorrect_guesses_are_rate_limited_with_retry_after(self) -> None:
        """Incorrect attempts count toward the configured rolling minute limit."""
        placement = await self.prepare_playing_player()
        self.application.state.game_settings.user_guess_rate_limit = 1
        request_body = {
            "round_id": str(self.game.game_id),
            "placement_id": str(placement.placement_id),
            "answer": "incorrect",
        }

        first = self.client.post("/game/guess/", json=request_body)
        limited = self.client.post("/game/guess/", json=request_body)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["guess"]["result"], "incorrect")
        self.assertEqual(limited.status_code, 429)
        self.assertEqual(limited.json()["error"]["code"], "rate_limited")
        self.assertGreaterEqual(int(limited.headers["Retry-After"]), 1)

    async def test_hint_repeats_are_free_and_solved_words_return_unknown(self) -> None:
        """Repeated hints are free and solved placements disclose no hint value."""
        self.user.score = 50
        self.session.scores[self.user.id] = 50
        placement = await self.prepare_playing_player()
        body = {
            "round_id": str(self.game.game_id),
            "placement_id": str(placement.placement_id),
            "hint_type": "first_letter",
        }

        first = self.client.post("/game/hint/", json=body)
        repeated = self.client.post("/game/hint/", json=body)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["hint"]["value"], "s")
        self.assertEqual(first.json()["hint"]["points_charged"], 20)
        self.assertEqual(first.json()["player"]["round_score"], -20)
        self.assertEqual(repeated.status_code, 200)
        self.assertTrue(repeated.json()["hint"]["already_used"])
        self.assertEqual(repeated.json()["hint"]["points_charged"], 0)

        placement.guessed_by_user_id = uuid4()
        placement.guessed_at = datetime.now(timezone.utc)
        solved = self.client.post(
            "/game/hint/",
            json={**body, "hint_type": "last_letter"},
        )
        self.assertEqual(solved.status_code, 200)
        self.assertEqual(solved.json()["hint"]["hint_type"], "unknown")
        self.assertEqual(solved.json()["hint"]["value"], "")
        self.assertEqual(solved.json()["hint"]["points_charged"], 0)

    async def test_sync_returns_shared_word_state_without_mutating_the_round(self) -> None:
        """Return a full safe snapshot even when the client version is current."""
        placement = await self.prepare_playing_player()
        original_version = self.game.state_version
        url = f"/game/sync/?round_id={self.game.game_id}&after_version={original_version}"

        unsolved = self.client.get(url)

        self.assertEqual(unsolved.status_code, 200)
        self.assertEqual(unsolved.json()["round"]["id"], str(self.game.game_id))
        self.assertEqual(unsolved.json()["round"]["status"], "active")
        self.assertEqual(unsolved.json()["player"]["state"], "playing")
        self.assertEqual(unsolved.json()["crossword"][0]["placement_id"], str(placement.placement_id))
        self.assertFalse(unsolved.json()["crossword"][0]["is_guessed"])
        self.assertIsNone(unsolved.json()["crossword"][0]["revealed_word"])
        self.assertEqual(self.game.state_version, original_version)

        placement.guessed_by_user_id = uuid4()
        placement.guessed_at = datetime.now(timezone.utc)
        solved = self.client.get(url)

        self.assertEqual(solved.status_code, 200)
        self.assertTrue(solved.json()["crossword"][0]["is_guessed"])
        self.assertEqual(solved.json()["crossword"][0]["revealed_word"], "secret_one")
        self.assertFalse(solved.json()["crossword"][0]["guessed_by_me"])
        self.assertEqual(self.game.state_version, original_version)

    async def test_sync_at_zero_does_not_finish_the_round(self) -> None:
        """Leave timeout finalization exclusively to the lifecycle task."""
        await self.prepare_playing_player()
        self.game.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)

        response = self.client.get(f"/game/sync/?round_id={self.game.game_id}")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["round"]["status"], "active")
        self.assertEqual(response.json()["round"]["remaining_seconds"], 0)
        self.assertEqual(self.game.status, "active")
        self.assertEqual(self.application.state.activeGame.game_id, self.game.game_id)

    async def test_sync_returns_previous_result_and_rejects_stale_rounds(self) -> None:
        """Return only exact PrevGame outcomes and never substitute the current round."""
        old_round_id = self.game.game_id
        self.application.state.activeGame = ActiveGame.create(settings=self.settings)
        self.application.state.prevGame = PrevGame(
            round_id=old_round_id,
            winner_user_id=uuid4(),
            finish_reason="winner",
        )

        previous = self.client.get(f"/game/sync/?round_id={old_round_id}")
        stale = self.client.get(f"/game/sync/?round_id={uuid4()}")

        self.assertEqual(previous.status_code, 200)
        self.assertEqual(previous.json()["round"]["id"], str(old_round_id))
        self.assertEqual(previous.json()["round"]["status"], "finished")
        self.assertEqual(previous.json()["round"]["finish_reason"], "winner")
        self.assertEqual(previous.json()["player"]["state"], "lost")
        self.assertEqual(previous.json()["crossword"], [])
        self.application.state.prevGame = PrevGame(
            round_id=old_round_id,
            winner_user_id=self.user.id,
            finish_reason="winner",
        )
        winner_previous = self.client.get(
            f"/game/sync/?round_id={old_round_id}&after_version=999"
        )
        self.assertEqual(winner_previous.status_code, 200)
        self.assertEqual(winner_previous.json()["player"]["state"], "won")
        self.assertEqual(
            winner_previous.json()["round"]["id"],
            str(old_round_id),
        )
        self.assertEqual(stale.status_code, 404)
        self.assertEqual(stale.json()["error"]["code"], "stale_round")
        self.assertIsNone(stale.json()["round"])
        invalid_version = self.client.get(
            f"/game/sync/?round_id={self.application.state.activeGame.game_id}&after_version=-1"
        )
        self.assertEqual(invalid_version.status_code, 422)

    async def test_late_guess_from_nonwinner_gets_only_previous_terminal_state(self) -> None:
        """Do not replay scoring or expose the current round to a late loser."""
        old_round_id = self.game.game_id
        winner_id = uuid4()
        self.application.state.activeGame = ActiveGame.create(settings=self.settings)
        self.application.state.prevGame = PrevGame(
            round_id=old_round_id,
            winner_user_id=winner_id,
            finish_reason="winner",
        )

        response = self.client.post(
            "/game/guess/",
            json={
                "round_id": str(old_round_id),
                "placement_id": str(uuid4()),
                "answer": "anything",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["round"]["id"], str(old_round_id))
        self.assertEqual(response.json()["round"]["finish_reason"], "winner")
        self.assertEqual(response.json()["player"]["state"], "lost")
        self.assertNotIn("guess", response.json())
        self.assertNotIn("round_score", response.json()["player"])

    async def test_late_guess_after_timeout_recovers_timeout_without_game_mutation(self) -> None:
        """Recover timeout state without repeating score or game mutations."""
        old_round_id = self.game.game_id
        self.application.state.activeGame = ActiveGame.create(settings=self.settings)
        self.application.state.prevGame = PrevGame(
            round_id=old_round_id,
            winner_user_id=None,
            finish_reason="timeout",
        )
        active_round_id = self.application.state.activeGame.game_id

        response = self.client.post(
            "/game/guess/",
            json={
                "round_id": str(old_round_id),
                "placement_id": str(uuid4()),
                "answer": "anything",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["round"]["id"], str(old_round_id))
        self.assertEqual(response.json()["round"]["finish_reason"], "timeout")
        self.assertEqual(response.json()["player"]["state"], "lost")
        self.assertIsNone(response.json().get("guess"))
        self.assertEqual(self.application.state.activeGame.game_id, active_round_id)
        self.assertEqual(self.session.game_results, {})


if __name__ == "__main__":
    unittest.main()
