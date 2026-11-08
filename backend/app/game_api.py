from __future__ import annotations

import asyncio
import itertools
import math
import secrets
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, status
from loguru import logger
from sqlalchemy import exists, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api_errors import ApiException
from app.config import get_settings
from app.database import get_db
from app.game_state import (
    ActiveGame,
    GameExpiredError,
    GameFinishResult,
    GameFinishedError,
    GameStateError,
    InsufficientScoreError,
    PlayerNotFoundError,
    PrevGame,
    RouteNotAvailableError,
    WordNotAvailableError,
    WordPlacement,
)
from app.models import City, DictionaryWord, GameResult, Route, User
from app.schemas import (
    CityRead,
    CrosswordPlacementResponse,
    GameJoinResponse,
    GameGuessRequest,
    GameGuessResponse,
    GameHintRequest,
    GameHintResponse,
    GameRouteRequest,
    GameRouteResponse,
    GameRoutesResponse,
    GameStatsResponse,
    GameSyncQuery,
    GameSyncResponse,
    GuessProjection,
    HintProjection,
    LeaderboardEntry,
    PlayerProjection,
    RoundProjection,
    RouteRead,
)
from app.security import get_current_user, require_csrf_token


router = APIRouter(
    prefix="/game",
    tags=["game"],
    dependencies=[Depends(get_current_user)],
)
write_router = APIRouter(
    prefix="/game",
    tags=["game"],
    dependencies=[Depends(get_current_user), Depends(require_csrf_token)],
)
CROSSWORD_ENDPOINT_RESERVED_CELLS = 2


def _player_projection(
    game: ActiveGame | None,
    user_id: UUID,
) -> PlayerProjection:
    """Project only the current user's public gameplay state."""
    if game is None:
        return PlayerProjection(state="watching_stats")

    player = game.players.get(user_id)
    if player is None:
        return PlayerProjection(state="watching_stats")

    if game.status == "finished":
        player_state = "won" if game.winner_user_id == user_id else "lost"
    elif player.chosen_route_ids:
        player_state = "playing"
    else:
        player_state = "selecting_route"

    return PlayerProjection(
        state=player_state,
        source_city_id=player.start_city_id,
        target_city_id=player.end_city_id,
        route_ids=player.chosen_route_ids.copy(),
        round_score=player.score_earned - player.hint_points_spent,
    )


def _round_projection(
    game: ActiveGame | None,
    now: datetime,
    *,
    status_override: Literal["active", "finished", "being_created"] | None = None,
) -> RoundProjection:
    if game is None:
        return RoundProjection(id=None, status="being_created", state_version=0)

    return RoundProjection(
        id=game.game_id,
        status=status_override or game.status,
        started_at=game.started_at,
        expires_at=game.expires_at,
        remaining_seconds=(
            max(0, math.ceil((game.expires_at - now).total_seconds()))
            if game.status == "active"
            else 0
        ),
        finish_reason=game.finish_reason,
        state_version=game.state_version,
    )


def _state_error(
    *,
    code: str,
    message: str,
    round_projection: RoundProjection,
    player_projection: PlayerProjection,
    status_code: int = status.HTTP_409_CONFLICT,
    details: dict[str, object] | None = None,
) -> ApiException:
    """Build the standard error envelope with current round/player state."""
    return ApiException(
        status_code=status_code,
        code=code,
        message=message,
        details=details,
        round_projection=round_projection.model_dump(mode="json"),
        player_projection=player_projection.model_dump(mode="json"),
    )


def _guess_rate_limit(
    request: Request,
    user_id: UUID,
    *,
    now: datetime,
    limit: int,
) -> int | None:
    """Record a guess attempt or return seconds until its rate window opens."""
    attempts: dict[UUID, deque[datetime]] = getattr(
        request.app.state,
        "guess_rate_limits",
        {},
    )
    cutoff = now - timedelta(minutes=1)
    for existing_user_id, timestamps in list(attempts.items()):
        while timestamps and timestamps[0] <= cutoff:
            timestamps.popleft()
        if not timestamps:
            attempts.pop(existing_user_id, None)

    user_attempts = attempts.setdefault(user_id, deque())
    if len(user_attempts) >= limit:
        retry_after = math.ceil((user_attempts[0] + timedelta(minutes=1) - now).total_seconds())
        return max(1, retry_after)

    user_attempts.append(now)
    request.app.state.guess_rate_limits = attempts
    return None


def _word_hint(word_text: str, hint_type: Literal["first_letter", "last_letter"]) -> str | None:
    """Return one endpoint letter without exposing the full dictionary answer."""
    letters = [character for character in word_text if character.isalpha()]
    if not letters:
        return None
    return letters[0] if hint_type == "first_letter" else letters[-1]


def _previous_round_response(
    previous: PrevGame,
    user_id: UUID,
) -> GameGuessResponse:
    """Return the saved terminal outcome without replaying the previous round."""
    if previous.round_id is None or previous.finish_reason is None:
        raise ValueError("The previous round snapshot is incomplete.")
    round_projection = RoundProjection(
        id=previous.round_id,
        status="finished",
        finish_reason=previous.finish_reason,
    )
    state = "won" if previous.winner_user_id == user_id else "lost"
    return GameGuessResponse(
        round=round_projection,
        player=PlayerProjection(state=state),
    )


