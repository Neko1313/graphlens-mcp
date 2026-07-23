from __future__ import annotations

# Root of the fixture plugin chain: every reusable factory and store fixture is
# discovered from here, so test files never import fixtures explicitly. See
# tests/fixtures/__init__.py for the next link.
pytest_plugins = ["tests.fixtures"]
