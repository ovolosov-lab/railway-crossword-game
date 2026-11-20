from collections.abc import AsyncGenerator
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.templating import Jinja2Templates
from loguru import logger
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

from app.admin_api import router as admin_router
from app.api_errors import api_exception_handler, request_validation_exception_handler
from app.config import Settings, get_settings
from app.database import engine, get_db, AsyncSessionLocal
from app.game_generation import (
    GameGeneration,
    GameGenerationUnavailable,
    StaleGameGeneration,
    create_redis_client,
)
from app.auth import router as auth_router
from app.continuous_game_api import (
    router as continuous_game_router,
    write_router as continuous_game_write_router,
)
from app.game_api import router as game_router, write_router as game_write_router
from app.game_state import ActiveGame, GameFinishedError, PrevGame
from app.game_sessions import GameSessions
from app.game_segments import GameSegments
from app.game_victories import GameVictories
from app.models import User
from app.periodic_task import AsyncPeriodicTask
from app.security import get_authenticated_user

settings = get_settings()


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncGenerator[None, None]:
    """Initialize database and game lifecycle services for the application."""
    logger.info("Starting {}", settings.app_name)
    periodic_tasks: list[AsyncPeriodicTask] = []
    redis_client = None
    try:
        await check_database_connection()
        application.state.game_lock = asyncio.Lock()
        application.state.game_settings = settings
        application.state.game_generation = None
        application.state.game_sessions = None
        application.state.game_segments = None
        application.state.game_victories = None
        if settings.redis_url is not None:
            redis_client = create_redis_client(settings.redis_url)
            try:
                application.state.game_generation = await GameGeneration.load_current(
                    redis_client
                )
                application.state.game_sessions = GameSessions(application.state.game_generation)
                application.state.game_segments = GameSegments(application.state.game_sessions)
                application.state.game_victories = await GameVictories.create(
                    application.state.game_sessions,
                    AsyncSessionLocal,
                )
            except (RedisError, GameGenerationUnavailable):
                logger.exception(
                    "Application startup failed: Redis generation is unavailable"
                )
                raise
            logger.info(
                "Using game generation {}",
                application.state.game_generation.generation_id,
            )
        else:
            logger.warning(
                "REDIS_URL is unset; running the legacy MVP without generation fencing"
            )
        application.state.activeGame = ActiveGame.create(settings=settings)
        application.state.prevGame = PrevGame()
        logger.info("Created initial game round {}", application.state.activeGame.game_id)

        periodic_tasks.append(
            AsyncPeriodicTask(
                interval=settings.check_interval,
                task_func=partial(
                    check_game_duration,
                    application,
                    app_settings=settings,
                ),
            )
        )

        if application.state.game_victories is not None:
            periodic_tasks.append(
                AsyncPeriodicTask(
                    interval=settings.check_interval,
                    task_func=partial(
                        retry_pending_victories,
                        application,
                    ),
                )
            )

        for task in periodic_tasks:
            task.start()

        yield
    finally:
        try:
            for task in periodic_tasks:
                try:
                    await task.stop()
                except Exception:
                    logger.exception("Error stopping periodic task")
        finally:
            try:
                if redis_client is not None:
                    await redis_client.aclose()
            finally:
                await engine.dispose()
                logger.info("Stopped {}", settings.app_name)


async def check_database_connection() -> None:
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except SQLAlchemyError:
        logger.exception("Application startup failed: database is unavailable")
        raise


async def check_game_duration(
    application: FastAPI,
    *,
    app_settings: Settings | None = None,
    now: datetime | None = None,
) -> None:
    """Finish expired rounds and retain their result before creating the next one."""
    current_time = now or datetime.now(timezone.utc)
    game_settings = app_settings or get_settings()
    async with application.state.game_lock:
        game: ActiveGame = application.state.activeGame
        if game.status == "active":
            if current_time < game.expires_at:
                return
            try:
                result = await game.finish_timeout(now=current_time)
                logger.info("Game round {} timed out", result.game_id)
            except GameFinishedError:
                if game.status != "finished":
                    raise

        if game.status == "finished":
            previous: PrevGame = getattr(application.state, "prevGame", PrevGame())
            if previous.round_id != game.game_id:
                application.state.prevGame = PrevGame.from_finished_game(game)
            next_game = ActiveGame.create(
                settings=game_settings,
                started_at=game.finished_at or current_time,
            )
            application.state.activeGame = next_game
            logger.info("Created next game round {}", next_game.game_id)


