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

from app.game_segments import WordProposal, normalize_answer

CELL_WIDTH_PX = 26
MIN_GAP_PX = 5
TWO_LETTER_WORD = 2
SMALL_SEGMENT_MAX_CELLS = 31
SMALL_SEGMENT_FREE_TARGET_PX = 52
LARGE_SEGMENT_FREE_TARGET_PX = 78
DEFAULT_ATTEMPTS = 6


class CrosswordGenerationError(ValueError):
    """The generator input cannot describe a valid crossword layout."""


class TwoLetterAction(Enum):
    """The three ADR 0007 actions for a two-letter candidate."""

    KEEP = "keep"
    LONGER = "longer"
    EMPTY = "empty"


ActionChooser = Callable[[random.Random], TwoLetterAction]


def random_action(rng: random.Random) -> TwoLetterAction:
    return rng.choice(list(TwoLetterAction))


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
        object.__setattr__(
            self,
            "letters",
            sum(1 for char in text if unicodedata.category(char).startswith("L")),
        )
        object.__setattr__(self, "key", normalize_answer(text))
        if self.id <= 0 or self.letters == 0 or not self.definition.strip():
            raise CrosswordGenerationError(f"Invalid dictionary word {self.id}.")


@dataclass(frozen=True)
class SegmentSpec:
    """A new route segment: its stored capacity and geometric length in pixels."""

    route_id: int
    length_in_letters: int
    length_px: float

    def __post_init__(self) -> None:
        if (
            self.length_in_letters <= 0
            or not math.isfinite(self.length_px)
            or self.length_px < 0
        ):
            raise CrosswordGenerationError(f"Invalid geometry of segment {self.route_id}.")

    @classmethod
    def from_cities(
        cls,
        route_id: int,
        length_in_letters: int,
        source: tuple[float, float],
        target: tuple[float, float],
    ) -> SegmentSpec:
        """Use the straight-line distance between the cities' map coordinates."""
        return cls(route_id, length_in_letters, math.dist(source, target))

    @property
    def available_px(self) -> int:
        return int(min(self.length_px, self.length_in_letters * CELL_WIDTH_PX))

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
) -> list[WordCandidate]:
    words: list[WordCandidate] = []
    while True:
        used = blocked | {word.key for word in words}
        room = available_px - _width(words) - (MIN_GAP_PX if words else 0)
        options = [
            word for word in pool
            if word.key not in used and word.cells * CELL_WIDTH_PX <= room
        ]
        if not options:
            return words
        pick = rng.choice(options)
        if pick.letters == TWO_LETTER_WORD:
            action = choose_action(rng)
            if action is TwoLetterAction.EMPTY:
                if words:
                    return words
                # A segment must not become empty only because of this random choice.
                action = TwoLetterAction.LONGER
            if action is TwoLetterAction.LONGER:
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
    two_letter: WordCandidate,
    rng: random.Random,
) -> list[WordCandidate] | None:
    """Replace the two-letter word and the previous word with one longer word."""
    kept = words[:-1]
    previous = words[-1] if words else None
    minimum = max(two_letter.cells, previous.cells if previous else 0)
    used = blocked | {word.key for word in kept}
    options = [
        word for word in pool
        if word.cells > minimum
        and word.letters > TWO_LETTER_WORD
        and word.key not in used
        and _width([*kept, word]) <= available_px
    ]
    if not options:
        return None
    return [*kept, rng.choice(options)]


def _score(words: Sequence[WordCandidate], spec: SegmentSpec) -> tuple[bool, int, int]:
    free = spec.available_px - _width(words)
    two_letter = sum(1 for word in words if word.letters == TWO_LETTER_WORD)
    return free > spec.free_space_target_px, two_letter, free


def _place(
    words: Sequence[WordCandidate], spec: SegmentSpec, rng: random.Random
) -> list[WordProposal]:
    """Spread the free space evenly: edges and gaps get the same extra pixels."""
    slot = (spec.available_px - _width(words)) // (len(words) + 1)
    offset = slot
    proposals: list[WordProposal] = []
    for word in words:
        proposals.append(WordProposal(
            placement_id=UUID(int=rng.getrandbits(128), version=4),
            dictionary_word_id=word.id,
            word_text=word.word_text,
            definition=word.definition,
            offset_px=float(offset),
        ))
        offset += word.cells * CELL_WIDTH_PX + MIN_GAP_PX + slot
    return proposals


def generate_segment_proposals(
    segments: Sequence[SegmentSpec],
    candidates: Iterable[WordCandidate],
    *,
    occupied_words: Iterable[str] = (),
    rng: random.Random | None = None,
    choose_action: ActionChooser = random_action,
    attempts: int = DEFAULT_ATTEMPTS,
) -> dict[int, list[WordProposal]]:
    """Create words and offsets for new segments, in route order.

    ``occupied_words`` are the words already present on the other, published
    segments of the route; a word is used at most once per route. Segments with
    no fitting word get an empty list. With ``attempts`` greater than one, the
    best of several randomized layouts is kept: first the one reaching the
    free-space target, then the fewest two-letter words, then the least free space.
    """
    if attempts < 1:
        raise CrosswordGenerationError("At least one generation attempt is required.")
    route_ids = [segment.route_id for segment in segments]
    if len(route_ids) != len(set(route_ids)):
        raise CrosswordGenerationError("A route must not repeat a segment.")
    rng = rng or random.SystemRandom()
    pool = sorted(candidates, key=lambda word: word.id)
    if len({word.id for word in pool}) != len(pool):
        raise CrosswordGenerationError("Dictionary candidates must be unique.")
    blocked = frozenset(normalize_answer(word) for word in occupied_words)
    result: dict[int, list[WordProposal]] = {}
    for spec in segments:
        layouts = (
            _fill(spec.available_px, pool, blocked, rng, choose_action)
            for _ in range(attempts)
        )
        best = min(layouts, key=lambda words: _score(words, spec))
        blocked |= {word.key for word in best}
        result[spec.route_id] = _place(best, spec, rng)
    return result


def route_is_empty(
    proposals: Mapping[int, Sequence[WordProposal]], published_word_count: int = 0
) -> bool:
    """A route without any word is a generation failure, not a win."""
    return published_word_count == 0 and not any(proposals.values())
