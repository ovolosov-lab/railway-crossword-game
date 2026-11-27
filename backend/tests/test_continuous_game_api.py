import os
import random
import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from pydantic import SecretStr
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api_errors import api_exception_handler, request_validation_exception_handler
from app.config import Settings, get_settings
from app.continuous_game_api import _add_database_crossings, router, write_router
from app.crossword_generator import SegmentSpec
from app.database import get_db
from app.game_generation import GameGeneration, bootstrap_generation
from app.game_segments import GameSegments, PublishedWord, WordProposal
from app.game_sessions import GameSessions
from app.game_victories import GameVictories
from app.models import City, DictionaryWord, GameResult, PhysicalCity, Route, User
from app.security import get_current_user, require_csrf_token
from app.victory_results import persist_session_victory

# 1 -(260px)- 2 -(208px)- 3 -(182px)- 5, 1 -(234px)- 4 -(520px)- 3
CITIES = {1: (0, 0), 2: (260, 0), 3: (468, 0), 4: (0, 234), 5: (468, 182)}
ROUTES = [(1, 2, 10), (2, 3, 8), (1, 4, 9), (4, 3, 20), (3, 5, 7)]
WORDS = [
    "cat", "dog", "sun", "tree", "road", "rail", "train", "track", "signal", "station",
    "railway", "tunnel", "bridge", "engine", "ticket", "cargo", "depot", "wagon", "switch",
    "freight", "compass", "harbor", "journey", "platform",
]
FIRST_PAIR = [(1, 3)]
DEFINITIONS = {f"Clue number {index}": word for index, word in enumerate(WORDS)}
DEFINITION_OF = {word: definition for definition, word in DEFINITIONS.items()}


def word_of(placement: dict) -> str:
    return DEFINITIONS[placement["definition"]]


class DatabaseCrossingTests(unittest.IsolatedAsyncioTestCase):
    def base_words(self) -> tuple[SegmentSpec, list[WordProposal]]:
        spec = SegmentSpec.from_cities(1, 18, (0, 0), (468, 0))
        words = [
            WordProposal(
                placement_id=UUID(int=index),
                dictionary_word_id=index,
                word_text=text,
                definition=f"Clue for {text}",
                offset_px=offset,
            )
            for index, (text, offset) in enumerate(
                (("train", 55), ("plane", 180), ("sword", 305)), 1
            )
        ]
        return spec, words

    @staticmethod
    def db_with_rows(*row_sets):
        return SimpleNamespace(
            execute=AsyncMock(
                side_effect=[
                    SimpleNamespace(all=lambda rows=rows: rows) for rows in row_sets
                ]
            )
        )

    async def test_queries_same_length_and_keeps_base_words_along(self) -> None:
        spec, base = self.base_words()
        db = self.db_with_rows([
            (2, "PLANE", "Already in the base layout after normalization"),
            (4, "trace", "A separate perpendicular candidate"),
        ])
        proposals = {spec.route_id: list(base)}

        result = await _add_database_crossings(
            db, "en", [spec], [spec], proposals, {}, random.Random(1)
        )

        self.assertEqual(db.execute.await_count, 1)
        statement = db.execute.await_args.args[0]
        parameters = statement.compile().params
        self.assertEqual(parameters["language_code_1"], "en")
        self.assertEqual(parameters["letter_count_1"], 5)
        self.assertIn("is_active IS true", statement.compile().string)

        aligned = [item for item in result[spec.route_id] if item.orientation == "along"]
        across = [item for item in result[spec.route_id] if item.orientation == "perpendicular"]
        self.assertEqual(
            [(item.word_text, item.offset_px) for item in aligned],
            [(item.word_text, item.offset_px) for item in base],
        )
        self.assertEqual([item.word_text for item in across], ["trace"])
        self.assertEqual(aligned[0].crossing_id, across[0].crossing_id)
        self.assertEqual(aligned[0].crossing_index, 2)
        self.assertEqual(across[0].crossing_index, 2)

    async def test_randomly_selects_among_eligible_database_candidates(self) -> None:
        selected = set()
        for seed in range(12):
            spec, base = self.base_words()
            db = self.db_with_rows([
                (4, "trace", "Candidate one"),
                (5, "grace", "Candidate two"),
            ])
            proposals = {spec.route_id: list(base)}
            result = await _add_database_crossings(
                db, "en", [spec], [spec], proposals, {}, random.Random(seed)
            )
            across = [
                item for item in result[spec.route_id]
                if item.orientation == "perpendicular"
            ]
            self.assertEqual(len(across), 1)
            selected.add(across[0].word_text)
        self.assertEqual(selected, {"trace", "grace"})

    async def test_empty_query_or_endpoint_only_letter_leaves_layout_unchanged(self) -> None:
        for rows in ([], [(4, "azzzz", "The a occurs only at an endpoint")]):
            with self.subTest(rows=rows):
                spec, base = self.base_words()
                db = self.db_with_rows(rows)
                proposals = {spec.route_id: list(base)}
                result = await _add_database_crossings(
                    db, "en", [spec], [spec], proposals, {}, random.Random(1)
                )
                self.assertEqual(db.execute.await_count, 1)
                self.assertEqual(result[spec.route_id], base)

    async def test_last_or_ineligible_slot_does_not_trigger_a_query(self) -> None:
        spec = SegmentSpec.from_cities(1, 18, (0, 0), (468, 0))
        base = [
            WordProposal(
                placement_id=UUID(int=index), dictionary_word_id=index,
                word_text=text, definition=f"Clue for {text}", offset_px=offset,
            )
            for index, (text, offset) in enumerate((("train", 90), ("plane", 275)), 1)
        ]
        db = SimpleNamespace(execute=AsyncMock())
        proposals = {spec.route_id: list(base)}
        result = await _add_database_crossings(
            db, "en", [spec], [spec], proposals, {}, random.Random(1)
        )
        db.execute.assert_not_awaited()
        self.assertEqual(result[spec.route_id], base)

    async def test_previous_candidates_and_published_words_are_excluded(self) -> None:
        spec = SegmentSpec.from_cities(1, 20, (0, 0), (520, 0))
        base = [
            WordProposal(
                placement_id=UUID(int=index), dictionary_word_id=index,
                word_text=text, definition=f"Clue for {text}", offset_px=offset,
            )
            for index, (text, offset) in enumerate(
                (("cat", 50), ("bar", 140), ("dad", 230), ("map", 320), ("rag", 410)),
                1,
            )
        ]
        db = self.db_with_rows(
            [(1, "cat", "Already in the base layout"), (10, "man", "First extra")],
            [(10, "MAN", "Already selected after normalization"), (11, "hat", "Second extra")],
        )
        proposals = {spec.route_id: list(base)}
        result = await _add_database_crossings(
            db, "en", [spec], [spec], proposals, {}, random.Random(1)
        )
        self.assertEqual(db.execute.await_count, 2)
        across = [item for item in result[spec.route_id] if item.orientation == "perpendicular"]
        self.assertEqual([item.word_text for item in across], ["man", "hat"])
        self.assertEqual(len({item.dictionary_word_id for item in across}), 2)
        aligned = [item for item in result[spec.route_id] if item.orientation == "along"]
        self.assertEqual([item.word_text for item in aligned], [item.word_text for item in base])

    async def test_geometry_collision_skips_candidate_after_one_query(self) -> None:
        spec, base = self.base_words()
        crossing_axis = SegmentSpec.from_cities(
            2, 8, (107, -104), (107, 104)
        )
        db = self.db_with_rows([(4, "trace", "Crosses another route axis")])
        proposals = {spec.route_id: list(base)}
        result = await _add_database_crossings(
            db,
            "en",
            [spec, crossing_axis],
            [spec],
            proposals,
            {},
            random.Random(1),
        )
        self.assertEqual(db.execute.await_count, 1)
        self.assertEqual(result[spec.route_id], base)

    async def test_published_word_is_not_reused_for_a_crossing(self) -> None:
        spec, base = self.base_words()
        published_word = PublishedWord.model_validate(WordProposal(
            placement_id=uuid4(), dictionary_word_id=4,
            word_text="trace", definition="Already published", offset_px=52,
        ).stored())
        db = self.db_with_rows([(4, "TRACE", "Same normalized word")])
        proposals = {spec.route_id: list(base)}
        result = await _add_database_crossings(
            db,
            "en",
            [spec],
            [spec],
            proposals,
            {99: [published_word]},
            random.Random(1),
        )
        self.assertEqual(db.execute.await_count, 1)
        self.assertEqual(result[spec.route_id], base)