def _advance_finished_game(request: Request, game: ActiveGame, now: datetime) -> None:
    """Preserve a terminal round before starting its successor."""
    if game.status != "finished":
        raise ValueError("Only a finished round can be advanced.")
    previous: PrevGame | None = getattr(request.app.state, "prevGame", None)
    if previous is not None and previous.round_id == game.game_id:
        return
    request.app.state.prevGame = PrevGame.from_finished_game(game)
    game_settings = getattr(request.app.state, "game_settings", None)
    if game_settings is None:
        game_settings = get_settings()
    request.app.state.activeGame = ActiveGame.create(
        settings=game_settings,
        started_at=game.finished_at or now,
    )


async def _round_and_player_projection(
    request: Request,
    user_id: UUID,
) -> tuple[RoundProjection, PlayerProjection, ActiveGame | None]:
    game: ActiveGame | None = getattr(request.app.state, "activeGame", None)
    if game is None:
        return (
            RoundProjection(
                id=None,
                status="being_created",
                state_version=0,
            ),
            PlayerProjection(state="watching_stats"),
            None,
        )

    game_lock: asyncio.Lock | None = getattr(
        request.app.state,
        "game_lock",
        None,
    )
    if game_lock is None:
        logger.error("Active game exists without its application state lock")
        raise ApiException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            code="service_unavailable",
            message="Game state is temporarily unavailable.",
        )

    async with game_lock:
        game = getattr(request.app.state, "activeGame", None)
        if game is None:
            return (
                RoundProjection(
                    id=None,
                    status="being_created",
                    state_version=0,
                ),
                PlayerProjection(state="watching_stats"),
                None,
            )

        async with game.lock:
            now = datetime.now(timezone.utc)
            return _round_projection(game, now), _player_projection(game, user_id), game


@router.get("/stats/", response_model=GameStatsResponse)
async def get_game_stats(
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> GameStatsResponse:
    round_projection, player_projection, game = await _round_and_player_projection(
        request,
        user.id,
    )
    try:
        winner_exists = exists(
            select(GameResult.game_id).where(
                GameResult.winner_user_id == User.id,
                GameResult.finish_reason == "winner",
            )
        )
        leaderboard_rows = await session.execute(
            select(User.username, User.score)
            .where(winner_exists)
            .order_by(User.score.desc(), User.username.asc(), User.id.asc())
        )
    except SQLAlchemyError as exc:
        logger.exception("Could not load game statistics leaderboard")
        raise ApiException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            code="service_unavailable",
            message="Game statistics are temporarily unavailable.",
        ) from exc

    leaderboard = [
        LeaderboardEntry(username=username, score=score)
        for username, score in leaderboard_rows.all()
    ]
    return GameStatsResponse(
        round=round_projection,
        player=player_projection,
        participant_count=len(game.players) if game is not None else 0,
        leaderboard=leaderboard,
    )


@router.get("/routes/", response_model=GameRoutesResponse)
async def get_game_routes(
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> GameRoutesResponse:
    round_projection, player_projection, _ = await _round_and_player_projection(
        request,
        user.id,
    )
    try:
        city_result = await session.scalars(select(City).order_by(City.id))
        route_result = await session.scalars(select(Route).order_by(Route.id))
        cities = [CityRead.model_validate(city) for city in city_result.all()]
        routes = [RouteRead.model_validate(route) for route in route_result.all()]
    except SQLAlchemyError as exc:
        logger.exception("Could not load game map catalog")
        raise ApiException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            code="service_unavailable",
            message="Game map data is temporarily unavailable.",
        ) from exc

    return GameRoutesResponse(
        round=round_projection,
        player=player_projection,
        cities=cities,
        routes=routes,
    )


def _previous_sync_response(
    previous: PrevGame,
    user_id: UUID,
) -> GameSyncResponse:
    """Project only a matching previous round's terminal outcome."""
    if previous.round_id is None or previous.finish_reason is None:
        raise ValueError("The previous round snapshot is incomplete.")
    return GameSyncResponse(
        round=RoundProjection(
            id=previous.round_id,
            status="finished",
            finish_reason=previous.finish_reason,
        ),
        player=PlayerProjection(
            state="won" if previous.winner_user_id == user_id else "lost",
        ),
        crossword=[],
    )


