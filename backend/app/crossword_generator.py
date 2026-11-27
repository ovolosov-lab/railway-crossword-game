"""Crossword layout for new route segments (ADR 0007 section 6, ADR 0009 geometry).

The generator is pure: it takes already loaded dictionary candidates and returns
``WordProposal`` objects with ``offset_px`` for segments that have not been
published yet. Published shared segments are never regenerated; callers pass
only new segments and the words already used on the rest of the player's route.
The result is accepted by ``GameSegments.confirm_route``, which also handles a
completely empty route (it releases the session and the city pair).
"""
from __future__ import annotations

import math
import random
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from uuid import UUID

from app.game_segments import PublishedWord, WordProposal, normalize_answer

CELL_WIDTH_PX = 26
CITY_CLEARANCE_PX = 26
MIN_GAP_PX = 5
SHORT_WORD_MAX_LETTERS = 2
CROSSING_WORD_MIN_CELLS = 3
CROSSING_WORD_MAX_CELLS = 7
CROSSING_MIN_ANGLE_DEGREES = 80
CROSSING_MAX_PAIRS_SHORT = 1
CROSSING_MAX_PAIRS_LONG = 3
SMALL_SEGMENT_MAX_CELLS = 31
SMALL_SEGMENT_FREE_TARGET_PX = 52
LARGE_SEGMENT_FREE_TARGET_PX = 78
DEFAULT_ATTEMPTS = 6


class CrosswordGenerationError(ValueError):
    """The generator input cannot describe a valid crossword layout."""


class ShortWordAction(Enum):
    """The three ADR 0007 actions for a one- or two-letter candidate."""

    KEEP = "keep"
    LONGER = "longer"
    EMPTY = "empty"


ActionChooser = Callable[[random.Random], ShortWordAction]


def random_action(rng: random.Random) -> ShortWordAction:
    return rng.choice(list(ShortWordAction))


def _letter_count(word_text: str) -> int:
    normalized = unicodedata.normalize("NFC", word_text)
    return sum(1 for char in normalized if unicodedata.category(char).startswith("L"))


@dataclass(frozen=True)
class WordCandidate:
    """A dictionary word of the game language, independent of the ORM model."""

    id: int
    word_text: str
    definition: str
    cells: int = field(init=False)
    letters: int = field(init=False)
    key: str = field(init=False)

    def __post_init__(self) -> None:
        text = unicodedata.normalize("NFC", self.word_text)
        object.__setattr__(self, "word_text", text)
        object.__setattr__(self, "cells", len(text))
        object.__setattr__(self, "letters", _letter_count(text))
        object.__setattr__(self, "key", normalize_answer(text))
        if self.id <= 0 or self.letters == 0 or not self.definition.strip():
            raise CrosswordGenerationError(f"Invalid dictionary word {self.id}.")


@dataclass(frozen=True)
class SegmentSpec:
    """A new route segment: its stored capacity and geometric length in pixels."""

    route_id: int
    length_in_letters: int
    length_px: float
    source: tuple[float, float] | None = None
    target: tuple[float, float] | None = None

    def __post_init__(self) -> None:
        if (
            self.length_in_letters <= 0
            or not math.isfinite(self.length_px)
            or self.length_px < 0
        ):
            raise CrosswordGenerationError(f"Invalid geometry of segment {self.route_id}.")
        if (self.source is None) != (self.target is None):
            raise CrosswordGenerationError(f"Incomplete coordinates of segment {self.route_id}.")
        if self.source is not None and self.target is not None:
            coordinates = (*self.source, *self.target)
            if (
                not all(math.isfinite(value) for value in coordinates)
                or math.dist(self.source, self.target) <= 0
            ):
                raise CrosswordGenerationError(f"Invalid coordinates of segment {self.route_id}.")

    @classmethod
    def from_cities(
        cls,
        route_id: int,
        length_in_letters: int,
        source: tuple[float, float],
        target: tuple[float, float],
    ) -> SegmentSpec:
        """Use the straight-line distance between the cities' map coordinates."""
        return cls(route_id, length_in_letters, math.dist(source, target), source, target)

    @property
    def available_px(self) -> int:
        return int(min(self.length_px, self.length_in_letters * CELL_WIDTH_PX))

    @property
    def word_area_px(self) -> int:
        """Space available to words after reserving both city clearances."""
        return max(0, self.available_px - CITY_CLEARANCE_PX * 2)

    @property
    def free_space_target_px(self) -> int:
        if self.length_in_letters <= SMALL_SEGMENT_MAX_CELLS:
            return SMALL_SEGMENT_FREE_TARGET_PX
        return LARGE_SEGMENT_FREE_TARGET_PX


