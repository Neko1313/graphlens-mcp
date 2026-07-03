"""Install / probe the arm binaries and the oracle toolchain.

    uv run scripts/setup_arms.py            # report what's present / missing
    uv run scripts/setup_arms.py --install  # attempt installs (uv tool / cargo / go)

Arms: graphlens-mcp (this repo, via uv), semble, codegraph.
Oracle: ripgrep, ast-grep, and optionally gopls / rust-analyzer for deeper checks.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys

# Install recipes (best-effort; printed if the binary is missing).
RECIPES = {
    "semble": "uv tool install semble",
    "codegraph": "see https://github.com/<codegraph> install.sh (already at ~/.local/bin)",
    "ast-grep": "cargo install ast-grep --locked   # or: npm i -g @ast-grep/cli",
    "rg": "apt install ripgrep   # or cargo install ripgrep",
    "gopls": "go install golang.org/x/tools/gopls@latest",
    "rust-analyzer": "rustup component add rust-analyzer",
}

INSTALLERS = {
    "semble": ["uv", "tool", "install", "semble"],
    "ast-grep": ["cargo", "install", "ast-grep", "--locked"],
    "gopls": ["go", "install", "golang.org/x/tools/gopls@latest"],
    "rust-analyzer": ["rustup", "component", "add", "rust-analyzer"],
}


def probe(cmd: str) -> str:
    path = shutil.which(cmd)
    return path or ""


def semble_mcp_help() -> None:
    """Discover semble's MCP subcommand (the README points at `semble install`)."""
    if not shutil.which("semble"):
        return
    print("  semble subcommands:")
    out = subprocess.run(
        ["semble", "--help"], capture_output=True, text=True, check=False
    )
    for line in (out.stdout or out.stderr).splitlines():
        if line.strip():
            print(f"    {line.rstrip()}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--install", action="store_true")
    args = ap.parse_args()

    arms = ["semble", "codegraph"]
    oracle = ["rg", "ast-grep", "gopls", "rust-analyzer"]

    print("== arm binaries ==")
    for c in arms:
        p = probe(c)
        print(
            f"  {c:24} {'OK ' + p if p else 'MISSING  -> ' + RECIPES.get(c, '?')}"
        )
    print(
        "  graphlens-mcp            via `uv run --project <repo> graphlens-mcp serve`"
    )

    print("\n== oracle toolchain ==")
    for c in oracle:
        p = probe(c)
        print(
            f"  {c:24} {'OK ' + p if p else 'MISSING  -> ' + RECIPES.get(c, '?')}"
        )

    if args.install:
        print("\n== installing missing ==")
        for c, cmd in INSTALLERS.items():
            if not probe(c):
                print(f"  $ {' '.join(cmd)}")
                subprocess.run(cmd, check=False)

    print()
    semble_mcp_help()
    return 0


if __name__ == "__main__":
    sys.path.insert(
        0, str(__import__("pathlib").Path(__file__).resolve().parent.parent)
    )
    raise SystemExit(main())
