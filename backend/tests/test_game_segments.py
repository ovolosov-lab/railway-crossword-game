import asyncio
import multiprocessing
import os
import unittest
from concurrent.futures import ProcessPoolExecutor
from uuid import UUID, uuid4

from redis.asyncio import Redis

from app.game_generation import GameGeneration, StaleGameGeneration, bootstrap_generation
from app.game_segments import (
    CrosswordGenerationFailed,
    GameSegments,
    InsufficientPoints,
    RouteAlreadyConfirmed,
    RouteAlreadySolved,
    WordProposal,
    normalize_answer,
)
from app.game_sessions import GameSessions, SessionStorageError, VictoryFinishing


def _process_action(url, namespace, user, session, placement, action):
    async def run():
        redis = Redis.from_url(url, decode_responses=True)
        try:
            generation = await GameGeneration.load_current(redis, namespace=namespace)
            service = GameSegments(GameSessions(generation))
            if action == "guess":
                result = await service.guess(UUID(user), UUID(session), UUID(placement), "нью йорк")
                return result.result, result.points_awarded
            if action == "surrender":
                try:
                    await service.sessions.surrender(UUID(user), UUID(session), uuid4())
                    return "surrendered", 0
                except VictoryFinishing:
                    return "finishing", 0
            if action == "confirm":
                try:
                    await service.confirm_route(UUID(user), UUID(session), [7], {})
                    return "confirmed", 0
                except RouteAlreadySolved:
                    return "solved", 0
            result = await service.hint(UUID(user), UUID(session), UUID(placement), "first_letter", 20)
            return result.value, result.points_charged
        finally:
            await redis.aclose()
    return asyncio.run(run())


def proposal(word: str, definition: str = "clue") -> WordProposal:
    return WordProposal(
        placement_id=uuid4(), dictionary_word_id=1,
        word_text=word, definition=definition, offset_px=10,
    )


class SharedWordUnitTests(unittest.TestCase):
    def test_normalization_and_unicode_letter_scoring(self) -> None:
        self.assertEqual(normalize_answer(" НЬЮ\u00a0— ЙОРК "), "ньюйорк")
        self.assertEqual(normalize_answer("е\u0308"), "е")
        self.assertNotEqual(normalize_answer("a.b"), normalize_answer("ab"))
        self.assertNotEqual(normalize_answer("a\u001cb"), normalize_answer("ab"))
        entry = proposal("Нью-Йорк").stored()
        self.assertEqual(entry["letter_count"], 8)
        self.assertEqual(entry["points"], 70)
        with self.assertRaises(ValueError):
            proposal("007")


