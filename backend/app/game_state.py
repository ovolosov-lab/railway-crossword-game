from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Annotated, Awaitable, Callable, Literal, Self
from uuid import UUID, uuid4

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    field_validator,
    model_validator,
)

from app.config import Settings, get_settings


HintType = Literal["first_letter", "last_letter"]
HintResponseType = Literal["first_letter", "last_letter", "unknown"]
FinishReason = Literal["winner", "timeout"]
PositiveId = Annotated[int, Field(gt=0)]


def _normalize_guess(value: str) -> str:
    """Apply only the agreed trimming, casing, and Russian yo normalization."""
    return value.strip().lower().replace("ё", "е")


class GameStateError(Exception):
    """Base exception for invalid active-game operations."""


class GameFinishedError(GameStateError):
    pass


class GameExpiredError(GameStateError):
    pass


class RoundNotExpiredError(GameStateError):
    pass


class PlayerNotFoundError(GameStateError):
    pass


class RouteNotAvailableError(GameStateError):
    pass


class WordNotAvailableError(GameStateError):
    pass


class WordAlreadyGuessedError(GameStateError):
    pass


class HintUnavailableError(GameStateError):
    pass


class InsufficientScoreError(GameStateError):
    pass


class DomainModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _require_utc(value: datetime) -> datetime:
    if value.utcoffset() != timedelta(0):
        raise ValueError("Datetime values must use UTC.")
    return value


class HintUsed(DomainModel):
    first_letter_revealed: bool = False
    last_letter_revealed: bool = False

    def is_used(self, hint_type: HintType) -> bool:
        return {
            "first_letter": self.first_letter_revealed,
            "last_letter": self.last_letter_revealed,
        }[hint_type]

    def mark_used(self, hint_type: HintType) -> None:
        field_name = {
            "first_letter": "first_letter_revealed",
            "last_letter": "last_letter_revealed",
        }[hint_type]
        setattr(self, field_name, True)


class WordPlacement(DomainModel):
    placement_id: UUID = Field(default_factory=uuid4)
    dictionary_word_id: PositiveId
    route_id: PositiveId
    position_index: Annotated[int, Field(ge=0)]
    letter_count: Annotated[int, Field(gt=0, le=32767)]
    guessed_by_user_id: UUID | None = None
    guessed_at: AwareDatetime | None = None

    @field_validator("guessed_at")
    @classmethod
    def validate_guess_time(cls, value: AwareDatetime | None) -> AwareDatetime | None:
        if value is not None:
            _require_utc(value)
        return value

    @model_validator(mode="after")
    def validate_guess_state(self) -> Self:
        if (self.guessed_by_user_id is None) != (self.guessed_at is None):
            raise ValueError("guessed_by_user_id and guessed_at must be set together.")
        return self

    @property
    def is_guessed(self) -> bool:
        return self.guessed_by_user_id is not None


class PlayerGameState(DomainModel):
    user_id: UUID
    start_city_id: PositiveId
    end_city_id: PositiveId
    chosen_route_ids: list[PositiveId] = Field(default_factory=list)
    hints: dict[PositiveId, HintUsed] = Field(default_factory=dict)
    starting_score: Annotated[int, Field(ge=0)] = 0
    score_earned: Annotated[int, Field(ge=0)] = 0
    hint_points_spent: Annotated[int, Field(ge=0)] = 0

    @model_validator(mode="after")
    def validate_cities_and_route(self) -> Self:
        if self.start_city_id == self.end_city_id:
            raise ValueError("A player must be assigned two different cities.")
        if len(self.chosen_route_ids) != len(set(self.chosen_route_ids)):
            raise ValueError("A chosen route cannot contain a route more than once.")
        return self

    @property
    def score_balance(self) -> int:
        return self.starting_score + self.score_earned - self.hint_points_spent


class HintResult(DomainModel):
    hint_type: HintResponseType
    value: str
    points_charged: Annotated[int, Field(ge=0)]
    already_used: bool
    score_balance: int


