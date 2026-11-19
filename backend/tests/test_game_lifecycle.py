import asyncio
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI
from pydantic import SecretStr

from app.config import Settings
from app.game_generation import GameGenerationUnavailable, StaleGameGeneration
from app.game_state import ActiveGame, PrevGame
from app.periodic_task import AsyncPeriodicTask
from main import check_game_duration, fence_stale_game_worker, lifespan


def test_settings() -> Settings:
    return Settings(
        jwt_secret=SecretStr("test-secret-for-game-lifecycle-tests-only"),
        language="ru",
        game_duration_hours=1,
        check_interval=1,
    )


class AsyncPeriodicTaskTests(unittest.IsolatedAsyncioTestCase):
    async def test_runs_sequentially_and_stops(self) -> None:
        started_three_times = asyncio.Event()
        active = False
        overlap_detected = False
        call_count = 0

        async def work() -> None:
            nonlocal active, overlap_detected, call_count
            if active:
                overlap_detected = True
            active = True
            call_count += 1
            if call_count == 3:
                started_three_times.set()
            try:
                await asyncio.sleep(0.002)
            finally:
                active = False

        task = AsyncPeriodicTask(interval=0.001, task_func=work)
        task.start()
        await asyncio.wait_for(started_three_times.wait(), timeout=1)
        await task.stop()
        stopped_count = call_count
        await asyncio.sleep(0.01)

        self.assertFalse(overlap_detected)
        self.assertFalse(active)
        self.assertEqual(call_count, stopped_count)

    async def test_continues_after_task_function_error(self) -> None:
        second_call = asyncio.Event()
        call_count = 0

        async def work() -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("temporary failure")
            second_call.set()

        task = AsyncPeriodicTask(interval=0.001, task_func=work)
        task.start()
        await asyncio.wait_for(second_call.wait(), timeout=1)
        await task.stop()

        self.assertGreaterEqual(call_count, 2)

    def test_rejects_nonpositive_interval(self) -> None:
        async def work() -> None:
            return None

        with self.assertRaises(ValueError):
            AsyncPeriodicTask(interval=0, task_func=work)


class GameLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.settings = test_settings()
        self.started_at = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)
        self.application = FastAPI()
        self.application.state.game_lock = asyncio.Lock()
        self.application.state.activeGame = ActiveGame.create(
            settings=self.settings,
            started_at=self.started_at,
        )

    async def test_round_remains_active_before_expiration(self) -> None:
        game = self.application.state.activeGame

        await check_game_duration(
            self.application,
            app_settings=self.settings,
            now=game.expires_at - timedelta(seconds=1),
        )

        self.assertIs(self.application.state.activeGame, game)
        self.assertEqual(game.status, "active")

    async def test_expired_round_times_out_and_is_replaced_once(self) -> None:
        """Preserve the timed-out round before advancing the active game."""
        expired_game = self.application.state.activeGame
        expiration = expired_game.expires_at

        await check_game_duration(
            self.application,
            app_settings=self.settings,
            now=expiration,
        )
        next_game = self.application.state.activeGame

        self.assertEqual(expired_game.status, "finished")
        self.assertEqual(expired_game.finish_reason, "timeout")
        self.assertEqual(expired_game.finished_at, expiration)
        self.assertEqual(expired_game.score_awarded, 0)
        self.assertEqual(self.application.state.prevGame.round_id, expired_game.game_id)
        self.assertIsNone(self.application.state.prevGame.winner_user_id)
        self.assertEqual(self.application.state.prevGame.finish_reason, "timeout")
        self.assertIsNot(next_game, expired_game)
        self.assertEqual(next_game.status, "active")
        self.assertEqual(next_game.started_at, expiration)
        self.assertEqual(
            next_game.expires_at - next_game.started_at,
            timedelta(hours=self.settings.game_duration_hours),
        )

        await check_game_duration(
            self.application,
            app_settings=self.settings,
            now=expiration,
        )
        self.assertIs(self.application.state.activeGame, next_game)

        next_expiration = next_game.expires_at
        await check_game_duration(
            self.application,
            app_settings=self.settings,
            now=next_expiration,
        )
        self.assertEqual(self.application.state.prevGame.round_id, next_game.game_id)
        self.assertNotEqual(self.application.state.prevGame.round_id, expired_game.game_id)

    async def test_concurrent_expiration_checks_create_only_one_next_round(self) -> None:
        """Serialize concurrent lifecycle checks and only advance once."""
        expired_game = self.application.state.activeGame
        expiration = expired_game.expires_at

        await asyncio.gather(
            check_game_duration(
                self.application,
                app_settings=self.settings,
                now=expiration,
            ),
            check_game_duration(
                self.application,
                app_settings=self.settings,
                now=expiration,
            ),
        )

        next_game = self.application.state.activeGame
        self.assertIsNot(next_game, expired_game)
        self.assertEqual(next_game.started_at, expiration)
        self.assertEqual(expired_game.finish_reason, "timeout")

    async def test_lifespan_initializes_round_and_cleans_up_in_order(self) -> None:
        events: list[str] = []
        background_task = MagicMock()
        background_task.stop = AsyncMock(side_effect=lambda: events.append("stop"))

        async def database_check() -> None:
            events.append("database")

        def create_periodic_task(*, interval: float, task_func) -> MagicMock:
            self.assertEqual(interval, self.settings.check_interval)
            self.assertTrue(callable(task_func))
            periodic_task = MagicMock()
            periodic_task.start.side_effect = lambda: events.append("start")
            periodic_task.stop = background_task.stop
            return periodic_task

        async def dispose_engine() -> None:
            events.append("dispose")

        mocked_engine = MagicMock()
        mocked_engine.dispose = AsyncMock(side_effect=dispose_engine)
        application = FastAPI()
        with (
            patch("main.settings", self.settings),
            patch("main.check_database_connection", side_effect=database_check),
            patch("main.AsyncPeriodicTask", side_effect=create_periodic_task),
            patch("main.engine", mocked_engine),
        ):
            async with lifespan(application):
                events.append("yield")
                self.assertIsInstance(application.state.activeGame, ActiveGame)
                self.assertEqual(application.state.activeGame.status, "active")
                self.assertIsInstance(application.state.game_lock, asyncio.Lock)
                self.assertIsInstance(application.state.prevGame, PrevGame)

        self.assertEqual(
            events,
            ["database", "start", "yield", "stop", "dispose"],
        )
        mocked_engine.dispose.assert_awaited_once()

    async def test_lifespan_disposes_engine_if_database_check_fails(self) -> None:
        application = FastAPI()
        mocked_engine = MagicMock()
        mocked_engine.dispose = AsyncMock()
        with (
            patch("main.check_database_connection", side_effect=RuntimeError("db down")),
            patch("main.engine", mocked_engine),
        ):
            with self.assertRaisesRegex(RuntimeError, "db down"):
                async with lifespan(application):
                    self.fail("lifespan must not start without database access")

        mocked_engine.dispose.assert_awaited_once()

    async def test_lifespan_requires_a_bootstrapped_generation_when_redis_is_configured(
        self,
    ) -> None:
        application = FastAPI()
        configured_settings = self.settings.model_copy(
            update={"redis_url": SecretStr("redis://redis:6379/0")}
        )
        redis_client = MagicMock()
        redis_client.ping = AsyncMock()
        redis_client.get = AsyncMock(return_value=None)
        redis_client.aclose = AsyncMock()
        mocked_engine = MagicMock()
        mocked_engine.dispose = AsyncMock()

        with (
            patch("main.settings", configured_settings),
            patch("main.check_database_connection", new_callable=AsyncMock),
            patch("main.create_redis_client", return_value=redis_client),
            patch("main.engine", mocked_engine),
        ):
            with self.assertRaises(GameGenerationUnavailable):
                async with lifespan(application):
                    self.fail("worker must not start without a generation")

        redis_client.aclose.assert_awaited_once()
        mocked_engine.dispose.assert_awaited_once()

    async def test_stale_worker_requests_fail_closed(self) -> None:
        application = FastAPI()
        application.state.game_generation = MagicMock()
        application.state.game_generation.generation_id = "stale-generation"
        application.state.game_generation.assert_current = AsyncMock(
            side_effect=StaleGameGeneration("stale worker")
        )
        request = MagicMock()
        request.app = application
        call_next = AsyncMock()

        response = await fence_stale_game_worker(request, call_next)

        self.assertEqual(response.status_code, 503)
        assert isinstance(response.body, bytes)
        self.assertEqual(
            json.loads(response.body.decode("utf-8")),
            {
                "error": {
                    "code": "game_state_unavailable",
                    "message": "Game state is temporarily unavailable.",
                    "details": None,
                }
            },
        )
        call_next.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
