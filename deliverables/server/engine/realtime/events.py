"""有界异步事件总线和丢最旧队列。"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable


@dataclass(frozen=True, slots=True)
class QueueStats:
    published: int
    dropped: int


class DropOldestQueue(asyncio.Queue):
    """实时数据队列：满时丢弃最旧项，保证内存有界且优先处理最新音频。"""

    def __init__(self, maxsize: int, on_drop: Callable[[Any], None] | None = None):
        if maxsize <= 0:
            raise ValueError("maxsize 必须大于 0")
        super().__init__(maxsize=maxsize)
        self.on_drop = on_drop
        self.published = 0
        self.dropped = 0

    def put_latest_nowait(self, item: Any) -> bool:
        dropped = False
        if self.full():
            old = self.get_nowait()
            self.task_done()
            self.dropped += 1
            dropped = True
            if self.on_drop:
                self.on_drop(old)
        self.put_nowait(item)
        self.published += 1
        return dropped

    @property
    def stats(self) -> QueueStats:
        return QueueStats(self.published, self.dropped)


_CLOSE = object()


class AsyncEventBus:
    """进程内轻量 pub/sub；每个订阅者都有独立有界队列。"""

    def __init__(self, queue_size: int = 64):
        self.queue_size = queue_size
        self._subscribers: dict[str, list[DropOldestQueue]] = defaultdict(list)
        self._closed = False

    def subscribe(self, topic: str) -> AsyncIterator[Any]:
        queue = DropOldestQueue(self.queue_size)
        self._subscribers[topic].append(queue)

        async def iterator() -> AsyncIterator[Any]:
            try:
                while True:
                    item = await queue.get()
                    queue.task_done()
                    if item is _CLOSE:
                        return
                    yield item
            finally:
                if queue in self._subscribers.get(topic, []):
                    self._subscribers[topic].remove(queue)

        return iterator()

    async def publish(self, topic: str, event: Any) -> int:
        if self._closed:
            return 0
        dropped = 0
        for queue in tuple(self._subscribers.get(topic, ())):
            dropped += int(queue.put_latest_nowait(event))
        return dropped

    async def close(self) -> None:
        self._closed = True
        for queues in self._subscribers.values():
            for queue in tuple(queues):
                queue.put_latest_nowait(_CLOSE)


async def iter_queue(queue: asyncio.Queue) -> AsyncIterator[Any]:
    """把普通 asyncio.Queue 转为异步迭代器；None 作为结束哨兵。"""
    while True:
        item = await queue.get()
        queue.task_done()
        if item is None:
            return
        yield item