class GuessResult(DomainModel):
    is_correct: bool
    already_guessed: bool
    guessed_by_user_id: UUID | None
    guessed_at: AwareDatetime | None
    points_awarded: Annotated[int, Field(ge=0)]
    winner_user_id: UUID | None
    game_finished: bool


class GameFinishResult(DomainModel):
    """Describe the terminal result persisted for a completed round."""

    game_id: UUID
    winner_user_id: UUID | None
    finished_at: AwareDatetime
    finish_reason: FinishReason
    score_awarded: Annotated[int, Field(ge=0)]

    @field_validator("finished_at")
    @classmethod
    def validate_finish_time(cls, value: AwareDatetime) -> AwareDatetime:
        _require_utc(value)
        return value


class PrevGame(DomainModel):
    """Keep the most recent terminal round available for response recovery."""

    round_id: UUID | None = None
    winner_user_id: UUID | None = None
    finish_reason: FinishReason | None = None

    @classmethod
    def from_finished_game(cls, game: ActiveGame) -> Self:
        """Create a recovery snapshot from a terminal round."""
        if game.status != "finished" or game.finish_reason is None:
            raise GameStateError("Only a finished round can be preserved.")
        return cls(
            round_id=game.game_id,
            winner_user_id=game.winner_user_id,
            finish_reason=game.finish_reason,
        )


