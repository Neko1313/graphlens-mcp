import re
from typing import Any, LiteralString, cast

from neo4j import AsyncDriver, AsyncGraphDatabase, Query

__all__ = ["Neo4jGraphStore"]

# Kuzu-only DDL: Neo4j is schemaless, so these statements are no-ops.
_DDL_PREFIXES = ("CREATE NODE TABLE", "CREATE REL TABLE")
# rels(<var>) is Kuzu's extractor for a variable-length relationship; in Neo4j
# that variable is already the relationship list, so the wrapper is dropped.
_RELS_RE = re.compile(r"\brels\(\s*(\w+)\s*\)")


class Neo4jGraphStore:
    """GraphStore backed by a Neo4j server — the server-tier graph backend.

    Speaks the same Cypher as the embedded Kuzu backend after a thin dialect
    shim: Kuzu's ``CREATE NODE/REL TABLE`` declarations are skipped (Neo4j is
    schemaless), and its ``rels(...)`` extractor is unwrapped to the bare
    variable Neo4j expects on a variable-length path. Every non-trivial query
    is verified against a real Neo4j in the tests. Both engines accept the rest
    of the query surface as written (``MERGE``/``UNWIND``/``DETACH DELETE``,
    ``toLower`` + ``CONTAINS``, and the ``max``-then-rematch ``state_at``
    aggregation).
    """

    def __init__(self, driver: AsyncDriver) -> None:
        self._driver = driver

    @classmethod
    def connect(
        cls,
        uri: str,
        user: str,
        password: str,
    ) -> "Neo4jGraphStore":
        return cls(AsyncGraphDatabase.driver(uri, auth=(user, password)))

    @staticmethod
    def _translate(query: str) -> str | None:
        """Rewrite Kuzu-isms; ``None`` for a statement Neo4j should skip."""
        if query.lstrip().upper().startswith(_DDL_PREFIXES):
            return None
        return _RELS_RE.sub(r"\1", query)

    async def execute(
        self,
        query: str,
        parameters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        translated = self._translate(query)
        if translated is None:
            return []
        async with self._driver.session() as session:
            # The driver types query text as LiteralString to deter f-string
            # injection; our text comes only from our own source queries (data
            # travels via parameters), so casting is sound.
            text = cast("LiteralString", translated)
            result = await session.run(Query(text), parameters or {})
            return [dict(record) async for record in result]

    async def check(self) -> None:
        await self.execute("RETURN 1 AS ok")

    async def close(self) -> None:
        await self._driver.close()
