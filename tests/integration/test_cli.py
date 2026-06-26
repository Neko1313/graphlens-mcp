"""Integration smoke tests for the CLI via click's CliRunner."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from click.testing import CliRunner

from graphlens_mcp.cli import main

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = [pytest.mark.integration, pytest.mark.cli]


def test_init_indexes_and_creates_db_without_touching_agents(py_project: Path):
    runner = CliRunner()
    result = runner.invoke(
        main, ["init", "--root", str(py_project), "--no-agent", "--no-skills"]
    )
    assert result.exit_code == 0, result.output
    assert "Indexed" in result.output
    assert (py_project / ".graphlens" / "graph.db").exists()


def test_status_reports_graph_after_init(py_project: Path):
    runner = CliRunner()
    runner.invoke(
        main, ["init", "--root", str(py_project), "--no-agent", "--no-skills"]
    )
    result = runner.invoke(main, ["status", "--root", str(py_project)])
    assert result.exit_code == 0, result.output
    assert "Graph:" in result.output
    assert "nodes" in result.output


def test_remove_purges_database(py_project: Path):
    runner = CliRunner()
    runner.invoke(
        main, ["init", "--root", str(py_project), "--no-agent", "--no-skills"]
    )
    db = py_project / ".graphlens" / "graph.db"
    assert db.exists()

    result = runner.invoke(
        main, ["remove", "--root", str(py_project), "--purge-db", "--yes"]
    )
    assert result.exit_code == 0, result.output
    assert not db.exists()
