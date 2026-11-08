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
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.templating import Jinja2Templates
from loguru import logger
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

from app.config import Settings, get_settings
from app.database import engine, get_db
from app.api_errors import api_exception_handler, request_validation_exception_handler
from app.auth import router as auth_router
from app.game_api import router as game_router, write_router as game_write_router
from app.game_state import ActiveGame, GameFinishedError, PrevGame
from app.models import User
from app.periodic_task import AsyncPeriodicTask
from app.security import get_authenticated_user

settings = get_settings()


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncGenerator[None, None]:
    """Initialize database and game lifecycle services for the application."""
    logger.info("Starting {}", settings.app_name)
    periodic_task: AsyncPeriodicTask | None = None
    try:
        await check_database_connection()
        application.state.game_lock = asyncio.Lock()
        application.state.game_settings = settings
        application.state.activeGame = ActiveGame.create(settings=settings)
        application.state.prevGame = PrevGame()
        logger.info("Created initial game round {}", application.state.activeGame.game_id)

        periodic_task = AsyncPeriodicTask(
            interval=settings.check_interval,
            task_func=partial(
                check_game_duration,
                application,
                app_settings=settings,
            ),
        )
        periodic_task.start()
        yield
    finally:
        try:
            if periodic_task is not None:
                await periodic_task.stop()
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


app = FastAPI(
    title=settings.app_name,
    lifespan=lifespan,
    exception_handlers={
        StarletteHTTPException: api_exception_handler,
        RequestValidationError: request_validation_exception_handler,
    },
)
app.include_router(auth_router)
app.include_router(game_router)
app.include_router(game_write_router)
project_root = Path(__file__).resolve().parents[1]
templates = Jinja2Templates(directory=project_root / "frontend" / "templates")
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