@router.get("/sync/", response_model=GameSyncResponse)
async def sync_game(
    request: Request,
    round_id: UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    after_version: Annotated[int | None, Query(ge=0)] = None,
) -> GameSyncResponse:
    """Return a full current or retained terminal snapshot without mutating state."""
    query = GameSyncQuery(round_id=round_id, after_version=after_version)
    application_lock: asyncio.Lock | None = getattr(
        request.app.state,
        "game_lock",
        None,
    )
    if application_lock is None:
        raise ApiException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            code="service_unavailable",
            message="Game state is temporarily unavailable.",
        )

    async with application_lock:
        game: ActiveGame | None = getattr(request.app.state, "activeGame", None)
        previous: PrevGame | None = getattr(request.app.state, "prevGame", None)
        if game is None or query.round_id != game.game_id:
            if (
                previous is not None
                and query.round_id == previous.round_id
                and previous.finish_reason is not None
            ):
                return _previous_sync_response(previous, user.id)
            raise ApiException(
                status_code=status.HTTP_404_NOT_FOUND,
                code="stale_round",
                message="The requested round is no longer available.",
            )

        async with game.lock:
            now = datetime.now(timezone.utc)
            player = game.players.get(user.id)
            crossword: list[CrosswordPlacementResponse] = []
            if player is not None and player.chosen_route_ids:
                word_ids = {
                    placement.dictionary_word_id
                    for route_id in player.chosen_route_ids
                    for placement in game.shared_words.get(route_id, [])
                }
                dictionary_words: dict[int, DictionaryWord] = {}
                if word_ids:
                    try:
                        word_result = await session.scalars(
                            select(DictionaryWord).where(
                                DictionaryWord.id.in_(word_ids)
                            )
                        )
                        dictionary_words = {
                            word.id: word for word in word_result.all()
                        }
                    except SQLAlchemyError as exc:
                        logger.exception(
                            "Could not load crossword state for round sync"
                        )
                        raise ApiException(
                            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            code="service_unavailable",
                            message="Game state is temporarily unavailable.",
                        ) from exc

                    missing_ids = word_ids.difference(dictionary_words)
                    if missing_ids:
                        logger.critical(
                            "Round {} references missing dictionary entries during sync: {}",
                            game.game_id,
                            sorted(missing_ids),
                        )
                        raise ApiException(
                            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            code="crossword_unavailable",
                            message="Crossword data is temporarily unavailable.",
                        )
                    crossword = _placement_responses(
                        game,
                        user.id,
                        player.chosen_route_ids,
                        dictionary_words,
                    )

            return GameSyncResponse(
                round=_round_projection(game, now),
                player=_player_projection(game, user.id),
                crossword=crossword,
            )


