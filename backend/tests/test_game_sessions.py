import asyncio
import multiprocessing
import os
import socket
import unittest
from concurrent.futures import ProcessPoolExecutor
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import RedisError

from app.game_generation import GameGeneration, StaleGameGeneration, bootstrap_generation
from app.game_sessions import (
    GameSessions,
    IdempotencyKeyConflict,
    JoinResultUnavailable,
    NoAvailableCityPair,
    SessionNotFound,
    SessionStorageError,
    VictoryFinishing,
)


def _process_join(url: str, namespace: str, user: str, operation: str) -> tuple[str, str]:
    async def run() -> tuple[str, str]:
        redis = Redis.from_url(url, decode_responses=True)
        try:
            generation = await GameGeneration.load_current(redis, namespace=namespace)
            service = GameSessions(generation)
            try:
                session = await service.join(
                    UUID(user), UUID(operation), [(1, 2), (2, 1), (2, 3), (3, 1)]
                )
                return str(session.id), f"{min(session.source_city_id, session.target_city_id)}:{max(session.source_city_id, session.target_city_id)}"
            except NoAvailableCityPair:
                return "no_pair", ""
        finally:
            await redis.aclose()
    return asyncio.run(run())


def _process_surrender(url: str, namespace: str, user: str, session: str, operation: str) -> str:
    async def run() -> str:
        redis = Redis.from_url(url, decode_responses=True)
        try:
            generation = await GameGeneration.load_current(redis, namespace=namespace)
            result = await GameSessions(generation).surrender(
                UUID(user), UUID(session), UUID(operation)
            )
            return "surrendered" if result is None else result.state
        finally:
            await redis.aclose()
    return asyncio.run(run())


class GameSessionUnitTests(unittest.IsolatedAsyncioTestCase):
    async def test_unavailable_redis_is_not_a_missing_session(self) -> None:
        generation = MagicMock()
        generation.key.side_effect = lambda suffix: suffix
        generation.execute = AsyncMock(side_effect=RedisConnectionError("offline"))
        with self.assertRaises(RedisConnectionError):
            await GameSessions(generation).restore(uuid4())

    async def test_invalid_city_pair_is_rejected(self) -> None:
        generation = MagicMock()
        with self.assertRaises(ValueError):
            await GameSessions(generation).join(uuid4(), uuid4(), [(2, 2)])
        with self.assertRaises(ValueError):
            await GameSessions(generation).join(uuid4(), uuid4(), [(1, 2**31)])
        generation.execute.assert_not_called()


