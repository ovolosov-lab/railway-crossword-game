"""HTTP API of the continuous game (ADR 0007, ADR 0009).

The handlers translate HTTP requests: shared state and idempotency live in the
Redis services (``GameSessions``, ``GameSegments``), victories are committed to
PostgreSQL by ``GameVictories`` and crosswords are laid out by
``crossword_generator``. This is the application's only game API.
"""
from __future__ import annotations

import asyncio
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, Header, Query, Request, Response, status
from loguru import logger
from redis.exceptions import RedisError
from sqlalchemy import exists, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api_errors import ApiException
from app.config import Settings, get_settings
from app.continuous_schemas import (
    AutoRouteResponse,
    CityProjection,
    CrosswordPlacement,
    GuessProjection,
    GuessRequest,
    GuessResponse,
    HintEntry,
    HintProjection,
    HintRequest,
    HintResponse,
    PlayerProjection,
    RouteRequest,
    RouteResponse,
    RoutesResponse,
    SessionProjection,
    SessionRequest,
    SessionResponse,
    StatsResponse,
    SurrenderProjection,
    SurrenderResponse,
    SyncResponse,
)
from app.crossword_generator import (
    CROSSING_WORD_MAX_CELLS,
    CrosswordGenerationError,
    SegmentSpec,
    WordCandidate,
    generate_segment_proposals,
)
from app.database import get_db
from app.game_generation import GameGenerationError, StaleGameGeneration
from app.game_messages import message_for
from app.game_segments import (
    CrosswordGenerationFailed,
    GameSegments,
    InsufficientPoints,
    Placement,
    PlacementNotFound,
    PublishedWord,
    RouteAlreadyConfirmed,
    RouteAlreadySolved,
    SessionNotPlaying,
    WordProposal,
)
from app.game_sessions import (
    GameSessions,
    IdempotencyKeyConflict,
    JoinResultUnavailable,
    NoAvailableCityPair,
    PlayerSession,
    SessionNotFound,
    SessionStorageError,
    VictoryFinishing,
)
from app.game_victories import GameVictories
from app.models import City, DictionaryWord, GameResult, Route, User
from app.route_paths import (
    eligible_city_pairs,
    find_random_route_path,
    is_continuous_path,
)
from app.schemas import LeaderboardEntry, RouteRead
from app.security import get_current_user, require_csrf_token
from app.victory_results import VictoryResultConflict

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

IdempotencyKey = Annotated[UUID, Header(alias="Idempotency-Key")]
_CONFIRM_ATTEMPTS = 3
_RESPONSE_OPTIONS = {"response_model_exclude_unset": True}

_SESSION_STATUS: dict[str, Literal["active", "finishing", "finished"]] = {
    "selecting_route": "active",
    "playing": "active",
    "finishing": "finishing",
    "won": "finished",
}


@dataclass(frozen=True)
class _Services:
    sessions: GameSessions
    segments: GameSegments
    victories: GameVictories
    settings: Settings


def _settings(request: Request) -> Settings:
    return getattr(request.app.state, "game_settings", None) or get_settings()


def _error(
    request: Request,
    code: str,
    status_code: int,
    session: PlayerSession | None = None,
    *,
    detached_player: bool = False,
    details: dict[str, object] | None = None,
    headers: dict[str, str] | None = None,
) -> ApiException:
    """Build an error carrying the current projections when a session is known."""
    session_projection = None
    player_projection = None
    if session is not None:
        session_projection = _session_projection(session).model_dump(mode="json")
        player_projection = _player_projection(session, route=False)
    elif detached_player:
        player_projection = _player_projection(None, route=False)
    return ApiException(
        status_code=status_code,
        code=code,
        message=message_for(code, _settings(request).language),
        details=details,
        session_projection=session_projection,
        player_projection=(
            player_projection.model_dump(mode="json", exclude_unset=True)
            if player_projection is not None
            else None
        ),
        headers=headers,
    )


def _services(request: Request) -> _Services:
    state = request.app.state
    sessions = getattr(state, "game_sessions", None)
    segments = getattr(state, "game_segments", None)
    victories = getattr(state, "game_victories", None)
    if sessions is None or segments is None or victories is None:
        logger.error("Continuous game API requires Redis game services")
        raise _error(request, "game_state_unavailable", status.HTTP_503_SERVICE_UNAVAILABLE)
    return _Services(sessions, segments, victories, _settings(request))


