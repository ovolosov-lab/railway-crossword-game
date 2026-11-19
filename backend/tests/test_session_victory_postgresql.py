import asyncio
import importlib.util
import os
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import GameResult, User
from app.victory_results import (
    SessionVictory,
    VictoryResultConflict,
    find_session_victory,
    persist_session_victory,
)


MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "migrations"
    / "versions"
    / "1c24e0aa0320_add_session_victory_id.py"
)
MIGRATION_SPEC = importlib.util.spec_from_file_location("session_victory_migration", MIGRATION_PATH)
if MIGRATION_SPEC is None or MIGRATION_SPEC.loader is None:
    raise RuntimeError(f"Cannot load migration at {MIGRATION_PATH}.")
MIGRATION = importlib.util.module_from_spec(MIGRATION_SPEC)
MIGRATION_SPEC.loader.exec_module(MIGRATION)


class SessionVictoryValidationTests(unittest.TestCase):
    def test_invalid_result_is_rejected(self) -> None:
        user_id, session_id = uuid4(), uuid4()
        now = datetime.now(timezone.utc)
        with self.assertRaises(ValueError):
            SessionVictory(user_id, user_id, now, now, 0)
        with self.assertRaises(ValueError):
            SessionVictory(session_id, user_id, now, now, -1)
        with self.assertRaises(ValueError):
            SessionVictory(session_id, user_id, now, now - timedelta(seconds=1), 0)
        with self.assertRaises(ValueError):
            SessionVictory(session_id, user_id, now.replace(tzinfo=None), now, 0)


