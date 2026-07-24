import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

import kuzu

from shared.common.db.graph.port import GraphExecutor, GraphStoreError

__all__ = ["KuzuGraphStore"]


def _rows(result: Any) -> list[dict[str, Any]]:
    if isinstance(result, list):
        result = result[-1]
    if not isinstance(result, kuzu.QueryResult):
        msg = f"unexpected kuzu result type: {type(result)!r}"
        raise TypeError(msg)
    # rows_as_dict() switches the row shape at runtime; the stub can't
    # express that, so the dict shape here is a checked invariant.
    return cast("list[dict[str, Any]]", result.rows_as_dict().get_all())


class _WriteGate:
    """Keeps an open transaction the only statement in flight.

    Kuzu allows exactly one write transaction system-wide, so anything issued
    on the pooled connections while an explicit transaction is open fails with
    "only one write transaction at a time". This gate lets ordinary statements
    run concurrently with each other (the common case, mostly reads) but
    drains them before a transaction starts and holds new ones back until it
    finishes.
    """

    def __init__(self) -> None:
        self._exclusive = asyncio.Lock()
        self._open = asyncio.Event()
        self._open.set()
        self._drained = asyncio.Event()
        self._drained.set()
        self._running = 0

    @asynccontextmanager
    async def shared(self) -> AsyncIterator[None]:
        # Re-check after waking: another waiter may have taken the gate.
        while not self._open.is_set():
            await self._open.wait()
        self._running += 1
        self._drained.clear()
        try:
            yield
        finally:
            self._running -= 1
            if self._running == 0:
                self._drained.set()

    @asynccontextmanager
    async def exclusive(self) -> AsyncIterator[None]:
        async with self._exclusive:
            self._open.clear()
            try:
                await self._drained.wait()
                yield
            finally:
                self._open.set()


class _KuzuTransaction:
    """Statements issued on one dedicated connection inside BEGIN…COMMIT.

    A connection of its own is required: the pooled ``AsyncConnection`` hands
    each statement to whichever connection is least busy, which would scatter
    the transaction across connections and leak unrelated statements into it.
    """

    def __init__(self, connection: kuzu.Connection) -> None:
        self._connection = connection

    async def execute(
        self,
        query: str,
        parameters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._run, query, parameters)

    def _run(
        self,
        query: str,
        parameters: dict[str, Any] | None,
    ) -> list[dict[str, Any]]:
        try:
            result = self._connection.execute(query, parameters)
        except RuntimeError as exc:
            raise GraphStoreError(str(exc)) from exc
        return _rows(result)


class KuzuGraphStore:
    """Embedded GraphStore backend — one process, no server to run."""

    def __init__(self, database: kuzu.Database) -> None:
        self._database = database
        self._connection = kuzu.AsyncConnection(database)
        self._gate = _WriteGate()

    @classmethod
    def open(cls, path: Path) -> "KuzuGraphStore":
        return cls(kuzu.Database(path))

    async def execute(
        self,
        query: str,
        parameters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        async with self._gate.shared():
            try:
                result = await self._connection.execute(query, parameters)
            except RuntimeError as exc:
                raise GraphStoreError(str(exc)) from exc
            return _rows(result)

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[GraphExecutor]:
        """Run statements atomically on a dedicated connection.

        No explicit ROLLBACK on the failure path: closing a connection with an
        open transaction discards its writes, and doing it synchronously means
        it still happens when the block is cancelled (where a further ``await``
        would not run).
        """
        async with self._gate.exclusive():
            connection = await asyncio.to_thread(
                kuzu.Connection,
                self._database,
            )
            transaction = _KuzuTransaction(connection)
            try:
                await transaction.execute("BEGIN TRANSACTION")
                yield transaction
                await transaction.execute("COMMIT")
            finally:
                connection.close()

    async def check(self) -> None:
        await self.execute("RETURN 1")

    async def close(self) -> None:
        self._connection.close()
