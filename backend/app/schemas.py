from __future__ import annotations

import unicodedata
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)

LanguageCode = Annotated[
    str,
    Field(
        min_length=2,
        max_length=5,
        pattern=r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2})?$",
    ),
]


class Schema(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)


class UserRegister(Schema):
    username: str = Field(min_length=1, max_length=32)
    email: EmailStr = Field(max_length=254)
    password: SecretStr = Field(min_length=8, max_length=72)

    @field_validator("username", mode="before")
    @classmethod
    def strip_username(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip()
        return value

    @field_validator("password")
    @classmethod
    def enforce_bcrypt_byte_limit(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value().encode("utf-8")) > 72:
            raise ValueError("Password must not exceed 72 UTF-8 bytes.")
        return value


class UserLogin(Schema):
    email: EmailStr = Field(max_length=254)
    password: SecretStr = Field(min_length=1, max_length=72)

    @field_validator("password")
    @classmethod
    def enforce_bcrypt_byte_limit(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value().encode("utf-8")) > 72:
            raise ValueError("Password must not exceed 72 UTF-8 bytes.")
        return value


class UserRead(Schema):
    id: UUID
    username: str = Field(min_length=1, max_length=32)
    email: EmailStr = Field(max_length=254)
    score: Annotated[int, Field(ge=0)]
    is_active: bool
    created_at: AwareDatetime
    updated_at: AwareDatetime


class CityRead(Schema):
    id: Annotated[int, Field(gt=0)]
    name: str = Field(min_length=1, max_length=120)
    country_code: Annotated[str, Field(pattern=r"^[A-Z]{2}$")]
    top: Annotated[Decimal, Field(ge=0, max_digits=6, decimal_places=2)]
    left: Annotated[Decimal, Field(ge=0, max_digits=6, decimal_places=2)]
    is_capital: bool


class RouteRead(Schema):
    id: Annotated[int, Field(gt=0)]
    source_city_id: Annotated[int, Field(gt=0)]
    target_city_id: Annotated[int, Field(gt=0)]
    length_in_letters: Annotated[int, Field(gt=0, le=32767)]

    @model_validator(mode="after")
    def endpoints_must_differ(self) -> RouteRead:
        if self.source_city_id == self.target_city_id:
            raise ValueError("A route must connect two different cities.")
        return self


class DictionaryWordCreate(Schema):
    word_text: str = Field(min_length=1, max_length=100)
    definition: str = Field(min_length=1)
    language_code: LanguageCode
    letter_count: Annotated[int, Field(gt=0, le=32767)]
    is_active: bool = True

    @field_validator("word_text", "definition", mode="before")
    @classmethod
    def strip_required_text(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip()
        return value


class DictionaryImportWord(Schema):
    word: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1)

    @field_validator("word", mode="before")
    @classmethod
    def normalize_word(cls, value: object) -> object:
        if isinstance(value, str):
            return unicodedata.normalize("NFC", value.strip())
        return value

    @field_validator("description", mode="before")
    @classmethod
    def strip_description(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip()
        return value


class DictionaryImportRequest(Schema):
    dictionary: list[DictionaryImportWord] = Field(min_length=1)


class DictionaryImportResponse(Schema):
    added: Annotated[int, Field(ge=0)]


class DictionaryWordClue(Schema):
    id: Annotated[int, Field(gt=0)]
    definition: str
    language_code: LanguageCode
    letter_count: Annotated[int, Field(gt=0, le=32767)]


class RevealedDictionaryWord(DictionaryWordClue):
    word_text: str = Field(min_length=1, max_length=100)
    guessed_by_user_id: UUID
    guessed_at: AwareDatetime

    @field_validator("guessed_at")
    @classmethod
    def require_utc_guess_time(cls, value: datetime) -> datetime:
        if value.utcoffset() != timedelta(0):
            raise ValueError("guessed_at must be in UTC.")
        return value


class GameResultRead(Schema):
    game_id: UUID
    winner_user_id: UUID | None
    started_at: AwareDatetime
    finished_at: AwareDatetime
    finish_reason: Literal["winner", "timeout"]
    score_awarded: Annotated[int, Field(ge=0)]

    @field_validator("started_at", "finished_at")
    @classmethod
    def require_utc(cls, value: datetime) -> datetime:
        if value.utcoffset() != timedelta(0):
            raise ValueError("Game result timestamps must be in UTC.")
        return value

    @model_validator(mode="after")
    def validate_result_consistency(self) -> GameResultRead:
        if self.finished_at < self.started_at:
            raise ValueError("finished_at must not precede started_at.")
        if self.finish_reason == "winner" and self.winner_user_id is None:
            raise ValueError("A winner result must include winner_user_id.")
        if self.finish_reason == "timeout":
            if self.winner_user_id is not None:
                raise ValueError("A timeout result must not include a winner.")
            if self.score_awarded != 0:
                raise ValueError("A timeout result must award zero points.")
        return self


RoundStatus = Literal["active", "finished", "being_created"]
PlayerState = Literal[
    "watching_stats",
    "selecting_route",
    "playing",
    "won",
    "lost",
]
HintType = Literal["first_letter", "last_letter"]
HintResponseType = Literal["first_letter", "last_letter", "unknown"]
PositiveId = Annotated[int, Field(gt=0)]
NonNegativeInt = Annotated[int, Field(ge=0)]


class RoundProjection(Schema):
    id: UUID | None = None
    status: RoundStatus
    started_at: AwareDatetime | None = None
    expires_at: AwareDatetime | None = None
    remaining_seconds: NonNegativeInt | None = None
    finish_reason: Literal["winner", "timeout"] | None = None
    state_version: NonNegativeInt | None = None

    @field_validator("started_at", "expires_at")
    @classmethod
    def require_utc_round_times(
        cls,
        value: datetime | None,
    ) -> datetime | None:
        if value is not None and value.utcoffset() != timedelta(0):
            raise ValueError("Round timestamps must use UTC.")
        return value

    @model_validator(mode="after")
    def validate_round_finish_reason(self) -> RoundProjection:
        if self.status == "finished" and self.finish_reason is None:
            raise ValueError("A finished round requires finish_reason.")
        if self.status != "finished" and self.finish_reason is not None:
            raise ValueError("Only a finished round may have finish_reason.")
        return self


class PlayerProjection(Schema):
    state: PlayerState
    source_city_id: PositiveId | None = None
    target_city_id: PositiveId | None = None
    route_ids: list[PositiveId] = Field(default_factory=list)
    round_score: int | None = None

    @model_validator(mode="after")
    def validate_assigned_cities(self) -> PlayerProjection:
        if self.source_city_id == self.target_city_id and self.source_city_id is not None:
            raise ValueError("A player must be assigned two different cities.")
        if (self.source_city_id is None) != (self.target_city_id is None):
            raise ValueError("Both assigned cities must be provided together.")
        return self


class LeaderboardEntry(Schema):
    username: str = Field(min_length=1, max_length=32)
    score: NonNegativeInt


class GameStatsResponse(Schema):
    round: RoundProjection
    player: PlayerProjection
    participant_count: NonNegativeInt
    leaderboard: list[LeaderboardEntry]


class GameRoutesResponse(Schema):
    round: RoundProjection
    player: PlayerProjection
    cities: list[CityRead]
    routes: list[RouteRead]


class GameJoinResponse(Schema):
    round: RoundProjection
    player: PlayerProjection


class GameRouteRequest(Schema):
    round_id: UUID
    route_ids: list[PositiveId] = Field(min_length=1)

    @field_validator("route_ids")
    @classmethod
    def require_unique_routes(cls, value: list[int]) -> list[int]:
        if len(value) != len(set(value)):
            raise ValueError("route_ids must not contain duplicates.")
        return value


class CrosswordPlacementResponse(Schema):
    placement_id: UUID
    route_id: PositiveId
    position_index: NonNegativeInt
    letter_count: PositiveId
    definition: str
    is_guessed: bool
    guessed_by_me: bool
    hint_types_used: list[HintType] = Field(default_factory=list)
    revealed_word: str | None = Field(default=None, min_length=1, max_length=100)

    @model_validator(mode="after")
    def only_reveal_solved_words(self) -> CrosswordPlacementResponse:
        if self.revealed_word is not None and not self.is_guessed:
            raise ValueError("An unsolved placement must not reveal its word.")
        return self


class GameRouteResponse(Schema):
    round: RoundProjection
    player: PlayerProjection
    crossword: list[CrosswordPlacementResponse]
    state_version: NonNegativeInt


class GameGuessRequest(Schema):
    round_id: UUID
    placement_id: UUID
    answer: str = Field(min_length=1, max_length=100)


class GuessProjection(Schema):
    result: Literal["correct", "incorrect", "already_solved"]
    already_solved: bool
    points_awarded: NonNegativeInt
    revealed_word: str | None = Field(default=None, min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_revealed_word(self) -> GuessProjection:
        if self.result == "incorrect" and self.revealed_word is not None:
            raise ValueError("An incorrect guess must not reveal the word.")
        if self.result in {"correct", "already_solved"} and self.revealed_word is None:
            raise ValueError("A solved word must be revealed.")
        if self.already_solved != (self.result == "already_solved"):
            raise ValueError("already_solved must match the guess result.")
        return self


class GameGuessResponse(Schema):
    round: RoundProjection
    player: PlayerProjection
    guess: GuessProjection | None = None
    state_version: NonNegativeInt | None = None


class GameHintRequest(Schema):
    round_id: UUID
    placement_id: UUID
    hint_type: HintType


class HintProjection(Schema):
    hint_type: HintResponseType
    value: str
    points_charged: NonNegativeInt
    already_used: bool

    @model_validator(mode="after")
    def validate_unknown_hint(self) -> HintProjection:
        if self.hint_type == "unknown":
            if self.value or self.points_charged or self.already_used:
                raise ValueError("An unavailable hint must not reveal or charge.")
        elif not self.value:
            raise ValueError("An available hint must include a value.")
        return self


class GameHintResponse(Schema):
    round: RoundProjection
    player: PlayerProjection
    hint: HintProjection


class GameSyncQuery(Schema):
    round_id: UUID
    after_version: NonNegativeInt | None = None


class GameSyncResponse(Schema):
    round: RoundProjection
    player: PlayerProjection
    crossword: list[CrosswordPlacementResponse] = Field(default_factory=list)


class ErrorProjection(Schema):
    code: str = Field(min_length=1, max_length=64)
    message: str = Field(min_length=1, max_length=500)
    details: dict[str, object] | list[dict[str, object]] | None = None


class ApiErrorResponse(Schema):
    error: ErrorProjection
    round: RoundProjection | None = None
    player: PlayerProjection | None = None