@write_router.post("/join/", response_model=GameJoinResponse)
async def join_game(
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> GameJoinResponse:
    application_lock: asyncio.Lock | None = getattr(
        request.app.state,
        "game_lock",
        None,
    )
    if application_lock is None:
        logger.error("Cannot join game: application game lock is unavailable")
        raise _state_error(
            code="service_unavailable",
            message="Game state is temporarily unavailable.",
            round_projection=_round_projection(
                getattr(request.app.state, "activeGame", None),
                datetime.now(timezone.utc),
            ),
            player_projection=PlayerProjection(state="watching_stats"),
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    async with application_lock:
        game: ActiveGame | None = getattr(request.app.state, "activeGame", None)
        now = datetime.now(timezone.utc)
        if game is None:
            return GameJoinResponse(
                round=_round_projection(None, now),
                player=PlayerProjection(state="watching_stats"),
            )

        if game.status == "active" and now >= game.expires_at:
            await game.finish_timeout(now=now)

        if user.id in game.players:
            return GameJoinResponse(
                round=_round_projection(game, now),
                player=_player_projection(game, user.id),
            )

        if game.status != "active":
            return GameJoinResponse(
                round=_round_projection(game, now),
                player=PlayerProjection(state="watching_stats"),
            )

        remaining_seconds = (game.expires_at - now).total_seconds()
        if remaining_seconds <= 60:
            return GameJoinResponse(
                round=_round_projection(game, now, status_override="being_created"),
                player=PlayerProjection(state="watching_stats"),
            )

        try:
            city_result = await session.scalars(select(City.id).order_by(City.id))
            city_ids = list(city_result.all())
        except SQLAlchemyError as exc:
            logger.exception("Could not load cities for game participation")
            raise ApiException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                code="service_unavailable",
                message="City data is temporarily unavailable.",
            ) from exc

        if len(city_ids) < 2:
            logger.error("Cannot assign game cities: the city catalog has fewer than two entries")
            raise _state_error(
                code="city_catalog_unavailable",
                message="City data is temporarily unavailable.",
                round_projection=_round_projection(game, now),
                player_projection=PlayerProjection(state="watching_stats"),
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        now = datetime.now(timezone.utc)
        if now >= game.expires_at:
            await game.finish_timeout(now=now)
            return GameJoinResponse(
                round=_round_projection(game, now),
                player=PlayerProjection(state="watching_stats"),
            )
        if (game.expires_at - now).total_seconds() <= 60:
            return GameJoinResponse(
                round=_round_projection(game, now, status_override="being_created"),
                player=PlayerProjection(state="watching_stats"),
            )

        reserved_pairs = {
            frozenset((player.start_city_id, player.end_city_id))
            for player in game.players.values()
        }
        available_pairs = [
            pair
            for pair in itertools.combinations(city_ids, 2)
            if frozenset(pair) not in reserved_pairs
        ]
        if not available_pairs:
            raise _state_error(
                code="city_pairs_exhausted",
                message="No unique city pair is available in this round.",
                round_projection=_round_projection(game, now),
                player_projection=PlayerProjection(state="watching_stats"),
            )

        start_city_id, end_city_id = secrets.choice(available_pairs)
        if secrets.randbelow(2):
            start_city_id, end_city_id = end_city_id, start_city_id

        try:
            await game.add_player(
                user.id,
                start_city_id,
                end_city_id,
                starting_score=user.score,
                now=now,
            )
        except GameStateError as exc:
            logger.exception("Could not assign a player to the active round")
            raise _state_error(
                code="city_pair_conflict",
                message="The city pair could not be assigned.",
                round_projection=_round_projection(game, now),
                player_projection=PlayerProjection(state="watching_stats"),
            ) from exc

        return GameJoinResponse(
            round=_round_projection(game, now),
            player=_player_projection(game, user.id),
        )


def _placement_responses(
    game: ActiveGame,
    user_id: UUID,
    route_ids: list[int],
    dictionary_words: dict[int, DictionaryWord],
) -> list[CrosswordPlacementResponse]:
    player = game.players[user_id]
    responses: list[CrosswordPlacementResponse] = []
    for route_id in route_ids:
        placements = sorted(
            game.shared_words[route_id],
            key=lambda placement: placement.position_index,
        )
        for placement in placements:
            dictionary_word = dictionary_words[placement.dictionary_word_id]
            used_hints = player.hints.get(placement.dictionary_word_id)
            hint_types_used = []
            if used_hints is not None:
                if used_hints.first_letter_revealed:
                    hint_types_used.append("first_letter")
                if used_hints.last_letter_revealed:
                    hint_types_used.append("last_letter")
            is_guessed = placement.is_guessed
            responses.append(
                CrosswordPlacementResponse(
                    placement_id=placement.placement_id,
                    route_id=route_id,
                    position_index=placement.position_index,
                    letter_count=placement.letter_count,
                    definition=dictionary_word.definition,
                    is_guessed=is_guessed,
                    guessed_by_me=placement.guessed_by_user_id == user_id,
                    hint_types_used=hint_types_used,
                    revealed_word=dictionary_word.word_text if is_guessed else None,
                )
            )
    return responses


@write_router.post("/route/", response_model=GameRouteResponse)
async def confirm_game_route(
    request: Request,
    payload: GameRouteRequest,
    user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> GameRouteResponse:
    application_lock: asyncio.Lock | None = getattr(
        request.app.state,
        "game_lock",
        None,
    )
    if application_lock is None:
        logger.error("Cannot confirm route: application game lock is unavailable")
        raise ApiException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            code="service_unavailable",
            message="Game state is temporarily unavailable.",
        )
    async with application_lock:
        game: ActiveGame | None = getattr(request.app.state, "activeGame", None)
        game: ActiveGame | None = getattr(request.app.state, "activeGame", None)
        now = datetime.now(timezone.utc)
        if game is None:
            raise _state_error(
                code="round_unavailable",
                message="There is no active round.",
                round_projection=_round_projection(None, now),
                player_projection=PlayerProjection(state="watching_stats"),
            )

        if payload.round_id != game.game_id:
            raise _state_error(
                code="round_id_mismatch",
                message="The request belongs to a different round.",
                round_projection=_round_projection(game, now),
                player_projection=_player_projection(game, user.id),
            )

        if game.status == "active" and now >= game.expires_at:
            await game.finish_timeout(now=now)

        if game.status != "active":
            raise _state_error(
                code="round_not_active",
                message="The round no longer accepts route selections.",
                round_projection=_round_projection(game, now),
                player_projection=_player_projection(game, user.id),
            )

        player = game.players.get(user.id)
        if player is None:
            raise _state_error(
                code="player_not_joined",
                message="Join the active round before selecting a route.",
                round_projection=_round_projection(game, now),
                player_projection=PlayerProjection(state="watching_stats"),
            )

        if player.chosen_route_ids and player.chosen_route_ids != payload.route_ids:
            raise _state_error(
                code="route_already_confirmed",
                message="A confirmed route cannot be replaced.",
                round_projection=_round_projection(game, now),
                player_projection=_player_projection(game, user.id),
            )

        try:
            route_result = await session.scalars(
                select(Route).where(Route.id.in_(payload.route_ids))
            )
            route_rows = route_result.all()
        except SQLAlchemyError as exc:
            logger.exception("Could not load route catalog for route confirmation")
            raise ApiException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                code="service_unavailable",
                message="Route data is temporarily unavailable.",
            ) from exc

        routes_by_id = {route.id: route for route in route_rows}
        missing_route_ids = [
            route_id for route_id in payload.route_ids if route_id not in routes_by_id
        ]
        if missing_route_ids:
            raise _state_error(
                code="route_not_found",
                message="One or more selected routes do not exist.",
                round_projection=_round_projection(game, now),
                player_projection=_player_projection(game, user.id),
                status_code=status.HTTP_404_NOT_FOUND,
                details={"route_ids": missing_route_ids},
            )

        route_endpoints = {
            route_id: (
                routes_by_id[route_id].source_city_id,
                routes_by_id[route_id].target_city_id,
            )
            for route_id in payload.route_ids
        }
        if not player.chosen_route_ids:
            try:
                await game.validate_player_route(
                    user.id,
                    payload.route_ids,
                    route_endpoints,
                    now=datetime.now(timezone.utc),
                )
            except RouteNotAvailableError as exc:
                raise _state_error(
                    code="route_not_connected",
                    message="The selected routes do not form a path between the assigned cities.",
                    round_projection=_round_projection(game, now),
                    player_projection=_player_projection(game, user.id),
                ) from exc
            except (GameExpiredError, GameFinishedError) as exc:
                raise _state_error(
                    code="round_not_active",
                    message="The round no longer accepts route selections.",
                    round_projection=_round_projection(game, now),
                    player_projection=_player_projection(game, user.id),
                ) from exc
            except GameStateError as exc:
                raise _state_error(
                    code="route_already_confirmed",
                    message="A confirmed route cannot be replaced.",
                    round_projection=_round_projection(game, now),
                    player_projection=_player_projection(game, user.id),
                ) from exc

        placements_by_route: dict[int, list[WordPlacement]] = {}
        used_word_ids = {
            placement.dictionary_word_id
            for route_id in payload.route_ids
            for placement in game.shared_words.get(route_id, [])
        }
        new_route_ids = [
            route_id
            for route_id in payload.route_ids
            if route_id not in game.shared_words
        ]
        dictionary_candidates: list[DictionaryWord] = []
        if new_route_ids:
            max_capacity = max(
                max(
                    0,
                    routes_by_id[route_id].length_in_letters
                    - CROSSWORD_ENDPOINT_RESERVED_CELLS,
                )
                for route_id in new_route_ids
            )
            try:
                dictionary_result = await session.scalars(
                    select(DictionaryWord)
                    .where(
                        DictionaryWord.is_active.is_(True),
                        DictionaryWord.letter_count <= max_capacity,
                    )
                    .order_by(DictionaryWord.id)
                )
                dictionary_candidates = list(dictionary_result.all())
            except SQLAlchemyError as exc:
                logger.exception("Could not load dictionary words for crossword generation")
                raise ApiException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    code="service_unavailable",
                    message="Dictionary data is temporarily unavailable.",
                ) from exc

        for route_id in payload.route_ids:
            if route_id in game.shared_words:
                placements_by_route[route_id] = game.shared_words[route_id]
                continue

            capacity = max(
                0,
                routes_by_id[route_id].length_in_letters
                - CROSSWORD_ENDPOINT_RESERVED_CELLS,
            )
            position_index = 0
            remaining_capacity = capacity
            route_placements: list[WordPlacement] = []
            if capacity >= 3:
                available_long_words = [
                    word
                    for word in dictionary_candidates
                    if word.id not in used_word_ids
                    and 3 <= word.letter_count <= remaining_capacity
                ]
                if not available_long_words and dictionary_candidates:
                    logger.critical(
                        "No word of at least three characters fits route {} with capacity {}",
                        route_id,
                        capacity,
                    )
                    raise _state_error(
                        code="crossword_generation_failed",
                        message="A route has room for a three-character word, but no suitable dictionary entry is available. Choose another route or retry later.",
                        round_projection=_round_projection(game, now),
                        player_projection=_player_projection(game, user.id),
                        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    )

                if available_long_words:
                    selected_word = secrets.choice(available_long_words)
                    route_placements.append(
                        WordPlacement(
                            dictionary_word_id=selected_word.id,
                            route_id=route_id,
                            position_index=position_index,
                            letter_count=selected_word.letter_count,
                        )
                    )
                    used_word_ids.add(selected_word.id)
                    position_index += selected_word.letter_count
                    remaining_capacity -= selected_word.letter_count
                    if remaining_capacity:
                        position_index += 1
                        remaining_capacity -= 1

            while remaining_capacity > 0:
                available_words = [
                    word
                    for word in dictionary_candidates
                    if word.id not in used_word_ids
                    and word.letter_count <= remaining_capacity
                ]
                if not available_words:
                    break

                selected_word = secrets.choice(available_words)
                route_placements.append(
                    WordPlacement(
                        dictionary_word_id=selected_word.id,
                        route_id=route_id,
                        position_index=position_index,
                        letter_count=selected_word.letter_count,
                    )
                )
                used_word_ids.add(selected_word.id)
                position_index += selected_word.letter_count
                remaining_capacity -= selected_word.letter_count
                if remaining_capacity:
                    position_index += 1
                    remaining_capacity -= 1
            placements_by_route[route_id] = route_placements

        all_word_ids = {
            placement.dictionary_word_id
            for placements in placements_by_route.values()
            for placement in placements
        }
        dictionary_words: dict[int, DictionaryWord] = {}
        if all_word_ids:
            try:
                word_result = await session.scalars(
                    select(DictionaryWord).where(DictionaryWord.id.in_(all_word_ids))
                )
                dictionary_words = {
                    word.id: word for word in word_result.all()
                }
            except SQLAlchemyError as exc:
                logger.exception("Could not load crossword word descriptions")
                raise ApiException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    code="service_unavailable",
                    message="Dictionary data is temporarily unavailable.",
                ) from exc

            missing_word_ids = all_word_ids.difference(dictionary_words)
            if missing_word_ids:
                logger.critical(
                    "Crossword state references missing dictionary entries in round {}: {}",
                    game.game_id,
                    sorted(missing_word_ids),
                )
                raise _state_error(
                    code="crossword_generation_failed",
                    message="Crossword data is temporarily unavailable. Please retry the same route request.",
                    round_projection=_round_projection(game, now),
                    player_projection=_player_projection(game, user.id),
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                )
            dictionary_words = {
                word_id: dictionary_words[word_id] for word_id in all_word_ids
            }

        try:
            confirmed = await game.confirm_player_route(
                user.id,
                payload.route_ids,
                route_endpoints,
                placements_by_route,
                now=datetime.now(timezone.utc),
            )
        except GameExpiredError as exc:
            raise _state_error(
                code="round_not_active",
                message="The round no longer accepts route selections.",
                round_projection=_round_projection(game, now),
                player_projection=_player_projection(game, user.id),
            ) from exc
        except PlayerNotFoundError as exc:
            raise _state_error(
                code="player_not_joined",
                message="Join the active round before selecting a route.",
                round_projection=_round_projection(game, now),
                player_projection=PlayerProjection(state="watching_stats"),
            ) from exc
        except RouteNotAvailableError as exc:
            raise _state_error(
                code="route_not_connected",
                message="The selected routes do not form a path between the assigned cities.",
                round_projection=_round_projection(game, now),
                player_projection=_player_projection(game, user.id),
            ) from exc
        except GameFinishedError as exc:
            raise _state_error(
                code="round_not_active",
                message="The round no longer accepts route selections.",
                round_projection=_round_projection(game, now),
                player_projection=_player_projection(game, user.id),
            ) from exc
        except GameStateError as exc:
            raise _state_error(
                code="route_already_confirmed",
                message="A confirmed route cannot be replaced.",
                round_projection=_round_projection(game, now),
                player_projection=_player_projection(game, user.id),
            ) from exc
        except ValueError as exc:
            raise _state_error(
                code="route_word_conflict",
                message="The selected route contains conflicting word placements.",
                round_projection=_round_projection(game, now),
                player_projection=_player_projection(game, user.id),
            ) from exc

        if not confirmed:
            logger.critical(
                "Crossword generation produced no words; removed player {} from round {}",
                user.id,
                game.game_id,
            )
            raise _state_error(
                code="crossword_generation_failed",
                message="No crossword words fit the selected route. Join the round again and choose another route.",
                round_projection=_round_projection(game, now),
                player_projection=PlayerProjection(state="watching_stats"),
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        return GameRouteResponse(
            round=_round_projection(game, now),
            player=_player_projection(game, user.id),
            crossword=_placement_responses(
                game,
                user.id,
                payload.route_ids,
                dictionary_words,
            ),
            state_version=game.state_version,
        )


async def _persist_winner(
    session: AsyncSession,
    game: ActiveGame,
    finish: GameFinishResult,
) -> None:
    """Persist the round result and permanent score in one database transaction."""
    if finish.winner_user_id is None:
        raise ValueError("Winner persistence requires a winner finish result.")

    await session.rollback()
    async with session.begin():
        existing = await session.scalar(
            select(GameResult).where(GameResult.game_id == finish.game_id)
        )
        if existing is not None:
            if (
                existing.winner_user_id != finish.winner_user_id
                or existing.finish_reason != "winner"
                or existing.started_at != game.started_at
                or existing.finished_at != finish.finished_at
                or existing.score_awarded != finish.score_awarded
            ):
                logger.critical(
                    "Stored result conflicts with winner state for round {}",
                    finish.game_id,
                )
                raise ApiException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    code="winner_persistence_failed",
                    message="The round result could not be saved.",
                )
            return

        session.add(
            GameResult(
                game_id=finish.game_id,
                winner_user_id=finish.winner_user_id,
                started_at=game.started_at,
                finished_at=finish.finished_at,
                finish_reason="winner",
                score_awarded=finish.score_awarded,
            )
        )
        await session.flush()
        score_update = await session.execute(
            update(User)
            .where(User.id == finish.winner_user_id)
            .values(score=User.score + finish.score_awarded)
            .returning(User.id)
        )
        if score_update.scalar_one_or_none() is None:
            logger.error(
                "Winner {} does not exist while persisting round {}",
                finish.winner_user_id,
                finish.game_id,
            )
            raise ApiException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                code="winner_persistence_failed",
                message="The round result could not be saved.",
            )