@unittest.skipUnless(os.environ.get("REDIS_TEST_URL"), "Set REDIS_TEST_URL for isolated Redis tests.")
class SharedWordRedisTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.url = os.environ["REDIS_TEST_URL"]
        self.redis = Redis.from_url(self.url, decode_responses=True)
        self.namespace = f"railway-test:{{{uuid4()}}}"
        await bootstrap_generation(self.redis, namespace=self.namespace)
        self.generation = await GameGeneration.load_current(self.redis, namespace=self.namespace)
        self.sessions = GameSessions(self.generation)
        self.game = GameSegments(self.sessions)

    async def asyncTearDown(self) -> None:
        async for key in self.redis.scan_iter(match=f"{self.namespace}:*"):
            await self.redis.delete(key)
        await self.redis.aclose()

    async def join(self, pair):
        user = uuid4()
        session = await self.sessions.join(user, uuid4(), [pair])
        return user, session

    async def parallel_actions(self, actions):
        def run():
            with ProcessPoolExecutor(max_workers=4, mp_context=multiprocessing.get_context("spawn")) as pool:
                return list(pool.map(_process_action, *zip(*actions)))
        return await asyncio.to_thread(run)

    async def test_shared_segments_reuse_assignments_and_last_surrender_releases(self) -> None:
        user1, first = await self.join((1, 2))
        user2, second = await self.join((3, 4))
        common, other = proposal("train"), proposal("rail")
        replacement = proposal("different")
        one = await self.game.confirm_route(user1, first.id, [7, 8], {7: [common], 8: [other]})
        two = await self.game.confirm_route(user2, second.id, [7], {7: [replacement]})
        self.assertEqual(one.state, "playing")
        self.assertEqual(two.state, "playing")
        _, shown1 = await self.game.crossword(user1, first.id)
        _, shown2 = await self.game.crossword(user2, second.id)
        self.assertEqual(shown1[0].placement_id, common.placement_id)
        self.assertEqual(shown2[0].placement_id, common.placement_id)
        self.assertNotIn("word_text", shown2[0].model_dump(exclude_none=True))
        incorrect = await self.game.guess(user1, first.id, common.placement_id, "wrong")
        self.assertEqual(incorrect.result, "incorrect")
        self.assertNotIn("revealed_word", incorrect.model_dump(exclude_none=True))
        self.assertEqual(incorrect.session.state_version, one.state_version)
        retried = await self.game.confirm_route(user1, first.id, [7, 8], {})
        self.assertEqual(retried.state_version, one.state_version)
        with self.assertRaises(RouteAlreadyConfirmed):
            await self.game.confirm_route(user1, first.id, [8], {})
        refs, state = self.sessions.segment_keys(first.round_id, 7)
        await self.sessions.surrender(user1, first.id, uuid4())
        self.assertEqual(await self.redis.exists(refs, state), 2)
        owner = await self.generation.execute(
            "return redis.call('HGET', KEYS[2], ARGV[1])",
            keys=[state], args=[f"owner:{first.id}"],
        )
        self.assertIsNone(owner)
        await self.sessions.surrender(user2, second.id, uuid4())
        self.assertEqual(await self.redis.exists(refs, state), 0)

    async def test_concurrent_process_guesses_award_once_and_update_both_versions(self) -> None:
        u1, s1 = await self.join((1, 2))
        u2, s2 = await self.join((3, 4))
        word = proposal("Нью-Йорк")
        await self.game.confirm_route(u1, s1.id, [7, 8], {7: [word], 8: [proposal("other")]})
        await self.game.confirm_route(u2, s2.id, [7, 9], {9: [proposal("extra")]})
        before1 = await self.sessions.restore(u1)
        before2 = await self.sessions.restore(u2)
        assert before1 is not None and before2 is not None
        actions = [
            (self.url, self.namespace, str(u1 if i % 2 == 0 else u2),
             str(s1.id if i % 2 == 0 else s2.id), str(word.placement_id), "guess")
            for i in range(8)
        ]
        results = await self.parallel_actions(actions)
        self.assertEqual(sum(points for _, points in results), 70)
        self.assertEqual([kind for kind, _ in results].count("correct"), 1)
        self.assertEqual([kind for kind, _ in results].count("already_solved"), 7)
        after1 = await self.sessions.restore(u1)
        after2 = await self.sessions.restore(u2)
        assert after1 is not None and after2 is not None
        self.assertEqual(after1.session_score + after2.session_score, 70)
        self.assertEqual(after1.state_version, before1.state_version + 1)
        self.assertEqual(after2.state_version, before2.state_version + 1)
        _, words = await self.game.crossword(u1, s1.id)
        self.assertEqual(words[0].word_text, "Нью-Йорк")
        self.assertTrue(words[0].is_guessed)

    async def test_hint_races_repeat_before_balance_and_after_shared_guess(self) -> None:
        u1, s1 = await self.join((1, 2))
        u2, s2 = await self.join((3, 4))
        target, earned, remaining = proposal("Нью-Йорк"), proposal("cat"), proposal("extra")
        await self.game.confirm_route(u1, s1.id, [7, 8], {7: [target], 8: [earned, remaining]})
        await self.game.confirm_route(u2, s2.id, [7, 9], {9: [proposal("other")]})
        await self.game.guess(u1, s1.id, earned.placement_id, "CAT")
        actions = [(self.url, self.namespace, str(u1), str(s1.id), str(target.placement_id), "hint")] * 8
        results = await self.parallel_actions(actions)
        self.assertEqual(sum(points for _, points in results), 20)
        self.assertEqual({value for value, _ in results}, {"Н"})
        with self.assertRaises(InsufficientPoints):
            await self.game.hint(u1, s1.id, target.placement_id, "last_letter", 20)
        await self.game.guess(u2, s2.id, target.placement_id, "Нью-Йорк")
        retry = await self.game.hint(u1, s1.id, target.placement_id, "first_letter", 100)
        self.assertEqual((retry.value, retry.points_charged), ("Н", 0))
        unavailable = await self.game.hint(u1, s1.id, target.placement_id, "last_letter", 20)
        self.assertEqual(unavailable.hint_type, "unknown")

    async def test_one_guess_atomically_queues_all_completed_sessions(self) -> None:
        u1, s1 = await self.join((1, 2))
        u2, s2 = await self.join((3, 4))
        word = proposal("cat")
        await self.game.confirm_route(u1, s1.id, [7], {7: [word]})
        await self.game.confirm_route(u2, s2.id, [7], {})
        result = await self.game.guess(u1, s1.id, word.placement_id, "cat")
        self.assertEqual(result.session.state, "finishing")
        other = await self.sessions.restore(u2)
        assert other is not None
        self.assertEqual(other.state, "finishing")
        self.assertEqual((result.session.session_score, other.session_score), (30, 0))
        self.assertEqual(other.victory_snapshot["session_id"], str(s2.id))
        self.assertEqual(other.victory_snapshot["segments"][0]["words"][0]["word_text"], "cat")
        pending = await self.generation.execute(
            "return redis.call('HGETALL', KEYS[2])", keys=[self.game.pending_victories_key()]
        )
        self.assertEqual(dict(zip(pending[::2], pending[1::2])), {str(s1.id): str(u1), str(s2.id): str(u2)})
        restarted = GameSessions(await GameGeneration.load_current(self.redis, namespace=self.namespace))
        recovered = await restarted.restore(u2)
        assert recovered is not None
        self.assertEqual(recovered.victory_snapshot, other.victory_snapshot)

    async def test_completion_snapshots_include_empty_and_previously_solved_segments(self) -> None:
        u1, s1 = await self.join((1, 2))
        u2, s2 = await self.join((3, 4))
        shared, earlier = proposal("Нью-Йорк"), proposal("cat")
        await self.game.confirm_route(
            u1, s1.id, [7, 8, 9], {7: [shared], 8: [earlier], 9: []}
        )
        await self.game.guess(u1, s1.id, earlier.placement_id, "cat")
        await self.game.confirm_route(u2, s2.id, [7], {})
        result = await self.game.guess(u2, s2.id, shared.placement_id, "Нью-Йорк")
        self.assertEqual(result.session.state, "finishing")
        first = await self.sessions.restore(u1)
        assert first is not None and first.victory_snapshot is not None
        self.assertEqual(first.state, "finishing")
        self.assertEqual(first.session_score, 30)
        segments = first.victory_snapshot["segments"]
        self.assertEqual(segments[2]["words"], [])
        self.assertEqual(segments[1]["words"][0]["word_text"], "cat")
        self.assertGreaterEqual(
            float(first.victory_snapshot["finished_at_unix"]),
            first.started_at.timestamp(),
        )

    async def test_cross_process_guess_and_surrender_do_not_lose_completion(self) -> None:
        u1, s1 = await self.join((1, 2))
        u2, s2 = await self.join((3, 4))
        word = proposal("Нью-Йорк")
        await self.game.confirm_route(u1, s1.id, [7], {7: [word]})
        await self.game.confirm_route(u2, s2.id, [7], {})
        results = await self.parallel_actions([
            (self.url, self.namespace, str(u1), str(s1.id), str(word.placement_id), "guess"),
            (self.url, self.namespace, str(u2), str(s2.id), str(word.placement_id), "surrender"),
        ])
        self.assertEqual(results[0], ("correct", 70))
        other = await self.sessions.restore(u2)
        if results[1][0] == "finishing":
            assert other is not None
            self.assertEqual(other.state, "finishing")
        else:
            self.assertEqual(results[1][0], "surrendered")
            self.assertIsNone(other)
        pending = await self.generation.execute(
            "return redis.call('HGETALL', KEYS[2])", keys=[self.game.pending_victories_key()]
        )
        pending_map = dict(zip(pending[::2], pending[1::2]))
        self.assertEqual(set(pending_map), {str(s1.id), str(s2.id)} if other else {str(s1.id)})

    async def test_cross_process_attach_and_guess_cannot_miss_victory(self) -> None:
        u1, s1 = await self.join((1, 2))
        u2, s2 = await self.join((3, 4))
        word = proposal("Нью-Йорк")
        await self.game.confirm_route(u1, s1.id, [7], {7: [word]})
        results = await self.parallel_actions([
            (self.url, self.namespace, str(u1), str(s1.id), str(word.placement_id), "guess"),
            (self.url, self.namespace, str(u2), str(s2.id), str(word.placement_id), "confirm"),
        ])
        self.assertEqual(results[0], ("correct", 70))
        other = await self.sessions.restore(u2)
        assert other is not None
        if results[1][0] == "confirmed":
            self.assertEqual(other.state, "finishing")
        else:
            self.assertEqual(results[1][0], "solved")
            self.assertEqual(other.state, "selecting_route")

    async def test_cross_process_hint_and_guess_are_atomically_ordered(self) -> None:
        user, session = await self.join((1, 2))
        target, earned, remaining = proposal("Нью-Йорк"), proposal("cat"), proposal("other")
        await self.game.confirm_route(user, session.id, [7], {7: [target, earned, remaining]})
        await self.game.guess(user, session.id, earned.placement_id, "cat")
        results = await self.parallel_actions([
            (self.url, self.namespace, str(user), str(session.id), str(target.placement_id), "guess"),
            (self.url, self.namespace, str(user), str(session.id), str(target.placement_id), "hint"),
        ])
        self.assertEqual(results[0], ("correct", 70))
        self.assertIn(results[1], [("Н", 20), (None, 0)])
        restored = await self.sessions.restore(user)
        assert restored is not None
        self.assertEqual(restored.session_score, 100 - results[1][1])

    async def test_empty_and_already_solved_routes_do_not_create_partial_state(self) -> None:
        user, session = await self.join((1, 2))
        with self.assertRaises(CrosswordGenerationFailed):
            await self.game.confirm_route(user, session.id, [7], {7: []})
        self.assertIsNone(await self.sessions.restore(user))
        other, active = await self.join((3, 4))
        word = proposal("cat")
        await self.game.confirm_route(other, active.id, [7], {7: [word]})
        await self.game.guess(other, active.id, word.placement_id, "cat")
        new_user, new = await self.join((1, 2))
        with self.assertRaises(RouteAlreadySolved):
            await self.game.confirm_route(new_user, new.id, [7], {})
        restored = await self.sessions.restore(new_user)
        assert restored is not None
        self.assertEqual(restored.state, "selecting_route")

    async def test_corrupt_session_prevents_partial_route_publication(self) -> None:
        user, session = await self.join((1, 2))
        await self.generation.execute(
            "return redis.call('HSET', KEYS[2], 'state_version', 'invalid')",
            keys=[self.sessions.session_key(user)],
        )
        with self.assertRaises(SessionStorageError):
            await self.game.confirm_route(user, session.id, [7], {7: [proposal("cat")]})
        refs, state = self.sessions.segment_keys(session.round_id, 7)
        self.assertEqual(await self.redis.exists(refs, state), 0)
        result = await self.generation.execute(
            "return redis.call('HGET', KEYS[2], 'state')", keys=[self.sessions.session_key(user)]
        )
        self.assertEqual(result, "selecting_route")

    async def test_stale_generation_cannot_read_or_change_segments(self) -> None:
        user, session = await self.join((1, 2))
        word = proposal("cat")
        await self.game.confirm_route(user, session.id, [7], {7: [word]})
        await bootstrap_generation(self.redis, namespace=self.namespace)
        with self.assertRaises(StaleGameGeneration):
            await self.game.guess(user, session.id, word.placement_id, "cat")


if __name__ == "__main__":
    unittest.main()