def _width(words: Sequence[WordCandidate]) -> int:
    if not words:
        return 0
    return sum(word.cells for word in words) * CELL_WIDTH_PX + MIN_GAP_PX * (len(words) - 1)


def _fill(
    available_px: int,
    pool: Sequence[WordCandidate],
    blocked: frozenset[str],
    rng: random.Random,
    choose_action: ActionChooser,
    *,
    allow_two_letter_words: bool = True,
) -> list[WordCandidate]:
    words: list[WordCandidate] = []
    while True:
        used = blocked | {word.key for word in words}
        room = available_px - _width(words) - (MIN_GAP_PX if words else 0)
        options = [
            word for word in pool
            if word.key not in used and word.cells * CELL_WIDTH_PX <= room
            and (allow_two_letter_words or word.letters != 2)
        ]
        if not options:
            return words
        pick = rng.choice(options)
        if pick.letters <= SHORT_WORD_MAX_LETTERS:
            action = choose_action(rng)
            if action is ShortWordAction.EMPTY:
                if words:
                    return words
                # A segment must not become empty only because of this random choice.
                action = ShortWordAction.LONGER
            if action is ShortWordAction.LONGER:
                replaced = _longer_replacement(
                    available_px, pool, blocked, words, pick, rng
                )
                if replaced is not None:
                    words = replaced
                    continue
        words.append(pick)


def _longer_replacement(
    available_px: int,
    pool: Sequence[WordCandidate],
    blocked: frozenset[str],
    words: list[WordCandidate],
    short_word: WordCandidate,
    rng: random.Random,
) -> list[WordCandidate] | None:
    """Replace a one- or two-letter word and its predecessor with one longer word."""
    kept = words[:-1]
    previous = words[-1] if words else None
    minimum = max(short_word.cells, previous.cells if previous else 0)
    used = blocked | {word.key for word in kept}
    options = [
        word for word in pool
        if word.cells > minimum
        and word.letters > SHORT_WORD_MAX_LETTERS
        and word.key not in used
        and _width([*kept, word]) <= available_px
    ]
    if not options:
        return None
    return [*kept, rng.choice(options)]


def _score(
    words: Sequence[WordCandidate],
    spec: SegmentSpec,
    *,
    penalize_two_letter_words: bool,
) -> tuple[bool, int, int, int]:
    # City clearances are outside the placement area and must not count toward
    # the ADR's free-space target for the usable segment.
    free = spec.word_area_px - _width(words)
    one_letter_words = sum(1 for word in words if word.letters == 1)
    two_letter_words = (
        sum(1 for word in words if word.letters == 2)
        if penalize_two_letter_words
        else 0
    )
    return (
        free > spec.free_space_target_px,
        one_letter_words,
        two_letter_words,
        free,
    )


def _place(
    words: Sequence[WordCandidate], spec: SegmentSpec, rng: random.Random
) -> list[WordProposal]:
    """Keep 26 px clear at both ends and spread the remaining space evenly."""
    free_space = (
        spec.available_px
        - CITY_CLEARANCE_PX * 2
        - _width(words)
    )
    slot, remainder = divmod(free_space, len(words) + 1)
    spaces = [slot + (index < remainder) for index in range(len(words) + 1)]
    offset = CITY_CLEARANCE_PX + spaces[0]
    proposals: list[WordProposal] = []
    for index, word in enumerate(words):
        proposals.append(WordProposal(
            placement_id=UUID(int=rng.getrandbits(128), version=4),
            dictionary_word_id=word.id,
            word_text=word.word_text,
            definition=word.definition,
            # ADR 0010 defines offset_px as the center of the first cell.
            offset_px=float(offset + CELL_WIDTH_PX / 2),
        ))
        offset += word.cells * CELL_WIDTH_PX
        if index < len(words) - 1:
            offset += MIN_GAP_PX + spaces[index + 1]
    return proposals


def _basis(spec: SegmentSpec) -> tuple[tuple[float, float], tuple[float, float]] | None:
    if spec.source is None or spec.target is None:
        return None
    dx = spec.target[0] - spec.source[0]
    dy = spec.target[1] - spec.source[1]
    length = math.hypot(dx, dy)
    if length <= 0:
        return None
    u = (dx / length, dy / length)
    return u, (-u[1], u[0])


