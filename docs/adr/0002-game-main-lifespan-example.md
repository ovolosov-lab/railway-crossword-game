```python
@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncGenerator[None, None]:
    periodic_task: AsyncPeriodicTask | None = None
    try:
        await check_database_connection()
        application.state.game_lock = asyncio.Lock()
        application.state.activeGame = ActiveGame.create(settings=settings)

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
```
