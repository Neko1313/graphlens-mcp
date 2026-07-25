---
id: commands
title: CLI & invocation
sidebar_position: 3
---

# CLI & invocation

`graphlens-mcp` is an MCP **server**, not a task runner — its command line only chooses a
transport. Everything else (indexing a project, searching, navigating, removing a project)
is done through [MCP tools](./agent-tools.md) that the agent calls over that transport, not
through subcommands.

| Invocation | What it does |
|---|---|
| `graphlens-mcp` | Serve over **stdio**. The common local case — your agent spawns the process and talks to it over the pipe. |
| `graphlens-mcp --http` | Serve over **Streamable HTTP** for a standalone or Kubernetes deployment. |

There are no `init` / `serve` / `status` / `reindex` / `remove` subcommands — those actions
live in the MCP tool surface.

## Flags

```bash
graphlens-mcp --http [--host 127.0.0.1] [--port 8000]
```

- `--http` — serve Streamable HTTP instead of stdio.
- `--host` — bind host for `--http` (default `127.0.0.1`).
- `--port` — bind port for `--http` (default `8000`).

That is the entire command-line surface (`app.cli:main`).

## Managing a project

Adding, refreshing, and removing projects are **tool calls the agent makes**, not CLI
commands:

| Task | MCP tool |
|---|---|
| Add / refresh a project | `index(directory=…)` locally, or `index(repo_url=…)` for a server-side clone. Re-running it refreshes in place (diff-driven — there is no separate reindex). |
| List indexed projects | `list_projects()` |
| Drop a project's index | `remove_project(project=…)` |

A project is always a **whole git repository** — its identity is a hash of the git remote, so
a local checkout and a CI clone of the same repo map to the same project. See
[Agent tools](./agent-tools.md) for the full signatures and [Freshness](./freshness.md) for
how re-indexing works.

## Where data lives

In local mode (no database DSN configured) the graph and embeddings are written to your OS
user-data directory — on Linux, `$XDG_DATA_HOME/graphlens-mcp/` — as `graph.db` (embedded
Kuzu), `vector.db` (Milvus Lite), and `registry.db` (the project registry). It is a
regenerable cache; deleting it and re-indexing rebuilds it. For a hosted deployment set
`DB__GRAPH` (a `neo4j://` DSN) and `DB__VECTOR` to point at host Neo4j + Milvus behind the
same ports — see [Architecture](./architecture.md).