@write_router.post(
    "/guess/",
    response_model=GameGuessResponse,
    response_model_exclude_none=True,
)
async def submit_game_guess(
    request: Request,
    payload: GameGuessRequest,
    user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> GameGuessResponse:
    """Check a guess, persist a winner, and return a non-leaking projection."""
    application_lock: asyncio.Lock | None = getattr(
        request.app.state,
        "game_lock",
        None,
    )
    if application_lock is None:
        raise ApiException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            code="service_unavailable",
            message="Game state is temporarily unavailable.",
        )

    async with application_lock:
        game: ActiveGame | None = getattr(request.app.state, "activeGame", None)
        user_id = user.id
        now = datetime.now(timezone.utc)
        if game is None:
            raise _state_error(
                code="round_unavailable",
                message="There is no active round.",
                round_projection=_round_projection(None, now),
                player_projection=PlayerProjection(state="watching_stats"),
                status_code=status.HTTP_404_NOT_FOUND,
            )
        if payload.round_id != game.game_id:
            previous: PrevGame | None = getattr(request.app.state, "prevGame", None)
            if previous is not None and payload.round_id == previous.round_id:
                return _previous_round_response(previous, user.id)
            raise _state_error(
                code="round_id_mismatch",
                message="The request belongs to a different round.",
                round_projection=_round_projection(game, now),
                player_projection=_player_projection(game, user.id),
            )
        if game.status == "finished":
            response = GameGuessResponse(
                round=_round_projection(game, now),
                player=_player_projection(game, user.id),
                state_version=game.state_version,
            )
            _advance_finished_game(request, game, now)
            return response

        player = game.players.get(user.id)
        if player is None or not player.chosen_route_ids:
            raise _state_error(
                code="player_not_playing",
                message="Join the round and confirm a route before guessing.",
                round_projection=_round_projection(game, now),
                player_projection=_player_projection(game, user.id),
                status_code=status.HTTP_409_CONFLICT,
            )

        rate_settings = getattr(request.app.state, "game_settings", get_settings())
        retry_after = _guess_rate_limit(
            request,
            user.id,
            now=now,
            limit=rate_settings.user_guess_rate_limit,
        )
        if retry_after is not None:
            raise ApiException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                code="rate_limited",
                message="Too many guesses. Please wait before trying again.",
                headers={"Retry-After": str(retry_after)},
            )

        selected_placement: WordPlacement | None = None
        selected_route_id: int | None = None
        for route_id in player.chosen_route_ids:
            selected_placement = next(
                (
                    placement
                    for placement in game.shared_words.get(route_id, [])
                    if placement.placement_id == payload.placement_id
                ),
                None,
            )
            if selected_placement is not None:
                selected_route_id = route_id
                break
        if selected_placement is None or selected_route_id is None:
            raise _state_error(
                code="word_not_available",
                message="The word is not on this player's route.",
                round_projection=_round_projection(game, now),
                player_projection=_player_projection(game, user.id),
                status_code=status.HTTP_404_NOT_FOUND,
            )

        try:
            dictionary_word = await session.scalar(
                select(DictionaryWord).where(
                    DictionaryWord.id == selected_placement.dictionary_word_id
                )
            )
        except SQLAlchemyError as exc:
            logger.exception("Could not load a dictionary answer for a guess")
            raise ApiException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                code="dictionary_unavailable",
                message="Dictionary data is temporarily unavailable.",
            ) from exc
        if dictionary_word is None:
            logger.critical(
                "Round {} references missing dictionary word {}",
                game.game_id,
                selected_placement.dictionary_word_id,
            )
            raise ApiException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                code="dictionary_unavailable",
                message="Dictionary data is temporarily unavailable.",
            )

        answer_word = dictionary_word.word_text
        action_time = datetime.now(timezone.utc)

        async def persist_winner(finish: GameFinishResult) -> None:
            """Save the terminal winner before publishing the tentative game state."""
            try:
                await _persist_winner(session, game, finish)
            except SQLAlchemyError as exc:
                logger.exception("Could not persist winner for round {}", game.game_id)
                raise ApiException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    code="winner_persistence_failed",
                    message="The round result could not be saved.",
                ) from exc

        try:
            result = await game.guess_word(
                user.id,
                selected_route_id,
                selected_placement.dictionary_word_id,
                payload.answer,
                answer_word,
                now=action_time,
                before_finish=persist_winner,
            )
        except GameExpiredError:
            response = GameGuessResponse(
                round=_round_projection(game, action_time),
                player=_player_projection(game, user.id),
                state_version=game.state_version,
            )
            _advance_finished_game(request, game, action_time)
            return response
        except GameFinishedError as exc:
            raise _state_error(
                code="round_not_active",
                message="The round no longer accepts guesses.",
                round_projection=_round_projection(game, action_time),
                player_projection=_player_projection(game, user.id),
            ) from exc
        except PlayerNotFoundError as exc:
            raise _state_error(
                code="player_not_playing",
                message="Join the active round before guessing.",
                round_projection=_round_projection(game, action_time),
                player_projection=PlayerProjection(state="watching_stats"),
            ) from exc
        except WordNotAvailableError as exc:
            raise _state_error(
                code="word_not_available",
                message="The word is not on this player's route.",
                round_projection=_round_projection(game, action_time),
                player_projection=_player_projection(game, user.id),
                status_code=status.HTTP_404_NOT_FOUND,
            ) from exc

        if result.already_guessed:
            guess = GuessProjection(
                result="already_solved",
                already_solved=True,
                points_awarded=0,
                revealed_word=answer_word,
            )
        elif result.is_correct:
            guess = GuessProjection(
                result="correct",
                already_solved=False,
                points_awarded=result.points_awarded,
                revealed_word=answer_word,
            )
        else:
            guess = GuessProjection(
                result="incorrect",
                already_solved=False,
                points_awarded=0,
                revealed_word=None,
            )

        response = GameGuessResponse(
            round=_round_projection(game, action_time),
            player=_player_projection(game, user_id),
            guess=guess,
            state_version=game.state_version,
        )
        if game.status == "finished":
            _advance_finished_game(request, game, action_time)
        return response


