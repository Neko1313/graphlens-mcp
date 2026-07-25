import os
import shutil
import subprocess

import pytest

from features.projects import git


@pytest.fixture(autouse=True)
def _isolated_checkout_root(tmp_path, monkeypatch):
    """Redirect the checkout root into a per-test tmp dir (never real /tmp)."""
    monkeypatch.setattr(git, "_CHECKOUT_ROOT", tmp_path / "checkouts")


def _head(repo):
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


@pytest.mark.integration
@pytest.mark.project
def test_clone_to_tmp_checks_out_the_repo(git_repo):
    # Act
    clone = git.clone_to_tmp(str(git_repo), "main")

    # Assert
    try:
        assert clone is not None
        assert (clone / "m.py").exists()
    finally:
        if clone is not None:
            shutil.rmtree(clone, ignore_errors=True)


@pytest.mark.integration
@pytest.mark.project
def test_clone_never_writes_the_token_to_disk(git_repo):
    # A leaked token in a shared pod is a breach, so it must never land in the
    # clone's .git/config (it travels via env-based askpass).
    # Act
    clone = git.clone_to_tmp(str(git_repo), "main", token="SUPERSECRET-abc123")

    # Assert
    try:
        assert clone is not None
        config = (clone / ".git" / "config").read_text()
        askpass = (git._CHECKOUT_ROOT / "askpass.sh").read_text()
        assert "SUPERSECRET" not in config
        assert "SUPERSECRET" not in askpass
    finally:
        if clone is not None:
            shutil.rmtree(clone, ignore_errors=True)


@pytest.mark.integration
@pytest.mark.project
def test_insecure_checkout_root_is_refused(git_repo):
    # A co-tenant-plantable, group/other-writable root must be rejected before
    # any token-bearing git call runs.
    # Arrange
    git._CHECKOUT_ROOT.mkdir()
    git._CHECKOUT_ROOT.chmod(0o777)

    # Act / Assert
    with pytest.raises(RuntimeError, match="insecure checkout root"):
        git.new_checkout_dir()


@pytest.mark.integration
@pytest.mark.project
def test_ls_remote_resolves_the_ref_to_head(git_repo):
    # Act / Assert
    assert git.ls_remote(str(git_repo), "main") == _head(git_repo)


@pytest.mark.integration
@pytest.mark.project
@pytest.mark.parametrize(
    "bad_url",
    [
        'ext::sh -c "touch pwned"',  # RCE via git's ext transport
        "fd::7",  # another transport helper
        "--upload-pack=touch pwned",  # leading-dash git option injection
        "",
    ],
)
def test_clone_rejects_command_injection_urls(bad_url):
    # A malicious repo_url must never reach git as an executable transport.
    with pytest.raises(ValueError, match="repo url"):
        git.clone_into(bad_url, None, None, git.new_checkout_dir())


@pytest.mark.integration
@pytest.mark.project
def test_retained_checkout_survives_the_liveness_sweep(git_repo):
    # Server reads need the tree after indexing, so a retained checkout must
    # not be reaped by the (clone-only) sweep.
    # Arrange
    dest = git.clone_to_tmp(str(git_repo), "main")
    assert dest is not None

    # Act
    stable = git.retain_checkout(dest, "proj_retain")
    git.sweep_stale_checkouts()

    # Assert — moved out of the clone- namespace and kept.
    assert not dest.exists()
    assert stable.exists()
    assert (stable / "m.py").exists()


@pytest.mark.integration
@pytest.mark.project
def test_sweep_removes_only_dead_process_checkouts():
    # Arrange — one checkout owned by this (live) process, one by a dead pid.
    root = git._CHECKOUT_ROOT
    root.mkdir(mode=0o700)
    live = root / f"{git._CHECKOUT_PREFIX}{os.getpid()}-live"
    live.mkdir()
    dead = root / f"{git._CHECKOUT_PREFIX}999999999-dead"
    dead.mkdir()

    # Act
    removed = git.sweep_stale_checkouts()

    # Assert — the live one survives, the orphan is reclaimed.
    assert live.exists()
    assert not dead.exists()
    assert removed == 1
