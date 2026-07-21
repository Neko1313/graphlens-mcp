from pathlib import Path
from typing import Any, cast

import kuzu

__all__ = ["KuzuGraphStore"]


class KuzuGraphStore:
    """Embedded GraphStore backend — one process, no server to run."""

    def __init__(self, database: kuzu.Database) -> None:
        self._connection = kuzu.AsyncConnection(database)

    @classmethod
    def open(cls, path: Path) -> "KuzuGraphStore":
        return cls(kuzu.Database(path))

    async def execute(
        self,
        query: str,
        parameters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        result = await self._connection.execute(query, parameters)
        if isinstance(result, list):
            result = result[-1]
        if not isinstance(result, kuzu.QueryResult):
            msg = f"unexpected kuzu result type: {type(result)!r}"
            raise TypeError(msg)
        # rows_as_dict() switches the row shape at runtime; the stub can't
        # express that, so the dict shape here is a checked invariant.
        return cast("list[dict[str, Any]]", result.rows_as_dict().get_all())

    async def check(self) -> None:
        await self.execute("RETURN 1")

    async def close(self) -> None:
        self._connection.close()