class ActiveGame(DomainModel):
    """Single-process round state; all state changes go through locked methods."""

    game_id: UUID
    started_at: AwareDatetime
    expires_at: AwareDatetime
    status: Literal["active", "finished"] = "active"
    winner_user_id: UUID | None = None
    finished_at: AwareDatetime | None = None
    finish_reason: FinishReason | None = None
    score_awarded: Annotated[int, Field(ge=0)] = 0
    state_version: Annotated[int, Field(ge=0)] = 0
    players: dict[UUID, PlayerGameState] = Field(default_factory=dict)
    shared_words: dict[PositiveId, list[WordPlacement]] = Field(default_factory=dict)
    hint_first_letter_cost: Annotated[int, Field(ge=0)]
    hint_last_letter_cost: Annotated[int, Field(ge=0)]

    _lock: asyncio.Lock = PrivateAttr(default_factory=asyncio.Lock)

    @field_validator("started_at", "expires_at", "finished_at")
    @classmethod
    def validate_utc_timestamps(
        cls, value: AwareDatetime | None
    ) -> AwareDatetime | None:
        if value is not None:
            _require_utc(value)
        return value

    @model_validator(mode="after")
    def validate_round_state(self) -> Self:
        if self.expires_at <= self.started_at:
            raise ValueError("expires_at must be later than started_at.")
        if self.status == "active":
            if self.winner_user_id is not None or self.finished_at is not None:
                raise ValueError("An active game cannot have a winner or finish time.")
            if self.finish_reason is not None:
                raise ValueError("An active game cannot have a finish reason.")
        else:
            if self.finished_at is None or self.finish_reason is None:
                raise ValueError("A finished game requires finish time and reason.")
            if self.finished_at < self.started_at:
                raise ValueError("finished_at cannot precede started_at.")
            if self.finish_reason == "winner" and self.winner_user_id is None:
                raise ValueError("A winner finish requires winner_user_id.")
            if self.finish_reason == "timeout":
                if self.winner_user_id is not None or self.score_awarded != 0:
                    raise ValueError("A timeout must have no winner and award zero points.")

        for user_id, player in self.players.items():
            if player.user_id != user_id:
                raise ValueError("Player map keys must match each player's user_id.")
        for route_id, placements in self.shared_words.items():
            word_ids = [placement.dictionary_word_id for placement in placements]
            positions = [placement.position_index for placement in placements]
            if any(placement.route_id != route_id for placement in placements):
                raise ValueError("Shared word placements must match their route key.")
            if len(word_ids) != len(set(word_ids)):
                raise ValueError("A route cannot contain the same dictionary word twice.")
            if len(positions) != len(set(positions)):
                raise ValueError("Word positions must be unique on a route.")
        return self

    @classmethod
    def create(
        cls,
        *,
        settings: Settings | None = None,
        started_at: datetime | None = None,
        game_id: UUID | None = None,
    ) -> Self:
        settings = settings or get_settings()
        start_time = started_at or datetime.now(timezone.utc)
        _require_utc(start_time)
        return cls(
            game_id=game_id or uuid4(),
            started_at=start_time,
            expires_at=start_time + timedelta(hours=settings.game_duration_hours),
            hint_first_letter_cost=settings.hint_first_letter_cost,
            hint_last_letter_cost=settings.hint_last_letter_cost,
        )

    @property
    def lock(self) -> asyncio.Lock:
        return self._lock

    def _time(self, value: datetime | None) -> datetime:
        current = value or datetime.now(timezone.utc)
        _require_utc(current)
        if current < self.started_at:
            raise ValueError("Operation time cannot precede the round start.")
        return current

    def _finish(
        self,
        *,
        finished_at: datetime,
        reason: FinishReason,
        winner_user_id: UUID | None = None,
        score_awarded: int = 0,
    ) -> GameFinishResult:
        self.status = "finished"
        self.finished_at = finished_at
        self.finish_reason = reason
        self.winner_user_id = winner_user_id
        self.score_awarded = score_awarded
        self._touch()
        return GameFinishResult(
            game_id=self.game_id,
            winner_user_id=winner_user_id,
            finished_at=finished_at,
            finish_reason=reason,
            score_awarded=score_awarded,
        )

    def _touch(self) -> None:
        self.state_version += 1

    def _ensure_active(self, now: datetime) -> None:
        if self.status != "active":
            raise GameFinishedError("The round has already finished.")
        if now >= self.expires_at:
            self._finish(finished_at=now, reason="timeout")
            raise GameExpiredError("The round has expired.")

    def _player(self, user_id: UUID) -> PlayerGameState:
        try:
            return self.players[user_id]
        except KeyError as exc:
            raise PlayerNotFoundError("The player is not in this round.") from exc

    def _placement(self, route_id: int, dictionary_word_id: int) -> WordPlacement:
        for placement in self.shared_words.get(route_id, []):
            if placement.dictionary_word_id == dictionary_word_id:
                return placement
        raise WordNotAvailableError("The word is not placed on this route.")

    def _player_completed(self, player: PlayerGameState) -> bool:
        """Return whether every word on the player's selected routes is solved."""
        if not player.chosen_route_ids:
            return False
        if any(route_id not in self.shared_words for route_id in player.chosen_route_ids):
            return False
        return all(
            placement.is_guessed
            for route_id in player.chosen_route_ids
            for placement in self.shared_words[route_id]
        )

    async def add_player(
        self,
        user_id: UUID,
        start_city_id: int,
        end_city_id: int,
        *,
        starting_score: int = 0,
        now: datetime | None = None,
    ) -> PlayerGameState:
        current = self._time(now)
        async with self._lock:
            self._ensure_active(current)
            if user_id in self.players:
                raise GameStateError("The player already belongs to this round.")
            if start_city_id == end_city_id:
                raise ValueError("A player must be assigned two different cities.")
            assigned_pair = frozenset((start_city_id, end_city_id))
            if any(
                frozenset((player.start_city_id, player.end_city_id)) == assigned_pair
                for player in self.players.values()
            ):
                raise GameStateError("This city pair is already assigned in the round.")
            player = PlayerGameState(
                user_id=user_id,
                start_city_id=start_city_id,
                end_city_id=end_city_id,
                starting_score=starting_score,
            )
            self.players[user_id] = player
            self._touch()
            return player

    async def add_shared_words(
        self,
        route_id: int,
        placements: Sequence[WordPlacement],
        *,
        now: datetime | None = None,
    ) -> None:
        current = self._time(now)
        async with self._lock:
            self._ensure_active(current)
            if route_id in self.shared_words:
                raise RouteNotAvailableError("Words have already been assigned to this route.")
            if not placements:
                raise ValueError("A shared route must have at least one word.")
            route_placements = [placement.model_copy(deep=True) for placement in placements]
            if any(placement.route_id != route_id for placement in route_placements):
                raise ValueError("Every word placement must belong to the given route.")
            self._validate_player_word_uniqueness(route_id, route_placements)
            self.shared_words[route_id] = route_placements
            self._touch()

    def _validate_player_word_uniqueness(
        self,
        route_id: int,
        new_placements: Sequence[WordPlacement],
    ) -> None:
        new_word_ids = [placement.dictionary_word_id for placement in new_placements]
        if len(new_word_ids) != len(set(new_word_ids)):
            raise ValueError("A route cannot repeat a dictionary word.")
        for player in self.players.values():
            if route_id not in player.chosen_route_ids:
                continue
            existing_ids = {
                placement.dictionary_word_id
                for selected_route_id in player.chosen_route_ids
                if selected_route_id != route_id
                for placement in self.shared_words.get(selected_route_id, [])
            }
            if existing_ids.intersection(new_word_ids):
                raise ValueError("A player's route cannot repeat a dictionary word.")

    def _validate_route(
        self,
        player: PlayerGameState,
        route_ids: Sequence[int],
        route_endpoints: Mapping[int, tuple[int, int]],
        *,
        placements_by_route: Mapping[int, Sequence[WordPlacement]] | None = None,
    ) -> None:
        chosen_route_ids = list(route_ids)
        if not chosen_route_ids:
            raise ValueError("A player's route cannot be empty.")
        if len(chosen_route_ids) != len(set(chosen_route_ids)):
            raise ValueError("A chosen route cannot contain a route more than once.")

        current_city = player.start_city_id
        visited_cities = {current_city}
        seen_word_ids: set[int] = set()
        for route_id in chosen_route_ids:
            if (
                route_id not in self.shared_words
                and (placements_by_route is None or route_id not in placements_by_route)
            ):
                raise RouteNotAvailableError("A selected route is not available.")
            try:
                endpoint_a, endpoint_b = route_endpoints[route_id]
            except KeyError as exc:
                raise RouteNotAvailableError(
                    f"Route {route_id} has no endpoint data."
                ) from exc

            if current_city == endpoint_a:
                next_city = endpoint_b
            elif current_city == endpoint_b:
                next_city = endpoint_a
            else:
                raise RouteNotAvailableError(
                    "Selected route segments are not connected."
                )
            if next_city in visited_cities:
                raise RouteNotAvailableError("A player's route cannot contain a cycle.")
            current_city = next_city
            visited_cities.add(current_city)

            placements = self.shared_words.get(
                route_id,
                (placements_by_route or {}).get(route_id, ()),
            )
            for placement in placements:
                if placement.dictionary_word_id in seen_word_ids:
                    raise ValueError("A player's route cannot repeat a dictionary word.")
                seen_word_ids.add(placement.dictionary_word_id)

        if current_city != player.end_city_id:
            raise RouteNotAvailableError(
                "The selected route does not connect the assigned cities."
            )

    async def validate_player_route(
        self,
        user_id: UUID,
        route_ids: Sequence[int],
        route_endpoints: Mapping[int, tuple[int, int]],
        *,
        now: datetime | None = None,
    ) -> None:
        current = self._time(now)
        async with self._lock:
            self._ensure_active(current)
            player = self._player(user_id)
            if player.chosen_route_ids:
                if player.chosen_route_ids == list(route_ids):
                    return
                raise GameStateError("The player's route has already been confirmed.")
            self._validate_route(
                player,
                route_ids,
                route_endpoints,
                placements_by_route={route_id: () for route_id in route_ids},
            )

    async def confirm_player_route(
        self,
        user_id: UUID,
        route_ids: Sequence[int],
        route_endpoints: Mapping[int, tuple[int, int]],
        placements_by_route: Mapping[int, Sequence[WordPlacement]],
        *,
        now: datetime | None = None,
    ) -> bool:
        current = self._time(now)
        async with self._lock:
            self._ensure_active(current)
            player = self._player(user_id)
            chosen_route_ids = list(route_ids)
            if player.chosen_route_ids:
                if player.chosen_route_ids == chosen_route_ids:
                    return True
                raise GameStateError("The player's route has already been confirmed.")

            if set(placements_by_route) != set(chosen_route_ids):
                raise ValueError("Placements must be supplied for every selected route.")
            self._validate_route(
                player,
                chosen_route_ids,
                route_endpoints,
                placements_by_route=placements_by_route,
            )
            if not any(
                self.shared_words.get(
                    route_id,
                    placements_by_route[route_id],
                )
                for route_id in chosen_route_ids
            ):
                self.players.pop(user_id)
                self._touch()
                return False

            new_route_placements: dict[int, list[WordPlacement]] = {}
            for route_id in chosen_route_ids:
                if route_id not in self.shared_words:
                    copied_placements = [
                        placement.model_copy(deep=True)
                        for placement in placements_by_route[route_id]
                    ]
                    if any(
                        placement.route_id != route_id
                        for placement in copied_placements
                    ):
                        raise ValueError(
                            "Every word placement must belong to its selected route."
                        )
                    self._validate_player_word_uniqueness(
                        route_id,
                        copied_placements,
                    )
                    new_route_placements[route_id] = copied_placements

            self.shared_words.update(new_route_placements)
            player.chosen_route_ids = chosen_route_ids
            self._touch()
            return True

    async def set_player_route(
        self,
        user_id: UUID,
        route_ids: Sequence[int],
        route_endpoints: Mapping[int, tuple[int, int]],
        *,
        now: datetime | None = None,
    ) -> None:
        current = self._time(now)
        async with self._lock:
            self._ensure_active(current)
            player = self._player(user_id)
            chosen_route_ids = list(route_ids)
            self._validate_route(player, chosen_route_ids, route_endpoints)
            player.chosen_route_ids = chosen_route_ids
            self._touch()

    async def use_hint(
        self,
        user_id: UUID,
        route_id: int,
        dictionary_word_id: int,
        hint_type: HintType,
        value: str | None,
        *,
        now: datetime | None = None,
    ) -> HintResult:
        """Apply one available hint charge per player, word, and hint type."""
        current = self._time(now)
        async with self._lock:
            self._ensure_active(current)
            player = self._player(user_id)
            if route_id not in player.chosen_route_ids:
                raise WordNotAvailableError("The word is not on this player's route.")
            placement = self._placement(route_id, dictionary_word_id)
            used_hints = player.hints.get(dictionary_word_id)
            if placement.is_guessed:
                return HintResult(
                    hint_type="unknown",
                    value="",
                    points_charged=0,
                    already_used=False,
                    score_balance=player.score_balance,
                )
            if used_hints is not None and used_hints.is_used(hint_type):
                if value is None:
                    return HintResult(
                        hint_type="unknown",
                        value="",
                        points_charged=0,
                        already_used=False,
                        score_balance=player.score_balance,
                    )
                return HintResult(
                    hint_type=hint_type,
                    value=value,
                    points_charged=0,
                    already_used=True,
                    score_balance=player.score_balance,
                )
            if value is None or not value:
                return HintResult(
                    hint_type="unknown",
                    value="",
                    points_charged=0,
                    already_used=False,
                    score_balance=player.score_balance,
                )

            cost = {
                "first_letter": self.hint_first_letter_cost,
                "last_letter": self.hint_last_letter_cost,
            }[hint_type]
            if player.score_balance < cost:
                raise InsufficientScoreError("The player does not have enough points.")
            used_hints = player.hints.setdefault(dictionary_word_id, HintUsed())
            used_hints.mark_used(hint_type)
            player.hint_points_spent += cost
            self._touch()
            return HintResult(
                hint_type=hint_type,
                value=value,
                points_charged=cost,
                already_used=False,
                score_balance=player.score_balance,
            )

    async def guess_word(
        self,
        user_id: UUID,
        route_id: int,
        dictionary_word_id: int,
        answer: str,
        expected_word: str,
        *,
        now: datetime | None = None,
        before_finish: Callable[[GameFinishResult], Awaitable[None]] | None = None,
    ) -> GuessResult:
        """Commit a guess only after any required winner transaction succeeds."""
        current = self._time(now)
        async with self._lock:
            tentative = ActiveGame.model_validate(self.model_dump())
            try:
                result = tentative._guess_word_unlocked(
                    user_id,
                    route_id,
                    dictionary_word_id,
                    answer,
                    expected_word,
                    current,
                )
            except GameExpiredError:
                self._apply_guess_snapshot(tentative)
                raise

            if tentative.status == "finished" and before_finish is not None:
                await before_finish(tentative._finish_result())
            self._apply_guess_snapshot(tentative)
            return result

    def _guess_word_unlocked(
        self,
        user_id: UUID,
        route_id: int,
        dictionary_word_id: int,
        answer: str,
        expected_word: str,
        current: datetime,
    ) -> GuessResult:
        """Calculate a guess outcome on an isolated round snapshot."""
        self._ensure_active(current)
        player = self._player(user_id)
        if route_id not in player.chosen_route_ids:
            raise WordNotAvailableError("The word is not on this player's route.")
        placement = self._placement(route_id, dictionary_word_id)

        if placement.is_guessed:
            if self._player_completed(player):
                final_score = max(0, player.score_earned - player.hint_points_spent)
                self._finish(
                    finished_at=current,
                    reason="winner",
                    winner_user_id=user_id,
                    score_awarded=final_score,
                )
            return GuessResult(
                is_correct=True,
                already_guessed=True,
                guessed_by_user_id=placement.guessed_by_user_id,
                guessed_at=placement.guessed_at,
                points_awarded=0,
                winner_user_id=self.winner_user_id,
                game_finished=self.status == "finished",
            )

        normalized_answer = _normalize_guess(answer)
        if normalized_answer != _normalize_guess(expected_word):
            return GuessResult(
                is_correct=False,
                already_guessed=False,
                guessed_by_user_id=None,
                guessed_at=None,
                points_awarded=0,
                winner_user_id=None,
                game_finished=False,
            )

        placement.guessed_by_user_id = user_id
        placement.guessed_at = current
        points_awarded = sum(character.isalpha() for character in normalized_answer) * 10
        player.score_earned += points_awarded

        completed_players = [
            candidate
            for candidate in self.players.values()
            if self._player_completed(candidate)
        ]
        winner = next(
            (candidate for candidate in completed_players if candidate.user_id == user_id),
            completed_players[0] if completed_players else None,
        )
        if winner is not None:
            final_score = max(0, winner.score_earned - winner.hint_points_spent)
            self._finish(
                finished_at=current,
                reason="winner",
                winner_user_id=winner.user_id,
                score_awarded=final_score,
            )
        else:
            self._touch()

        return GuessResult(
            is_correct=True,
            already_guessed=False,
            guessed_by_user_id=user_id,
            guessed_at=current,
            points_awarded=points_awarded,
            winner_user_id=self.winner_user_id,
            game_finished=self.status == "finished",
        )

    def _apply_guess_snapshot(self, snapshot: ActiveGame) -> None:
        """Publish the validated temporary snapshot after persistence succeeds."""
        self.status = snapshot.status
        self.winner_user_id = snapshot.winner_user_id
        self.finished_at = snapshot.finished_at
        self.finish_reason = snapshot.finish_reason
        self.score_awarded = snapshot.score_awarded
        self.state_version = snapshot.state_version
        self.players = snapshot.players
        self.shared_words = snapshot.shared_words

    async def finish_timeout(
        self,
        *,
        now: datetime | None = None,
    ) -> GameFinishResult:
        current = self._time(now)
        async with self._lock:
            if self.status == "finished":
                if self.finish_reason == "timeout":
                    return self._finish_result()
                raise GameFinishedError("The round has already finished with a winner.")
            if current < self.expires_at:
                raise RoundNotExpiredError("The round has not reached its expiration time.")
            return self._finish(finished_at=current, reason="timeout")

    def _finish_result(self) -> GameFinishResult:
        if self.status != "finished" or self.finished_at is None or self.finish_reason is None:
            raise GameStateError("The round is not finished.")
        return GameFinishResult(
            game_id=self.game_id,
            winner_user_id=self.winner_user_id,
            finished_at=self.finished_at,
            finish_reason=self.finish_reason,
            score_awarded=self.score_awarded,
        )