@contextmanager
def _storage_errors(request: Request) -> Iterator[None]:
    """Report unreadable Redis state as unavailable, never as a missing session."""
    try:
        yield
    except (RedisError, GameGenerationError, SessionStorageError, OSError, TimeoutError):
        logger.exception("Game state is unavailable")
        raise _error(
            request, "game_state_unavailable", status.HTTP_503_SERVICE_UNAVAILABLE
        ) from None


@contextmanager
def _database_errors(request: Request) -> Iterator[None]:
    try:
        yield
    except SQLAlchemyError:
        logger.exception("Game catalog data is unavailable")
        raise _error(
            request, "service_unavailable", status.HTTP_503_SERVICE_UNAVAILABLE
        ) from None


def _session_projection(session: PlayerSession) -> SessionProjection:
    return SessionProjection(
        id=session.id,
        status=_SESSION_STATUS[session.state],
        finish_reason="winner" if session.state == "won" else None,
        state_version=session.state_version,
    )


def _player_projection(session: PlayerSession | None, *, route: bool) -> PlayerProjection:
    if session is None:
        return PlayerProjection(state="watching_stats")
    if not route:
        return PlayerProjection(state=session.state, session_score=session.session_score)
    return PlayerProjection(
        state=session.state,
        session_score=session.session_score,
        source_city_id=session.source_city_id,
        target_city_id=session.target_city_id,
        route_ids=list(session.route_ids),
    )


def _placement_response(placement: Placement) -> CrosswordPlacement:
    hints = [
        HintEntry(hint_type=hint_type, value=placement.hints[hint_type])
        for hint_type in ("first_letter", "last_letter")
        if hint_type in placement.hints
    ]
    values = {
        "placement_id": placement.placement_id,
        "route_id": placement.route_id,
        "offset_px": placement.offset_px,
        "orientation": placement.orientation,
        "y_offset_px": placement.y_offset_px,
        "crossing_id": placement.crossing_id,
        "crossing_index": placement.crossing_index,
        "letter_count": placement.letter_count,
        "definition": placement.definition,
        "is_guessed": placement.is_guessed,
        "guessed_by_me": placement.guessed_by_me,
        "hint_types_used": [hint.hint_type for hint in hints],
        "hints": hints,
    }
    if placement.is_guessed and placement.word_text is not None:
        values["word_text"] = placement.word_text
    return CrosswordPlacement(**values)


async def _finalize(services: _Services, session: PlayerSession) -> PlayerSession:
    """Try to commit a finishing victory now; keep ``finishing`` if it cannot be saved yet."""
    if session.state != "finishing":
        return session
    try:
        return await services.victories.finish(session.user_id, session.id)
    except GameGenerationError:
        raise
    except VictoryResultConflict:
        logger.critical("A stored victory conflicts with session {}", session.id)
    except (SessionStorageError, SQLAlchemyError, RedisError, OSError, TimeoutError):
        logger.exception("Victory for session {} remains queued", session.id)
    return session


async def _drain_pending_victories(victories: GameVictories) -> None:
    try:
        await victories.retry_pending()
    except Exception:
        logger.exception("Could not process pending victories")


async def _current_session(
    request: Request, services: _Services, user_id: UUID, session_id: UUID
) -> PlayerSession:
    session = await services.sessions.restore(user_id)
    if session is None or session.id != session_id:
        raise _error(request, "session_not_found", status.HTTP_404_NOT_FOUND)
    return session


async def _crossword(
    request: Request, services: _Services, session: PlayerSession
) -> tuple[PlayerSession, list[CrosswordPlacement]]:
    """Read the crossword (a stored snapshot after a victory) and the matching session."""
    if session.state == "selecting_route":
        return session, []
    try:
        current, placements = await services.segments.crossword(session.user_id, session.id)
    except SessionNotFound:
        raise _error(request, "session_not_found", status.HTTP_404_NOT_FOUND) from None
    return current, [_placement_response(placement) for placement in placements]


# --- statistics and map -------------------------------------------------------

