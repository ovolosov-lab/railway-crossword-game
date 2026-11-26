import json
import os
import unittest
from unittest.mock import patch
from uuid import uuid4

from redis.asyncio import Redis

from app.game_generation import GameGeneration, bootstrap_generation
from app.game_segments import GameSegments, WordProposal
from app.game_sessions import GameSessions, SessionNotFound, SessionStorageError


@unittest.skipUnless(os.environ.get("REDIS_TEST_URL"), "Set REDIS_TEST_URL for isolated Redis tests.")
class VictorySnapshotRedisTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.redis = Redis.from_url(os.environ["REDIS_TEST_URL"], decode_responses=True)
        self.namespace = f"railway-test:{{{uuid4()}}}"
        await bootstrap_generation(self.redis, namespace=self.namespace)
        self.generation = await GameGeneration.load_current(self.redis, namespace=self.namespace)
        self.sessions = GameSessions(self.generation)
        self.segments = GameSegments(self.sessions)
        self.user = uuid4()
        self.placement = uuid4()
        session = await self.sessions.join(self.user, uuid4(), [(1, 2)])
        self.session_id = session.id
        await self.segments.confirm_route(
            self.user,
            self.session_id,
            [10],
            {10: [WordProposal(
                placement_id=self.placement,
                dictionary_word_id=1,
                word_text="Station",
                definition="Railway stop",
                offset_px=12,
            )]},
        )
        result = await self.segments.guess(self.user, self.session_id, self.placement, "station")
        self.assertEqual(result.session.state, "finishing")

    async def asyncTearDown(self) -> None:
        async for key in self.redis.scan_iter(match=f"{self.namespace}:*"):
            await self.redis.delete(key)
        await self.redis.aclose()

    async def publish_and_release(self) -> None:
        # Simulate the future publisher; task 36's database transaction is not tested here.
        session = await self.sessions.restore(self.user)
        assert session is not None
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.hset(self.sessions.session_key(self.user), "state", "won")
            pipe.delete(*self.sessions.segment_keys(session.round_id, 10))
            await pipe.execute()

    async def test_won_crossword_uses_snapshot_after_segment_release(self) -> None:
        await self.publish_and_release()
        session, placements = await self.segments.crossword(self.user, self.session_id)
        self.assertEqual(session.state, "won")
        self.assertEqual(len(placements), 1)
        self.assertEqual(placements[0].placement_id, self.placement)
        self.assertEqual(placements[0].word_text, "Station")
        self.assertTrue(placements[0].guessed_by_me)
        with self.assertRaises(SessionNotFound):
            await self.segments.crossword(uuid4(), self.session_id)

    async def test_switch_to_won_during_read_uses_snapshot(self) -> None:
        from app.game_segments import _READ_SEGMENTS

        execute = self.generation.execute

        async def transition(script, **kwargs):
            if script == _READ_SEGMENTS:
                await self.publish_and_release()
            return await execute(script, **kwargs)

        with patch.object(self.generation, "execute", side_effect=transition):
            session, placements = await self.segments.crossword(self.user, self.session_id)
        self.assertEqual(session.state, "won")
        self.assertEqual(placements[0].word_text, "Station")

    async def test_crossing_geometry_survives_victory_snapshot(self) -> None:
        user = uuid4()
        session = await self.sessions.join(user, uuid4(), [(1, 2)])
        crossing_id = uuid4()
        along_id, perpendicular_id = uuid4(), uuid4()
        await self.segments.confirm_route(
            user,
            session.id,
            [11],
            {11: [
                WordProposal(
                    placement_id=along_id, dictionary_word_id=1, word_text="train",
                    definition="Rail vehicle", offset_px=90,
                    crossing_id=crossing_id, crossing_index=2,
                ),
                WordProposal(
                    placement_id=perpendicular_id, dictionary_word_id=2, word_text="plane",
                    definition="Aircraft", offset_px=142,
                    orientation="perpendicular", y_offset_px=-52,
                    crossing_id=crossing_id, crossing_index=2,
                ),
            ]},
        )
        await self.segments.guess(user, session.id, along_id, "train")
        result = await self.segments.guess(user, session.id, perpendicular_id, "plane")
        self.assertEqual(result.session.state, "finishing")
        await self.redis.hset(self.sessions.session_key(user), "state", "won")
        await self.redis.delete(*self.sessions.segment_keys(session.round_id, 11))

        _, placements = await self.segments.crossword(user, session.id)
        by_id = {placement.placement_id: placement for placement in placements}
        self.assertEqual(by_id[along_id].crossing_id, crossing_id)
        self.assertEqual(by_id[perpendicular_id].orientation, "perpendicular")
        self.assertEqual(by_id[perpendicular_id].y_offset_px, -52)

    async def test_invalid_snapshot_is_rejected(self) -> None:
        await self.publish_and_release()
        key = self.sessions.session_key(self.user)
        raw = await self.generation.execute(
            "return redis.call('HGET', KEYS[2], 'victory_snapshot')", keys=[key]
        )
        self.assertIsInstance(raw, str)
        if not isinstance(raw, str):
            raise AssertionError("The victory snapshot is missing.")
        snapshot = json.loads(raw)
        for field, value in (
            ("user_id", str(uuid4())),
            ("route_ids", [11]),
            ("finished_at_unix", "not-a-timestamp"),
        ):
            with self.subTest(field=field):
                corrupt = {**snapshot, field: value}
                await self.generation.execute(
                    "return redis.call('HSET', KEYS[2], 'victory_snapshot', ARGV[1])",
                    keys=[key],
                    args=[json.dumps(corrupt)],
                )
                with self.assertRaisesRegex(SessionStorageError, "Invalid victory snapshot"):
                    await self.segments.crossword(self.user, self.session_id)
