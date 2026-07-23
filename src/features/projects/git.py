import os
import shutil
import stat
import subprocess
import tempfile
import uuid
from pathlib import Path

from entities.commit import CommitInfo

__all__ = [
    "clone_into",
    "clone_to_tmp",
    "get_remote_url",
    "head_commit",
    "is_git_repo",
    "ls_remote",
    "new_checkout_dir",
    "remove_retained",
    "retain_checkout",
    "sweep_stale_checkouts",
]

_GIT = shutil.which("git")
_TIMEOUT_S = 5.0
_CLONE_TIMEOUT_S = 300.0

# Per-uid so distinct users never share one root; created 0700 and verified
# ours before use, so a co-tenant can't plant files git would trust (§06).
_CHECKOUT_ROOT = (
    Path(tempfile.gettempdir()) / f"graphlens-checkouts-{os.getuid()}"
)
_CHECKOUT_PREFIX = "clone-"
# Retained per-project working trees: named (not pid-keyed) so the liveness
# sweep never reaps them — server-mode reads need the tree after indexing.
_RETAINED_PREFIX = "project-"


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


def _out(proc: subprocess.CompletedProcess[str] | None) -> str | None:
    """The trimmed stdout of a successful git call, else ``None``."""
    if proc is None or proc.returncode != 0:
        return None
    text = proc.stdout.strip()
    return text or None


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


def head_commit(root: Path) -> CommitInfo | None:
    """The checkout's HEAD as a ``CommitInfo`` for the temporal log.

    Reports the commit ``sha`` (its stable identity across full and shallow
    clones) and committer time — deliberately not a commit-depth number, which
    a shallow clone reports differently for the same commit. Ordering is the
    temporal log's job. ``ref`` is the current branch, or the sha when
    detached. Returns ``None`` when ``root`` isn't a git repo (so indexing
    still works, just without history).
    """
    if not is_git_repo(root):
        return None
    sha = _out(_git(root, "rev-parse", "HEAD"))
    if sha is None:
        return None
    ref = _out(_git(root, "rev-parse", "--abbrev-ref", "HEAD"))
    if ref is None or ref == "HEAD":
        ref = sha
    epoch = _out(_git(root, "log", "-1", "--format=%ct", "HEAD"))
    return CommitInfo(
        ref=ref,
        sha=sha,
        time_update=int(epoch) if epoch is not None and epoch.isdigit() else 0,
    )


def _ensure_secure_root() -> Path:
    """The checkout root, created 0700 and verified to belong to this uid.

    Refuses a pre-existing root that isn't a real directory owned by us with no
    group/other access — so a co-tenant who plants a dir (or a symlink) at the
    predictable path can't make git read a token from, or write a checkout
    into, attacker-controlled storage.
    """
    root = _CHECKOUT_ROOT
    try:
        info = os.lstat(root)
    except FileNotFoundError:
        try:
            root.mkdir(mode=0o700)
        except FileExistsError:
            return _ensure_secure_root()  # lost a create race — re-verify
        root.chmod(0o700)
        return root
    ours = (
        stat.S_ISDIR(info.st_mode)
        and info.st_uid == os.geteuid()
        and not (info.st_mode & 0o077)
    )
    if not ours:
        msg = f"refusing insecure checkout root: {root}"
        raise RuntimeError(msg)
    return root


def _askpass_script() -> Path:
    """A tiny, secret-free helper git calls for credentials.

    It echoes the token from the environment (``GRAPHLENS_GIT_TOKEN``), so the
    token never lands in argv, the clone URL, or ``.git/config``. Rewritten
    (never trusted) every time inside the verified 0700 root, so a planted file
    is overwritten rather than executed.
    """
    root = _ensure_secure_root()
    script = root / "askpass.sh"
    script.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        '  Username*) printf %s "x-access-token" ;;\n'
        '  *) printf %s "$GRAPHLENS_GIT_TOKEN" ;;\n'
        "esac\n",
    )
    script.chmod(0o700)
    return script


def _auth_env(token: str | None) -> dict[str, str]:
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    # Allowlist real transports (incl. file/local for local repos) but NOT
    # ext:: — git's ext transport runs an arbitrary shell command, so excluding
    # it here blocks that RCE even if a URL slips past _require_safe_remote.
    env["GIT_ALLOW_PROTOCOL"] = "https:http:ssh:git:file"
    env["GIT_PROTOCOL_FROM_USER"] = "0"
    if token:
        env["GIT_ASKPASS"] = str(_askpass_script())
        env["GRAPHLENS_GIT_TOKEN"] = token
    return env


