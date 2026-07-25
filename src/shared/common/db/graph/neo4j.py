import asyncio
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, LiteralString, cast

import neo4j.exceptions
from neo4j import AsyncDriver, AsyncGraphDatabase, AsyncTransaction, Query

from shared.common.db.graph.port import GraphExecutor, GraphStoreError

__all__ = ["Neo4jGraphStore"]

# Kuzu's ``CREATE NODE TABLE foo(id STRING, PRIMARY KEY(id))`` declares the
# uniqueness Neo4j needs a constraint for; Kuzu's ``CREATE REL TABLE`` has no
# Neo4j equivalent (relationships aren't uniquely keyed) and is dropped.
_NODE_TABLE_RE = re.compile(
    r"CREATE\s+NODE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)\s*"
    r"\([^)]*PRIMARY\s+KEY\s*\(\s*(\w+)\s*\)",
    re.IGNORECASE,
)
_REL_TABLE_PREFIX = "CREATE REL TABLE"
# rels(<var>) is Kuzu's extractor for a variable-length relationship; in Neo4j
# that variable is already the relationship list, so the wrapper is dropped.
_RELS_RE = re.compile(r"\brels\(\s*(\w+)\s*\)")


def _translate(query: str) -> str | None:
    """Rewrite Kuzu-isms; ``None`` for a statement Neo4j should skip."""
    stripped = query.lstrip()
    node_table = _NODE_TABLE_RE.match(stripped)
    if node_table is not None:
        label, key = node_table.group(1), node_table.group(2)
        return (
            f"CREATE CONSTRAINT IF NOT EXISTS FOR (n:{label}) "
            f"REQUIRE n.{key} IS UNIQUE"
        )
    if stripped.upper().startswith(_REL_TABLE_PREFIX):
        return None
    return _RELS_RE.sub(r"\1", query)


def _text(query: str) -> LiteralString:
    # The driver types query text as LiteralString to deter f-string
    # injection; our text comes only from our own source queries (data
    # travels via parameters), so casting is sound.
    return cast("LiteralString", query)


class _Neo4jTransaction:
    """Statements issued inside one explicit Neo4j transaction."""

    def __init__(self, transaction: AsyncTransaction) -> None:
        self._transaction = transaction

    async def execute(
        self,
        query: str,
        parameters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        translated = _translate(query)
        if translated is None:
            return []
        try:
            result = await self._transaction.run(
                _text(translated),
                parameters or {},
            )
            return [dict(record) async for record in result]
        except neo4j.exceptions.GqlError as exc:
            raise GraphStoreError(str(exc)) from exc


class Neo4jGraphStore:
    """GraphStore backed by a Neo4j server — the server-tier graph backend.

    Speaks the same Cypher as the embedded Kuzu backend after a thin dialect
    shim: Kuzu's ``CREATE NODE TABLE ... PRIMARY KEY(...)`` becomes a Neo4j
    uniqueness constraint (which also backs the lookup with an index), its
    ``CREATE REL TABLE`` is dropped (relationships aren't uniquely keyed in
    Neo4j), and its ``rels(...)`` extractor is unwrapped to the bare variable
    Neo4j expects on a variable-length path. Every non-trivial query is
    verified against a real Neo4j in the tests. Both engines accept the rest
    of the query surface as written (``MERGE``/``UNWIND``/``DETACH DELETE``,
    ``toLower`` + ``CONTAINS``, and the ``max``-then-rematch ``state_at``
    aggregation).
    """

    def __init__(self, driver: AsyncDriver) -> None:
        self._driver = driver
        # Neo4j's default isolation takes write locks at write time, not at
        # read time: a transaction that reads a value (e.g. the temporal
        # log's head seq) and later writes the value it computed from that
        # read can still race another transaction doing the same read before
        # either has written. Kuzu is exempt from this because it allows
        # only one write transaction system-wide; this lock gives our own
        # transactions the same one-at-a-time guarantee here.
        self._write_lock = asyncio.Lock()

    @classmethod
    def connect(
        cls,
        uri: str,
        user: str,
        password: str,
    ) -> "Neo4jGraphStore":
        return cls(AsyncGraphDatabase.driver(uri, auth=(user, password)))

    async def execute(
        self,
        query: str,
        parameters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        translated = _translate(query)
        if translated is None:
            return []
        try:
            async with self._driver.session() as session:
                result = await session.run(
                    Query(_text(translated)),
                    parameters or {},
                )
                return [dict(record) async for record in result]
        except neo4j.exceptions.GqlError as exc:
            raise GraphStoreError(str(exc)) from exc

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[GraphExecutor]:
        """Run statements atomically in one explicit Neo4j transaction.

        Serialized process-wide by ``_write_lock``: read-then-write
        sequences inside a transaction (the temporal log's seq allocation)
        are only safe from lost updates if no other transaction from this
        store can be in flight at the same time. This does not protect
        against a second process writing the same project concurrently —
        same limitation the embedded Kuzu backend has.
        """
        async with self._write_lock, self._driver.session() as session:
            transaction = await session.begin_transaction()
            try:
                yield _Neo4jTransaction(transaction)
            except BaseException:
                await transaction.rollback()
                raise
            await transaction.commit()

    async def check(self) -> None:
        await self.execute("RETURN 1 AS ok")

    async def close(self) -> None:
        await self._driver.close()
