import shutil
import subprocess
from collections.abc import AsyncGenerator, Iterator

import pytest
import pytest_asyncio


def _docker_available() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        proc = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


@pytest.fixture(scope="session")
def _neo4j_container() -> Iterator[object]:
    """One Neo4j server for the whole session (skipped without Docker)."""
    if not _docker_available():
        pytest.skip("docker is not available for the Neo4j parity test")
    from testcontainers.neo4j import Neo4jContainer

    with Neo4jContainer("neo4j:5.26") as container:
        yield container


@pytest_asyncio.fixture
async def neo4j_store(_neo4j_container) -> AsyncGenerator[object]:
    """A Neo4jGraphStore on the shared container, wiped after each test."""
    from shared.common.db.graph.neo4j import Neo4jGraphStore

    store = Neo4jGraphStore.connect(
        _neo4j_container.get_connection_url(),
        "neo4j",
        _neo4j_container.password,
    )
    try:
        yield store
    finally:
        await store.execute("MATCH (n) DETACH DELETE n")
        await store.close()
