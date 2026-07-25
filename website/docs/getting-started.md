---
id: getting-started
title: Getting started
sidebar_position: 2
---

# Getting started

## Install

Requires **Python ≥ 3.13** (a constraint inherited from `graphlens`).

```bash
uv tool install graphlens-mcp      # or: pipx install graphlens-mcp
```

Language support ships in the engine extra `graphlens[go,python,rust,typescript,php]`. Python
and the other languages parse out of the box; Go and Rust unlock **full** cross-file
resolution only when their language server is on `PATH` — `gopls` for Go, `rust-analyzer` for
Rust. Without them those languages still index, but in a `degraded` mode (fewer resolved
cross-file edges); indexing never blocks on a missing toolchain, it just reports the
`resolver_status` per language in the `index` result.

## Quickstart

There is no installer that edits your agent's config — you register `graphlens-mcp` as a
normal **stdio MCP server**, then let the agent index your code by calling the `index` tool.

1. **Register the server.** Add a stdio MCP entry pointing at `graphlens-mcp`. The exact file
   and format depend on your agent; a typical MCP config block looks like:

   ```json
   {
     "mcpServers": {
       "graphlens": { "command": "graphlens-mcp" }
     }
   }
   ```

2. **Index your project.** In the agent, ask it to index the repository (or call the tool
   directly): `index(directory="/path/to/your-project")`. The project must be a git
   repository with a remote — its identity is a hash of that remote. graphlens parses the
   **whole repository**, stores symbols + relations, embeds functions/classes/methods, and
   records the current commit.

3. **Ask questions.** e.g. *"what breaks if I change the signature of `create_order`?"* — the
   agent uses `search` / `relations` / `info` (see [Agent tools](./agent-tools.md)).

Re-running `index` on the same repo later refreshes it in place: unchanged symbols are skipped,
only what changed is re-written and re-embedded (see [Freshness](./freshness.md)).

## Where the graph lives

In local mode the data is a regenerable cache under your OS user-data directory — on Linux,
`$XDG_DATA_HOME/graphlens-mcp/`:

- `graph.db` — the code graph (embedded **Kuzu**)
- `vector.db` — the embeddings (**Milvus Lite**)
- `registry.db` — the project registry (Kuzu)

Delete the directory to reset; re-indexing rebuilds everything. There is no per-project
`.graphlens/` folder and nothing to add to your VCS ignore.

## Removing a project

Dropping a project's index is a tool call, `remove_project(project=…)`, which deletes that
project's graph nodes, embeddings, and registry entry. (Uninstalling the server itself is
just removing the MCP entry from your agent's config and, optionally, deleting the user-data
directory above.)

## Server deployment

For a hosted, multi-tenant deployment run `graphlens-mcp --http` behind an ingress and point
it at host backends with `DB__GRAPH` (a `neo4j://` DSN) and `DB__VECTOR` — the same code path,
only the stores change. See [Architecture](./architecture.md).
