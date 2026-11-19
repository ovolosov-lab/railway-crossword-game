import asyncio
import os
import unittest
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError

from app.game_generation import (
    GameGeneration,
    GameGenerationUnavailable,
    GenerationBootstrapInProgress,
    StaleGameGeneration,
    bootstrap_generation,
    cleanup_generation_keys,
)


class GameGenerationLoadingTests(unittest.IsolatedAsyncioTestCase):
    async def test_loading_requires_an_existing_generation(self) -> None:
        redis = MagicMock()
        redis.ping = AsyncMock()
        redis.get = AsyncMock(return_value=None)

        with self.assertRaisesRegex(GameGenerationUnavailable, "bootstrap step"):
            await GameGeneration.load_current(redis, namespace="test:{game}")

        redis.ping.assert_awaited_once()

    async def test_loading_surfaces_redis_unavailability(self) -> None:
        redis = MagicMock()
        redis.ping = AsyncMock(side_effect=RedisConnectionError("offline"))
        redis.get = AsyncMock()

        with self.assertRaises(RedisConnectionError):
            await GameGeneration.load_current(redis, namespace="test:{game}")

        redis.get.assert_not_awaited()

    async def test_game_operations_reject_keys_outside_the_generation_namespace(
        self,
    ) -> None:
        redis = MagicMock()
        generation = GameGeneration(redis, str(uuid4()), namespace="test:{game}")

        with self.assertRaisesRegex(ValueError, "worker generation"):
            await generation.execute(
                "return redis.call('SET', KEYS[2], ARGV[1])",
                keys=["foreign:game:key"],
                args=["value"],
            )

        redis.eval.assert_not_called()


@unittest.skipUnless(
    os.environ.get("REDIS_TEST_URL"),
    "Set REDIS_TEST_URL to run Redis integration tests.",
)
class GameGenerationRedisIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        redis_url = os.environ["REDIS_TEST_URL"]
        self.redis = Redis.from_url(redis_url, decode_responses=True)
        await self.redis.ping()
        self.namespace = f"railway-test:{{{uuid4()}}}"

    async def asyncTearDown(self) -> None:
        async for key in self.redis.scan_iter(match=f"{self.namespace}:*"):
            await self.redis.delete(key)
        await self.redis.aclose()

    async def test_bootstrap_and_worker_restart_fence_old_operations(self) -> None:
        first_generation = await bootstrap_generation(
            self.redis,
            namespace=self.namespace,
        )
        first_worker = await GameGeneration.load_current(
            self.redis,
            namespace=self.namespace,
        )
        key_from_first_generation = first_worker.key("session:one")
        await first_worker.execute(
            "return redis.call('SET', KEYS[2], ARGV[1])",
            keys=[key_from_first_generation],
            args=["before-restart"],
        )

        restarted_worker = await GameGeneration.load_current(
            self.redis,
            namespace=self.namespace,
        )
        self.assertEqual(restarted_worker.generation_id, first_generation)

        unrelated_key = f"{self.namespace}:unrelated"
        await self.redis.set(unrelated_key, "preserve")
        second_generation = await bootstrap_generation(
            self.redis,
            namespace=self.namespace,
        )
        self.assertNotEqual(second_generation, first_generation)

        with self.assertRaises(StaleGameGeneration):
            await first_worker.execute(
                "return redis.call('SET', KEYS[2], ARGV[1])",
                keys=[key_from_first_generation],
                args=["must-not-write"],
            )

        self.assertIsNone(await self.redis.get(key_from_first_generation))
        self.assertEqual(await self.redis.get(unrelated_key), "preserve")

        current_worker = await GameGeneration.load_current(
            self.redis,
            namespace=self.namespace,
        )
        self.assertEqual(current_worker.generation_id, second_generation)
        await current_worker.assert_current()

    async def test_concurrent_old_operation_cannot_survive_generation_switch(self) -> None:
        await bootstrap_generation(self.redis, namespace=self.namespace)
        old_worker = await GameGeneration.load_current(
            self.redis,
            namespace=self.namespace,
        )
        old_key = old_worker.key("operation:delayed")

        async def old_operation() -> object:
            return await old_worker.execute(
                "return redis.call('SET', KEYS[2], ARGV[1])",
                keys=[old_key],
                args=["stale"],
            )

        operation_result, new_generation = await asyncio.gather(
            old_operation(),
            bootstrap_generation(self.redis, namespace=self.namespace),
            return_exceptions=True,
        )

        self.assertIsInstance(new_generation, str)
        self.assertTrue(
            operation_result == "OK"
            or isinstance(operation_result, StaleGameGeneration)
        )
        self.assertIsNone(await self.redis.get(old_key))

    async def test_bootstrap_lock_prevents_concurrent_generation_switch(self) -> None:
        lock_key = f"{self.namespace}:bootstrap_lock"
        await self.redis.set(lock_key, "held", ex=60)

        with self.assertRaises(GenerationBootstrapInProgress):
            await bootstrap_generation(self.redis, namespace=self.namespace)

    async def test_cleanup_is_limited_to_the_previous_generation_namespace(self) -> None:
        generation_id = str(uuid4())
        prefix = f"{self.namespace}:generation:{generation_id}:"
        for index in range(3):
            await self.redis.set(f"{prefix}key:{index}", "value")
        unrelated_key = f"{self.namespace}:other"
        await self.redis.set(unrelated_key, "preserve")

        deleted = await cleanup_generation_keys(
            self.redis,
            namespace=self.namespace,
            generation_id=generation_id,
            max_keys=2,
        )

        self.assertEqual(deleted, 2)
        remaining_keys = [
            key async for key in self.redis.scan_iter(match=f"{prefix}*")
        ]
        self.assertEqual(len(remaining_keys), 1)
        self.assertEqual(await self.redis.get(unrelated_key), "preserve")


if __name__ == "__main__":
    unittest.main()