@unittest.skipUnless(
    os.environ.get("REDIS_TEST_URL") and os.environ.get("RAILWAY_TEST_DATABASE_URL"),
    "Set disposable Redis and PostgreSQL test URLs.",
)
class ContinuousGameApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.redis = Redis.from_url(os.environ["REDIS_TEST_URL"], decode_responses=True)
        self.namespace = f"railway-test:{{{uuid4()}}}"
        await bootstrap_generation(self.redis, namespace=self.namespace)
        self.generation = await GameGeneration.load_current(self.redis, namespace=self.namespace)
        self.sessions = GameSessions(self.generation)
        self.schema = f"test_continuous_{uuid4().hex}"
        self.admin_engine = create_async_engine(os.environ["RAILWAY_TEST_DATABASE_URL"])
        async with self.admin_engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{self.schema}"'))
        self.engine = create_async_engine(
            os.environ["RAILWAY_TEST_DATABASE_URL"],
            connect_args={"server_settings": {"search_path": self.schema}},
        )
        async with self.engine.begin() as connection:
            for model in (PhysicalCity, City, Route, DictionaryWord, User, GameResult):
                await connection.run_sync(model.__table__.create)
        self.db = async_sessionmaker(self.engine, expire_on_commit=False)
        await self.seed_catalog()

        self.victories = GameVictories(
            self.sessions, self.db, commit_victory=self.commit_victory
        )
        self.settings = Settings(
            jwt_secret=SecretStr("test-secret-for-continuous-game-api-tests"),
            language="en",
            redis_url=SecretStr(os.environ["REDIS_TEST_URL"]),
        )
        self.commit_fails = False
        self.csrf_cookie_name = get_settings().csrf_cookie_name
        self.clients: list[httpx.AsyncClient] = []
        self.app, self.client = self.make_client()

    def make_client(
        self, *, real_auth: bool = False, real_csrf: bool = False
    ) -> tuple[FastAPI, httpx.AsyncClient]:
        app = FastAPI(exception_handlers={
            StarletteHTTPException: api_exception_handler,
            RequestValidationError: request_validation_exception_handler,
        })
        app.state.game_sessions = self.sessions
        app.state.game_segments = GameSegments(self.sessions)
        app.state.game_victories = self.victories
        app.state.game_settings = self.settings
        app.include_router(router)
        app.include_router(write_router)

        async def current_user(request: Request):
            return SimpleNamespace(id=UUID(request.headers["x-test-user"]), score=0)

        async def database():
            async with self.db() as session:
                yield session

        if not real_auth:
            app.dependency_overrides[get_current_user] = current_user
        if not real_csrf:
            app.dependency_overrides[require_csrf_token] = lambda: None
        app.dependency_overrides[get_db] = database
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        self.clients.append(client)
        return app, client

    async def asyncTearDown(self) -> None:
        for client in self.clients:
            await client.aclose()
        await self.engine.dispose()
        async with self.admin_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{self.schema}" CASCADE'))
        await self.admin_engine.dispose()
        async for key in self.redis.scan_iter(match=f"{self.namespace}:*"):
            await self.redis.delete(key)
        await self.redis.aclose()

    async def seed_catalog(self) -> None:
        async with self.db.begin() as session:
            for city_id in CITIES:
                session.add(PhysicalCity(id=city_id))
            await session.flush()
            for city_id, (left, top) in CITIES.items():
                session.add(City(
                    id=city_id, lang="en", name=f"City {city_id}", country_code="GB",
                    left=Decimal(left), top=Decimal(top), is_capital=False,
                ))
            for source, target, length in ROUTES:
                session.add(Route(source_city_id=source, target_city_id=target, length_in_letters=length))
            for word in WORDS:
                session.add(DictionaryWord(
                    word_text=word, definition=DEFINITION_OF[word], language_code="en",
                    letter_count=len(word),
                ))

    async def commit_victory(self, victory) -> None:
        if self.commit_fails:
            raise ConnectionError("PostgreSQL is unavailable")
        async with self.db() as session:
            await persist_session_victory(session, victory)

    async def player(self) -> UUID:
        user_id = uuid4()
        async with self.db.begin() as session:
            session.add(User(
                id=user_id, username=f"user-{user_id.hex[:12]}", email=f"{user_id.hex}@example.com",
                hashed_password="test-only", score=0,
            ))
        return user_id

    async def call(self, user: UUID, method: str, path: str, **kwargs) -> httpx.Response:
        headers = {"X-Test-User": str(user), **kwargs.pop("headers", {})}
        return await self.client.request(method, f"/game{path}", headers=headers, **kwargs)

    async def join(self, user: UUID, pairs=None, key: UUID | None = None) -> dict:
        headers = {"Idempotency-Key": str(key or uuid4())}
        if pairs is None:
            response = await self.call(user, "POST", "/join/", headers=headers)
        else:
            with patch("app.continuous_game_api.eligible_city_pairs", return_value=list(pairs)):
                response = await self.call(user, "POST", "/join/", headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    async def confirm(self, user: UUID, session_id: str, route_ids: list[int]) -> httpx.Response:
        return await self.call(
            user, "POST", "/route/", json={"session_id": session_id, "route_ids": route_ids}
        )

    async def playing(self, pairs=FIRST_PAIR, route_ids=(3, 4)) -> tuple[UUID, str, list[dict]]:
        user = await self.player()
        joined = await self.join(user, pairs)
        session_id = joined["session"]["id"]
        response = await self.confirm(user, session_id, list(route_ids))
        self.assertEqual(response.status_code, 200, response.text)
        return user, session_id, response.json()["crossword"]

    async def guess(self, user: UUID, session_id: str, placement: dict, answer: str | None = None):
        return await self.call(user, "POST", "/guess/", json={
            "session_id": session_id, "placement_id": placement["placement_id"],
            "answer": answer or word_of(placement),
        })

    async def score_and_results(self, user: UUID) -> tuple[int, int]:
        async with self.db() as session:
            return (
                await session.scalar(select(User.score).where(User.id == user)),
                await session.scalar(
                    select(func.count()).select_from(GameResult).where(GameResult.winner_user_id == user)
                ),
            )

    # --- statistics, map and joining ---------------------------------------

    async def test_stats_without_session_and_requires_redis_services(self) -> None:
        user = await self.player()
        response = await self.call(user, "GET", "/stats/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "session": None, "player": {"state": "watching_stats"},
            "participant_count": 0, "leaderboard": [],
        })
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.app.state.game_sessions = None
        unavailable = await self.call(user, "GET", "/stats/")
        self.assertEqual(unavailable.status_code, 503)
        self.assertEqual(unavailable.json()["error"]["code"], "game_state_unavailable")

    async def test_routes_endpoint_returns_map_and_state_dependent_data(self) -> None:
        user = await self.player()
        idle = (await self.call(user, "GET", "/routes/")).json()
        self.assertIsNone(idle["session"])
        self.assertNotIn("crossword", idle)
        self.assertEqual(idle["cities"][0], {
            "id": 1, "name": "City 1", "country_code": "GB", "top": 0.0, "left": 0.0,
            "is_capital": False,
        })
        self.assertEqual(len(idle["routes"]), len(ROUTES))
        joined = await self.join(user, FIRST_PAIR)
        selecting = (await self.call(user, "GET", "/routes/")).json()
        self.assertEqual(selecting["player"], joined["player"])
        self.assertNotIn("crossword", selecting)
        await self.confirm(user, joined["session"]["id"], [3, 4])
        playing = (await self.call(user, "GET", "/routes/")).json()
        self.assertEqual(playing["player"]["route_ids"], [3, 4])
        self.assertTrue(playing["crossword"])

    async def test_join_is_idempotent_and_keeps_one_active_session(self) -> None:
        user = await self.player()
        key = uuid4()
        first = await self.join(user, FIRST_PAIR, key)
        self.assertEqual(first["session"]["status"], "active")
        self.assertEqual(first["session"]["state_version"], 1)
        self.assertIsNone(first["session"]["finish_reason"])
        self.assertEqual(first["player"], {
            "state": "selecting_route", "session_score": 0,
            "source_city_id": 1, "target_city_id": 3, "route_ids": [],
        })
        self.assertEqual(await self.join(user, FIRST_PAIR, key), first)
        self.assertEqual(await self.join(user, FIRST_PAIR, uuid4()), first)
        missing = await self.call(user, "POST", "/join/")
        self.assertEqual(missing.status_code, 422)
        self.assertEqual(missing.json()["error"]["code"], "validation_error")
        stats = (await self.call(user, "GET", "/stats/")).json()
        self.assertEqual(stats["participant_count"], 1)
        self.assertEqual(stats["session"]["id"], first["session"]["id"])
        self.assertEqual(stats["player"], {"state": "selecting_route", "session_score": 0})

    async def test_city_pairs_are_unique_and_exhaustion_is_reported(self) -> None:
        pairs = set()
        for _ in range(10):
            joined = await self.join(await self.player())
            pairs.add(frozenset((joined["player"]["source_city_id"], joined["player"]["target_city_id"])))
        self.assertEqual(len(pairs), 10)
        late = await self.player()
        response = await self.call(late, "POST", "/join/", headers={"Idempotency-Key": str(uuid4())})
        self.assertEqual(response.status_code, 409)
        body = response.json()
        self.assertEqual(body["error"]["code"], "city_pairs_exhausted")
        self.assertIsNone(body["error"]["details"])
        self.assertEqual(body["player"], {"state": "watching_stats"})
        self.assertNotIn("round", body)

    # --- route selection ---------------------------------------------------

    async def test_automatic_route_is_a_valid_unconfirmed_path(self) -> None:
        user = await self.player()
        joined = await self.join(user, FIRST_PAIR)
        seen = set()
        for _ in range(25):
            response = await self.call(
                user, "POST", "/route/auto/", json={"session_id": joined["session"]["id"]}
            )
            self.assertEqual(response.status_code, 200, response.text)
            body = response.json()
            seen.add(tuple(body["route_ids"]))
            self.assertEqual(body["player"]["route_ids"], [])
            self.assertEqual(body["session"], joined["session"])
        self.assertEqual(seen, {(1, 2), (3, 4)})
        stored = await self.sessions.restore(user)
        self.assertEqual(stored.state, "selecting_route")

    async def test_confirm_creates_public_crossword_and_is_idempotent(self) -> None:
        user, session_id, crossword = await self.playing()
        self.assertGreaterEqual(len(crossword), 3)
        for placement in crossword:
            self.assertEqual(set(placement), {
                "placement_id", "route_id", "offset_px", "orientation", "y_offset_px",
                "crossing_id", "crossing_index", "letter_count", "definition",
                "is_guessed", "guessed_by_me", "hint_types_used", "hints",
            })
            self.assertFalse(placement["is_guessed"])
            self.assertEqual(placement["hints"], [])
            self.assertGreaterEqual(placement["offset_px"], 0)
        by_route = {}
        for placement in crossword:
            by_route.setdefault(placement["route_id"], []).append(placement)
        for route_id, limit in ((3, 234), (4, 520)):
            for placement in by_route[route_id]:
                if placement["orientation"] == "along":
                    self.assertGreaterEqual(placement["offset_px"] - 13, 26)
                    self.assertLessEqual(
                        placement["offset_px"] + (placement["letter_count"] - 1) * 26 + 13,
                        limit - 26,
                    )
                else:
                    self.assertIsNotNone(placement["crossing_id"])
                    self.assertGreaterEqual(placement["crossing_index"], 1)
                    self.assertLess(placement["crossing_index"], placement["letter_count"] - 1)
        words = [word_of(item) for item in crossword]
        self.assertEqual(len(words), len(set(words)))
        again = await self.confirm(user, session_id, [3, 4])
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.json()["crossword"], crossword)
        self.assertEqual(again.json()["session"]["state_version"], 2)
        replaced = await self.confirm(user, session_id, [1, 2])
        self.assertEqual(replaced.status_code, 409)
        self.assertEqual(replaced.json()["error"]["code"], "route_already_confirmed")
        self.assertEqual(replaced.json()["session"]["id"], session_id)

    async def test_confirm_rejects_invalid_paths(self) -> None:
        user = await self.player()
        session_id = (await self.join(user, FIRST_PAIR))["session"]["id"]
        unknown = await self.confirm(user, session_id, [3, 99])
        self.assertEqual(unknown.status_code, 404)
        self.assertEqual(unknown.json()["error"]["code"], "route_not_found")
        self.assertEqual(unknown.json()["error"]["details"], {"route_ids": [99]})
        for route_ids in ([5], [4, 3], [1], [1, 2, 5]):
            response = await self.confirm(user, session_id, route_ids)
            self.assertEqual(response.status_code, 409, route_ids)
            self.assertEqual(response.json()["error"]["code"], "route_not_connected")
        duplicated = await self.confirm(user, session_id, [3, 3])
        self.assertEqual(duplicated.status_code, 422)
        stranger = await self.confirm(await self.player(), session_id, [3, 4])
        self.assertEqual(stranger.status_code, 404)
        self.assertEqual(stranger.json()["error"]["code"], "session_not_found")
        self.assertEqual((await self.sessions.restore(user)).state, "selecting_route")

    async def test_unknown_session_is_not_found_for_every_action(self) -> None:
        user = await self.player()
        await self.join(user, FIRST_PAIR)
        ghost = str(uuid4())
        placement = str(uuid4())
        requests = [
            ("POST", "/route/auto/", {"json": {"session_id": ghost}}),
            ("POST", "/route/", {"json": {"session_id": ghost, "route_ids": [3, 4]}}),
            ("POST", "/guess/", {"json": {"session_id": ghost, "placement_id": placement, "answer": "x"}}),
            ("POST", "/hint/", {"json": {"session_id": ghost, "placement_id": placement, "hint_type": "first_letter"}}),
            ("POST", "/surrender/", {"json": {"session_id": ghost}, "headers": {"Idempotency-Key": str(uuid4())}}),
            ("GET", f"/sync/?session_id={ghost}", {}),
        ]
        for method, path, kwargs in requests:
            response = await self.call(user, method, path, **kwargs)
            self.assertEqual(response.status_code, 404, path)
            self.assertEqual(response.json()["error"]["code"], "session_not_found")
            self.assertNotIn("session", response.json())

    async def test_guess_and_hint_require_a_confirmed_route(self) -> None:
        user = await self.player()
        session_id = (await self.join(user, FIRST_PAIR))["session"]["id"]
        placement = {"placement_id": str(uuid4()), "definition": DEFINITION_OF["cat"]}
        response = await self.guess(user, session_id, placement)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "route_not_confirmed")

    async def test_failed_generation_releases_participation(self) -> None:
        async with self.db.begin() as session:
            await session.execute(text("DELETE FROM dictionary_words"))
        user = await self.player()
        session_id = (await self.join(user, FIRST_PAIR))["session"]["id"]
        response = await self.confirm(user, session_id, [3, 4])
        self.assertEqual(response.status_code, 503)
        body = response.json()
        self.assertEqual(body["error"]["code"], "crossword_generation_failed")
        self.assertEqual(body["player"], {"state": "watching_stats"})
        self.assertIsNone(await self.sessions.restore(user))
        self.assertEqual((await self.call(user, "GET", "/stats/")).json()["participant_count"], 0)
        again = await self.join(user, FIRST_PAIR)
        self.assertNotEqual(again["session"]["id"], session_id)

    # --- guesses, hints and victories --------------------------------------

    async def test_guess_scores_once_and_hides_unsolved_words(self) -> None:
        user, session_id, crossword = await self.playing()
        target, other = crossword[0], crossword[1]
        wrong = await self.guess(user, session_id, target, "zzz")
        self.assertEqual(wrong.status_code, 200)
        body = wrong.json()
        self.assertEqual(body["guess"]["result"], "incorrect")
        self.assertIsNone(body["guess"]["revealed_word"])
        self.assertEqual(body["player"], {"state": "playing", "session_score": 0})
        version = body["session"]["state_version"]

        correct = await self.guess(user, session_id, target, f"  {word_of(target).upper()} ")
        body = correct.json()
        points = len(word_of(target)) * 10
        self.assertEqual(body["guess"], {
            "result": "correct", "already_solved": False, "points_awarded": points,
            "revealed_word": word_of(target),
        })
        self.assertEqual(body["player"], {"state": "playing", "session_score": points})
        self.assertGreater(body["session"]["state_version"], version)

        repeated = (await self.guess(user, session_id, target, "anything")).json()
        self.assertEqual(repeated["guess"]["result"], "already_solved")
        self.assertEqual(repeated["guess"]["points_awarded"], 0)
        self.assertEqual(repeated["session"], body["session"])

        sync = (await self.call(user, "GET", f"/sync/?session_id={session_id}&after_version=1")).json()
        solved = next(item for item in sync["crossword"] if item["placement_id"] == target["placement_id"])
        hidden = next(item for item in sync["crossword"] if item["placement_id"] == other["placement_id"])
        self.assertEqual(solved["word_text"], word_of(target))
        self.assertTrue(solved["guessed_by_me"])
        self.assertNotIn("word_text", hidden)
        self.assertNotIn(word_of(other), (await self.call(user, "GET", "/routes/")).text)

        missing = await self.guess(user, session_id, {"placement_id": str(uuid4()), "definition": DEFINITION_OF["cat"]})
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.json()["error"]["code"], "placement_not_found")

    async def test_hints_are_charged_once_and_need_points(self) -> None:
        user, session_id, crossword = await self.playing()
        first, second = crossword[0], crossword[1]

        def hint(placement: dict, hint_type: str = "first_letter"):
            return self.call(user, "POST", "/hint/", json={
                "session_id": session_id, "placement_id": placement["placement_id"],
                "hint_type": hint_type,
            })

        poor = await hint(second)
        self.assertEqual(poor.status_code, 403)
        self.assertEqual(poor.json()["error"]["code"], "insufficient_points")
        self.assertEqual(poor.json()["error"]["message"], "Not enough points to get a hint.")
        self.assertEqual(poor.json()["player"], {"state": "playing", "session_score": 0})
        await self.guess(user, session_id, first)
        cost = self.settings.hint_first_letter_cost
        score = len(word_of(first)) * 10
        self.assertGreaterEqual(score, cost)

        charged = (await hint(second)).json()
        self.assertEqual(charged["hint"], {
            "placement_id": second["placement_id"], "hint_type": "first_letter",
            "value": word_of(second)[0], "points_charged": cost,
        })
        self.assertEqual(charged["player"]["session_score"], score - cost)
        free = (await hint(second)).json()
        self.assertEqual(free["hint"]["points_charged"], 0)
        self.assertEqual(free["hint"]["value"], word_of(second)[0])
        self.assertEqual(free["session"], charged["session"])
        self.assertEqual(free["player"], charged["player"])

        unavailable = (await hint(first, "last_letter")).json()
        self.assertEqual(unavailable["hint"], {
            "placement_id": first["placement_id"], "hint_type": "unknown",
            "value": None, "points_charged": 0,
        })
        sync = (await self.call(user, "GET", "/sync/")).json()
        shown = next(item for item in sync["crossword"] if item["placement_id"] == second["placement_id"])
        self.assertEqual(shown["hints"], [{"hint_type": "first_letter", "value": word_of(second)[0]}])
        self.assertEqual(shown["hint_types_used"], ["first_letter"])
        invalid = await self.call(user, "POST", "/hint/", json={
            "session_id": session_id, "placement_id": second["placement_id"], "hint_type": "unknown",
        })
        self.assertEqual(invalid.status_code, 422)

    async def test_guess_attempts_are_rate_limited(self) -> None:
        user, session_id, crossword = await self.playing()
        self.settings.user_guess_rate_limit = 2
        for _ in range(2):
            self.assertEqual((await self.guess(user, session_id, crossword[0], "zzz")).status_code, 200)
        limited = await self.guess(user, session_id, crossword[0], "zzz")
        self.assertEqual(limited.status_code, 429)
        self.assertEqual(limited.json()["error"]["code"], "rate_limited")
        self.assertGreaterEqual(int(limited.headers["retry-after"]), 1)

    async def test_winning_saves_result_and_keeps_snapshot_until_next_join(self) -> None:
        user, session_id, crossword = await self.playing()
        for placement in crossword[:-1]:
            await self.guess(user, session_id, placement)
        final = await self.guess(user, session_id, crossword[-1])
        body = final.json()
        total = sum(len(word_of(item)) for item in crossword) * 10
        self.assertEqual(final.status_code, 200)
        self.assertEqual(body["session"]["status"], "finished")
        self.assertEqual(body["session"]["finish_reason"], "winner")
        self.assertEqual(body["player"], {"state": "won", "session_score": total})
        self.assertEqual(body["guess"]["result"], "correct")
        self.assertEqual(await self.score_and_results(user), (total, 1))

        again = await self.guess(user, session_id, crossword[0])
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.json(), {"session": body["session"], "player": body["player"]})
        self.assertEqual(await self.score_and_results(user), (total, 1))

        snapshot = (await self.call(user, "GET", f"/sync/?session_id={session_id}")).json()
        self.assertEqual(snapshot["player"]["state"], "won")
        self.assertTrue(all(item["is_guessed"] and "word_text" in item for item in snapshot["crossword"]))
        self.assertEqual((await self.call(user, "GET", "/routes/")).json()["crossword"], snapshot["crossword"])
        stats = (await self.call(user, "GET", "/stats/")).json()
        self.assertEqual(stats["participant_count"], 0)
        self.assertEqual(stats["leaderboard"][0]["score"], total)
        late = await self.confirm(user, session_id, [3, 4])
        self.assertEqual(late.status_code, 200)
        self.assertNotIn("crossword", late.json())

        new = await self.join(user, [(2, 4)])
        self.assertNotEqual(new["session"]["id"], session_id)
        old = await self.call(user, "GET", f"/sync/?session_id={session_id}")
        self.assertEqual(old.status_code, 404)

    async def test_unconfirmed_victory_is_finishing_until_postgresql_commits(self) -> None:
        user, session_id, crossword = await self.playing()
        for placement in crossword[:-1]:
            await self.guess(user, session_id, placement)
        self.commit_fails = True
        final = (await self.guess(user, session_id, crossword[-1])).json()
        self.assertEqual(final["session"]["status"], "finishing")
        self.assertIsNone(final["session"]["finish_reason"])
        self.assertEqual(final["player"]["state"], "finishing")
        self.assertEqual(final["guess"]["result"], "correct")
        self.assertEqual(await self.score_and_results(user), (0, 0))

        sync = (await self.call(user, "GET", "/sync/")).json()
        self.assertEqual(sync["session"]["status"], "finishing")
        surrender = await self.call(
            user, "POST", "/surrender/", json={"session_id": session_id},
            headers={"Idempotency-Key": str(uuid4())},
        )
        self.assertEqual(surrender.status_code, 409)
        self.assertEqual(surrender.json()["error"]["code"], "victory_finishing")
        self.assertEqual(surrender.json()["session"]["status"], "finishing")
        self.assertEqual(surrender.json()["player"]["state"], "finishing")
        joining = await self.call(user, "POST", "/join/", headers={"Idempotency-Key": str(uuid4())})
        self.assertEqual(joining.status_code, 409)
        self.assertEqual(joining.json()["error"]["code"], "victory_finishing")

        self.commit_fails = False
        recovered = (await self.call(user, "GET", "/sync/")).json()
        self.assertEqual(recovered["session"]["status"], "finished")
        self.assertEqual(recovered["player"]["state"], "won")
        self.assertEqual((await self.score_and_results(user))[1], 1)
        repeated = await self.call(
            user, "POST", "/surrender/", json={"session_id": session_id},
            headers={"Idempotency-Key": str(uuid4())},
        )
        self.assertEqual(repeated.status_code, 200)
        self.assertEqual(repeated.json()["session"]["status"], "finished")
        self.assertNotIn("surrender", repeated.json())

    async def test_shared_segment_words_and_victory_by_another_players_guesses(self) -> None:
        first, first_id, first_words = await self.playing(FIRST_PAIR, (3, 4))
        second = await self.player()
        joined = await self.join(second, [(3, 4)])
        response = await self.confirm(second, joined["session"]["id"], [4])
        self.assertEqual(response.status_code, 200, response.text)
        second_id = joined["session"]["id"]
        shared = [item for item in first_words if item["route_id"] == 4]
        self.assertEqual(response.json()["crossword"], shared)

        for placement in shared[:-1]:
            await self.guess(first, first_id, placement)
        opened = (await self.call(second, "GET", "/sync/")).json()
        self.assertEqual(opened["session"]["status"], "active")
        revealed = [item for item in opened["crossword"] if item["is_guessed"]]
        self.assertEqual(len(revealed), len(shared) - 1)
        self.assertTrue(all(not item["guessed_by_me"] and "word_text" in item for item in revealed))
        self.assertEqual(opened["player"]["session_score"], 0)

        await self.guess(first, first_id, shared[-1])
        finished = (await self.call(second, "GET", f"/sync/?session_id={second_id}")).json()
        self.assertEqual(finished["session"]["status"], "finished")
        self.assertEqual(finished["player"], {
            "state": "won", "session_score": 0, "source_city_id": 3,
            "target_city_id": 4, "route_ids": [4],
        })
        self.assertTrue(all(item["is_guessed"] and not item["guessed_by_me"] for item in finished["crossword"]))
        self.assertEqual(await self.score_and_results(second), (0, 1))
        self.assertEqual(await self.score_and_results(first), (0, 0))
        stats = (await self.call(first, "GET", "/stats/")).json()
        self.assertEqual(stats["participant_count"], 1)

        # The finished pair is free again, and the solved shared segment is not offered.
        third = await self.player()
        joined_third = await self.join(third, [(3, 4)])
        solved = await self.confirm(third, joined_third["session"]["id"], [4])
        self.assertEqual(solved.status_code, 409)
        self.assertEqual(solved.json()["error"]["code"], "route_already_solved")
        for _ in range(8):
            auto = await self.call(
                third, "POST", "/route/auto/", json={"session_id": joined_third["session"]["id"]}
            )
            self.assertEqual(auto.json()["route_ids"], [2, 1, 3])
        remaining = [item for item in first_words if item["route_id"] == 3]
        for placement in remaining:
            last = await self.guess(first, first_id, placement)
        self.assertEqual(last.json()["session"]["status"], "finished")
        self.assertEqual((await self.score_and_results(first))[1], 1)

    async def test_crossing_geometry_is_shared_through_sync_and_victory_snapshot(self) -> None:
        first = await self.player()
        first_join = await self.join(first, FIRST_PAIR)
        first_id = first_join["session"]["id"]
        crossing_id = uuid4()
        along = WordProposal(
            placement_id=uuid4(), dictionary_word_id=WORDS.index("train") + 1,
            word_text="train", definition=DEFINITION_OF["train"], offset_px=90,
            crossing_id=crossing_id, crossing_index=2,
        )
        perpendicular = WordProposal(
            placement_id=uuid4(), dictionary_word_id=WORDS.index("plane") + 1,
            word_text="plane", definition=DEFINITION_OF["plane"], offset_px=142,
            orientation="perpendicular", y_offset_px=-52,
            crossing_id=crossing_id, crossing_index=2,
        )
        endpoint = WordProposal(
            placement_id=uuid4(), dictionary_word_id=WORDS.index("rail") + 1,
            word_text="rail", definition=DEFINITION_OF["rail"], offset_px=280,
        )
        # Force a deterministic layout on the first publication; later participants
        # read the immutable segment from Redis.
        with patch(
            "app.continuous_game_api._build_proposals",
            new=AsyncMock(return_value={
                3: [WordProposal(
                    placement_id=uuid4(), dictionary_word_id=WORDS.index("cat") + 1,
                    word_text="cat", definition=DEFINITION_OF["cat"], offset_px=26,
                )],
                4: [along, perpendicular, endpoint],
            }),
        ):
            first_route = await self.confirm(first, first_id, [3, 4])
        self.assertEqual(first_route.status_code, 200, first_route.text)
        first_shared = [
            placement for placement in first_route.json()["crossword"]
            if placement["route_id"] == 4
        ]
        pair = [item for item in first_shared if item["crossing_id"] == str(crossing_id)]
        self.assertEqual(len(pair), 2)
        self.assertEqual({item["orientation"] for item in pair}, {"along", "perpendicular"})
        self.assertTrue(all("word_text" not in item for item in first_shared))

        second = await self.player()
        second_join = await self.join(second, [(3, 4)])
        second_id = second_join["session"]["id"]
        second_route = await self.confirm(second, second_id, [4])
        self.assertEqual(second_route.status_code, 200, second_route.text)
        shared = second_route.json()["crossword"]
        self.assertEqual(shared, first_shared)
        sync = (await self.call(second, "GET", f"/sync/?session_id={second_id}")).json()
        self.assertEqual(sync["crossword"], shared)

        for placement in shared:
            self.assertEqual((await self.guess(second, second_id, placement)).status_code, 200)
        snapshot = (await self.call(second, "GET", f"/sync/?session_id={second_id}")).json()
        self.assertEqual(snapshot["player"]["state"], "won")
        self.assertEqual(
            [
                (item["placement_id"], item["orientation"], item["offset_px"],
                 item["y_offset_px"], item["crossing_id"], item["crossing_index"])
                for item in snapshot["crossword"]
            ],
            [
                (item["placement_id"], item["orientation"], item["offset_px"],
                 item["y_offset_px"], item["crossing_id"], item["crossing_index"])
                for item in shared
            ],
        )

    async def test_same_route_is_shared_within_round_but_isolated_after_new_session(self) -> None:
        first, second = await self.player(), await self.player()
        first_join = await self.join(first, [(1, 2)])
        second_join = await self.join(second, [(1, 3)])
        first_id, second_id = first_join["session"]["id"], second_join["session"]["id"]
        first_state = await self.sessions.restore(first)
        second_state = await self.sessions.restore(second)
        assert first_state is not None and second_state is not None
        self.assertEqual(first_state.round_id, second_state.round_id)
        old_round = first_state.round_id

        def proposal(word: str) -> WordProposal:
            return WordProposal(
                placement_id=uuid4(), dictionary_word_id=WORDS.index(word) + 1,
                word_text=word, definition=DEFINITION_OF[word], offset_px=26,
            )

        old_word, second_route_word = proposal("train"), proposal("rail")
        with patch(
            "app.continuous_game_api._build_proposals",
            new=AsyncMock(return_value={1: [old_word]}),
        ):
            confirmed = await self.confirm(first, first_id, [1])
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        with patch(
            "app.continuous_game_api._build_proposals",
            new=AsyncMock(return_value={2: [second_route_word]}),
        ):
            confirmed = await self.confirm(second, second_id, [1, 2])
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        shared = [
            item for item in confirmed.json()["crossword"] if item["route_id"] == 1
        ]
        self.assertEqual(len(shared), 1)
        self.assertEqual(shared[0]["placement_id"], str(old_word.placement_id))

        solved = await self.guess(first, first_id, shared[0])
        self.assertEqual(solved.status_code, 200, solved.text)
        self.assertEqual(solved.json()["session"]["status"], "finished")
        old_active = await self.sessions.restore(second)
        assert old_active is not None
        self.assertEqual(old_active.state, "playing")
        self.assertEqual(old_active.round_id, old_round)
        old_refs, old_segment = self.sessions.segment_keys(old_round, 1)
        self.assertEqual(await self.redis.scard(old_refs), 1)

        next_join = await self.join(first, [(1, 2)])
        next_id = next_join["session"]["id"]
        new_session = await self.sessions.restore(first)
        assert new_session is not None
        self.assertNotEqual(new_session.round_id, old_round)
        self.assertEqual(new_session.route_ids, [])
        self.assertIsNone(new_session.victory_snapshot)

        fresh_word = proposal("platform")
        with patch(
            "app.continuous_game_api._build_proposals",
            new=AsyncMock(return_value={1: [fresh_word]}),
        ):
            fresh = await self.confirm(first, next_id, [1])
        self.assertEqual(fresh.status_code, 200, fresh.text)
        fresh_crossword = fresh.json()["crossword"]
        self.assertEqual(len(fresh_crossword), 1)
        self.assertEqual(fresh_crossword[0]["placement_id"], str(fresh_word.placement_id))
        self.assertFalse(fresh_crossword[0]["is_guessed"])
        self.assertNotIn("word_text", fresh_crossword[0])
        new_refs, new_segment = self.sessions.segment_keys(new_session.round_id, 1)
        self.assertEqual(await self.redis.scard(old_refs), 1)
        self.assertEqual(await self.redis.scard(new_refs), 1)

        old_sync = await self.call(second, "GET", f"/sync/?session_id={second_id}")
        self.assertEqual(old_sync.status_code, 200, old_sync.text)
        old_revealed = next(
            item for item in old_sync.json()["crossword"] if item["route_id"] == 1
        )
        self.assertEqual(old_revealed["placement_id"], str(old_word.placement_id))
        self.assertTrue(old_revealed["is_guessed"])
        self.assertEqual(old_revealed["word_text"], "train")

        surrendered = await self.call(
            second, "POST", "/surrender/", json={"session_id": second_id},
            headers={"Idempotency-Key": str(uuid4())},
        )
        self.assertEqual(surrendered.status_code, 200, surrendered.text)
        self.assertEqual(await self.redis.exists(old_refs, old_segment), 0)
        self.assertEqual(await self.redis.exists(new_refs, new_segment), 2)

    async def test_no_available_route_when_the_only_path_is_solved(self) -> None:
        # Route 5 (cities 3-5) is the only way to city 5; its words get solved by someone else.
        solver, solver_id, crossword = await self.playing([(4, 5)], (4, 5))
        for placement in (item for item in crossword if item["route_id"] == 5):
            await self.guess(solver, solver_id, placement)
        user = await self.player()
        joined = await self.join(user, [(3, 5)])
        auto = await self.call(user, "POST", "/route/auto/", json={"session_id": joined["session"]["id"]})
        self.assertEqual(auto.status_code, 409)
        self.assertEqual(auto.json()["error"]["code"], "no_available_route")
        self.assertEqual(auto.json()["session"]["id"], joined["session"]["id"])
        solved = await self.confirm(user, joined["session"]["id"], [5])
        self.assertEqual(solved.status_code, 409)
        self.assertEqual(solved.json()["error"]["code"], "route_already_solved")
        self.assertEqual((await self.sessions.restore(user)).state, "selecting_route")

    # --- surrender ---------------------------------------------------------

    async def test_surrender_is_idempotent_and_does_not_touch_a_new_session(self) -> None:
        user, session_id, _ = await self.playing()
        key = uuid4()

        def surrender(session: str, surrender_key: UUID | None = key):
            headers = {"Idempotency-Key": str(surrender_key)} if surrender_key else {}
            return self.call(user, "POST", "/surrender/", json={"session_id": session}, headers=headers)

        self.assertEqual((await surrender(session_id, None)).status_code, 422)
        done = await surrender(session_id)
        self.assertEqual(done.status_code, 200)
        self.assertEqual(done.json(), {"surrender": {"session_id": session_id, "result": "surrendered"}})
        self.assertIsNone(await self.sessions.restore(user))
        self.assertEqual(await self.score_and_results(user), (0, 0))
        stats = (await self.call(user, "GET", "/stats/")).json()
        self.assertEqual((stats["session"], stats["participant_count"]), (None, 0))

        new = await self.join(user, FIRST_PAIR)
        self.assertEqual((await surrender(session_id)).json(), done.json())
        self.assertEqual((await self.sessions.restore(user)).id, UUID(new["session"]["id"]))
        conflict = await surrender(new["session"]["id"])
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.json()["error"]["code"], "idempotency_key_conflict")

    async def test_join_key_of_a_replaced_session_is_reported_unavailable(self) -> None:
        user = await self.player()
        key = uuid4()
        first = await self.join(user, FIRST_PAIR, key)
        await self.call(
            user, "POST", "/surrender/", json={"session_id": first["session"]["id"]},
            headers={"Idempotency-Key": str(uuid4())},
        )
        await self.join(user, FIRST_PAIR)
        retry = await self.call(user, "POST", "/join/", headers={"Idempotency-Key": str(key)})
        self.assertEqual(retry.status_code, 409)
        self.assertEqual(retry.json()["error"]["code"], "join_result_unavailable")

    # --- failures ----------------------------------------------------------

    async def test_authentication_is_required_for_every_endpoint(self) -> None:
        _, client = self.make_client(real_auth=True)
        ghost = str(uuid4())
        requests = [
            ("GET", "/game/stats/", {}),
            ("GET", "/game/routes/", {}),
            ("GET", "/game/sync/", {}),
            ("POST", "/game/join/", {"headers": {"Idempotency-Key": str(uuid4())}}),
            ("POST", "/game/route/auto/", {"json": {"session_id": ghost}}),
            ("POST", "/game/route/", {"json": {"session_id": ghost, "route_ids": [1]}}),
            ("POST", "/game/guess/", {"json": {"session_id": ghost, "placement_id": ghost, "answer": "x"}}),
            ("POST", "/game/hint/", {"json": {"session_id": ghost, "placement_id": ghost, "hint_type": "first_letter"}}),
            ("POST", "/game/surrender/", {"json": {"session_id": ghost}, "headers": {"Idempotency-Key": str(uuid4())}}),
        ]
        for method, path, kwargs in requests:
            response = await client.request(method, path, **kwargs)
            self.assertEqual(response.status_code, 401, path)
            self.assertEqual(response.json()["error"]["code"], "authentication_required")

    async def test_state_changes_require_a_csrf_token(self) -> None:
        from app.security import create_csrf_token

        _, client = self.make_client(real_csrf=True)
        user = await self.player()
        headers = {"X-Test-User": str(user), "Idempotency-Key": str(uuid4())}
        refused = await client.post("/game/join/", headers=headers)
        self.assertEqual(refused.status_code, 403)
        self.assertEqual(refused.json()["error"]["code"], "csrf_validation_failed")
        self.assertIsNone(await self.sessions.restore(user))
        token = create_csrf_token()
        client.cookies.set(self.csrf_cookie_name, token)
        accepted = await client.post("/game/join/", headers={**headers, "X-CSRF-Token": token})
        self.assertEqual(accepted.status_code, 200, accepted.text)
        reading = await client.get("/game/stats/", headers={"X-Test-User": str(user)})
        self.assertEqual(reading.status_code, 200)

    async def test_redis_failure_is_unavailable_not_a_missing_session(self) -> None:
        user = await self.player()
        with patch.object(
            self.sessions, "restore", side_effect=RedisConnectionError("Redis is down")
        ):
            for method, path in (("GET", "/stats/"), ("GET", "/sync/"), ("GET", "/routes/")):
                response = await self.call(user, method, path)
                self.assertEqual(response.status_code, 503, path)
                self.assertEqual(response.json()["error"]["code"], "game_state_unavailable")

    async def test_stale_worker_generation_is_unavailable(self) -> None:
        user = await self.player()
        await self.redis.delete(self.generation.current_generation_key)
        response = await self.call(user, "GET", "/sync/")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "game_state_unavailable")

    async def test_error_messages_follow_the_configured_language(self) -> None:
        user = await self.player()
        english = await self.call(user, "GET", f"/sync/?session_id={uuid4()}")
        self.assertEqual(english.json()["error"]["message"], "The game session was not found.")
        self.settings.language = "ru"
        russian = await self.call(user, "GET", f"/sync/?session_id={uuid4()}")
        self.assertEqual(russian.json()["error"]["message"], "Игровая сессия не найдена.")
        self.assertEqual(russian.json()["error"]["code"], english.json()["error"]["code"])


if __name__ == "__main__":
    unittest.main()
