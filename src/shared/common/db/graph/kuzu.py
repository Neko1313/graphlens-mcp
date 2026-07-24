import asyncio
import os
import tempfile
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

import kuzu

from shared.common.db.graph.port import GraphExecutor, GraphStoreError

__all__ = ["KuzuGraphStore"]


# Field/quote/escape chars for bulk_copy's staging file — control chars that
# never appear in the data (hex ids, enum kinds, json.dumps'd JSON, source
# snippets). Every non-null field is WRAPPED in the quote char, so a value may
# freely contain commas, quotes, or raw newlines (the graphlens engine emits
# multi-line names for grouped imports, e.g. `std::{\n  a,\n  b,\n}`): the
# serial reader (PARALLEL=false) reads across the quoted newline exactly. Only
# the three format chars themselves can never occur in a value.
_COPY_DELIM = "\x01"
_COPY_QUOTE = "\x02"
_COPY_ESCAPE = "\x03"
_COPY_RESERVED = (_COPY_DELIM, _COPY_QUOTE, _COPY_ESCAPE)


def _encode_copy_row(row: "Sequence[Any]") -> str:
    """One staging line.

    ``None`` is written as an *unquoted* empty field, which Kuzu reads as NULL;
    every other value is wrapped in the quote char so its contents (commas,
    quotes, newlines) survive verbatim. A value containing one of the three
    reserved format chars is a bug in the caller, so fail loudly.
    """
    cells = []
    for value in row:
        if value is None:
            cells.append("")  # unquoted empty -> NULL
            continue
        cell = str(value)
        if any(ch in cell for ch in _COPY_RESERVED):
            msg = f"bulk_copy value holds a reserved char: {cell[:80]!r}"
            raise GraphStoreError(msg)
        cells.append(f"{_COPY_QUOTE}{cell}{_COPY_QUOTE}")
    return _COPY_DELIM.join(cells) + "\n"


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

    async def bulk_copy(
        self,
        table: str,
        rows: Sequence[Sequence[Any]],
    ) -> None:
        """Load ``rows`` into ``table`` with one ``COPY``.

        Implements SupportsBulkCopy — the O(n²)-avoiding write path.

        Staged to a temp file that is always removed. The format is chosen to
        need **no escaping at all**: fields are joined by a control-char
        delimiter (``\\x01``) and the quote/escape chars are set to other
        control chars (``\\x02``/``\\x03``). None of these — nor a raw
        newline — can occur in the data (ids are hex, ``kind`` is an enum word,
        and every JSON column comes from ``json.dumps``, which escapes control
        chars and newlines). Kuzu's own CSV quoting was tried first and
        desynchronised on doubled quotes across a large file; sidestepping
        quoting entirely is what makes the round-trip exact. A value that does
        contain a reserved char raises rather than silently corrupting.

        Runs under the write gate like any other write; ``COPY`` appends, so
        rows for other projects already in a shared table are untouched.
        """
        if not rows:
            return

        def _run() -> None:
            # A dedicated *synchronous* connection: self._connection is a
            # kuzu.AsyncConnection whose execute() is a coroutine, and calling
            # it off the event loop (here, in a worker thread) would create a
            # coroutine that never runs — the COPY would silently no-op. A
            # plain Connection on the same Database commits normally and its
            # writes are visible to every other connection.
            connection = kuzu.Connection(self._database)
            fd, path = tempfile.mkstemp(suffix=".csv")
            try:
                with os.fdopen(fd, "w", newline="", encoding="utf-8") as fh:
                    for row in rows:
                        fh.write(_encode_copy_row(row))
                # Kuzu has no parameter binding for COPY paths; the path comes
                # from mkstemp (no user input), and any embedded quote is
                # doubled to stay inside the string literal.
                safe = path.replace('"', '""')
                # PARALLEL=false: the parallel reader splits the file into byte
                # chunks blind to record boundaries; the serial reader is the
                # one that reads our single-line, delimiter-clean rows exactly.
                # COPY is already sub-second, so the lost parallelism is free.
                connection.execute(
                    f'COPY {table} FROM "{safe}" '
                    f"(HEADER=false, PARALLEL=false, "
                    f"DELIM='{_COPY_DELIM}', QUOTE='{_COPY_QUOTE}', "
                    f"ESCAPE='{_COPY_ESCAPE}')",
                )
            finally:
                os.unlink(path)

        async with self._gate.shared():
            try:
                await asyncio.to_thread(_run)
            except RuntimeError as exc:
                raise GraphStoreError(str(exc)) from exc

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
