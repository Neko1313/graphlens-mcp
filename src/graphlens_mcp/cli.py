"""CLI entry points: init, serve, status, reindex."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path

import click

from graphlens_mcp.agents import REGISTRY
from graphlens_mcp.agents import configure as configure_agent
from graphlens_mcp.agents import deregister as deregister_agent
from graphlens_mcp.indexer.resolvers import doctor
from graphlens_mcp.indexer.workspace import Workspace, default_db_path
from graphlens_mcp.server.mcp_server import run_server
from graphlens_mcp.store.sqlite_store import SqliteStore

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s %(message)s")
logger = logging.getLogger("graphlens_mcp")


@click.group()
@click.option("--verbose", "-v", is_flag=True, help="Enable verbose logging")
def main(verbose: bool) -> None:
    """graphlens-mcp — semantic code graph MCP server."""
    if verbose:
        logging.getLogger("graphlens_mcp").setLevel(logging.DEBUG)


@main.command()
@click.option(
    "--root",
    "-r",
    default=".",
    show_default=True,
    help="Project root directory",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
)
@click.option(
    "--db",
    default=None,
    help="Path to the graph database (default: <root>/.graphlens/graph.db)",
    type=click.Path(path_type=Path),
)
@click.option(
    "--agent",
    "-a",
    multiple=True,
    type=click.Choice(list(REGISTRY)),
    help="Agent(s) to configure non-interactively. Repeatable. "
    "If omitted, an interactive selector is shown (TTY) or detected agents are used.",
)
@click.option("--no-agent", is_flag=True, help="Skip agent configuration")
@click.option("--no-skills", is_flag=True, help="Skip skill installation")
@click.option(
    "--yes",
    "-y",
    is_flag=True,
    help="Non-interactive: accept detected agents without prompting.",
)
def init(
    root: Path,
    db: Path | None,
    agent: tuple[str, ...],
    no_agent: bool,
    no_skills: bool,
    yes: bool,
) -> None:
    """Index the project and configure the MCP server in your agent."""
    root = root.resolve()
    db_path = db or default_db_path(root)

    click.echo(f"Initialising graphlens-mcp for {root}")
    click.echo()

    # Doctor
    click.echo("Checking toolchains…")
    report = doctor(root)
    if not report:
        click.echo(click.style("  No supported languages detected in this directory.", fg="yellow"))
        click.echo("  Supported: python, go, rust (and typescript/php with adapters installed)")
        return

    for lang, info in sorted(report.items()):
        status = info["status"]
        hint = info["hint"]
        icon = "✓" if status == "ok" else ("~" if status == "degraded" else "✗")
        color = "green" if status == "ok" else ("yellow" if status == "degraded" else "red")
        click.echo(f"  {click.style(icon, fg=color)} {lang}: {status}", nl=False)
        if hint:
            click.echo(f" — {hint}", nl=False)
        click.echo()

    click.echo()
    click.echo("Indexing…")

    async def _index() -> dict:
        workspace = await Workspace.create(root, db_path)
        try:
            return await workspace.full_index()
        finally:
            await workspace.close()

    stats = asyncio.run(_index())

    click.echo(f"  Indexed {stats['files']} files, {stats['nodes']} nodes, {stats['edges']} edges")
    click.echo()

    # Configure agents
    if not no_agent:
        selected = _resolve_agents(root, agent, yes)
        _configure_agents(selected, root, db_path, no_skills)

    click.echo()
    click.echo(click.style("Done.", fg="green") + f" DB at {db_path}")
    click.echo("Restart your agent to pick up the new MCP server.")


@main.command()
@click.option(
    "--root",
    "-r",
    default=".",
    show_default=True,
    help="Project root directory",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
)
@click.option(
    "--db",
    default=None,
    help="Path to the graph database",
    type=click.Path(path_type=Path),
)
def serve(root: Path, db: Path | None) -> None:
    """Start the MCP server (stdio transport). Launched by agents via config."""
    root = root.resolve()
    db_path = db or default_db_path(root)

    if not db_path.exists():
        click.echo(
            f"Graph DB not found at {db_path}. Run `graphlens-mcp init` first.",
            err=True,
        )
        sys.exit(1)

    run_server(db_path, root)


@main.command()
@click.option(
    "--root",
    "-r",
    default=".",
    show_default=True,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
)
@click.option(
    "--db",
    default=None,
    type=click.Path(path_type=Path),
)
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def status(root: Path, db: Path | None, as_json: bool) -> None:
    """Show the graph status: toolchains, node/edge counts, file freshness."""
    root = root.resolve()
    db_path = db or default_db_path(root)

    report = doctor(root)

    async def _stats() -> dict:
        if not db_path.exists():
            return {}
        store_info: dict = {}
        store = await SqliteStore.create(db_path)
        store_info["nodes"] = await store.node_count()
        store_info["edges"] = await store.edge_count()
        store_info["files"] = await store.file_count()
        store_info["file_list"] = await store.list_files()
        await store.close()
        return store_info

    store_info = asyncio.run(_stats())

    if as_json:
        click.echo(json.dumps({"toolchains": report, "store": store_info}, indent=2))
        return

    click.echo(f"Project: {root}")
    click.echo(f"DB: {db_path}" + ("" if db_path.exists() else " (not found — run init)"))
    click.echo()

    click.echo("Toolchains:")
    for lang, info in sorted(report.items()):
        color = {"ok": "green", "degraded": "yellow"}.get(info["status"], "red")
        click.echo(f"  {lang}: {click.style(info['status'], fg=color)}", nl=False)
        if info["hint"]:
            click.echo(f"  → {info['hint']}", nl=False)
        click.echo()

    if store_info:
        click.echo()
        click.echo(
            f"Graph: {store_info['nodes']} nodes, "
            f"{store_info['edges']} edges, "
            f"{store_info['files']} files"
        )
        stale = [f for f in store_info.get("file_list", []) if f["status"] != "ok"]
        if stale:
            click.echo(f"  {len(stale)} file(s) with status != ok (skeleton/degraded)")


@main.command()
@click.option(
    "--root",
    "-r",
    default=".",
    show_default=True,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
)
@click.option(
    "--db",
    default=None,
    type=click.Path(path_type=Path),
)
def reindex(root: Path, db: Path | None) -> None:
    """Force a full re-index (e.g. after installing a new toolchain)."""
    root = root.resolve()
    db_path = db or default_db_path(root)

    click.echo(f"Re-indexing {root}…")

    async def _reindex() -> dict:
        if db_path.exists():
            store = await SqliteStore.create(db_path)
            await store.clear_all()
            await store.close()
        workspace = await Workspace.create(root, db_path)
        try:
            return await workspace.full_index()
        finally:
            await workspace.close()

    stats = asyncio.run(_reindex())
    click.echo(f"Done. {stats['files']} files, {stats['nodes']} nodes, {stats['edges']} edges")


@main.command()
@click.option(
    "--root",
    "-r",
    default=".",
    show_default=True,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
)
@click.option("--db", default=None, type=click.Path(path_type=Path))
@click.option(
    "--agent",
    "-a",
    multiple=True,
    type=click.Choice(list(REGISTRY)),
    help="Agent(s) to deregister from. Default: all known agents.",
)
@click.option("--purge-db", is_flag=True, help="Also delete the local graph database.")
@click.option("--yes", "-y", is_flag=True, help="Do not prompt for confirmation.")
def remove(
    root: Path,
    db: Path | None,
    agent: tuple[str, ...],
    purge_db: bool,
    yes: bool,
) -> None:
    """Deregister the MCP server from agents and optionally delete the graph."""
    root = root.resolve()
    db_path = db or default_db_path(root)
    targets = list(agent) or list(REGISTRY)

    click.echo(f"Removing graphlens-mcp for {root}")
    removed_any = False
    for name in targets:
        spec = REGISTRY[name]
        try:
            if deregister_agent(spec, root):
                click.echo(f"  {click.style('✓', fg='green')} deregistered from {spec.label}")
                removed_any = True
        except Exception as exc:
            click.echo(f"  Failed to update {spec.label}: {exc}", err=True)
    if not removed_any:
        click.echo("  No agent config contained a graphlens entry.")

    if purge_db:
        _purge_graph_db(db_path, yes=yes)

    click.echo(click.style("Done.", fg="green"))


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _is_interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _purge_graph_db(db_path: Path, *, yes: bool) -> None:
    """Delete the graph database (and its WAL/SHM siblings) after confirmation."""
    if not db_path.exists():
        click.echo("  No graph database to delete.")
        return
    if not (
        yes
        or (
            _is_interactive()
            and click.confirm(f"  Delete graph database {db_path}?", default=False)
        )
    ):
        return
    for suffix in ("", "-wal", "-shm"):
        Path(str(db_path) + suffix).unlink(missing_ok=True)
    graph_dir = db_path.parent
    try:
        if graph_dir.name == ".graphlens" and not any(graph_dir.iterdir()):
            graph_dir.rmdir()
    except OSError:
        logger.debug("Could not remove %s (not empty?)", graph_dir)
    click.echo(f"  {click.style('✓', fg='green')} deleted {db_path}")


def _resolve_agents(root: Path, agent_flags: tuple[str, ...], yes: bool) -> list[str]:
    """Decide which agents to configure: explicit flags > interactive > detected."""
    if agent_flags:
        return list(dict.fromkeys(agent_flags))  # dedupe, preserve order

    detected = [name for name, spec in REGISTRY.items() if spec.detect(root)]
    detected = detected or ["claude_code"]

    if not yes and _is_interactive():
        return _select_agents_interactive(root, detected)

    click.echo("  Auto-selected: " + ", ".join(detected) + " (use --agent to override)")
    return detected


def _select_agents_interactive(root: Path, preselect: list[str]) -> list[str]:
    try:
        import questionary  # noqa: PLC0415 — lazy so a non-TTY/CI run never needs it
    except ImportError:
        return preselect

    choices = [
        questionary.Choice(
            title=f"{spec.label}  [{'detected' if spec.detect(root) else spec.scope}]",
            value=name,
            checked=name in preselect,
        )
        for name, spec in REGISTRY.items()
    ]
    answer = questionary.checkbox(
        "Configure which agents? (space to toggle, enter to confirm)",
        choices=choices,
    ).ask()
    return answer if answer is not None else []


def _configure_agents(selected: list[str], root: Path, db_path: Path, no_skills: bool) -> None:
    if not selected:
        click.echo("  No agents configured.")
        return
    for name in selected:
        spec = REGISTRY.get(name)
        if spec is None:
            click.echo(f"  Unknown agent {name!r}, skipping")
            continue
        try:
            path = configure_agent(spec, root, db_path)
        except Exception as exc:
            click.echo(f"  Failed to configure {spec.label}: {exc}", err=True)
            continue
        click.echo(f"  {click.style('✓', fg='green')} {spec.label} → {path}")
        if not no_skills and spec.install_skill:
            try:
                dest = spec.install_skill(root)
            except Exception as exc:
                click.echo(f"      skill install failed: {exc}", err=True)
            else:
                if dest:
                    click.echo(f"      installed navigation skill → {dest}")
