"""新问题到来即取消旧生成任务。"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable


class QuestionWorkController:
    def __init__(self, on_interrupted: Callable[[str], None] | None = None):
        self.on_interrupted = on_interrupted
        self._task: asyncio.Task | None = None
        self._trace_id: str | None = None

    async def start(self, trace_id: str, work: Callable[[], Awaitable[object]]) -> asyncio.Task:
        await self.cancel_current()
        self._trace_id = trace_id
        self._task = asyncio.create_task(work(), name=f"qa:{trace_id}")
        return self._task

    async def cancel_current(self) -> None:
        task, trace_id = self._task, self._trace_id
        self._task = None
        self._trace_id = None
        if task is None or task.done():
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if self.on_interrupted and trace_id:
            self.on_interrupted(trace_id)

    async def close(self) -> None:
        await self.cancel_current()
