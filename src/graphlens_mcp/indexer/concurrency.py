"""Concurrency primitives for indexing: per-file in-flight dedup + limits."""

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any, TypeVar

T = TypeVar("T")

MAX_CONCURRENT_RESOLVERS = 4


class InFlightRegistry:
    """
    Ensures only one indexing task runs per file path at a time.

    Concurrent requests for the same file wait for the single in-flight task
    instead of spawning duplicate resolver processes.
    """

    def __init__(self) -> None:
        """Create an empty registry guarded by an asyncio lock."""
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self._lock = asyncio.Lock()

    async def get_or_create(
        self,
        key: str,
        coro_factory: Callable[[], Coroutine[Any, Any, T]],
    ) -> T:
        """Await the task for *key*, or start one from *coro_factory*."""
        async with self._lock:
            if key in self._tasks:
                task = self._tasks[key]
            else:
                task = asyncio.create_task(coro_factory())
                self._tasks[key] = task
                task.add_done_callback(lambda _: self._tasks.pop(key, None))

        return await asyncio.shield(task)
