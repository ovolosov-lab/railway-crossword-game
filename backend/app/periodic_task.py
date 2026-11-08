import asyncio
from collections.abc import Awaitable, Callable

from loguru import logger


class AsyncPeriodicTask:
    def __init__(
        self,
        *,
        interval: float,
        task_func: Callable[[], Awaitable[None]],
    ) -> None:
        if interval <= 0:
            raise ValueError("interval must be greater than zero.")

        self.interval = interval
        self.task_func = task_func
        self._task: asyncio.Task[None] | None = None

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self.interval)
            try:
                await self.task_func()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Periodic background task failed; it will be retried")

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._run())
        logger.info("Periodic background task started (interval: {}s)", self.interval)

    async def stop(self) -> None:
        task = self._task
        if task is None:
            return

        self._task = None
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        logger.info("Periodic background task stopped")
