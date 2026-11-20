import importlib.util
import json
import math
import os
import random
import unittest
from pathlib import Path
from uuid import uuid4

from redis.asyncio import Redis

from app.crossword_generator import (
    CELL_WIDTH_PX,
    MIN_GAP_PX,
    CrosswordGenerationError,
    SegmentSpec,
    TwoLetterAction,
    WordCandidate,
    generate_segment_proposals,
    route_is_empty,
)
from app.game_generation import GameGeneration, bootstrap_generation
from app.game_segments import CrosswordGenerationFailed, GameSegments
from app.game_sessions import GameSessions

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"


def word(word_id: int, text: str) -> WordCandidate:
    return WordCandidate(word_id, text, f"Clue for {text}")


def keep(_rng):
    return TwoLetterAction.KEEP


def longer(_rng):
    return TwoLetterAction.LONGER


def empty(_rng):
    return TwoLetterAction.EMPTY


def texts(proposals) -> list[str]:
    return [proposal.word_text for proposal in proposals]


class LayoutAssertions(unittest.TestCase):
    def assert_valid_layout(self, spec: SegmentSpec, proposals) -> None:
        end = 0
        extras = []
        for index, proposal in enumerate(proposals):
            self.assertGreaterEqual(proposal.offset_px, 0)
            self.assertEqual(proposal.offset_px, int(proposal.offset_px))
            gap = proposal.offset_px - end
            if index:
                self.assertGreaterEqual(gap, MIN_GAP_PX)
                extras.append(gap - MIN_GAP_PX)
            else:
                extras.append(gap)
            end = proposal.offset_px + len(proposal.word_text) * CELL_WIDTH_PX
        if proposals:
            self.assertLessEqual(end, spec.available_px)
            self.assertEqual(len(set(extras)), 1, "free space must be spread evenly")
            self.assertGreaterEqual(spec.available_px - end, extras[0])


class SegmentGeometryTests(unittest.TestCase):
    def test_available_length_is_the_smaller_of_map_and_capacity(self) -> None:
        self.assertEqual(SegmentSpec(1, 14, 500).available_px, 14 * CELL_WIDTH_PX)
        self.assertEqual(SegmentSpec(1, 14, 300.9).available_px, 300)

    def test_from_cities_uses_straight_line_distance(self) -> None:
        spec = SegmentSpec.from_cities(1, 14, (779, 1405), (465, 1224))
        self.assertAlmostEqual(spec.length_px, math.dist((779, 1405), (465, 1224)))
        self.assertEqual(spec.available_px, 362)

    def test_free_space_target_depends_on_capacity(self) -> None:
        self.assertEqual(SegmentSpec(1, 31, 900).free_space_target_px, 52)
        self.assertEqual(SegmentSpec(1, 32, 900).free_space_target_px, 78)

    def test_invalid_geometry_and_words_are_rejected(self) -> None:
        for args in ((1, 0, 10), (1, 5, -1), (1, 5, float("nan"))):
            with self.assertRaises(CrosswordGenerationError):
                SegmentSpec(*args)
        with self.assertRaises(CrosswordGenerationError):
            WordCandidate(1, "007", "clue")
        with self.assertRaises(CrosswordGenerationError):
            WordCandidate(1, "word", " ")