@unittest.skipUnless(
    os.environ.get("RAILWAY_TEST_DATABASE_URL"),
    "Set RAILWAY_TEST_DATABASE_URL to a disposable PostgreSQL database.",
)
class SessionVictoryPostgreSQLTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.schema = f"test_victory_{uuid4().hex}"
        self.admin_engine = create_async_engine(os.environ["RAILWAY_TEST_DATABASE_URL"])
        async with self.admin_engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{self.schema}"'))
        self.engine = create_async_engine(
            os.environ["RAILWAY_TEST_DATABASE_URL"],
            connect_args={"server_settings": {"search_path": self.schema}},
        )
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        async with self.admin_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{self.schema}" CASCADE'))
        await self.admin_engine.dispose()

    async def create_model_tables(self) -> None:
        async with self.engine.begin() as connection:
            await connection.run_sync(User.__table__.create)
            await connection.run_sync(GameResult.__table__.create)

    async def create_user(self, score: int = 0):
        user_id = uuid4()
        async with self.sessions.begin() as session:
            session.add(
                User(
                    id=user_id,
                    username=f"player-{user_id.hex[:12]}",
                    email=f"{user_id.hex}@example.com",
                    hashed_password="not-a-real-password",
                    score=score,
                )
            )
        return user_id

    async def state(self, user_id, session_id):
        async with self.sessions() as session:
            score = await session.scalar(select(User.score).where(User.id == user_id))
            count = await session.scalar(
                select(func.count()).select_from(GameResult).where(
                    GameResult.session_id == session_id
                )
            )
            return score, count

    async def test_concurrent_retries_reconcile_and_reject_conflicting_result(self) -> None:
        await self.create_model_tables()
        user_id = await self.create_user(score=50)
        now = datetime.now(timezone.utc)
        victory = SessionVictory(uuid4(), user_id, now, now + timedelta(seconds=1), 80)

        async def submit() -> bool:
            async with self.sessions() as session:
                return await persist_session_victory(session, victory)

        results = await asyncio.gather(*(submit() for _ in range(8)))
        self.assertEqual(results.count(True), 1)
        self.assertEqual(await self.state(user_id, victory.session_id), (130, 1))

        async with self.sessions() as session:
            stored = await find_session_victory(session, victory)
            self.assertIsNotNone(stored)
        self.assertFalse(await submit())

        for different in (
            replace(victory, score_awarded=81),
            replace(victory, winner_user_id=uuid4()),
            replace(victory, finished_at=victory.finished_at + timedelta(seconds=1)),
        ):
            async with self.sessions() as session:
                with self.assertRaises(VictoryResultConflict):
                    await persist_session_victory(session, different)
        self.assertEqual(await self.state(user_id, victory.session_id), (130, 1))

    async def test_failed_score_update_rolls_back_result_and_score(self) -> None:
        await self.create_model_tables()
        user_id = await self.create_user(score=20)
        now = datetime.now(timezone.utc)
        victory = SessionVictory(uuid4(), user_id, now, now, 30)

        async with self.sessions() as session:
            original_scalar = session.scalar
            calls = 0

            async def fail_on_score_update(*args, **kwargs):
                nonlocal calls
                calls += 1
                result = await original_scalar(*args, **kwargs)
                if calls == 2:
                    raise RuntimeError("injected failure after score update")
                return result

            with patch.object(session, "scalar", side_effect=fail_on_score_update):
                with self.assertRaisesRegex(RuntimeError, "injected"):
                    await persist_session_victory(session, victory)

        self.assertEqual(await self.state(user_id, victory.session_id), (20, 0))
        async with self.sessions() as session:
            self.assertIsNone(await find_session_victory(session, victory))
        async with self.sessions() as session:
            self.assertTrue(await persist_session_victory(session, victory))
        self.assertEqual(await self.state(user_id, victory.session_id), (50, 1))

    async def test_different_victories_and_unknown_commit_outcome(self) -> None:
        await self.create_model_tables()
        user_id = await self.create_user(score=10)
        now = datetime.now(timezone.utc)
        first = SessionVictory(uuid4(), user_id, now, now, 15)
        second = SessionVictory(uuid4(), user_id, now, now, 25)

        async def submit(victory: SessionVictory) -> bool:
            async with self.sessions() as session:
                return await persist_session_victory(session, victory)

        async def submit_with_lost_response(victory: SessionVictory) -> None:
            await submit(victory)
            raise ConnectionError("response lost after commit")

        outcome = await asyncio.gather(
            submit_with_lost_response(first), submit(second), return_exceptions=True
        )
        self.assertIsInstance(outcome[0], ConnectionError)
        self.assertIs(outcome[1], True)
        self.assertEqual(await self.state(user_id, first.session_id), (50, 1))
        self.assertEqual(await self.state(user_id, second.session_id), (50, 1))

        async with self.sessions() as session:
            stored = await find_session_victory(session, first)
            self.assertIsNotNone(stored)
        self.assertFalse(await submit(first))
        self.assertEqual(await self.state(user_id, first.session_id), (50, 1))

    async def test_schema_enforces_unique_session_and_rejects_timeout_session(self) -> None:
        await self.create_model_tables()
        user_id = await self.create_user()
        now = datetime.now(timezone.utc)
        victory = SessionVictory(uuid4(), user_id, now, now, 0)
        async with self.sessions() as session:
            await persist_session_victory(session, victory)

        async with self.sessions() as session:
            session.add(
                GameResult(
                    game_id=uuid4(), session_id=victory.session_id,
                    winner_user_id=user_id, started_at=now, finished_at=now,
                    finish_reason="winner", score_awarded=0,
                )
            )
            with self.assertRaises(IntegrityError):
                await session.commit()
        async with self.sessions() as session:
            session.add(
                GameResult(
                    game_id=uuid4(), session_id=uuid4(), winner_user_id=None,
                    started_at=now, finished_at=now,
                    finish_reason="timeout", score_awarded=0,
                )
            )
            with self.assertRaises(IntegrityError):
                await session.commit()

    async def test_migration_preserves_history_and_guards_downgrade(self) -> None:
        user_id, old_game_id, timeout_id, new_game_id, session_id = (
            uuid4() for _ in range(5)
        )
        now = datetime.now(timezone.utc)
        async with self.engine.begin() as connection:
            await connection.execute(text("CREATE TABLE users (id uuid PRIMARY KEY, score integer NOT NULL)"))
            await connection.execute(text(
                "CREATE TABLE game_results ("
                "game_id uuid PRIMARY KEY, winner_user_id uuid REFERENCES users(id), "
                "started_at timestamptz NOT NULL, finished_at timestamptz NOT NULL, "
                "finish_reason varchar(16) NOT NULL, score_awarded integer NOT NULL)"
            ))
            await connection.execute(
                text("INSERT INTO users (id, score) VALUES (:id, 75)"),
                {"id": user_id},
            )
            await connection.execute(
                text(
                    "INSERT INTO game_results VALUES (:game, :user, :now, :now, 'winner', 75)"
                ),
                {"game": old_game_id, "user": user_id, "now": now},
            )
            await connection.execute(
                text("INSERT INTO game_results VALUES (:game, NULL, :now, :now, 'timeout', 0)"),
                {"game": timeout_id, "now": now},
            )
            await connection.execute(
                text("CREATE TABLE result_refs (game_id uuid REFERENCES game_results(game_id))")
            )
            await connection.execute(
                text("INSERT INTO result_refs VALUES (:game)"),
                {"game": old_game_id},
            )

        def run_migration(connection, action):
            with Operations.context(MigrationContext.configure(connection)):
                with patch.object(MIGRATION.context, "is_offline_mode", return_value=False):
                    action()

        async with self.engine.begin() as connection:
            await connection.run_sync(run_migration, MIGRATION.upgrade)
            row = (await connection.execute(text(
                "SELECT game_id, winner_user_id, score_awarded, session_id "
                "FROM game_results WHERE finish_reason = 'winner'"
            ))).one()
            self.assertEqual(tuple(row), (old_game_id, user_id, 75, None))
            self.assertEqual(await connection.scalar(text("SELECT count(*) FROM game_results")), 2)
            self.assertEqual(
                await connection.scalar(text("SELECT game_id FROM result_refs")), old_game_id
            )
            self.assertEqual(await connection.scalar(text("SELECT score FROM users")), 75)
            await connection.run_sync(run_migration, MIGRATION.downgrade)
            self.assertEqual(await connection.scalar(text("SELECT score FROM users")), 75)
            self.assertEqual(
                await connection.scalar(text("SELECT game_id FROM result_refs")), old_game_id
            )
            await connection.run_sync(run_migration, MIGRATION.upgrade)
            await connection.execute(
                text(
                    "INSERT INTO game_results (game_id, session_id, winner_user_id, "
                    "started_at, finished_at, finish_reason, score_awarded) "
                    "VALUES (:game, :session, :user, :now, :now, 'winner', 0)"
                ),
                {"game": new_game_id, "session": session_id, "user": user_id, "now": now},
            )
        async with self.engine.begin() as connection:
            with self.assertRaisesRegex(RuntimeError, "idempotency keys"):
                await connection.run_sync(run_migration, MIGRATION.downgrade)
        async with self.engine.connect() as connection:
            self.assertEqual(
                await connection.scalar(text("SELECT count(*) FROM game_results")), 3
            )
            self.assertEqual(
                await connection.scalar(
                    text("SELECT session_id FROM game_results WHERE game_id = :game"),
                    {"game": new_game_id},
                ),
                session_id,
            )


if __name__ == "__main__":
    unittest.main()
