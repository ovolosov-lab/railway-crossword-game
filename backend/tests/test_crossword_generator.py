import importlib.util
import json
import math
import os
import random
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from loguru import logger
from redis.asyncio import Redis

import app.crossword_generator as crossword_generator
from app.crossword_generator import (
    CELL_WIDTH_PX,
    CITY_CLEARANCE_PX,
    MIN_GAP_PX,
    CrosswordGenerationError,
    SegmentSpec,
    ShortWordAction,
    WordCandidate,
    _score,
    _crossing_geometry_is_clear,
    _cell,
    _first_internal_match,
    _neighbor_angle_allows,
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
    return ShortWordAction.KEEP


def longer(_rng):
    return ShortWordAction.LONGER


def empty(_rng):
    return ShortWordAction.EMPTY


class FirstChoiceRandom(random.Random):
    def choice(self, values):
        return values[0]


def texts(proposals) -> list[str]:
    return [proposal.word_text for proposal in proposals]


class LayoutAssertions(unittest.TestCase):
    def assert_valid_layout(self, spec: SegmentSpec, proposals) -> None:
        along = [proposal for proposal in proposals if proposal.orientation == "along"]
        end = 0
        extras = []
        for index, proposal in enumerate(along):
            left = proposal.offset_px - CELL_WIDTH_PX / 2
            right = proposal.offset_px + (len(proposal.word_text) - 1) * CELL_WIDTH_PX + CELL_WIDTH_PX / 2
            self.assertGreaterEqual(left, CITY_CLEARANCE_PX)
            gap = left - end
            if index:
                self.assertGreaterEqual(gap, MIN_GAP_PX)
                extras.append(gap - MIN_GAP_PX)
            else:
                extras.append(gap - CITY_CLEARANCE_PX)
            end = right
        if along:
            trailing_space = spec.available_px - end
            self.assertGreaterEqual(trailing_space, CITY_CLEARANCE_PX)
            extras.append(trailing_space - CITY_CLEARANCE_PX)
            self.assertLessEqual(max(extras) - min(extras), 1, "free space must be spread evenly")


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
    def test_score_always_penalizes_one_letter_words_but_optionally_two(self) -> None:
        spec = SegmentSpec(1, 12, 312)
        one_letter = word(1, "a")
        two_letters = word(2, "to")

        without_two_letter_penalty = _score(
            [two_letters], spec, penalize_two_letter_words=False
        )
        with_two_letter_penalty = _score(
            [two_letters], spec, penalize_two_letter_words=True
        )
        one_letter_score = _score(
            [one_letter], spec, penalize_two_letter_words=False
        )

        self.assertEqual(without_two_letter_penalty[1:3], (0, 0))
        self.assertEqual(with_two_letter_penalty[1:3], (0, 1))
        self.assertEqual(one_letter_score[1:3], (1, 0))

    def test_single_word_is_centered(self) -> None:
        spec = SegmentSpec(1, 10, 260)
        proposals = generate_segment_proposals(
            [spec], [word(1, "railway")], rng=random.Random(1)
        )[1]
        self.assertEqual(texts(proposals), ["railway"])
        self.assertEqual(proposals[0].offset_px, (260 - 7 * 26) // 2 + 13)
        self.assert_valid_layout(spec, proposals)

    def test_several_words_keep_gaps_and_spread_free_space_evenly(self) -> None:
        spec = SegmentSpec(1, 14, 364)
        pool = [word(1, "train"), word(2, "rail")]
        proposals = generate_segment_proposals(
            [spec], pool, rng=random.Random(3), choose_action=keep
        )[1]
        self.assertEqual(sorted(texts(proposals)), ["rail", "train"])
        self.assert_valid_layout(spec, proposals)
        # Edge clearance and the word gap are reserved before spreading 73 px across three slots.
        self.assertEqual(proposals[0].offset_px, 64)

    def test_words_never_leave_the_geometric_limit(self) -> None:
        pool = [word(i, "w" * length) for i, length in enumerate(range(3, 12), 1)]
        for length_px in (40, 81, 130, 200, 333):
            spec = SegmentSpec(1, 20, length_px)
            proposals = generate_segment_proposals(
                [spec], pool, rng=random.Random(length_px), attempts=3
            )[1]
            self.assert_valid_layout(spec, proposals)

    def test_keep_action_uses_two_letter_words(self) -> None:
        spec = SegmentSpec(1, 9, 234)
        pool = [word(1, "to"), word(2, "go"), word(3, "up")]
        proposals = generate_segment_proposals(
            [spec], pool, rng=random.Random(2), choose_action=keep
        )[1]
        self.assertEqual(sorted(texts(proposals)), ["go", "to", "up"])

    def test_keep_action_also_uses_one_letter_words(self) -> None:
        spec = SegmentSpec(1, 5, 130)
        pool = [word(1, "I"), word(2, "a")]
        proposals = generate_segment_proposals(
            [spec], pool, rng=random.Random(2), choose_action=keep
        )[1]
        self.assertEqual(sorted(texts(proposals)), ["I", "a"])

    def test_empty_action_leaves_remaining_space_empty(self) -> None:
        spec = SegmentSpec(1, 10, 260)
        pool = [word(1, "station"), word(2, "to")]
        for seed in range(10):
            proposals = generate_segment_proposals(
                [spec], pool, rng=random.Random(seed), choose_action=empty
            )[1]
            self.assertEqual(texts(proposals), ["station"])

    def test_empty_action_does_not_empty_a_segment_for_a_one_letter_word(self) -> None:
        spec = SegmentSpec(1, 10, 260)
        pool = [word(1, "station"), word(2, "I")]
        for seed in range(10):
            proposals = generate_segment_proposals(
                [spec], pool, rng=random.Random(seed), choose_action=empty
            )[1]
            self.assertEqual(texts(proposals), ["station"])

    def test_keep_action_fills_the_same_space_with_the_two_letter_word(self) -> None:
        spec = SegmentSpec(1, 12, 312)
        pool = [word(1, "station"), word(2, "to")]
        proposals = generate_segment_proposals(
            [spec], pool, rng=random.Random(4), choose_action=keep
        )[1]
        self.assertEqual(sorted(texts(proposals)), ["station", "to"])

    def test_longer_action_replaces_previous_and_two_letter_words(self) -> None:
        spec = SegmentSpec(1, 9, 234)
        pool = [word(1, "cat"), word(2, "to"), word(3, "railway")]
        for seed in range(20):
            proposals = generate_segment_proposals(
                [spec], pool, rng=random.Random(seed), choose_action=longer
            )[1]
            self.assertNotIn("to", texts(proposals))
            self.assertTrue(set(texts(proposals)) <= {"cat", "railway"})

    def test_longer_action_replaces_previous_and_one_letter_words(self) -> None:
        spec = SegmentSpec(1, 9, 234)
        pool = [word(1, "cat"), word(2, "I"), word(3, "railway")]
        for seed in range(20):
            proposals = generate_segment_proposals(
                [spec], pool, rng=random.Random(seed), choose_action=longer
            )[1]
            self.assertNotIn("I", texts(proposals))
            self.assertTrue(set(texts(proposals)) <= {"cat", "railway"})

    def test_failed_replacement_falls_back_to_the_original_layout(self) -> None:
        spec = SegmentSpec(1, 8, 208)
        pool = [word(1, "to"), word(2, "go")]
        proposals = generate_segment_proposals(
            [spec], pool, rng=random.Random(5), choose_action=longer
        )[1]
        self.assertEqual(sorted(texts(proposals)), ["go", "to"])
        self.assert_valid_layout(spec, proposals)

    def test_failed_replacement_falls_back_to_a_one_letter_word(self) -> None:
        spec = SegmentSpec(1, 5, 130)
        pool = [word(1, "I"), word(2, "a")]
        proposals = generate_segment_proposals(
            [spec], pool, rng=random.Random(5), choose_action=longer
        )[1]
        self.assertEqual(sorted(texts(proposals)), ["I", "a"])
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


class OptionalCrossingTests(unittest.TestCase):
    def test_first_internal_match_scans_anchor_then_candidate(self) -> None:
        self.assertEqual(_first_internal_match("banana", "ananas"), (1, 2))
        self.assertIsNone(_first_internal_match("train", "to"))

    def test_candidate_is_perpendicular_and_does_not_consume_along_capacity(self) -> None:
        spec = SegmentSpec.from_cities(1, 18, (0, 0), (468, 0))
        pool = [word(1, "train"), word(2, "plane"), word(3, "sword")]
        crossing_candidate = word(4, "trace")
        proposals = generate_segment_proposals(
            [spec], pool, rng=FirstChoiceRandom(1), attempts=1, choose_action=keep,
            route_segments=[spec], crossing_candidates=[crossing_candidate],
        )[1]
        along = [item for item in proposals if item.orientation == "along"]
        perpendicular = [item for item in proposals if item.orientation == "perpendicular"]
        self.assertEqual([item.word_text for item in along], ["train", "plane", "sword"])
        self.assertEqual([item.word_text for item in perpendicular], ["trace"])
        self.assertEqual(len(perpendicular), 1)
        self.assertEqual(len(along), 3)
        self.assertEqual(perpendicular[0].crossing_id, along[0].crossing_id)
        self.assertEqual(along[0].crossing_index, 2)
        self.assertEqual(perpendicular[0].crossing_index, 2)
        self.assertEqual(perpendicular[0].offset_px, along[0].offset_px + 2 * CELL_WIDTH_PX)
        self.assertEqual(perpendicular[0].y_offset_px + 2 * CELL_WIDTH_PX, 0)

        baseline = generate_segment_proposals(
            [spec], pool, rng=FirstChoiceRandom(1), attempts=1, choose_action=keep,
            route_segments=[spec],
        )[1]
        self.assertEqual(
            [(item.word_text, item.offset_px) for item in along],
            [(item.word_text, item.offset_px) for item in baseline],
        )

    def test_seven_letter_candidate_is_allowed_but_eight_is_not(self) -> None:
        seven_cell_spec = SegmentSpec.from_cities(1, 20, (0, 0), (520, 0))
        seven_cell_words = [
            word(1, "train"), word(2, "railway"), word(3, "sword"),
        ]
        seven_cell = generate_segment_proposals(
            [seven_cell_spec], seven_cell_words, rng=FirstChoiceRandom(1),
            attempts=1, choose_action=keep, route_segments=[seven_cell_spec],
            crossing_candidates=[word(4, "seaside")],
        )[1]
        across = next(item for item in seven_cell if item.orientation == "perpendicular")
        self.assertEqual(across.word_text, "seaside")
        self.assertEqual(len(across.word_text), 7)

        eight_cell_spec = SegmentSpec.from_cities(1, 22, (0, 0), (572, 0))
        eight_cell_words = [
            word(1, "train"), word(2, "signaled"), word(3, "sword"),
        ]
        eight_cell = generate_segment_proposals(
            [eight_cell_spec], eight_cell_words, rng=FirstChoiceRandom(1),
            attempts=1, choose_action=keep, route_segments=[eight_cell_spec],
            crossing_candidates=[word(4, "seaside")],
        )[1]
        self.assertTrue(all(item.orientation == "along" for item in eight_cell))

    def test_last_word_cannot_be_perpendicular_and_two_word_layout_has_no_pair(self) -> None:
        spec = SegmentSpec.from_cities(1, 13, (0, 0), (338, 0))
        proposals = generate_segment_proposals(
            [spec], [word(1, "train"), word(2, "plane")],
            rng=random.Random(1), attempts=1, choose_action=keep,
            route_segments=[spec],
        )[1]
        self.assertEqual(len(proposals), 2)
        self.assertTrue(all(item.orientation == "along" for item in proposals))

    def test_long_segment_can_have_three_crossing_pairs(self) -> None:
        spec = SegmentSpec.from_cities(1, 30, (0, 0), (780, 0))
        pool = [
            word(i, value)
            for i, value in enumerate(("cat", "bar", "dad", "map", "rag", "pan", "hat"), 1)
        ]
        proposals = generate_segment_proposals(
            [spec], pool, rng=FirstChoiceRandom(1), attempts=1, choose_action=keep,
            route_segments=[spec],
            crossing_candidates=[
                word(i, value) for i, value in enumerate(
                    ("man", "hat", "pan", "sad", "jam", "lap"), 10
                )
            ],
        )[1]
        pairs = {item.crossing_id for item in proposals if item.crossing_id is not None}
        self.assertEqual(len(pairs), 3)
        self.assertEqual(sum(item.orientation == "perpendicular" for item in proposals), 3)
        self.assertEqual(proposals[-1].orientation, "along")

    def test_crossing_cells_match_for_either_segment_direction(self) -> None:
        forward = SegmentSpec.from_cities(1, 18, (0, 0), (468, 0))
        reverse = SegmentSpec.from_cities(1, 18, (468, 0), (0, 0))
        pool = [word(1, "train"), word(2, "plane"), word(3, "sword")]
        for spec in (forward, reverse):
            proposals = generate_segment_proposals(
                [spec], pool, rng=random.Random(1), attempts=1, choose_action=keep,
                route_segments=[spec], crossing_candidates=[word(4, "trace")],
            )[1]
            along = next(item for item in proposals if item.orientation == "along")
            across = next(item for item in proposals if item.orientation == "perpendicular")
            first_cell = _cell(spec, along, along.crossing_index)
            second_cell = _cell(spec, across, across.crossing_index)
            self.assertAlmostEqual(first_cell.center[0], second_cell.center[0])
            self.assertAlmostEqual(first_cell.center[1], second_cell.center[1])

    def test_crossing_pair_survives_redis_word_validation(self) -> None:
        from app.game_segments import (
            WordProposal,
            _SnapshotSegment,
            _WORD_LIST,
            _validate_crossing_pairs,
        )

        crossing_id = uuid4()
        along = WordProposal(
            placement_id=uuid4(), dictionary_word_id=1, word_text="train",
            definition="clue", offset_px=90, crossing_id=crossing_id, crossing_index=2,
        )
        perpendicular = WordProposal(
            placement_id=uuid4(), dictionary_word_id=2, word_text="plane",
            definition="clue", offset_px=142, orientation="perpendicular",
            y_offset_px=-52, crossing_id=crossing_id, crossing_index=2,
        )
        stored = _WORD_LIST.validate_python([along.stored(), perpendicular.stored()])
        _validate_crossing_pairs(stored)
        _SnapshotSegment.model_validate({"route_id": 1, "words": [
            along.stored(), perpendicular.stored(),
        ]})

        mismatch = WordProposal(
            placement_id=uuid4(), dictionary_word_id=3, word_text="plone",
            definition="clue", offset_px=142, orientation="perpendicular",
            y_offset_px=-52, crossing_id=crossing_id, crossing_index=2,
        )
        mismatched_stored = _WORD_LIST.validate_python([along.stored(), mismatch.stored()])
        with self.assertRaisesRegex(ValueError, "letters do not match"):
            _validate_crossing_pairs(mismatched_stored)

    def test_neighbor_ray_angle_must_be_strictly_greater_than_80_degrees(self) -> None:
        current = SegmentSpec.from_cities(1, 18, (0, 0), (468, 0))

        def neighbor_at_ray_angle(degrees: float) -> SegmentSpec:
            radians = math.radians(degrees)
            target = (468 - 200 * math.cos(radians), 200 * math.sin(radians))
            return SegmentSpec.from_cities(2, 8, (468, 0), target)

        self.assertFalse(_neighbor_angle_allows(current, [current, neighbor_at_ray_angle(80)]))
        self.assertTrue(_neighbor_angle_allows(current, [current, neighbor_at_ray_angle(81)]))

    def test_cells_on_another_route_axis_reject_the_crossing(self) -> None:
        from app.game_segments import WordProposal

        current = SegmentSpec.from_cities(1, 20, (0, 0), (520, 0))
        crossing_axis = SegmentSpec.from_cities(3, 6, (302, -80), (302, 80))
        aligned = WordProposal(
            placement_id=uuid4(), dictionary_word_id=1,
            word_text="train", definition="clue", offset_px=250,
        )
        candidate = WordProposal(
            placement_id=uuid4(), dictionary_word_id=2,
            word_text="plane", definition="clue", offset_px=302,
            orientation="perpendicular", y_offset_px=-52,
            crossing_id=uuid4(), crossing_index=2,
        )
        self.assertFalse(_crossing_geometry_is_clear(
            current, [current, crossing_axis], [aligned], {}, [], 0, 2, 2, candidate,
        ))

    def test_overlap_with_published_crossword_cell_rejects_the_crossing(self) -> None:
        from app.game_segments import WordProposal

        current = SegmentSpec.from_cities(1, 20, (0, 0), (520, 0))
        published_segment = SegmentSpec.from_cities(2, 20, (0, 100), (520, 100))
        aligned = WordProposal(
            placement_id=uuid4(), dictionary_word_id=1,
            word_text="train", definition="clue", offset_px=250,
        )
        candidate_id = uuid4()
        candidate = WordProposal(
            placement_id=uuid4(), dictionary_word_id=2,
            word_text="plane", definition="clue", offset_px=302,
            orientation="perpendicular", y_offset_px=-52,
            crossing_id=candidate_id, crossing_index=2,
        )
        published = WordProposal(
            placement_id=uuid4(), dictionary_word_id=3,
            word_text="plane", definition="clue", offset_px=302,
            orientation="perpendicular", y_offset_px=-130,
            crossing_id=uuid4(), crossing_index=2,
        )
        self.assertFalse(_crossing_geometry_is_clear(
            current, [current, published_segment], [aligned], {2: [published]}, [],
            0, 2, 2, candidate,
        ))


def _load_real_catalog() -> list[WordCandidate]:
    path = MIGRATIONS / "versions" / "72fd40c93b31_merge_english_dictionary.py"
    spec = importlib.util.spec_from_file_location("english_catalog_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return [
        WordCandidate(index, entry["word_text"], entry["definition"])
        for index, entry in enumerate(module._read_catalog(), 1)
    ]


def _load_current_catalog(language: str) -> list[WordCandidate]:
    if language not in {"ru", "en"}:
        raise ValueError(f"Unsupported benchmark language: {language}")

    def load_migration(filename: str, module_name: str):
        path = MIGRATIONS / "versions" / filename
        spec = importlib.util.spec_from_file_location(module_name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    if language == "ru":
        migration = load_migration(
            "f4a8c21d903b_merge_latest_dictionary_words.py",
            "current_russian_catalog_migration",
        )
        entries, _duplicate_count = migration._read_word_catalog()
    else:
        base_migration = load_migration(
            "72fd40c93b31_merge_english_dictionary.py",
            "current_english_base_catalog_migration",
        )
        additional_migration = load_migration(
            "ab3d7f51c9e2_import_remaining_english_words.py",
            "current_english_additional_catalog_migration",
        )
        entries_by_word = {
            entry["word_text"]: entry for entry in base_migration._read_catalog()
        }
        for entry in additional_migration._read_catalog():
            entries_by_word.setdefault(entry["word_text"], entry)
        entries = list(entries_by_word.values())

    return [
        WordCandidate(index, entry["word_text"], entry["definition"])
        for index, entry in enumerate(entries, 1)
    ]


def _real_segments(language: str = "en") -> list[SegmentSpec]:
    if language not in {"ru", "en"}:
        raise ValueError(f"Unsupported benchmark language: {language}")
    routes = json.loads((MIGRATIONS / "routes.json").read_text(encoding="utf-8"))["routes"]
    cities = {
        city["id"]: city
        for city in json.loads(
            (MIGRATIONS / f"cities_{language}.json").read_text(encoding="utf-8")
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
        cls.segments = _real_segments()

    def letter_count_share(self, layouts, letter_count: int) -> float:
        total = sum(len(words) for words in layouts)
        matching = sum(
            1 for words in layouts for word in words if word.letters == letter_count
        )
        return matching / total

    def generated(
        self,
        seed: int,
        attempts: int,
        *,
        segments: list[SegmentSpec] | None = None,
        catalog: list[WordCandidate] | None = None,
        mode_log: list[tuple[int, bool]] | None = None,
    ):
        segments = segments or self.segments
        catalog = catalog or self.catalog
        by_id = {word.id: word for word in catalog}

        def generate():
            return generate_segment_proposals(
                segments, catalog, rng=random.Random(seed), attempts=attempts
            )

        if mode_log is None:
            proposals = generate()
        else:
            original_score = crossword_generator._score

            def record_mode(words, spec, *, penalize_two_letter_words):
                mode_log.append((spec.route_id, penalize_two_letter_words))
                return original_score(
                    words,
                    spec,
                    penalize_two_letter_words=penalize_two_letter_words,
                )

            with patch.object(crossword_generator, "_score", side_effect=record_mode):
                proposals = generate()

        for spec in segments:
            self.assert_valid_layout(spec, proposals[spec.route_id])
        return [
            [by_id[p.dictionary_word_id] for p in proposals[spec.route_id]]
            for spec in segments
        ], proposals

    @staticmethod
    def _proposal_signature(proposals):
        return {
            route_id: [
                (
                    word.dictionary_word_id,
                    word.word_text,
                    word.orientation,
                    word.offset_px,
                    word.y_offset_px,
                    word.crossing_index,
                )
                for word in words
            ]
            for route_id, words in proposals.items()
        }

    def test_historical_english_benchmark_meets_frequency_targets(self) -> None:
        legacy, best, mode_log = [], [], []
        target_reached = total_segments = 0
        for seed in self.SEEDS:
            rng = random.Random(seed)
            legacy += [legacy_layout(spec, self.catalog, rng) for spec in self.segments]
            layouts, _ = self.generated(seed, attempts=6, mode_log=mode_log)
            best += layouts
            for segment, words in zip(self.segments, layouts):
                free = segment.word_area_px - sum(
                    item.cells for item in words
                ) * CELL_WIDTH_PX
                free -= MIN_GAP_PX * (len(words) - 1)
                target_reached += free <= segment.free_space_target_px
                total_segments += 1

        old_shares = {
            count: self.letter_count_share(legacy, count) for count in (1, 2)
        }
        one_share = self.letter_count_share(best, 1)
        two_share = self.letter_count_share(best, 2)
        self.assertAlmostEqual(old_shares[2], 0.0839, delta=0.001)
        self.assertGreaterEqual(two_share, 0.01)
        self.assertLessEqual(two_share, 0.05)
        self.assertLessEqual(one_share, 0.0002)
        self.assertGreaterEqual(target_reached / total_segments, 0.95)

        # Each of 30 seeds covers all 46 routes. The six scores for one segment
        # must all use the same penalty mode, and the selected modes should
        # approximate the configured 50/50 probability over the sample.
        self.assertEqual(len(mode_log), len(self.SEEDS) * len(self.segments) * 6)
        segment_modes = []
        for offset in range(0, len(mode_log), 6):
            group = mode_log[offset : offset + 6]
            self.assertEqual(len({value for _, value in group}), 1)
            self.assertEqual(len({route_id for route_id, _ in group}), 1)
            segment_modes.append(group[0][1])
        mode_share = sum(segment_modes) / len(segment_modes)
        self.assertGreaterEqual(mode_share, 0.45)
        self.assertLessEqual(mode_share, 0.55)
        self.assertIn(True, segment_modes)
        self.assertIn(False, segment_modes)
        two_letter_segment_share = sum(
            any(word.letters == 2 for word in words) for words in best
        ) / len(best)
        logger.info(
            "Frequency benchmark historical en: word_count={}, routes={}, seeds={}, "
            "one_letter_share={:.6%}, two_letter_share={:.6%}, "
            "two_letter_segment_share={:.6%}, penalty_mode_share={:.6%}, "
            "free_space_target_share={:.6%}",
            len(self.catalog),
            len(self.segments),
            len(self.SEEDS),
            one_share,
            two_share,
            two_letter_segment_share,
            mode_share,
            target_reached / total_segments,
        )

        first_modes: list[tuple[int, bool]] = []
        _, first_proposals = self.generated(0, attempts=6, mode_log=first_modes)
        repeated_modes: list[tuple[int, bool]] = []
        _, repeated_proposals = self.generated(0, attempts=6, mode_log=repeated_modes)
        self.assertEqual(first_modes, repeated_modes)
        self.assertEqual(
            self._proposal_signature(first_proposals),
            self._proposal_signature(repeated_proposals),
        )

    def test_real_routes_get_words_and_reach_the_free_space_target(self) -> None:
        reached = total = 0
        for seed in self.SEEDS:
            layouts, proposals = self.generated(seed, attempts=6)
            self.assertFalse(route_is_empty(proposals))
            for spec, words in zip(self.segments, layouts):
                self.assertTrue(words, f"segment {spec.route_id} is empty")
                free = spec.word_area_px - sum(w.cells for w in words) * CELL_WIDTH_PX
                free -= MIN_GAP_PX * (len(words) - 1)
                reached += free <= spec.free_space_target_px
                total += 1
        self.assertGreater(reached / total, 0.95)

    def test_current_language_catalogs_get_separate_benchmark_metrics(self) -> None:
        for language in ("ru", "en"):
            catalog = _load_current_catalog(language)
            segments = _real_segments(language)
            layouts = []
            reached = total_segments = 0
            for seed in self.SEEDS:
                generated, proposals = self.generated(
                    seed,
                    attempts=6,
                    segments=segments,
                    catalog=catalog,
                )
                self.assertFalse(route_is_empty(proposals))
                layouts.extend(generated)
                for segment, words in zip(segments, generated):
                    self.assertTrue(words, f"{language} segment {segment.route_id} is empty")
                    free = segment.word_area_px - sum(w.cells for w in words) * CELL_WIDTH_PX
                    free -= MIN_GAP_PX * (len(words) - 1)
                    reached += free <= segment.free_space_target_px
                    total_segments += 1

            metrics = {
                "word_count": len(catalog),
                "two_letter_share": self.letter_count_share(layouts, 2),
                "two_letter_segment_share": sum(
                    any(word.letters == 2 for word in words) for words in layouts
                ) / len(layouts),
                "one_letter_share": self.letter_count_share(layouts, 1),
                "free_space_target_share": reached / total_segments,
            }
            self.assertEqual(len(segments), 46)
            self.assertGreaterEqual(metrics["free_space_target_share"], 0.95)
            logger.info("Frequency benchmark {}: {}", language, metrics)


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

    async def test_shared_crossing_geometry_is_identical_within_a_round(self) -> None:
        crossing_id = uuid4()
        along = WordProposal(
            placement_id=uuid4(), dictionary_word_id=1, word_text="train",
            definition="Clue for train", offset_px=90,
            crossing_id=crossing_id, crossing_index=2,
        )
        perpendicular = WordProposal(
            placement_id=uuid4(), dictionary_word_id=2, word_text="plane",
            definition="Clue for plane", offset_px=142,
            orientation="perpendicular", y_offset_px=-52,
            crossing_id=crossing_id, crossing_index=2,
        )
        first_user, first = await self.join((1, 2))
        await self.segments.confirm_route(
            first_user, first.id, [7, 8],
            {7: [along, perpendicular], 8: [WordProposal(
                placement_id=uuid4(), dictionary_word_id=3,
                word_text="rail", definition="Clue for rail", offset_px=26,
            )]},
        )
        _, original = await self.segments.crossword(first_user, first.id)

        second_user, second = await self.join((3, 4))
        await self.segments.confirm_route(
            second_user, second.id, [7, 8],
            {7: [WordProposal(
                placement_id=uuid4(), dictionary_word_id=4,
                word_text="track", definition="Clue for track", offset_px=26,
            )], 8: [WordProposal(
                placement_id=uuid4(), dictionary_word_id=5,
                word_text="cargo", definition="Clue for cargo", offset_px=26,
            )]},
        )
        _, shared = await self.segments.crossword(second_user, second.id)
        geometry = lambda items: [
            (item.placement_id, item.route_id, item.offset_px, item.orientation,
             item.y_offset_px, item.crossing_id, item.crossing_index)
            for item in items
        ]
        self.assertEqual(geometry(shared), geometry(original))

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