def _neighbor_angle_allows(spec: SegmentSpec, route: Sequence[SegmentSpec]) -> bool:
    if _basis(spec) is None:
        return False
    cosine_limit = math.cos(math.radians(CROSSING_MIN_ANGLE_DEGREES))
    for neighbor in route:
        if neighbor.route_id == spec.route_id or neighbor.source is None or neighbor.target is None:
            continue
        shared = None
        if spec.source == neighbor.source:
            shared = spec.source
        elif spec.source == neighbor.target:
            shared = spec.source
        elif spec.target == neighbor.source:
            shared = spec.target
        elif spec.target == neighbor.target:
            shared = spec.target
        if shared is None:
            continue
        current_other = spec.target if shared == spec.source else spec.source
        neighbor_other = neighbor.target if shared == neighbor.source else neighbor.source
        current_ray = (current_other[0] - shared[0], current_other[1] - shared[1])
        neighbor_ray = (neighbor_other[0] - shared[0], neighbor_other[1] - shared[1])
        current_length = math.hypot(*current_ray)
        neighbor_length = math.hypot(*neighbor_ray)
        dot = (
            current_ray[0] * neighbor_ray[0] + current_ray[1] * neighbor_ray[1]
        ) / (current_length * neighbor_length)
        if dot >= cosine_limit:
            return False
    return True


@dataclass(frozen=True)
class _Cell:
    center: tuple[float, float]
    axis_x: tuple[float, float]
    axis_y: tuple[float, float]


def _cell(spec: SegmentSpec, placement: WordProposal, index: int) -> _Cell | None:
    basis = _basis(spec)
    if basis is None or spec.source is None:
        return None
    u, n = basis
    center = (
        spec.source[0] + u[0] * placement.offset_px + n[0] * placement.y_offset_px,
        spec.source[1] + u[1] * placement.offset_px + n[1] * placement.y_offset_px,
    )
    if placement.orientation == "along":
        step = u
        axis_x, axis_y = u, n
    else:
        step = n
        axis_x, axis_y = n, (-u[0], -u[1])
    return _Cell(
        center=(
            center[0] + step[0] * index * CELL_WIDTH_PX,
            center[1] + step[1] * index * CELL_WIDTH_PX,
        ),
        axis_x=axis_x,
        axis_y=axis_y,
    )


def _cells_overlap(first: _Cell, second: _Cell) -> bool:
    delta = (second.center[0] - first.center[0], second.center[1] - first.center[1])
    half = CELL_WIDTH_PX / 2
    for axis in (first.axis_x, first.axis_y, second.axis_x, second.axis_y):
        distance = abs(delta[0] * axis[0] + delta[1] * axis[1])
        first_radius = half * (
            abs(first.axis_x[0] * axis[0] + first.axis_x[1] * axis[1])
            + abs(first.axis_y[0] * axis[0] + first.axis_y[1] * axis[1])
        )
        second_radius = half * (
            abs(second.axis_x[0] * axis[0] + second.axis_x[1] * axis[1])
            + abs(second.axis_y[0] * axis[0] + second.axis_y[1] * axis[1])
        )
        # Touching edges are not overlapping cells.
        if distance >= first_radius + second_radius - 1e-6:
            return False
    return True


def _axis_hits_cell(spec: SegmentSpec, cell: _Cell) -> bool:
    if spec.source is None or spec.target is None:
        return False
    half = CELL_WIDTH_PX / 2 + 1.0
    start = (spec.source[0] - cell.center[0], spec.source[1] - cell.center[1])
    end = (spec.target[0] - cell.center[0], spec.target[1] - cell.center[1])
    local_start = (
        start[0] * cell.axis_x[0] + start[1] * cell.axis_x[1],
        start[0] * cell.axis_y[0] + start[1] * cell.axis_y[1],
    )
    local_end = (
        end[0] * cell.axis_x[0] + end[1] * cell.axis_x[1],
        end[0] * cell.axis_y[0] + end[1] * cell.axis_y[1],
    )
    lower, upper = 0.0, 1.0
    for origin, delta in zip(local_start, (local_end[0] - local_start[0], local_end[1] - local_start[1])):
        if abs(delta) < 1e-12:
            if abs(origin) >= half:
                return False
            continue
        first = (-half - origin) / delta
        second = (half - origin) / delta
        lower = max(lower, min(first, second))
        upper = min(upper, max(first, second))
        if lower >= upper:
            return False
    return True


