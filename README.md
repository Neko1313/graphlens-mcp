# graphlens-mcp

A free, MIT-licensed [MCP](https://modelcontextprotocol.io) server that gives coding
agents (Claude Code, Cursor, and compatible clients) a **semantic code graph** of your
project — symbols, cross-file calls, references, imports and cross-language boundaries.

Instead of reading files top-to-bottom or grepping for names, the agent **navigates the
structure**: *who calls this function*, *what does it depend on*, *what breaks if I change
its signature*. It is a thin runtime layer over the [`graphlens`](https://pypi.org/project/graphlens/)
analysis engine: `graphlens` provides the mechanisms (parsing, stable node identity,
resolvers); `graphlens-mcp` owns the storage, freshness and the agent-facing surface.

> Status: early. The core navigation works; see [Known limitations](#known-limitations).

## Why

A `filesystem`/grep MCP makes the agent read whole files and match text — slow, noisy, and
blind to which of three modules actually calls `OrderService.create`. Bare tree-sitter
gives single-file syntax but cannot resolve links *between* files. `graphlens-mcp` answers
the cross-file questions — call graphs and impact analysis — and keeps the graph fresh as
you edit, then teaches the agent to use it via a bundled navigation skill.

## Install

Requires **Python ≥ 3.13** (a constraint inherited from `graphlens`).

```bash
uv tool install graphlens-mcp      # or: pipx install graphlens-mcp
```

Python language analysis works out of the box (the `ty` type engine ships as a
dependency). Other languages parse immediately at the **skeleton** level and unlock full
semantics once their toolchain is present (Node for TypeScript, the Go toolchain, etc.).

## Quickstart (two commands)

```bash
uv tool install graphlens-mcp        # 1. install
cd your-project && graphlens-mcp init  # 2. index + configure your agent
```

`init` detects the project's languages, indexes the code into a local graph, writes the
MCP server entry into your agent's config and installs the navigation skill. You do **not**
run `serve` yourself — your agent launches it from the config. Restart the agent and ask
it something like *"what breaks if I change the signature of `create_order`?"*.

## Commands

| Command | What it does |
|---|---|
| `graphlens-mcp init` | Detect languages → toolchain doctor → full index → configure agents → install skill |
| `graphlens-mcp serve` | Start the MCP server over stdio. **Launched by the agent**, not by you |
| `graphlens-mcp status` | Show detected languages, toolchain status, and graph size/freshness |
| `graphlens-mcp reindex` | Force a full rebuild (e.g. after installing a new toolchain) |
| `graphlens-mcp remove` | Deregister from agents and (with `--purge-db`) delete the local graph |

Useful `init` flags: `--root <dir>`, `--agent claude_code --agent cursor` (repeatable),
`--no-agent`, `--no-skills`, `--db <path>`.

The graph lives at `<project>/.graphlens/graph.db` (SQLite). It is a regenerable cache —
safe to delete; `reindex` rebuilds it. Add `.graphlens/` to your VCS ignore (the bundled
`init` flow assumes it is not committed).

## Supported languages

| Language | Engine | Out-of-box |
|---|---|---|
| Python | `ty` (bundled) | Full semantics immediately |
| TypeScript | Node bridge | Skeleton without Node; full semantics with Node installed |
| Go | Go toolchain | Skeleton without toolchain |
| Rust | SCIP / rust-analyzer | Skeleton without toolchain |
| PHP | PHP parser | Skeleton without toolchain |

`graphlens-mcp status` reports the actual resolver status per language. When a toolchain is
missing, that language degrades to a **skeleton** (structure only) with an install hint —
it never blocks `init`.

## Agent tools

Each response carries a graph-quality status (`ok` | `degraded` | `skeleton`) so the agent
never mistakes a partial answer for a complete one.

| Tool | Purpose |
|---|---|
| `search_symbols` | Full-text search over symbol names — **start here** |
| `get_node_info` | Source snippet + signature + location for a node |
| `get_file_structure` | Symbol outline of a file |
| `get_callees` | What a function calls (outgoing, up to `max_depth`) |
| `get_callers` | Who calls a function — primary impact-analysis tool |
| `get_neighbors` | Nodes within N hops in any direction |
| `find_references` | Non-call usages (type annotations, assignments) |
| `get_cross_language_calls` | Connections across service boundaries (HTTP/gRPC/queues) |

## Freshness model

The graph is kept current on access. Before answering a query about a file, the server
compares the file's `mtime`/`size` (confirmed by content hash) against the store; if it
changed, the file is re-indexed in two phases — an instant **skeleton** (structure) and, on
demand for semantic queries, **full semantics** (resolved calls/types). Deleting a file on
disk prunes its symbols from the graph on the next access.

## Known limitations

- **Transitive freshness:** if file `B` changes, the semantics of a file `A` that imports it
  can reflect `B`'s old signature until a full `reindex`. A semantic query on `A` now
  **detects** this (it checks whether `A`'s recorded dependencies changed on disk) and
  reports `resolver_status: degraded` instead of a false `ok` — single-file analysis cannot
  re-resolve a cross-file call on its own. Run `reindex` for exact cross-file edges.
- **Cross-language edges on incremental edits:** synthesized `COMMUNICATES_WITH` edges are
  rebuilt on a full `reindex`; they can erode across incremental edits (the boundary-based
  query still resolves connections). Run `reindex` for an exact cross-language view.
- **Detaching:** `graphlens-mcp remove` deregisters the server from your agents; add
  `--purge-db` to also delete the local `.graphlens/` cache.

## License

MIT — see [LICENSE](LICENSE).
