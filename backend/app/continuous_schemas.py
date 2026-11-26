"""Request and response schemas of the continuous-game API (ADR 0009).

Responses are serialized with ``exclude_unset``: a field appears only when the
handler sets it explicitly, so absent data (for example ``word_text`` of an
unsolved word) is omitted while an explicit ``session=None`` stays ``null``.
"""
from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from app.schemas import (
    HintType,
    LeaderboardEntry,
    NonNegativeInt,
    PositiveId,
    RouteRead,
    Schema,
)

SessionStatus = Literal["active", "finishing", "finished"]
SessionPlayerState = Literal[
    "watching_stats", "selecting_route", "playing", "finishing", "won"
]
HintResultType = Literal["first_letter", "last_letter", "unknown"]
WordOrientation = Literal["along", "perpendicular"]


class SessionProjection(Schema):
    id: UUID
    status: SessionStatus
    finish_reason: Literal["winner"] | None
    state_version: Annotated[int, Field(gt=0)]

    @model_validator(mode="after")
    def validate_finish_reason(self) -> SessionProjection:
        if (self.status == "finished") != (self.finish_reason == "winner"):
            raise ValueError("Only a finished session has finish_reason winner.")
        return self


class PlayerProjection(Schema):
    state: SessionPlayerState
    session_score: int | None = None
    source_city_id: PositiveId | None = None
    target_city_id: PositiveId | None = None
    route_ids: list[PositiveId] | None = None


class CityProjection(Schema):
    id: PositiveId
    name: str
    country_code: str
    top: float
    left: float
    is_capital: bool


class HintEntry(Schema):
    hint_type: HintType
    value: str


class CrosswordPlacement(Schema):
    placement_id: UUID
    route_id: PositiveId
    offset_px: Annotated[float, Field(ge=0, allow_inf_nan=False)]
    orientation: WordOrientation = "along"
    y_offset_px: float = Field(default=0, allow_inf_nan=False)
    crossing_id: UUID | None = None
    crossing_index: int | None = Field(default=None, ge=0)
    letter_count: PositiveId
    definition: str
    is_guessed: bool
    guessed_by_me: bool
    hint_types_used: list[HintType]
    hints: list[HintEntry]
    word_text: str | None = Field(default=None, min_length=1, max_length=100)

    @model_validator(mode="after")
    def only_reveal_solved_words(self) -> CrosswordPlacement:
        if self.word_text is not None and not self.is_guessed:
            raise ValueError("An unsolved placement must not reveal its word.")
        if (self.crossing_id is None) != (self.crossing_index is None):
            raise ValueError("Crossing ID and index must be set together.")
        if self.orientation == "perpendicular" and self.crossing_id is None:
            raise ValueError("A perpendicular word must belong to a crossing pair.")
        if self.orientation == "along" and self.y_offset_px != 0:
            raise ValueError("An along word must lie on the route axis.")
        if self.crossing_index is not None and not 0 < self.crossing_index < self.letter_count - 1:
            raise ValueError("The crossing cell must be inside the word.")
        if self.orientation == "perpendicular" and not 3 <= self.letter_count <= 7:
            raise ValueError("A perpendicular word must contain 3 to 7 cells.")
        return self


class StatsResponse(Schema):
    session: SessionProjection | None
    player: PlayerProjection
    participant_count: NonNegativeInt
    leaderboard: list[LeaderboardEntry]


class RoutesResponse(Schema):
    session: SessionProjection | None
    player: PlayerProjection
    cities: list[CityProjection]
    routes: list[RouteRead]
    crossword: list[CrosswordPlacement] | None = None


class SessionResponse(Schema):
    session: SessionProjection
    player: PlayerProjection


class SessionRequest(Schema):
    session_id: UUID


class AutoRouteResponse(SessionResponse):
    route_ids: Annotated[list[PositiveId], Field(min_length=1)]


class RouteRequest(SessionRequest):
    route_ids: Annotated[list[PositiveId], Field(min_length=1)]

    @field_validator("route_ids")
    @classmethod
    def require_unique_routes(cls, value: list[int]) -> list[int]:
        if len(value) != len(set(value)):
            raise ValueError("route_ids must not contain duplicates.")
        return value


class RouteResponse(SessionResponse):
    crossword: list[CrosswordPlacement] | None = None


class GuessRequest(SessionRequest):
    placement_id: UUID
    answer: str = Field(min_length=1, max_length=100)


class GuessProjection(Schema):
    result: Literal["correct", "incorrect", "already_solved"]
    already_solved: bool
    points_awarded: NonNegativeInt
    revealed_word: str | None = Field(default=None, min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_revealed_word(self) -> GuessProjection:
        if (self.result == "incorrect") != (self.revealed_word is None):
            raise ValueError("Only an incorrect guess keeps the word hidden.")
        if self.already_solved != (self.result == "already_solved"):
            raise ValueError("already_solved must match the guess result.")
        return self


class GuessResponse(SessionResponse):
    guess: GuessProjection | None = None


class HintRequest(SessionRequest):
    placement_id: UUID
    hint_type: HintType


class HintProjection(Schema):
    placement_id: UUID
    hint_type: HintResultType
    value: str | None
    points_charged: NonNegativeInt

    @model_validator(mode="after")
    def validate_unknown_hint(self) -> HintProjection:
        if (self.hint_type == "unknown") != (self.value is None):
            raise ValueError("Only an unavailable hint has no value.")
        if self.hint_type == "unknown" and self.points_charged:
            raise ValueError("An unavailable hint must not charge points.")
        return self


class HintResponse(SessionResponse):
    hint: HintProjection


class SurrenderProjection(Schema):
    session_id: UUID
    result: Literal["surrendered"]


class SurrenderResponse(Schema):
    surrender: SurrenderProjection | None = None
    session: SessionProjection | None = None
    player: PlayerProjection | None = None


class SyncResponse(Schema):
    session: SessionProjection | None
    player: PlayerProjection
    crossword: list[CrosswordPlacement] | None = None
