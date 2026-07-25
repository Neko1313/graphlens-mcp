import subprocess
from pathlib import Path

import pytest


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """A tmp git repo with a remote, one Python file, and one commit on main."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text(
        "[project]\nname = 'sample'\nversion = '0'\n",
    )
    (repo / "m.py").write_text(
        "def foo():\n    return bar()\n\n\ndef bar():\n    return 1\n",
    )
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "test")
    _git(repo, "remote", "add", "origin", "git@github.com:sample/repo.git")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "initial")
    return repo
