import asyncio
import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from uuid import uuid4

from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.game_generation import GameGeneration, StaleGameGeneration, bootstrap_generation
from app.game_segments import GameSegments, WordProposal
from app.game_sessions import GameSessions, SessionNotFound, SessionStorageError, VictoryFinishing
from app.game_victories import GameVictories, _PUBLISH
from app.models import GameResult, User
from app.victory_results import SessionVictory, persist_session_victory


@unittest.skipUnless(
    os.environ.get("REDIS_TEST_URL") and os.environ.get("RAILWAY_TEST_DATABASE_URL"),
    "Set disposable Redis and PostgreSQL test URLs.",
)
class GameVictoriesIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.redis = Redis.from_url(os.environ["REDIS_TEST_URL"], decode_responses=True)
        self.namespace = f"railway-test:{{{uuid4()}}}"
        await bootstrap_generation(self.redis, namespace=self.namespace)
        self.generation = await GameGeneration.load_current(self.redis, namespace=self.namespace)
        self.sessions = GameSessions(self.generation)
        self.segments = GameSegments(self.sessions)
        self.schema = f"test_game_victories_{uuid4().hex}"
        self.admin_engine = create_async_engine(os.environ["RAILWAY_TEST_DATABASE_URL"])
        async with self.admin_engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{self.schema}"'))
        self.engine = create_async_engine(
            os.environ["RAILWAY_TEST_DATABASE_URL"],
            connect_args={"server_settings": {"search_path": self.schema}},
        )
        async with self.engine.begin() as connection:
            await connection.run_sync(User.__table__.create)
            await connection.run_sync(GameResult.__table__.create)
        self.db = async_sessionmaker(self.engine, expire_on_commit=False)
        self.victories = GameVictories(
            self.sessions, self.db, commit_victory=self.commit_for_test
        )

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        async with self.admin_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{self.schema}" CASCADE'))
        await self.admin_engine.dispose()
        async for key in self.redis.scan_iter(match=f"{self.namespace}:*"):
            await self.redis.delete(key)
        await self.redis.aclose()

    async def commit_for_test(self, victory) -> None:
        # This test callback is deliberately not a generation fence.
        async with self.db() as session:
            await persist_session_victory(session, victory)

    async def player(self, pair, word):
        user = uuid4()
        async with self.db.begin() as db:
            db.add(User(
                id=user, username=f"user-{user.hex[:12]}", email=f"{user.hex}@example.com",
                hashed_password="test-only", score=0,
            ))
        session = await self.sessions.join(user, uuid4(), [pair])
        await self.segments.confirm_route(user, session.id, [10], {10: [word]})
        return user, session.id

    async def score_and_count(self, user):
        async with self.db() as session:
            return (
                await session.scalar(select(User.score).where(User.id == user)),
                await session.scalar(
                    select(func.count()).select_from(GameResult).where(
                        GameResult.winner_user_id == user
                    )
                ),
            )

    def word(self):
        return WordProposal(
            placement_id=uuid4(), dictionary_word_id=1,
            word_text="Station", definition="Railway stop", offset_px=12,
        )

    async def finishing_player(self):
        word = self.word()
        user, session_id = await self.player((1, 2), word)
        await self.segments.guess(user, session_id, word.placement_id, "station")
        return user, session_id

    async def test_failure_before_commit_retries_without_losing_session(self) -> None:
        user, session_id = await self.finishing_player()

        async def fail_before(_victory):
            raise ConnectionError("before commit")

        self.victories.commit_victory = fail_before
        with self.assertRaises(ConnectionError):
            await self.victories.finish(user, session_id)
        self.assertEqual(await self.score_and_count(user), (0, 0))
        with self.assertRaises(VictoryFinishing):
            await self.sessions.surrender(user, session_id, uuid4())
        self.victories.commit_victory = self.commit_for_test
        await self.victories.retry_pending()
        self.assertEqual((await self.sessions.restore(user)).state, "won")
        self.assertEqual(await self.score_and_count(user), (70, 1))

    async def test_unknown_commit_is_reconciled_without_double_score(self) -> None:
        user, session_id = await self.finishing_player()

        async def lose_response(victory):
            await self.commit_for_test(victory)
            raise ConnectionError("response lost after commit")

        self.victories.commit_victory = lose_response
        won = await self.victories.finish(user, session_id)
        self.assertEqual(won.state, "won")
        await self.victories.finish(user, session_id)
        self.assertEqual(await self.score_and_count(user), (70, 1))
        _, placements = await self.segments.crossword(user, session_id)
        self.assertEqual(placements[0].word_text, "Station")

    async def test_retry_after_commit_and_failed_publication(self) -> None:
        user, session_id = await self.finishing_player()
        execute = self.generation.execute

        async def fail_publication(script, **kwargs):
            if script == _PUBLISH:
                raise RedisConnectionError("publication unavailable")
            return await execute(script, **kwargs)

        with patch.object(self.generation, "execute", side_effect=fail_publication):
            with self.assertRaises(RedisConnectionError):
                await self.victories.finish(user, session_id)
        self.assertEqual(await self.score_and_count(user), (70, 1))
        restarted = GameVictories(self.sessions, self.db, commit_victory=self.commit_for_test)
        await restarted.retry_pending()
        self.assertEqual((await self.sessions.restore(user)).state, "won")
        self.assertEqual(await self.score_and_count(user), (70, 1))

    async def test_shared_guess_completes_both_and_preserves_shared_segment(self) -> None:
        word = self.word()
        first, first_session = await self.player((1, 2), word)
        second, second_session = await self.player((3, 4), word)
        await self.segments.guess(first, first_session, word.placement_id, "station")
        self.assertEqual((await self.sessions.restore(second)).state, "finishing")
        await self.victories.finish(first, first_session)
        refs, state = self.sessions.segment_keys(10)
        self.assertTrue(await self.redis.exists(refs))
        self.assertTrue(await self.redis.exists(state))
        await asyncio.gather(
            self.victories.finish(second, second_session),
            self.victories.finish(second, second_session),
        )
        self.assertFalse(await self.redis.exists(refs))
        self.assertFalse(await self.redis.exists(state))
        self.assertEqual(await self.score_and_count(first), (70, 1))
        self.assertEqual(await self.score_and_count(second), (0, 1))
        for user, session_id in ((first, first_session), (second, second_session)):
            _, placements = await self.segments.crossword(user, session_id)
            self.assertEqual(placements[0].word_text, "Station")

    async def test_negative_snapshot_score_is_clamped_before_commit(self) -> None:
        user, session_id = await self.finishing_player()
        key = self.sessions.session_key(user)
        raw = await self.generation.execute(
            "return redis.call('HGET', KEYS[2], 'victory_snapshot')", keys=[key]
        )
        if not isinstance(raw, str):
            raise AssertionError("Missing victory snapshot.")
        snapshot = json.loads(raw)
        snapshot["session_score"] = -15
        await self.generation.execute(
            "return redis.call('HSET', KEYS[2], 'session_score', '-15', 'victory_snapshot', ARGV[1])",
            keys=[key], args=[json.dumps(snapshot)],
        )
        await self.victories.finish(user, session_id)
        self.assertEqual(await self.score_and_count(user), (0, 1))

    async def test_competing_guess_and_surrender_have_one_outcome(self) -> None:
        word = self.word()
        user, session_id = await self.player((1, 2), word)
        guess, surrender = await asyncio.gather(
            self.segments.guess(user, session_id, word.placement_id, "station"),
            self.sessions.surrender(user, session_id, uuid4()),
            return_exceptions=True,
        )
        if isinstance(surrender, VictoryFinishing):
            self.assertFalse(isinstance(guess, Exception))
            await self.victories.finish(user, session_id)
            self.assertEqual(await self.score_and_count(user), (70, 1))
        else:
            self.assertIsNone(surrender)
            self.assertIsInstance(guess, SessionNotFound)
            self.assertIsNone(await self.sessions.restore(user))
            self.assertEqual(await self.score_and_count(user), (0, 0))

    async def test_corrupt_snapshot_is_not_committed(self) -> None:
        user, session_id = await self.finishing_player()
        key = self.sessions.session_key(user)
        raw = await self.generation.execute(
            "return redis.call('HGET', KEYS[2], 'victory_snapshot')", keys=[key]
        )
        if not isinstance(raw, str):
            raise AssertionError("Missing victory snapshot.")
        snapshot = json.loads(raw)
        snapshot["user_id"] = str(uuid4())
        await self.generation.execute(
            "return redis.call('HSET', KEYS[2], 'victory_snapshot', ARGV[1])",
            keys=[key], args=[json.dumps(snapshot)],
        )
        with self.assertRaisesRegex(SessionStorageError, "Invalid victory snapshot"):
            await self.victories.finish(user, session_id)
        self.assertEqual(await self.score_and_count(user), (0, 0))
        self.assertEqual((await self.sessions.restore(user)).state, "finishing")

    async def test_failed_pending_victory_does_not_block_another(self) -> None:
        word = self.word()
        first, first_session = await self.player((1, 2), word)
        second, second_session = await self.player((3, 4), word)
        await self.segments.guess(first, first_session, word.placement_id, "station")

        async def fail_one(victory):
            if victory.winner_user_id == first:
                raise ConnectionError("temporarily unavailable")
            await self.commit_for_test(victory)

        self.victories.commit_victory = fail_one
        await self.victories.retry_pending()
        self.assertEqual((await self.sessions.restore(first)).state, "finishing")
        self.assertEqual((await self.sessions.restore(second)).state, "won")
        self.assertEqual(await self.score_and_count(first), (0, 0))
        self.assertEqual(await self.score_and_count(second), (0, 1))
        self.victories.commit_victory = self.commit_for_test
        await self.victories.retry_pending()
        self.assertEqual((await self.sessions.restore(first)).state, "won")
        self.assertEqual(await self.score_and_count(first), (70, 1))
        self.assertEqual((await self.sessions.restore(second)).id, second_session)

    async def test_created_service_commits_through_generation_fence(self) -> None:
        user, session_id = await self.finishing_player()
        victories = await GameVictories.create(self.sessions, self.db)
        self.assertIs(victories.generation, self.generation)
        with patch.object(
            self.generation, "assert_current", wraps=self.generation.assert_current
        ) as fence:
            won = await victories.finish(user, session_id)
        self.assertEqual(won.state, "won")
        self.assertEqual(fence.await_count, 2)
        self.assertEqual(await self.score_and_count(user), (70, 1))

    async def test_stale_generation_stops_commit_and_queue_processing(self) -> None:
        user, session_id = await self.finishing_player()
        victories = await GameVictories.create(self.sessions, self.db)
        await self.redis.delete(self.generation.current_generation_key)
        with self.assertRaises(StaleGameGeneration):
            await victories.retry_pending()
        with self.assertRaises(StaleGameGeneration):
            await victories.finish(user, session_id)
        self.assertEqual(await self.score_and_count(user), (0, 0))

    async def test_generation_change_before_commit_rolls_back_result_and_score(self) -> None:
        user, session_id = await self.finishing_player()
        victories = await GameVictories.create(self.sessions, self.db)
        now = datetime.now(timezone.utc)
        victory = SessionVictory(session_id, user, now - timedelta(seconds=5), now, 70)
        # The first check passes; the generation is superseded before commit.
        with patch.object(
            self.generation,
            "assert_current",
            side_effect=[None, StaleGameGeneration("superseded")],
        ):
            with self.assertRaises(StaleGameGeneration):
                await victories.commit_victory(victory)
        self.assertEqual(await self.score_and_count(user), (0, 0))
        await victories.retry_pending()
        self.assertEqual((await self.sessions.restore(user)).state, "won")
        self.assertEqual(await self.score_and_count(user), (70, 1))