def _proposal_from_published(word: PublishedWord) -> WordProposal:
    return WordProposal(
        placement_id=word.placement_id,
        dictionary_word_id=word.dictionary_word_id,
        word_text=word.word_text,
        definition=word.definition,
        offset_px=word.offset_px,
        orientation=word.orientation,
        y_offset_px=word.y_offset_px,
        crossing_id=word.crossing_id,
        crossing_index=word.crossing_index,
    )


def maximum_crossing_pairs(words: Sequence[WordProposal]) -> int:
    """Return the per-segment ceiling after excluding both endpoint words."""
    middle_words = words[1:-1]
    if len(words) >= 3 and any(
        len(word.word_text) >= CROSSING_WORD_MIN_CELLS for word in middle_words
    ):
        policy_limit = CROSSING_MAX_PAIRS_LONG
    else:
        policy_limit = CROSSING_MAX_PAIRS_SHORT
    interior_word_count = max(0, len(words) - 2)
    available_non_overlapping_pairs = interior_word_count // 2
    return min(policy_limit, available_non_overlapping_pairs)


def eligible_perpendicular_candidates(
    spec: SegmentSpec,
    route: Sequence[SegmentSpec],
    aligned_words: Sequence[WordProposal],
    published: Mapping[int, Sequence[PublishedWord]],
    prior_crossings: Sequence[WordProposal],
    anchor_index: int,
    anchor_letter_index: int,
    required_letter: str,
    required_length: int,
    candidates: Sequence[WordCandidate],
    used_words: Iterable[str],
) -> list[tuple[WordCandidate, tuple[int, ...]]]:
    """Filter candidates for an internal anchor to unique, collision-free options."""
    if not 1 <= anchor_index < len(aligned_words) - 1:
        return []
    anchor = aligned_words[anchor_index]
    required = normalize_answer(required_letter)
    used = {normalize_answer(word) for word in used_words}
    seen: set[str] = set()
    eligible: list[tuple[WordCandidate, tuple[int, ...]]] = []

    for candidate in candidates:
        if candidate.cells != required_length or candidate.key in used or candidate.key in seen:
            continue
        seen.add(candidate.key)
        positions: list[int] = []
        for candidate_letter_index in range(1, len(candidate.word_text) - 1):
            if normalize_answer(candidate.word_text[candidate_letter_index]) != required:
                continue
            probe = WordProposal(
                placement_id=UUID(int=0),
                dictionary_word_id=candidate.id,
                word_text=candidate.word_text,
                definition=candidate.definition,
                offset_px=anchor.offset_px + anchor_letter_index * CELL_WIDTH_PX,
                orientation="perpendicular",
                y_offset_px=-candidate_letter_index * CELL_WIDTH_PX,
                crossing_id=UUID(int=0),
                crossing_index=candidate_letter_index,
            )
            if _crossing_geometry_is_clear(
                spec,
                route,
                aligned_words,
                published,
                prior_crossings,
                anchor_index,
                anchor_letter_index,
                candidate_letter_index,
                probe,
            ):
                positions.append(candidate_letter_index)
        if positions:
            eligible.append((candidate, tuple(positions)))
    return eligible


def link_crossing_pair(
    anchor: WordProposal,
    candidate: WordCandidate,
    anchor_letter_index: int,
    candidate_letter_index: int,
    rng: random.Random,
) -> tuple[WordProposal, WordProposal]:
    """Add the shared crossing identifier and create the perpendicular word."""
    crossing_id = UUID(int=rng.getrandbits(128), version=4)
    paired_anchor = WordProposal.model_validate({
        **anchor.model_dump(),
        "crossing_id": crossing_id,
        "crossing_index": anchor_letter_index,
    })
    perpendicular = WordProposal(
        placement_id=UUID(int=rng.getrandbits(128), version=4),
        dictionary_word_id=candidate.id,
        word_text=candidate.word_text,
        definition=candidate.definition,
        offset_px=anchor.offset_px + anchor_letter_index * CELL_WIDTH_PX,
        orientation="perpendicular",
        y_offset_px=-candidate_letter_index * CELL_WIDTH_PX,
        crossing_id=crossing_id,
        crossing_index=candidate_letter_index,
    )
    return paired_anchor, perpendicular