@write_router.post("/hint/", response_model=GameHintResponse)
async def request_game_hint(
    request: Request,
    payload: GameHintRequest,
    user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> GameHintResponse:
    """Return one letter hint and charge its configured cost at most once."""
    application_lock: asyncio.Lock | None = getattr(
        request.app.state,
        "game_lock",
        None,
    )
    if application_lock is None:
        raise ApiException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            code="service_unavailable",
            message="Game state is temporarily unavailable.",
        )

    async with application_lock:
        game: ActiveGame | None = getattr(request.app.state, "activeGame", None)
        now = datetime.now(timezone.utc)
        if game is None:
            raise _state_error(
                code="round_unavailable",
                message="There is no active round.",
                round_projection=_round_projection(None, now),
                player_projection=PlayerProjection(state="watching_stats"),
                status_code=status.HTTP_404_NOT_FOUND,
            )
        if payload.round_id != game.game_id:
            raise _state_error(
                code="round_id_mismatch",
                message="The request belongs to a different round.",
                round_projection=_round_projection(game, now),
                player_projection=_player_projection(game, user.id),
            )
        if game.status != "active":
            raise _state_error(
                code="round_not_active",
                message="The round no longer accepts hints.",
                round_projection=_round_projection(game, now),
                player_projection=_player_projection(game, user.id),
            )
        player = game.players.get(user.id)
        if player is None or not player.chosen_route_ids:
            raise _state_error(
                code="player_not_playing",
                message="Join the round and confirm a route before requesting hints.",
                round_projection=_round_projection(game, now),
                player_projection=_player_projection(game, user.id),
            )
        selected_placement: WordPlacement | None = None
        selected_route_id: int | None = None
        for route_id in player.chosen_route_ids:
            selected_placement = next(
                (
                    placement
                    for placement in game.shared_words.get(route_id, [])
                    if placement.placement_id == payload.placement_id
                ),
                None,
            )
            if selected_placement is not None:
                selected_route_id = route_id
                break
        if selected_placement is None or selected_route_id is None:
            raise _state_error(
                code="word_not_available",
                message="The word is not on this player's route.",
                round_projection=_round_projection(game, now),
                player_projection=_player_projection(game, user.id),
                status_code=status.HTTP_404_NOT_FOUND,
            )

        try:
            dictionary_word = await session.scalar(
                select(DictionaryWord).where(
                    DictionaryWord.id == selected_placement.dictionary_word_id
                )
            )
        except SQLAlchemyError as exc:
            logger.exception("Could not load a dictionary word for a hint")
            raise ApiException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                code="dictionary_unavailable",
                message="Dictionary data is temporarily unavailable.",
            ) from exc
        if dictionary_word is None:
            logger.critical(
                "Round {} references missing dictionary word {}",
                game.game_id,
                selected_placement.dictionary_word_id,
            )
            raise ApiException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                code="dictionary_unavailable",
                message="Dictionary data is temporarily unavailable.",
            )

        hint_value = _word_hint(dictionary_word.word_text, payload.hint_type)
        action_time = datetime.now(timezone.utc)
        try:
            result = await game.use_hint(
                user.id,
                selected_route_id,
                selected_placement.dictionary_word_id,
                payload.hint_type,
                hint_value,
                now=action_time,
            )
        except GameExpiredError:
            _advance_finished_game(request, game, action_time)
            raise _state_error(
                code="round_not_active",
                message="The round has expired.",
                round_projection=_round_projection(game, action_time),
                player_projection=_player_projection(game, user.id),
            )
        except GameFinishedError as exc:
            raise _state_error(
                code="round_not_active",
                message="The round no longer accepts hints.",
                round_projection=_round_projection(game, action_time),
                player_projection=_player_projection(game, user.id),
            ) from exc
        except InsufficientScoreError as exc:
            raise _state_error(
                code="insufficient_score",
                message="There are not enough points for this hint.",
                round_projection=_round_projection(game, action_time),
                player_projection=_player_projection(game, user.id),
            ) from exc
        except (PlayerNotFoundError, WordNotAvailableError) as exc:
            raise _state_error(
                code="word_not_available",
                message="The word is not on this player's route.",
                round_projection=_round_projection(game, action_time),
                player_projection=_player_projection(game, user.id),
                status_code=status.HTTP_404_NOT_FOUND,
            ) from exc

        return GameHintResponse(
            round=_round_projection(game, action_time),
            player=_player_projection(game, user.id),
            hint=HintProjection(
                hint_type=result.hint_type,
                value=result.value,
                points_charged=result.points_charged,
                already_used=result.already_used,
            ),
        )
