class AsyncPeriodicTask:
    def __init__(self, interval: int, task_func: Callable):
        self.interval: int = interval
        self.task_func: Callable = task_func
        self._task: asyncio.Task | None = None
        self._is_running: bool = False  # флаг - запущена-ли задача
        self.olready_does: bool = False # ни один экземпляр функции пока не вызыван и не работает 

    async def _run(self):
        while self._is_running:
            try:
                if not self.olready_does:
                    self.olready_does = True
                    await self.task_func()
                    self.olready_does = False
                else:
                    logger.warning(f"Запуск задачи пропущен, т.к. один экзепляр все еще работает!")
            except Exception as e:
                logger.error(f"Error in background task: {e}")
                self.olready_does = False
            await asyncio.sleep(self.interval)   # спим заданный интервал до след. запуска задачи

    def start(self):
        if not self._is_running:
            self._is_running = True
            # Создаем задачу в текущем Event Loop
            self._task = asyncio.create_task(self._run())
            logger.info(f"Background task started (interval {self.interval}s)")

    async def stop(self):
        if self._is_running:
            self._is_running = False
            if self._task:
                self._task.cancel()  # Прерываем asyncio.sleep
                try:
                    await self._task
                except asyncio.CancelledError:
                    pass
            logger.info("Background task stopped.")