def _apply_optional_crossings(
    spec: SegmentSpec,
    route: Sequence[SegmentSpec],
    words: Sequence[WordProposal],
    published: Mapping[int, Sequence[PublishedWord]],
    crossing_candidates: Sequence[WordCandidate],
    used_words: Iterable[str],
    rng: random.Random,
) -> list[WordProposal]:
    if not words:
        return []
    aligned = list(words)
    if not crossing_candidates or not _neighbor_angle_allows(spec, route):
        return aligned

    max_pairs = maximum_crossing_pairs(aligned)
    used = {normalize_answer(word) for word in used_words}
    used.update(normalize_answer(word.word_text) for word in aligned)
    extras_after_anchor: dict[int, WordProposal] = {}
    accepted_crossings: list[WordProposal] = []
    # The first along word is next to a city endpoint and cannot anchor a pair.
    index, pair_count = 1, 0
    while index + 1 < len(aligned) and pair_count < max_pairs:
        slot_index = index + 1
        anchor, slot = aligned[index], aligned[slot_index]
        slot_length = len(slot.word_text)
        if (
            slot_index == len(aligned) - 1
            or not CROSSING_WORD_MIN_CELLS <= slot_length <= CROSSING_WORD_MAX_CELLS
        ):
            index += 1
            continue
        match = _first_internal_match(anchor.word_text, slot.word_text)
        if match is None:
            index += 1
            continue
        anchor_letter_index, _slot_letter_index = match
        required_letter = anchor.word_text[anchor_letter_index]
        options = eligible_perpendicular_candidates(
            spec,
            route,
            aligned,
            published,
            accepted_crossings,
            index,
            anchor_letter_index,
            required_letter,
            slot_length,
            crossing_candidates,
            used,
        )
        if not options:
            index += 1
            continue
        candidate, candidate_positions = rng.choice(options)
        candidate_letter_index = rng.choice(candidate_positions)
        aligned[index], perpendicular = link_crossing_pair(
            anchor, candidate, anchor_letter_index, candidate_letter_index, rng
        )
        extras_after_anchor[index] = perpendicular
        accepted_crossings.append(perpendicular)
        used.add(candidate.key)
        pair_count += 1
        index += 2

    result: list[WordProposal] = []
    for index, word in enumerate(aligned):
        result.append(word)
        if index in extras_after_anchor:
            result.append(extras_after_anchor[index])
    return result


def _first_internal_match(anchor: str, candidate: str) -> tuple[int, int] | None:
    for anchor_index in range(1, len(anchor) - 1):
        if not unicodedata.category(anchor[anchor_index]).startswith("L"):
            continue
        for candidate_index in range(1, len(candidate) - 1):
            if (
                unicodedata.category(candidate[candidate_index]).startswith("L")
                and normalize_answer(anchor[anchor_index])
                == normalize_answer(candidate[candidate_index])
            ):
                return anchor_index, candidate_index
    return None


def _crossing_geometry_is_clear(
    spec: SegmentSpec,
    route: Sequence[SegmentSpec],
    aligned_words: Sequence[WordProposal],
    published: Mapping[int, Sequence[PublishedWord]],
    prior_crossings: Sequence[WordProposal],
    anchor_aligned_index: int,
    anchor_letter_index: int,
    candidate_letter_index: int,
    candidate: WordProposal,
) -> bool:
    if not route or any(_basis(segment) is None for segment in route):
        return False
    word_spec = next((segment for segment in route if segment.route_id == spec.route_id), None)
    if word_spec is None:
        return False
    for candidate_index in range(len(candidate.word_text)):
        candidate_cell = _cell(spec, candidate, candidate_index)
        assert candidate_cell is not None
        for axis in route:
            if _axis_hits_cell(axis, candidate_cell):
                if axis.route_id != spec.route_id or candidate_index != candidate_letter_index:
                    return False
        for word_index, word in enumerate(aligned_words):
            for letter_index in range(len(word.word_text)):
                existing_cell = _cell(word_spec, word, letter_index)
                assert existing_cell is not None
                if _cells_overlap(candidate_cell, existing_cell):
                    if not (
                        word_index == anchor_aligned_index
                        and letter_index == anchor_letter_index
                        and candidate_index == candidate_letter_index
                    ):
                        return False
        for route_id, route_words in published.items():
            route_spec = next((segment for segment in route if segment.route_id == route_id), None)
            if route_spec is None:
                continue
            for stored in route_words:
                word = _proposal_from_published(stored)
                for letter_index in range(len(word.word_text)):
                    existing_cell = _cell(route_spec, word, letter_index)
                    assert existing_cell is not None
                    if _cells_overlap(candidate_cell, existing_cell):
                        return False
        for previous_word in prior_crossings:
            for letter_index in range(len(previous_word.word_text)):
                existing_cell = _cell(spec, previous_word, letter_index)
                assert existing_cell is not None
                if _cells_overlap(candidate_cell, existing_cell):
                    return False
    return True


