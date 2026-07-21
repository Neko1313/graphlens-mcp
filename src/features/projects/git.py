import shutil
import subprocess
from pathlib import Path

__all__ = ["get_remote_url", "is_git_repo"]

_GIT = shutil.which("git")
_TIMEOUT_S = 5.0


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str] | None:
    if _GIT is None:
        return None
    try:
        return subprocess.run(
            [_GIT, "-C", str(root), *args],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def is_git_repo(root: Path) -> bool:
    """True if ``root`` is inside a git work tree.

    Uses git itself, so linked worktrees (``.git`` as a file) and submodules
    resolve correctly — which hand-parsing ``.git/config`` gets wrong.
    """
    if not root.exists():
        return False
    proc = _git(root, "rev-parse", "--is-inside-work-tree")
    return (
        proc is not None
        and proc.returncode == 0
        and proc.stdout.strip() == "true"
    )


def get_remote_url(root: Path) -> str | None:
    """The repo's remote URL — ``origin`` if present, else the first remote.

    Returns ``None`` when ``root`` isn't a repo or has no remote. Kept as a
    plain string, since scp-style remotes (``git@host:org/repo.git``) aren't
    parseable URLs.
    """
    if not is_git_repo(root):
        return None
    origin = _git(root, "config", "--get", "remote.origin.url")
    if origin is not None and origin.returncode == 0 and origin.stdout.strip():
        return origin.stdout.strip()
    names = _git(root, "remote")
    if names is None or names.returncode != 0:
        return None
    for name in names.stdout.split():
        url = _git(root, "config", "--get", f"remote.{name}.url")
        if url is not None and url.returncode == 0 and url.stdout.strip():
            return url.stdout.strip()
    return None