class GeneratorTests(LayoutAssertions):
    def test_single_word_is_centered(self) -> None:
        spec = SegmentSpec(1, 10, 260)
        proposals = generate_segment_proposals(
            [spec], [word(1, "railway")], rng=random.Random(1)
        )[1]
        self.assertEqual(texts(proposals), ["railway"])
        self.assertEqual(proposals[0].offset_px, (260 - 7 * 26) // 2)
        self.assert_valid_layout(spec, proposals)

    def test_several_words_keep_gaps_and_spread_free_space_evenly(self) -> None:
        spec = SegmentSpec(1, 14, 364)
        pool = [word(1, "train"), word(2, "rail")]
        proposals = generate_segment_proposals(
            [spec], pool, rng=random.Random(3), choose_action=keep
        )[1]
        self.assertEqual(sorted(texts(proposals)), ["rail", "train"])
        self.assert_valid_layout(spec, proposals)
        # 364 - (9 cells * 26 + 5) = 125 free pixels, three equal slots of 41.
        self.assertEqual(proposals[0].offset_px, 41)

    def test_words_never_leave_the_geometric_limit(self) -> None:
        pool = [word(i, "w" * length) for i, length in enumerate(range(3, 12), 1)]
        for length_px in (40, 81, 130, 200, 333):
            spec = SegmentSpec(1, 20, length_px)
            proposals = generate_segment_proposals(
                [spec], pool, rng=random.Random(length_px), attempts=3
            )[1]
            self.assert_valid_layout(spec, proposals)

    def test_keep_action_uses_two_letter_words(self) -> None:
        spec = SegmentSpec(1, 8, 208)
        pool = [word(1, "to"), word(2, "go"), word(3, "up")]
        proposals = generate_segment_proposals(
            [spec], pool, rng=random.Random(2), choose_action=keep
        )[1]
        self.assertEqual(sorted(texts(proposals)), ["go", "to", "up"])

    def test_empty_action_leaves_remaining_space_empty(self) -> None:
        spec = SegmentSpec(1, 10, 260)
        pool = [word(1, "station"), word(2, "to")]
        for seed in range(10):
            proposals = generate_segment_proposals(
                [spec], pool, rng=random.Random(seed), choose_action=empty
            )[1]
            self.assertEqual(texts(proposals), ["station"])

    def test_keep_action_fills_the_same_space_with_the_two_letter_word(self) -> None:
        spec = SegmentSpec(1, 10, 260)
        pool = [word(1, "station"), word(2, "to")]
        proposals = generate_segment_proposals(
            [spec], pool, rng=random.Random(4), choose_action=keep
        )[1]
        self.assertEqual(sorted(texts(proposals)), ["station", "to"])

    def test_longer_action_replaces_previous_and_two_letter_words(self) -> None:
        spec = SegmentSpec(1, 7, 182)
        pool = [word(1, "cat"), word(2, "to"), word(3, "railway")]
        for seed in range(20):
            proposals = generate_segment_proposals(
                [spec], pool, rng=random.Random(seed), choose_action=longer
            )[1]
            self.assertNotIn("to", texts(proposals))
            self.assertTrue(set(texts(proposals)) <= {"cat", "railway"})

    def test_failed_replacement_falls_back_to_the_original_layout(self) -> None:
        spec = SegmentSpec(1, 8, 208)
        pool = [word(1, "to"), word(2, "go")]
        proposals = generate_segment_proposals(
            [spec], pool, rng=random.Random(5), choose_action=longer
        )[1]
        self.assertEqual(sorted(texts(proposals)), ["go", "to"])
        self.assert_valid_layout(spec, proposals)

    def test_replacement_must_fit_with_separators(self) -> None:
        spec = SegmentSpec(1, 5, 130)
        # "cat" + gap + "to" needs 135 px, so only one of them fits.
        pool = [word(1, "cat"), word(2, "to")]
        for seed in range(10):
            proposals = generate_segment_proposals(
                [spec], pool, rng=random.Random(seed), choose_action=keep
            )[1]
            self.assertEqual(len(proposals), 1)

    def test_words_are_unique_within_a_route_and_against_published_words(self) -> None:
        specs = [SegmentSpec(1, 9, 234), SegmentSpec(2, 9, 234)]
        pool = [word(1, "Station"), word(2, "railway"), word(3, "train"), word(4, "signal")]
        result = generate_segment_proposals(
            specs, pool, occupied_words=["STATION"], rng=random.Random(7), choose_action=keep
        )
        used = texts(result[1]) + texts(result[2])
        self.assertEqual(len(used), len(set(used)))
        self.assertNotIn("Station", used)

    def test_segment_without_fitting_word_stays_empty(self) -> None:
        result = generate_segment_proposals(
            [SegmentSpec(1, 6, 0), SegmentSpec(2, 6, 156)],
            [word(1, "railway")],
            rng=random.Random(1),
        )
        self.assertEqual(result, {1: [], 2: []})
        self.assertTrue(route_is_empty(result))
        self.assertFalse(route_is_empty(result, published_word_count=1))

    def test_no_segments_and_no_dictionary_words(self) -> None:
        self.assertEqual(generate_segment_proposals([], [word(1, "train")]), {})
        result = generate_segment_proposals([SegmentSpec(1, 9, 234)], [])
        self.assertEqual(result, {1: []})

    def test_fixed_seed_is_reproducible(self) -> None:
        spec = [SegmentSpec(1, 20, 520), SegmentSpec(2, 12, 312)]
        names = [
            "station", "railway", "train", "signal", "track", "engine",
            "ticket", "bridge", "tunnel", "cargo", "to", "go",
        ]
        pool = [word(index, name) for index, name in enumerate(names, 1)]
        first = generate_segment_proposals(spec, pool, rng=random.Random(11))
        second = generate_segment_proposals(spec, pool, rng=random.Random(11))
        third = generate_segment_proposals(spec, pool, rng=random.Random(12))
        self.assertEqual(first, second)
        self.assertNotEqual(first, third)

    def test_invalid_requests_are_rejected(self) -> None:
        spec = SegmentSpec(1, 9, 234)
        with self.assertRaises(CrosswordGenerationError):
            generate_segment_proposals([spec, spec], [word(1, "train")])
        with self.assertRaises(CrosswordGenerationError):
            generate_segment_proposals([spec], [word(1, "train")], attempts=0)
        with self.assertRaises(CrosswordGenerationError):
            generate_segment_proposals([spec], [word(1, "train"), word(1, "rail")])


def _load_real_catalog() -> list[WordCandidate]:
    path = MIGRATIONS / "versions" / "72fd40c93b31_merge_english_dictionary.py"
    spec = importlib.util.spec_from_file_location("english_catalog_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return [
        WordCandidate(index, entry["word_text"], entry["definition"])
        for index, entry in enumerate(module._read_catalog(), 1)
    ]


def _real_segments() -> list[SegmentSpec]:
    routes = json.loads((MIGRATIONS / "routes.json").read_text(encoding="utf-8"))["routes"]
    cities = {
        city["id"]: city
        for city in json.loads(
            (MIGRATIONS / "cities_en.json").read_text(encoding="utf-8")
        )["cities"]
    }
    return [
        SegmentSpec.from_cities(
            index,
            route["length_in_letters"],
            (cities[route["source_city_id"]]["left"], cities[route["source_city_id"]]["top"]),
            (cities[route["target_city_id"]]["left"], cities[route["target_city_id"]]["top"]),
        )
        for index, route in enumerate(routes, 1)
    ]


def legacy_layout(spec: SegmentSpec, pool, rng) -> list[WordCandidate]:
    """The MVP algorithm of confirm_game_route: one-cell separators, no reduction."""
    remaining = max(0, spec.length_in_letters - 2)
    words: list[WordCandidate] = []
    used: set[int] = set()

    def take(candidate: WordCandidate) -> None:
        nonlocal remaining
        words.append(candidate)
        used.add(candidate.id)
        remaining -= candidate.cells
        if remaining:
            remaining -= 1

    if remaining >= 3:
        long_words = [w for w in pool if w.id not in used and 3 <= w.cells <= remaining]
        if long_words:
            take(rng.choice(long_words))
    while remaining > 0:
        options = [w for w in pool if w.id not in used and w.cells <= remaining]
        if not options:
            break
        take(rng.choice(options))
    return words


class RealCatalogComparisonTests(LayoutAssertions):
    SEEDS = range(30)

    @classmethod
    def setUpClass(cls) -> None:
        cls.catalog = _load_real_catalog()
        cls.by_id = {word.id: word for word in cls.catalog}
        cls.segments = _real_segments()

    def two_letter_share(self, layouts) -> float:
        total = sum(len(words) for words in layouts)
        two = sum(1 for words in layouts for w in words if w.letters == 2)
        return two / total

    def generated(self, seed: int, attempts: int):
        proposals = generate_segment_proposals(
            self.segments, self.catalog, rng=random.Random(seed), attempts=attempts
        )
        for spec in self.segments:
            self.assert_valid_layout(spec, proposals[spec.route_id])
        return [
            [self.by_id[p.dictionary_word_id] for p in proposals[spec.route_id]]
            for spec in self.segments
        ], proposals

    def test_two_letter_share_is_lower_than_the_previous_algorithm(self) -> None:
        legacy, single, best = [], [], []
        for seed in self.SEEDS:
            rng = random.Random(seed)
            legacy += [legacy_layout(spec, self.catalog, rng) for spec in self.segments]
            single += self.generated(seed, attempts=1)[0]
            best += self.generated(seed, attempts=6)[0]
        old_share = self.two_letter_share(legacy)
        # Measured on this catalog: about 8.4%, 3.9% and 0%.
        self.assertGreater(old_share, 0.07)
        self.assertLess(self.two_letter_share(single), old_share * 0.6)
        self.assertLess(self.two_letter_share(best), 0.01)

    def test_real_routes_get_words_and_reach_the_free_space_target(self) -> None:
        reached = total = 0
        for seed in self.SEEDS:
            layouts, proposals = self.generated(seed, attempts=6)
            self.assertFalse(route_is_empty(proposals))
            for spec, words in zip(self.segments, layouts):
                self.assertTrue(words, f"segment {spec.route_id} is empty")
                free = spec.available_px - sum(w.cells for w in words) * CELL_WIDTH_PX
                free -= MIN_GAP_PX * (len(words) - 1)
                reached += free <= spec.free_space_target_px
                total += 1
        self.assertGreater(reached / total, 0.95)


@unittest.skipUnless(os.environ.get("REDIS_TEST_URL"), "Set REDIS_TEST_URL for isolated Redis tests.")
class GeneratorRedisContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.redis = Redis.from_url(os.environ["REDIS_TEST_URL"], decode_responses=True)
        self.namespace = f"railway-test:{{{uuid4()}}}"
        await bootstrap_generation(self.redis, namespace=self.namespace)
        generation = await GameGeneration.load_current(self.redis, namespace=self.namespace)
        self.sessions = GameSessions(generation)
        self.segments = GameSegments(self.sessions)

    async def asyncTearDown(self) -> None:
        async for key in self.redis.scan_iter(match=f"{self.namespace}:*"):
            await self.redis.delete(key)
        await self.redis.aclose()

    async def join(self, pair):
        user = uuid4()
        return user, await self.sessions.join(user, uuid4(), [pair])

    async def test_generated_segments_are_published_and_reused_with_same_offsets(self) -> None:
        catalog = _load_real_catalog()
        specs = [SegmentSpec(7, 14, 364), SegmentSpec(8, 12, 312)]
        first_user, first = await self.join((1, 2))
        proposals = generate_segment_proposals(specs, catalog, rng=random.Random(1))
        await self.segments.confirm_route(first_user, first.id, [7, 8], proposals)
        _, shown = await self.segments.crossword(first_user, first.id)
        self.assertEqual(
            [(p.route_id, p.offset_px) for p in shown],
            [(spec.route_id, proposal.offset_px)
             for spec in specs for proposal in proposals[spec.route_id]],
        )

        second_user, second = await self.join((3, 4))
        other = generate_segment_proposals(specs, catalog, rng=random.Random(2))
        await self.segments.confirm_route(second_user, second.id, [7, 8], other)
        _, reused = await self.segments.crossword(second_user, second.id)
        self.assertEqual([p.placement_id for p in reused], [p.placement_id for p in shown])
        self.assertEqual([p.offset_px for p in reused], [p.offset_px for p in shown])

    async def test_completely_empty_route_is_rejected_and_released(self) -> None:
        user, session = await self.join((1, 2))
        proposals = generate_segment_proposals([SegmentSpec(7, 6, 0)], [])
        self.assertTrue(route_is_empty(proposals))
        with self.assertRaises(CrosswordGenerationFailed):
            await self.segments.confirm_route(user, session.id, [7], proposals)
        replacement = await self.sessions.join(user, uuid4(), [(1, 2)])
        self.assertEqual(replacement.state, "selecting_route")


if __name__ == "__main__":
    unittest.main()