def generate_segment_proposals(
    segments: Sequence[SegmentSpec],
    candidates: Iterable[WordCandidate],
    *,
    occupied_words: Iterable[str] = (),
    rng: random.Random | None = None,
    choose_action: ActionChooser = random_action,
    attempts: int = DEFAULT_ATTEMPTS,
    route_segments: Sequence[SegmentSpec] | None = None,
    published_placements: Mapping[int, Sequence[PublishedWord]] | None = None,
    crossing_candidates: Iterable[WordCandidate] = (),
    suppress_two_letter_words_after_first_use: bool = True,
) -> dict[int, list[WordProposal]]:
    """Create words and offsets for new segments, in route order.

    ``occupied_words`` are the words already present on the other, published
    segments of the route; a word is used at most once per route. Optional
    crossings use only the separate ``crossing_candidates`` pool, never the
    base pool; they require the complete selected path in ``route_segments``.
    Without candidates or that geometry, words stay along their segments.
    Segments with no fitting word get an empty list. With ``attempts`` greater
    than one, the best of
    several randomized layouts is kept: first the one reaching the
    free-space target, then the fewest one-letter words, then (in a randomly
    selected per-segment mode) the fewest two-letter words, then the least
    free space.
    """
    if attempts < 1:
        raise CrosswordGenerationError("At least one generation attempt is required.")
    route_ids = [segment.route_id for segment in segments]
    if len(route_ids) != len(set(route_ids)):
        raise CrosswordGenerationError("A route must not repeat a segment.")
    rng = rng or random.SystemRandom()
    pool = sorted(candidates, key=lambda word: word.id)
    crossing_pool = sorted(crossing_candidates, key=lambda word: word.id)
    if len({word.id for word in pool}) != len(pool):
        raise CrosswordGenerationError("Dictionary candidates must be unique.")
    blocked = {normalize_answer(word) for word in occupied_words}
    two_letter_words_used = (
        sum(
            _letter_count(word.word_text) == 2
            for words in (published_placements or {}).values()
            for word in words
        )
        if suppress_two_letter_words_after_first_use
        else 0
    )
    aligned_by_route: dict[int, list[WordProposal]] = {}
    for spec in segments:
        penalize_two_letter_words = rng.random() < 0.5
        allow_two_letter_words = (
            not suppress_two_letter_words_after_first_use or two_letter_words_used == 0
        )
        layouts = (
            _fill(
                spec.word_area_px,
                pool,
                blocked,
                rng,
                choose_action,
                allow_two_letter_words=allow_two_letter_words,
            )
            for _ in range(attempts)
        )
        best = min(
            layouts,
            key=lambda words: _score(
                words,
                spec,
                penalize_two_letter_words=penalize_two_letter_words,
            ),
        )
        blocked.update(word.key for word in best)
        if suppress_two_letter_words_after_first_use:
            two_letter_words_used += sum(word.letters == 2 for word in best)
        aligned_by_route[spec.route_id] = _place(best, spec, rng)

    if not crossing_pool:
        return aligned_by_route

    result: dict[int, list[WordProposal]] = {}
    for spec in segments:
        placed = _apply_optional_crossings(
            spec,
            route_segments or (),
            aligned_by_route[spec.route_id],
            published_placements or {},
            crossing_pool,
            blocked,
            rng,
        )
        result[spec.route_id] = placed
        blocked.update(
            normalize_answer(word.word_text)
            for word in placed
            if word.orientation == "perpendicular"
        )
    return result


def route_is_empty(
    proposals: Mapping[int, Sequence[WordProposal]], published_word_count: int = 0
) -> bool:
    """A route without any word is a generation failure, not a win."""
    return published_word_count == 0 and not any(proposals.values())