@unittest.skipUnless(os.environ.get("REDIS_TEST_URL"), "Set REDIS_TEST_URL for isolated Redis tests.")
class GameSessionsRedisTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.url = os.environ["REDIS_TEST_URL"]
        self.redis = Redis.from_url(self.url, decode_responses=True)
        self.namespace = f"railway-test:{{{uuid4()}}}"
        await bootstrap_generation(self.redis, namespace=self.namespace)
        self.generation = await GameGeneration.load_current(self.redis, namespace=self.namespace)
        self.sessions = GameSessions(self.generation)

    async def asyncTearDown(self) -> None:
        async for key in self.redis.scan_iter(match=f"{self.namespace}:*"):
            await self.redis.delete(key)
        await self.redis.aclose()

    async def test_recovery_idempotency_surrender_and_key_conflicts(self) -> None:
        user, other = uuid4(), uuid4()
        join_key, surrender_key = uuid4(), uuid4()
        first = await self.sessions.join(user, join_key, [(1, 2)])
        restored = await self.sessions.restore(user)
        self.assertEqual(restored, first)
        self.assertEqual(first.session_score, 0)
        self.assertEqual(first.route_ids, [])
        self.assertEqual(first.hints, {})
        self.assertEqual(first.state_version, 1)
        restarted = GameSessions(await GameGeneration.load_current(self.redis, namespace=self.namespace))
        self.assertEqual(await restarted.restore(user), first)
        self.assertEqual(await restarted.join(user, uuid4(), [(3, 4)]), first)
        with self.assertRaises(NoAvailableCityPair):
            await self.sessions.join(other, uuid4(), [(2, 1)])
        with self.assertRaises(SessionNotFound):
            await self.sessions.surrender(other, first.id, uuid4())
        self.assertIsNone(await self.sessions.surrender(user, first.id, surrender_key))
        self.assertIsNone(await self.sessions.restore(user))
        replacement = await self.sessions.join(user, uuid4(), [(2, 1)])
        self.assertNotEqual(first.id, replacement.id)
        self.assertIsNone(await self.sessions.surrender(user, first.id, surrender_key))
        self.assertEqual(await self.sessions.restore(user), replacement)
        with self.assertRaises(IdempotencyKeyConflict):
            await self.sessions.surrender(user, replacement.id, surrender_key)
        with self.assertRaises(JoinResultUnavailable):
            await self.sessions.join(user, join_key, [(4, 5)])
        self.assertEqual(await self.sessions.restore(user), replacement)

    async def test_finishing_and_won_do_not_allow_surrender_or_replacement_early(self) -> None:
        user, join_key = uuid4(), uuid4()
        first = await self.sessions.join(user, join_key, [(1, 2)])
        await self.generation.execute(
            "return redis.call('HSET', KEYS[2], 'state', 'finishing', 'route_ids', '[7]', 'state_version', '2')",
            keys=[self.sessions.session_key(user)],
        )
        with self.assertRaises(VictoryFinishing):
            await self.sessions.join(user, uuid4(), [(3, 4)])
        with self.assertRaises(VictoryFinishing) as error:
            await self.sessions.surrender(user, first.id, uuid4())
        self.assertEqual(error.exception.session.id, first.id)
        self.assertEqual(error.exception.session.state, "finishing")
        restored = await self.sessions.restore(user)
        assert restored is not None
        self.assertEqual(restored.state, "finishing")
        await self.generation.execute(
            "redis.call('HDEL', KEYS[3], '1:2'); return redis.call('HSET', KEYS[2], "
            "'state', 'won', 'victory_snapshot', '{\"words\":[]}', 'state_version', '3')",
            keys=[self.sessions.session_key(user), self.sessions.city_pairs_key()],
        )
        won = await self.sessions.surrender(user, first.id, uuid4())
        assert won is not None
        self.assertEqual(won.state, "won")
        self.assertEqual(await self.sessions.join(user, join_key, [(1, 2)]), won)
        other_user = uuid4()
        other = await self.sessions.join(other_user, uuid4(), [(2, 1)])
        with self.assertRaises(NoAvailableCityPair):
            await self.sessions.join(user, uuid4(), [(1, 2)])
        self.assertEqual(await self.sessions.restore(user), won)
        await self.sessions.surrender(other_user, other.id, uuid4())
        second = await self.sessions.join(user, uuid4(), [(1, 2)])
        self.assertNotEqual(second.id, first.id)
        with self.assertRaises(JoinResultUnavailable):
            await self.sessions.join(user, join_key, [(3, 4)])

    async def test_current_round_rotates_once_for_winners_from_the_previous_round(self) -> None:
        first_user, second_user = uuid4(), uuid4()
        first = await self.sessions.join(first_user, uuid4(), [(1, 2)])
        second = await self.sessions.join(second_user, uuid4(), [(3, 4)])
        self.assertEqual(first.round_id, second.round_id)
        old_round = first.round_id

        for user, session in ((first_user, first), (second_user, second)):
            pair = f"{min(session.source_city_id, session.target_city_id)}:{max(session.source_city_id, session.target_city_id)}"
            await self.generation.execute(
                "redis.call('HDEL', KEYS[3], ARGV[2]); "
                "return redis.call('HSET', KEYS[2], 'state', 'won', 'route_ids', '[7]', "
                "'victory_snapshot', '{}')",
                keys=[self.sessions.session_key(user), self.sessions.city_pairs_key()],
                args=[str(session.id), pair],
            )

        first_next = await self.sessions.join(first_user, uuid4(), [(1, 2)])
        self.assertNotEqual(first_next.round_id, old_round)
        second_next = await self.sessions.join(second_user, uuid4(), [(3, 4)])
        self.assertEqual(second_next.round_id, first_next.round_id)
        self.assertEqual(await self.redis.get(self.sessions.current_round_key()), str(first_next.round_id))
        self.assertEqual(second_next.route_ids, [])
        self.assertIsNone(second_next.victory_snapshot)

    async def test_failed_join_after_win_does_not_change_current_round_or_snapshot(self) -> None:
        winner, blocker = uuid4(), uuid4()
        won = await self.sessions.join(winner, uuid4(), [(1, 2)])
        await self.generation.execute(
            "redis.call('HDEL', KEYS[3], '1:2'); "
            "return redis.call('HSET', KEYS[2], 'state', 'won', 'route_ids', '[7]', "
            "'victory_snapshot', '{\"preserved\":true}')",
            keys=[self.sessions.session_key(winner), self.sessions.city_pairs_key()],
        )
        await self.sessions.join(blocker, uuid4(), [(3, 4)])
        current_round = await self.redis.get(self.sessions.current_round_key())
        with self.assertRaises(NoAvailableCityPair):
            await self.sessions.join(winner, uuid4(), [(3, 4)])
        restored = await self.sessions.restore(winner)
        self.assertEqual(restored.id, won.id)
        self.assertEqual(restored.state, "won")
        self.assertEqual(restored.victory_snapshot, {"preserved": True})
        self.assertEqual(await self.redis.get(self.sessions.current_round_key()), current_round)

    async def test_playing_surrender_releases_shared_segment_only_after_last_user(self) -> None:
        users = [uuid4(), uuid4()]
        first = await self.sessions.join(users[0], uuid4(), [(1, 2)])
        second = await self.sessions.join(users[1], uuid4(), [(3, 4)])
        refs, state = self.sessions.segment_keys(first.round_id, 7)
        for user, session in zip(users, (first, second)):
            await self.generation.execute(
                "redis.call('SADD', KEYS[3], ARGV[1]); "
                "redis.call('HSET', KEYS[4], 'round_id', ARGV[2], 'value', 'shared', "
                "'owner:' .. ARGV[1], ARGV[3]); "
                "return redis.call('HSET', KEYS[2], 'state', 'playing', "
                "'route_ids', '[7]', 'hints', '{\"placement\":{\"first_letter\":\"A\"}}', "
                "'session_score', '10', 'state_version', '2')",
                keys=[self.sessions.session_key(user), refs, state],
                args=[str(session.id), str(session.round_id), str(user)],
            )
        restored = await self.sessions.restore(users[0])
        assert restored is not None
        self.assertEqual(restored.hints["placement"]["first_letter"], "A")
        self.assertEqual(restored.session_score, 10)
        await self.sessions.surrender(users[0], first.id, uuid4())
        self.assertEqual(await self.redis.hget(state, "value"), "shared")
        members = await self.generation.execute(
            "return redis.call('SMEMBERS', KEYS[2])", keys=[refs]
        )
        self.assertEqual(members, [str(second.id)])
        await self.sessions.surrender(users[1], second.id, uuid4())
        self.assertEqual(await self.redis.exists(refs, state), 0)

    async def test_missing_segment_reference_rejects_surrender_without_releasing_pair(self) -> None:
        user = uuid4()
        session = await self.sessions.join(user, uuid4(), [(1, 2)])
        await self.generation.execute(
            "return redis.call('HSET', KEYS[2], 'state', 'playing', 'route_ids', '[7]')",
            keys=[self.sessions.session_key(user)],
        )
        with self.assertRaises(SessionStorageError):
            await self.sessions.surrender(user, session.id, uuid4())
        with self.assertRaises(NoAvailableCityPair):
            await self.sessions.join(uuid4(), uuid4(), [(2, 1)])
        restored = await self.sessions.restore(user)
        assert restored is not None
        self.assertEqual(restored.id, session.id)

    async def test_surrender_retries_if_route_changes_between_read_and_script(self) -> None:
        user = uuid4()
        session = await self.sessions.join(user, uuid4(), [(1, 2)])
        refs, state = self.sessions.segment_keys(session.round_id, 7)
        original_read = self.sessions._read
        changed = False

        async def change_route_after_read(user_id):
            nonlocal changed
            result = await original_read(user_id)
            if not changed:
                changed = True
                await self.generation.execute(
                    "redis.call('SADD', KEYS[3], ARGV[1]); "
                    "redis.call('HSET', KEYS[4], 'round_id', ARGV[2], 'value', 'shared', "
                    "'owner:' .. ARGV[1], ARGV[3]); "
                    "return redis.call('HSET', KEYS[2], 'state', 'playing', "
                    "'route_ids', '[7]', 'state_version', '2')",
                    keys=[self.sessions.session_key(user), refs, state],
                    args=[str(session.id), str(session.round_id), str(user)],
                )
            return result

        with patch.object(self.sessions, "_read", side_effect=change_route_after_read):
            self.assertIsNone(await self.sessions.surrender(user, session.id, uuid4()))
        self.assertEqual(await self.redis.exists(refs, state), 0)
        self.assertIsNone(await self.sessions.restore(user))

    async def test_disconnected_redis_fails_instead_of_reporting_no_session(self) -> None:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
            disconnected = Redis(
                host="127.0.0.1", port=port, decode_responses=True,
                socket_connect_timeout=1,
            )
            try:
                service = GameSessions(GameGeneration(
                    disconnected, self.generation.generation_id, namespace=self.namespace
                ))
                with self.assertRaises(RedisError):
                    await service.restore(uuid4())
                with self.assertRaises(RedisError):
                    await service.join(uuid4(), uuid4(), [(1, 2)])
            finally:
                await disconnected.aclose()

    async def test_surrender_and_finishing_are_atomically_ordered(self) -> None:
        for _ in range(10):
            user = uuid4()
            session = await self.sessions.join(user, uuid4(), [(1, 2)])

            async def begin_finishing():
                return await self.generation.execute(
                    "if redis.call('HGET', KEYS[2], 'id') ~= ARGV[1] then return 0 end; "
                    "redis.call('HSET', KEYS[2], 'state', 'finishing', 'route_ids', '[7]', "
                    "'state_version', '2'); return 1",
                    keys=[self.sessions.session_key(user)], args=[str(session.id)],
                )

            result, finishing = await asyncio.gather(
                self.sessions.surrender(user, session.id, uuid4()),
                begin_finishing(),
                return_exceptions=True,
            )
            if finishing == 1:
                self.assertIsInstance(result, VictoryFinishing)
                restored = await self.sessions.restore(user)
                assert restored is not None
                self.assertEqual(restored.state, "finishing")
                await self.generation.execute(
                    "redis.call('HDEL', KEYS[3], '1:2'); return redis.call('DEL', KEYS[2])",
                    keys=[self.sessions.session_key(user), self.sessions.city_pairs_key()],
                )
            else:
                self.assertEqual(finishing, 0)
                self.assertIsNone(result)
                self.assertIsNone(await self.sessions.restore(user))

    async def test_generation_switch_rejects_old_worker(self) -> None:
        user = uuid4()
        await self.sessions.join(user, uuid4(), [(1, 2)])
        await bootstrap_generation(self.redis, namespace=self.namespace)
        with self.assertRaises(StaleGameGeneration):
            await self.sessions.restore(user)
        new_generation = await GameGeneration.load_current(self.redis, namespace=self.namespace)
        self.assertIsNone(await GameSessions(new_generation).restore(user))

    async def test_cross_process_unique_user_and_unordered_pairs(self) -> None:
        def run_jobs(users: list[UUID]) -> list[tuple[str, str]]:
            with ProcessPoolExecutor(max_workers=4, mp_context=multiprocessing.get_context("spawn")) as pool:
                return list(pool.map(
                    _process_join,
                    [self.url] * len(users),
                    [self.namespace] * len(users),
                    [str(user) for user in users],
                    [str(uuid4()) for _ in users],
                ))

        same_user = uuid4()
        same_results = await asyncio.to_thread(run_jobs, [same_user] * 8)
        self.assertEqual(len({result[0] for result in same_results}), 1)
        self.assertNotEqual(same_results[0][0], "no_pair")
        others = await asyncio.to_thread(run_jobs, [uuid4() for _ in range(8)])
        allocated = [result for result in others if result[0] != "no_pair"]
        self.assertEqual(len(allocated), 2)
        self.assertEqual(len({pair for _, pair in allocated} | {same_results[0][1]}), 3)

    async def test_cross_process_surrender_retry_releases_pair_once(self) -> None:
        user, operation = uuid4(), uuid4()
        session = await self.sessions.join(user, uuid4(), [(1, 2)])

        def run_jobs() -> list[str]:
            with ProcessPoolExecutor(max_workers=4, mp_context=multiprocessing.get_context("spawn")) as pool:
                return list(pool.map(
                    _process_surrender,
                    [self.url] * 8, [self.namespace] * 8, [str(user)] * 8,
                    [str(session.id)] * 8, [str(operation)] * 8,
                ))

        self.assertEqual(await asyncio.to_thread(run_jobs), ["surrendered"] * 8)
        self.assertIsNone(await self.sessions.restore(user))
        replacement = await self.sessions.join(uuid4(), uuid4(), [(2, 1)])
        self.assertNotEqual(replacement.id, session.id)


if __name__ == "__main__":
    unittest.main()
