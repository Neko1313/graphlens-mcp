from __future__ import annotations

pytest_plugins = [
    "tests.fixtures.stores.kuzu",
    "tests.fixtures.stores.neo4j",
    "tests.fixtures.stores.vector",
]
