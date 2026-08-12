"""Батч-буфер: копит сообщения и отдаёт их пачкой.

Флаш происходит по первому из двух условий:
  * накопилось `size` сообщений (по умолчанию 100);
  * с момента ПЕРВОГО сообщения в пачке прошло `timeout` секунд (по умолчанию 300).

Обработка пачки запускается отдельной задачей, чтобы буфер продолжал
принимать новые сообщения, пока LLM думает над предыдущей пачкой.
"""

from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable, Generic, TypeVar

from .utils import log

T = TypeVar("T")
Handler = Callable[[list[T]], Awaitable[None]]


class BatchBuffer(Generic[T]):
    def __init__(self, size: int, timeout: float, handler: Handler):
        self.size = max(1, size)
        self.timeout = max(1.0, timeout)
        self.handler = handler
        self._items: list[T] = []
        self._deadline: float = 0.0
        self._signal = asyncio.Event()
        self._closed = False
        self._loop_task: asyncio.Task | None = None
        self._jobs: set[asyncio.Task] = set()

    # ------------------------------------------------------------------ жизненный цикл

    def start(self) -> None:
        if self._loop_task is None:
            self._loop_task = asyncio.create_task(self._run(), name="batch-buffer")

    async def stop(self, flush: bool = True) -> None:
        self._closed = True
        self._signal.set()
        if self._loop_task is not None:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
            self._loop_task = None
        if flush and self._items:
            await self._invoke(self._take())
        for job in list(self._jobs):
            job.cancel()
        if self._jobs:
            await asyncio.gather(*self._jobs, return_exceptions=True)

    # ------------------------------------------------------------------ приём

    def add(self, item: T) -> None:
        if self._closed:
            return
        if not self._items:
            self._deadline = time.monotonic() + self.timeout
        self._items.append(item)
        self._signal.set()

    @property
    def pending(self) -> int:
        return len(self._items)

    @property
    def seconds_left(self) -> float:
        if not self._items:
            return 0.0
        return max(0.0, self._deadline - time.monotonic())

    # ------------------------------------------------------------------ внутреннее

    def _take(self) -> list[T]:
        batch, self._items = self._items, []
        self._deadline = 0.0
        self._signal.clear()
        return batch

    async def _run(self) -> None:
        while not self._closed:
            if not self._items:
                await self._signal.wait()
                self._signal.clear()
                continue

            remaining = self._deadline - time.monotonic()
            if len(self._items) >= self.size or remaining <= 0:
                reason = "по размеру" if len(self._items) >= self.size else "по таймауту"
                batch = self._take()
                log.info("Батч собран %s: %s сообщений", reason, len(batch))
                self._spawn(batch)
                continue

            try:
                await asyncio.wait_for(self._signal.wait(), timeout=remaining)
                self._signal.clear()
            except asyncio.TimeoutError:
                pass                                     # проснулись — условия перепроверит цикл

    def _spawn(self, batch: list[T]) -> None:
        task = asyncio.create_task(self._invoke(batch))
        self._jobs.add(task)
        task.add_done_callback(self._jobs.discard)

    async def _invoke(self, batch: list[T]) -> None:
        try:
            await self.handler(batch)
        except asyncio.CancelledError:
            raise
        except Exception:                                # noqa: BLE001
            log.exception("Обработчик батча упал")
