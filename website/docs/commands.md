---
id: commands
title: CLI commands
sidebar_position: 3
---

# CLI commands

| Command | What it does |
|---|---|
| `graphlens-mcp init` | Detect languages → toolchain doctor → full index → configure agents → install skill |
| `graphlens-mcp serve` | Start the MCP server over stdio. **Launched by the agent**, not by you |
| `graphlens-mcp status` | Show detected languages, toolchain status, and graph size/freshness |
| `graphlens-mcp reindex` | Force a full rebuild (e.g. after installing a new toolchain) |
| `graphlens-mcp remove` | Deregister from agents and (with `--purge-db`) delete the local graph |

## `init`

```bash
graphlens-mcp init [--root DIR] [--db PATH] \
  [--agent NAME ...] [--no-agent] [--no-skills] [--yes]
```

- `--root` — project root (default: current directory).
- `--db` — graph database path (default: `<root>/.graphlens/graph.db`).
- `--agent` — agent to configure, repeatable; skips the interactive selector.
- `--no-agent` / `--no-skills` — index only / skip the navigation skill install.
- `--yes` — accept detected agents without prompting (CI-friendly).

## `serve`

```bash
graphlens-mcp serve [--root DIR] [--db PATH] [--watch/--no-watch]
```

The agent launches this from its MCP config. The server answers queries from SQLite and, by
default, starts a **filesystem watcher** that keeps the graph fresh as you edit. Pass
`--no-watch` to disable it (the on-access freshness check still applies). See
[Freshness](./freshness.md).

## `reindex`

```bash
graphlens-mcp reindex [--root DIR] [--db PATH]
```

Clears and rebuilds the whole graph. Use it after installing a new language toolchain, or for
an exact cross-language / whole-project re-link.

## `remove`

```bash
graphlens-mcp remove [--root DIR] [--agent NAME ...] [--purge-db] [--yes]
```

Removes the `graphlens` entry from each agent's config (leaving your other servers intact).
With `--purge-db` it also deletes the `.graphlens/` cache.