def _require_safe_remote(repo_url: str) -> None:
    """Reject a repo url that could execute code or inject a git option.

    The critical block is git's ``ext::`` transport (and any other
    ``transport::`` helper), which runs an arbitrary shell command — an RCE.
    Also rejects a leading ``-`` (which git would parse as an option). Real
    network schemes, scp-style ``[user@]host:path``, and local/``file://``
    paths (needed for local repos) are allowed; ``GIT_ALLOW_PROTOCOL`` is the
    second line of defense that still blocks ``ext``.
    """
    url = repo_url.strip()
    if not url or url.startswith("-"):
        msg = f"invalid repo url: {repo_url!r}"
        raise ValueError(msg)
    if "://" in url:
        scheme = url.split("://", 1)[0].lower()
        if scheme not in ("https", "http", "ssh", "git", "file"):
            msg = f"unsupported repo url scheme: {scheme!r}"
            raise ValueError(msg)
        return
    # No scheme: an scp-style host:path or a local path — but a "::" is a
    # transport helper (ext::, fd::, …), the RCE vector, so reject it.
    if "::" in url:
        msg = f"unsupported repo url transport: {repo_url!r}"
        raise ValueError(msg)


def _run_git_url(
    args: list[str],
    token: str | None,
    timeout: float,
) -> subprocess.CompletedProcess[str] | None:
    """Run a git command against a remote URL: askpass auth, no cred store."""
    if _GIT is None:
        return None
    try:
        return subprocess.run(
            [_GIT, "-c", "credential.helper=", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=_auth_env(token),
        )
    except (OSError, subprocess.SubprocessError):
        return None


def ls_remote(
    repo_url: str,
    ref: str | None = None,
    token: str | None = None,
) -> str | None:
    """The sha a remote ref points at, without cloning.

    Drives the no-op fast path: compare against the last-indexed sha before
    paying for a clone. The token travels via askpass, never the URL or argv.
    """
    _require_safe_remote(repo_url)
    args = ["ls-remote", "--", repo_url]
    if ref:
        args.append(ref)
    out = _out(_run_git_url(args, token, _TIMEOUT_S))
    if out is None:
        return None
    return out.split(maxsplit=1)[0]


def new_checkout_dir() -> Path:
    """A fresh, unused checkout path under the verified root.

    Handed back *before* any clone runs, so a caller can guarantee cleanup in a
    ``finally`` even if the clone is cancelled mid-flight (a worker-thread
    clone can't be interrupted, so the destination must be known up front). The
    name carries this process's pid for the liveness sweep.
    """
    root = _ensure_secure_root()
    return root / f"{_CHECKOUT_PREFIX}{os.getpid()}-{uuid.uuid4().hex}"


def clone_into(
    repo_url: str,
    ref: str | None,
    token: str | None,
    dest: Path,
) -> bool:
    """Shallow-clone ``repo_url@ref`` into ``dest``; ``True`` on success.

    The token is injected via askpass (env), so it never reaches the clone's
    ``.git/config``, the process argv (``/proc``), or a clone-failure message —
    only a plain URL does. Removes a partial checkout on failure.
    """
    _require_safe_remote(repo_url)
    args = ["clone", "--depth", "1", "--single-branch"]
    if ref:
        args += ["--branch", ref]
    args += ["--", repo_url, str(dest)]
    proc = _run_git_url(args, token, _CLONE_TIMEOUT_S)
    if proc is None or proc.returncode != 0:
        shutil.rmtree(dest, ignore_errors=True)
        return False
    return True


def clone_to_tmp(
    repo_url: str,
    ref: str | None = None,
    token: str | None = None,
) -> Path | None:
    """Clone into a fresh checkout dir; the caller must clean it up.

    Convenience over ``new_checkout_dir`` + ``clone_into`` for callers that
    don't need the destination up front. Returns None on failure.
    """
    dest = new_checkout_dir()
    return dest if clone_into(repo_url, ref, token, dest) else None


def retain_checkout(dest: Path, project_id: str) -> Path:
    """Move a fresh clone to a stable per-project tree and return its path.

    Server-mode reads (source, signatures, content search) need the working
    tree after indexing, so a successful checkout is kept under a name derived
    from the project id — outside the ``clone-`` namespace the liveness sweep
    reaps, so it survives across requests and restarts. Any prior tree for the
    project is replaced. ``project_id`` is ``[A-Za-z0-9_]`` — a safe dir name.
    """
    root = _ensure_secure_root()
    stable = root / f"{_RETAINED_PREFIX}{project_id}"
    shutil.rmtree(stable, ignore_errors=True)
    dest.rename(stable)
    return stable


def remove_retained(project_id: str) -> None:
    """Delete a project's retained working tree (on project removal)."""
    shutil.rmtree(
        _CHECKOUT_ROOT / f"{_RETAINED_PREFIX}{project_id}",
        ignore_errors=True,
    )


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def sweep_stale_checkouts() -> int:
    """Delete tmp clones whose owning process is gone (liveness, not age).

    Keyed on the pid in each dir name, so a long-running index of a big
    monorepo is never reaped out from under itself — only dead-process orphans.
    Returns how many were reclaimed.
    """
    if not _CHECKOUT_ROOT.exists():
        return 0
    removed = 0
    for child in _CHECKOUT_ROOT.iterdir():
        if not child.is_dir() or not child.name.startswith(_CHECKOUT_PREFIX):
            continue
        head = child.name[len(_CHECKOUT_PREFIX) :].split("-", 1)[0]
        if head.isdigit() and not _pid_alive(int(head)):
            shutil.rmtree(child, ignore_errors=True)
            removed += 1
    return removed
