import asyncio
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI
from pydantic import SecretStr
from redis.exceptions import RedisError

from app.config import Settings
from app.game_generation import GameGenerationUnavailable, StaleGameGeneration
from app.game_sessions import GameSessions
from app.game_segments import GameSegments
from app.game_victories import GameVictories
from app.periodic_task import AsyncPeriodicTask
from main import (
    PENDING_VICTORY_RETRY_INTERVAL_SECONDS,
    fence_stale_game_worker,
    lifespan,
    retry_pending_victories,
)


def test_settings() -> Settings:
    return Settings(
        jwt_secret=SecretStr("test-secret-for-game-lifecycle-tests-only"),
        language="ru",
        redis_url=SecretStr("redis://redis:6379/0"),
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


class ApplicationLifecycleTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_lifespan_requires_a_bootstrapped_generation(self) -> None:
        application = FastAPI()
        redis_client = MagicMock()
        redis_client.ping = AsyncMock()
        redis_client.get = AsyncMock(return_value=None)
        redis_client.aclose = AsyncMock()
        mocked_engine = MagicMock()
        mocked_engine.dispose = AsyncMock()

        with (
            patch("main.settings", test_settings()),
            patch("main.check_database_connection", new_callable=AsyncMock),
            patch("main.create_redis_client", return_value=redis_client),
            patch("main.engine", mocked_engine),
        ):
            with self.assertRaises(GameGenerationUnavailable):
                async with lifespan(application):
                    self.fail("worker must not start without a generation")

        redis_client.aclose.assert_awaited_once()
        mocked_engine.dispose.assert_awaited_once()

    async def test_lifespan_exposes_session_services_for_loaded_generation(self) -> None:
        application = FastAPI()
        redis_client = MagicMock()
        redis_client.aclose = AsyncMock()
        generation = MagicMock()
        generation.generation_id = "test-generation"
        background_task = MagicMock()
        background_task.stop = AsyncMock()
        mocked_engine = MagicMock()
        mocked_engine.dispose = AsyncMock()

        with (
            patch("main.settings", test_settings()),
            patch("main.check_database_connection", new_callable=AsyncMock),
            patch("main.create_redis_client", return_value=redis_client),
            patch("main.GameGeneration.load_current", new_callable=AsyncMock, return_value=generation),
            patch("main.AsyncPeriodicTask", return_value=background_task) as task_class,
            patch("main.engine", mocked_engine),
        ):
            async with lifespan(application):
                self.assertIsInstance(application.state.game_sessions, GameSessions)
                self.assertIs(application.state.game_sessions.generation, generation)
                self.assertIsInstance(application.state.game_segments, GameSegments)
                self.assertIs(application.state.game_segments.sessions, application.state.game_sessions)
                self.assertIsInstance(application.state.game_victories, GameVictories)
                self.assertIs(application.state.game_victories.generation, generation)
                task_class.assert_called_once()
                self.assertEqual(
                    task_class.call_args.kwargs["interval"],
                    PENDING_VICTORY_RETRY_INTERVAL_SECONDS,
                )
                self.assertIs(
                    task_class.call_args.kwargs["task_func"].func,
                    retry_pending_victories,
                )
        redis_client.aclose.assert_awaited_once()
        mocked_engine.dispose.assert_awaited_once()

    async def test_retry_pending_victories_processes_queue(self) -> None:
        application = FastAPI()
        application.state.game_victories = MagicMock()
        application.state.game_victories.retry_pending = AsyncMock()
        await retry_pending_victories(application)
        application.state.game_victories.retry_pending.assert_awaited_once()

    async def test_retry_pending_victories_survives_redis_errors(self) -> None:
        for error in (StaleGameGeneration("stale"), RedisError("down")):
            with self.subTest(error=type(error).__name__):
                application = FastAPI()
                application.state.game_victories = MagicMock()
                application.state.game_victories.retry_pending = AsyncMock(side_effect=error)
                await retry_pending_victories(application)

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
