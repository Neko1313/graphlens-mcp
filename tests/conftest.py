from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterable

# Root of the fixture plugin chain: every reusable factory and store fixture is
# discovered from here, so test files never import fixtures explicitly. See
# tests/fixtures/__init__.py for the next link.
pytest_plugins = ["tests.fixtures"]

_MILVUS_GROUP = "milvus"


def pytest_collection_modifyitems(items: Iterable[pytest.Item]) -> None:
    """Pin every Milvus-backed test to a single xdist worker.

    Milvus Lite starts a local server per client and won't tolerate several
    coming up at once, which is why ``vector_store`` is one session-scoped
    instance. Under ``-n`` that would be one instance *per worker* and they
    race on startup, so the tests that need it share a worker while everything
    else still spreads out. Applied by fixture rather than by hand so a new
    vector test can't forget it.
    """
    for item in items:
        if "vector_store" in getattr(item, "fixturenames", ()):
            item.add_marker(pytest.mark.xdist_group(_MILVUS_GROUP))