async def retry_pending_victories(application: FastAPI) -> None:
    """Process pending victories from the queue using the generation guard."""
    game_victories: GameVictories | None = getattr(
        application.state,
        "game_victories",
        None,
    )
    if game_victories is None:
        return

    try:
        await game_victories.retry_pending()
    except StaleGameGeneration:
        logger.warning(
            "Skipping pending-victory retry: worker belongs to stale game generation"
        )
    except (RedisError, SQLAlchemyError):
        logger.exception("Error processing pending victories; will retry next cycle")


app = FastAPI(
    title=settings.app_name,
    lifespan=lifespan,
    exception_handlers={
        StarletteHTTPException: api_exception_handler,
        RequestValidationError: request_validation_exception_handler,
    },
)


@app.middleware("http")
async def fence_stale_game_worker(request: Request, call_next):
    game_generation: GameGeneration | None = getattr(
        request.app.state,
        "game_generation",
        None,
    )
    if game_generation is not None:
        try:
            await game_generation.assert_current()
        except StaleGameGeneration:
            logger.warning(
                "Rejected request from worker with stale game generation {}",
                game_generation.generation_id,
            )
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={
                    "error": {
                        "code": "game_state_unavailable",
                        "message": "Game state is temporarily unavailable.",
                        "details": None,
                    }
                },
            )
        except RedisError:
            logger.exception("Rejected request: Redis game state is unavailable")
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={
                    "error": {
                        "code": "game_state_unavailable",
                        "message": "Game state is temporarily unavailable.",
                        "details": None,
                    }
                },
            )
    return await call_next(request)


app.include_router(auth_router)
app.include_router(admin_router)
if settings.continuous_game_enabled:
    app.include_router(continuous_game_router)
    app.include_router(continuous_game_write_router)
else:
    app.include_router(game_router)
    app.include_router(game_write_router)
project_root = Path(__file__).resolve().parents[1]
templates = Jinja2Templates(
    directory=[
        project_root / "frontend" / "templates",
        project_root / "frontend" / "static" / "img",
    ]
)
app.mount(
    "/static",
    StaticFiles(directory=project_root / "frontend" / "static"),
    name="static",
)


@app.get("/", include_in_schema=False)
async def root(
    user: Annotated[User | None, Depends(get_authenticated_user)],
) -> RedirectResponse:
    destination = "/game/" if user is not None else "/login/"
    return RedirectResponse(url=destination, status_code=status.HTTP_303_SEE_OTHER)


@app.get("/login/", response_class=HTMLResponse, include_in_schema=False)
async def login_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="login.html",
    )


@app.get("/register/", response_class=HTMLResponse, include_in_schema=False)
async def register_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="register.html",
    )


@app.get("/game/", response_class=HTMLResponse, include_in_schema=False)
async def game_page(
    request: Request,
    user: Annotated[User | None, Depends(get_authenticated_user)],
) -> Response:
    if user is None:
        return RedirectResponse(url="/login/", status_code=status.HTTP_303_SEE_OTHER)

    return templates.TemplateResponse(
        request=request,
        name="game.html",
        context={
            "username": user.username,
            "csrf_cookie_name": settings.csrf_cookie_name,
            "frontend_sync_interval": settings.frontend_sync_interval,
        },
    )


@app.get("/health", tags=["health"])
async def health_check(
    session: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, str]:
    try:
        await session.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        logger.exception("Health check failed: database is unavailable")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database is unavailable",
        ) from exc

    logger.info("Health check passed")
    return {"status": "ok"}