async def _leaderboard(db: AsyncSession) -> list[LeaderboardEntry]:
    winner_exists = exists(
        select(GameResult.game_id).where(
            GameResult.winner_user_id == User.id,
            GameResult.finish_reason == "winner",
        )
    )
    rows = await db.execute(
        select(User.username, User.score)
        .where(winner_exists, User.username != "test")
        .order_by(User.score.desc(), User.username.asc(), User.id.asc())
    )
    return [LeaderboardEntry(username=name, score=score) for name, score in rows.all()]


@router.get("/stats/", response_model=StatsResponse, **_RESPONSE_OPTIONS)
async def get_stats(
    request: Request,
    response: Response,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> StatsResponse:
    response.headers["Cache-Control"] = "no-store"
    services = _services(request)
    with _storage_errors(request):
        session = await services.sessions.restore(user.id)
        participants = await services.sessions.participant_count()
    with _database_errors(request):
        leaderboard = await _leaderboard(db)
    return StatsResponse(
        session=_session_projection(session) if session else None,
        player=_player_projection(session, route=False),
        participant_count=participants,
        leaderboard=leaderboard,
    )


@router.get("/routes/", response_model=RoutesResponse, **_RESPONSE_OPTIONS)
async def get_routes(
    request: Request,
    response: Response,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> RoutesResponse:
    response.headers["Cache-Control"] = "no-store"
    services = _services(request)
    with _database_errors(request):
        city_rows = (
            await db.scalars(
                select(City).where(City.lang == services.settings.language).order_by(City.id)
            )
        ).all()
        route_rows = (await db.scalars(select(Route).order_by(Route.id))).all()
    cities = [
        CityProjection(
            id=city.id, name=city.name, country_code=city.country_code,
            top=float(city.top), left=float(city.left), is_capital=city.is_capital,
        )
        for city in city_rows
    ]
    routes = [RouteRead.model_validate(route) for route in route_rows]
    with _storage_errors(request):
        session = await services.sessions.restore(user.id)
        if session is None:
            return RoutesResponse(
                session=None, player=_player_projection(None, route=False),
                cities=cities, routes=routes,
            )
        session = await _finalize(services, session)
        session, crossword = await _crossword(request, services, session)
    values: dict[str, object] = {}
    if session.state != "selecting_route":
        values["crossword"] = crossword
    return RoutesResponse(
        session=_session_projection(session),
        player=_player_projection(session, route=True),
        cities=cities,
        routes=routes,
        **values,
    )


# --- joining and route selection ---------------------------------------------

@write_router.post("/join/", response_model=SessionResponse, **_RESPONSE_OPTIONS)
async def join_game(
    request: Request,
    key: IdempotencyKey,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SessionResponse:
    services = _services(request)
    with _database_errors(request):
        city_ids = list((await db.scalars(
            select(City.id).where(City.lang == services.settings.language).order_by(City.id)
        )).all())
        routes = list((await db.scalars(select(Route).order_by(Route.id))).all())
    pairs = eligible_city_pairs(city_ids, routes)
    with _storage_errors(request):
        try:
            session = await services.sessions.join(user.id, key, pairs)
        except VictoryFinishing as exc:
            raise _error(
                request, "victory_finishing", status.HTTP_409_CONFLICT, exc.session
            ) from None
        except JoinResultUnavailable:
            raise _error(
                request, "join_result_unavailable", status.HTTP_409_CONFLICT
            ) from None
        except NoAvailableCityPair:
            current = await services.sessions.restore(user.id)
            raise _error(
                request, "city_pairs_exhausted", status.HTTP_409_CONFLICT, current,
                detached_player=True,
            ) from None
    return SessionResponse(
        session=_session_projection(session),
        player=_player_projection(session, route=True),
    )


def _solved_path(published: dict[int, list[PublishedWord]], route_ids: Sequence[int]) -> bool:
    """A path made only of published segments whose words are all solved."""
    return all(
        route_id in published
        and all(word.solved_by is not None for word in published[route_id])
        for route_id in route_ids
    )


def _require_selecting(request: Request, session: PlayerSession) -> None:
    if session.state == "selecting_route":
        return
    if session.state == "finishing":
        raise _error(request, "victory_finishing", status.HTTP_409_CONFLICT, session)
    if session.state == "won":
        raise _error(request, "session_not_active", status.HTTP_409_CONFLICT, session)
    raise _error(request, "route_already_confirmed", status.HTTP_409_CONFLICT, session)


@write_router.post("/route/auto/", response_model=AutoRouteResponse, **_RESPONSE_OPTIONS)
async def auto_route(
    request: Request,
    payload: SessionRequest,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AutoRouteResponse:
    services = _services(request)
    with _storage_errors(request):
        session = await _current_session(request, services, user.id, payload.session_id)
    _require_selecting(request, session)
    with _database_errors(request):
        routes = list((await db.scalars(select(Route).order_by(Route.id))).all())
    with _storage_errors(request):
        published = await services.segments.published_words(
            session.round_id, [route.id for route in routes]
        )
    route_ids = await asyncio.to_thread(
        find_random_route_path,
        routes,
        session.source_city_id,
        session.target_city_id,
        accept=lambda path: not _solved_path(published, path),
    )
    if route_ids is None:
        raise _error(request, "no_available_route", status.HTTP_409_CONFLICT, session)
    return AutoRouteResponse(
        session=_session_projection(session),
        player=_player_projection(session, route=True),
        route_ids=route_ids,
    )


async def _load_candidates(
    db: AsyncSession, language: str, max_cells: int
) -> list[WordCandidate]:
    rows = await db.execute(
        select(DictionaryWord.id, DictionaryWord.word_text, DictionaryWord.definition)
        .where(
            DictionaryWord.is_active.is_(True),
            DictionaryWord.language_code == language,
            DictionaryWord.letter_count <= max_cells,
        )
        .order_by(DictionaryWord.id)
    )
    candidates = []
    for word_id, word_text, definition in rows.all():
        try:
            candidates.append(WordCandidate(word_id, word_text, definition))
        except CrosswordGenerationError:
            logger.warning("Skipping invalid dictionary word {}", word_id)
    return candidates


async def _build_proposals(
    db: AsyncSession,
    settings: Settings,
    routes: Sequence[Route],
    published: dict[int, list[PublishedWord]],
) -> dict[int, list[WordProposal]]:
    """Lay out the words of segments nobody has published yet."""
    new_routes = [route for route in routes if route.id not in published]
    if not new_routes:
        return {}
    city_ids = {city_id for route in routes for city_id in (route.source_city_id, route.target_city_id)}
    city_rows = await db.execute(
        select(City.id, City.left, City.top).where(
            City.lang == settings.language, City.id.in_(city_ids)
        )
    )
    coordinates = {city_id: (float(left), float(top)) for city_id, left, top in city_rows.all()}
    if city_ids - coordinates.keys():
        logger.critical("Map coordinates are missing for cities {}", sorted(city_ids - coordinates.keys()))
        raise CrosswordGenerationError("Map coordinates are missing.")
    candidates = await _load_candidates(
        db,
        settings.language,
        max(CROSSING_WORD_MAX_CELLS, max(route.length_in_letters for route in new_routes)),
    )
    specs = [
        SegmentSpec.from_cities(
            route.id,
            route.length_in_letters,
            coordinates[route.source_city_id],
            coordinates[route.target_city_id],
        )
        for route in routes
    ]
    occupied = [word.word_text for words in published.values() for word in words]
    return await asyncio.to_thread(
        generate_segment_proposals,
        [spec for spec in specs if spec.route_id not in published],
        candidates,
        occupied_words=occupied,
        route_segments=specs,
        published_placements=published,
    )


@write_router.post("/route/", response_model=RouteResponse, **_RESPONSE_OPTIONS)
async def confirm_route(
    request: Request,
    payload: RouteRequest,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> RouteResponse:
    services = _services(request)
    with _storage_errors(request):
        session = await _current_session(request, services, user.id, payload.session_id)
        if session.state != "selecting_route":
            if session.route_ids != payload.route_ids:
                raise _error(request, "route_already_confirmed", status.HTTP_409_CONFLICT, session)
            session = await _finalize(services, session)
            if session.state == "won":
                return RouteResponse(
                    session=_session_projection(session),
                    player=_player_projection(session, route=True),
                )
            session, crossword = await _crossword(request, services, session)
            return RouteResponse(
                session=_session_projection(session),
                player=_player_projection(session, route=True),
                crossword=crossword,
            )

    with _database_errors(request):
        rows = (await db.scalars(select(Route).where(Route.id.in_(payload.route_ids)))).all()
    routes_by_id = {route.id: route for route in rows}
    missing = [route_id for route_id in payload.route_ids if route_id not in routes_by_id]
    if missing:
        raise _error(
            request, "route_not_found", status.HTTP_404_NOT_FOUND, session,
            details={"route_ids": missing},
        )
    if not is_continuous_path(
        routes_by_id, payload.route_ids, session.source_city_id, session.target_city_id
    ):
        raise _error(request, "route_not_connected", status.HTTP_409_CONFLICT, session)
    routes = [routes_by_id[route_id] for route_id in payload.route_ids]

    confirmed: PlayerSession | None = None
    for attempt in range(_CONFIRM_ATTEMPTS):
        with _storage_errors(request):
            published = await services.segments.published_words(
                session.round_id, payload.route_ids
            )
        if _solved_path(published, payload.route_ids):
            raise _error(request, "route_already_solved", status.HTTP_409_CONFLICT, session)
        with _database_errors(request):
            try:
                proposals = await _build_proposals(db, services.settings, routes, published)
            except CrosswordGenerationError:
                raise _error(
                    request, "crossword_generation_failed", status.HTTP_503_SERVICE_UNAVAILABLE
                ) from None
        with _storage_errors(request):
            try:
                confirmed = await services.segments.confirm_route(
                    user.id, session.id, payload.route_ids, proposals
                )
                break
            except SessionNotFound:
                raise _error(request, "session_not_found", status.HTTP_404_NOT_FOUND) from None
            except RouteAlreadyConfirmed:
                current = await services.sessions.restore(user.id)
                raise _error(
                    request, "route_already_confirmed", status.HTTP_409_CONFLICT, current
                ) from None
            except RouteAlreadySolved:
                raise _error(
                    request, "route_already_solved", status.HTTP_409_CONFLICT, session
                ) from None
            except CrosswordGenerationFailed:
                current = await services.sessions.restore(user.id)
                if current is None:
                    # The empty route released the participation and the city pair.
                    raise _error(
                        request, "crossword_generation_failed",
                        status.HTTP_503_SERVICE_UNAVAILABLE, detached_player=True,
                    ) from None
                # A segment changed between reading and publishing; read it again.
                if attempt + 1 == _CONFIRM_ATTEMPTS:
                    raise
    assert confirmed is not None
    with _storage_errors(request):
        confirmed, crossword = await _crossword(request, services, confirmed)
    return RouteResponse(
        session=_session_projection(confirmed),
        player=_player_projection(confirmed, route=True),
        crossword=crossword,
    )


# --- guesses and hints --------------------------------------------------------

@write_router.post("/guess/", response_model=GuessResponse, **_RESPONSE_OPTIONS)
async def submit_guess(
    request: Request,
    payload: GuessRequest,
    background: BackgroundTasks,
    user: Annotated[User, Depends(get_current_user)],
) -> GuessResponse:
    services = _services(request)
    with _storage_errors(request):
        retry_after = await services.sessions.record_guess_attempt(
            user.id, services.settings.user_guess_rate_limit
        )
        if retry_after is not None:
            raise _error(
                request, "rate_limited", status.HTTP_429_TOO_MANY_REQUESTS,
                headers={"Retry-After": str(retry_after)},
            )
        try:
            result = await services.segments.guess(
                user.id, payload.session_id, payload.placement_id, payload.answer
            )
        except SessionNotFound:
            raise _error(request, "session_not_found", status.HTTP_404_NOT_FOUND) from None
        except PlacementNotFound:
            raise _error(request, "placement_not_found", status.HTTP_404_NOT_FOUND) from None
        except SessionNotPlaying as exc:
            if exc.session.state == "selecting_route":
                raise _error(
                    request, "route_not_confirmed", status.HTTP_409_CONFLICT, exc.session
                ) from None
            session = await _finalize(services, exc.session)
            return GuessResponse(
                session=_session_projection(session),
                player=_player_projection(session, route=False),
            )
        session = await _finalize(services, result.session)
    if result.result == "correct":
        background.add_task(_drain_pending_victories, services.victories)
    guess = GuessProjection(
        result=result.result,
        already_solved=result.result == "already_solved",
        points_awarded=result.points_awarded,
        revealed_word=result.revealed_word if result.result != "incorrect" else None,
    )
    return GuessResponse(
        session=_session_projection(session),
        player=_player_projection(session, route=False),
        guess=guess,
    )


@write_router.post("/hint/", response_model=HintResponse, **_RESPONSE_OPTIONS)
async def request_hint(
    request: Request,
    payload: HintRequest,
    user: Annotated[User, Depends(get_current_user)],
) -> HintResponse:
    services = _services(request)
    cost = (
        services.settings.hint_first_letter_cost
        if payload.hint_type == "first_letter"
        else services.settings.hint_last_letter_cost
    )
    with _storage_errors(request):
        try:
            result = await services.segments.hint(
                user.id, payload.session_id, payload.placement_id, payload.hint_type, cost
            )
        except SessionNotFound:
            raise _error(request, "session_not_found", status.HTTP_404_NOT_FOUND) from None
        except PlacementNotFound:
            raise _error(request, "placement_not_found", status.HTTP_404_NOT_FOUND) from None
        except InsufficientPoints:
            current = await services.sessions.restore(user.id)
            raise _error(
                request, "insufficient_points", status.HTTP_403_FORBIDDEN,
                current if current and current.id == payload.session_id else None,
            ) from None
        except SessionNotPlaying as exc:
            if exc.session.state == "selecting_route":
                raise _error(
                    request, "route_not_confirmed", status.HTTP_409_CONFLICT, exc.session
                ) from None
            # No new hint can be given once the route is finished.
            session = await _finalize(services, exc.session)
            return HintResponse(
                session=_session_projection(session),
                player=_player_projection(session, route=False),
                hint=HintProjection(
                    placement_id=payload.placement_id, hint_type="unknown",
                    value=None, points_charged=0,
                ),
            )
    return HintResponse(
        session=_session_projection(result.session),
        player=_player_projection(result.session, route=False),
        hint=HintProjection(
            placement_id=payload.placement_id,
            hint_type=result.hint_type,
            value=result.value,
            points_charged=result.points_charged,
        ),
    )


# --- surrender and synchronization -------------------------------------------

@write_router.post("/surrender/", response_model=SurrenderResponse, **_RESPONSE_OPTIONS)
async def surrender(
    request: Request,
    payload: SessionRequest,
    key: IdempotencyKey,
    user: Annotated[User, Depends(get_current_user)],
) -> SurrenderResponse:
    services = _services(request)
    with _storage_errors(request):
        try:
            won = await services.sessions.surrender(user.id, payload.session_id, key)
        except SessionNotFound:
            raise _error(request, "session_not_found", status.HTTP_404_NOT_FOUND) from None
        except VictoryFinishing as exc:
            raise _error(
                request, "victory_finishing", status.HTTP_409_CONFLICT, exc.session
            ) from None
        except IdempotencyKeyConflict:
            raise _error(
                request, "idempotency_key_conflict", status.HTTP_409_CONFLICT
            ) from None
    if won is not None:
        return SurrenderResponse(
            session=_session_projection(won), player=_player_projection(won, route=False)
        )
    return SurrenderResponse(
        surrender=SurrenderProjection(session_id=payload.session_id, result="surrendered")
    )


@router.get("/sync/", response_model=SyncResponse, **_RESPONSE_OPTIONS)
async def sync_game(
    request: Request,
    response: Response,
    user: Annotated[User, Depends(get_current_user)],
    session_id: UUID | None = None,
    after_version: Annotated[int | None, Query(ge=0)] = None,
) -> SyncResponse:
    # The full snapshot is always returned, so ``after_version`` only validates the query.
    response.headers["Cache-Control"] = "no-store"
    services = _services(request)
    with _storage_errors(request):
        session = await services.sessions.restore(user.id)
        if session is None:
            if session_id is not None:
                raise _error(request, "session_not_found", status.HTTP_404_NOT_FOUND)
            return SyncResponse(session=None, player=_player_projection(None, route=False))
        if session_id is not None and session.id != session_id:
            raise _error(request, "session_not_found", status.HTTP_404_NOT_FOUND)
        session = await _finalize(services, session)
        session, crossword = await _crossword(request, services, session)
    return SyncResponse(
        session=_session_projection(session),
        player=_player_projection(session, route=True),
        crossword=crossword,
    )
