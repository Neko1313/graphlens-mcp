---
id: architecture
title: Architecture
sidebar_position: 7
---

# Architecture

`graphlens-mcp` is a thin, stateful runtime over the stateless
[`graphlens`](https://github.com/Neko1313/graphlens) engine. The engine provides the
mechanisms (parsing, stable node identity, resolvers, cross-language linking); this product
owns all storage, freshness and the agent-facing surface. Nothing stateful leaks into the
engine.

## Components

```
src/graphlens_mcp/
  cli.py            # init / serve / status / reindex / remove
  store/            # SQLite: schema, patches, graph queries (CTEs, FTS5)
  indexer/          # workspace orchestration, watcher, resolver lifecycle
  server/           # FastMCP server, tools, Pydantic I/O models
  agents/           # per-agent MCP config registry (JSON + Codex TOML)
  skills/           # navigation skill installed into the agent
```

## Lifecycle

- **`init`** — detect languages → toolchain doctor → full index → persist → write agent
  config → install skill.
- **`serve`** — FastMCP over stdio, launched by the agent. Reconciles at startup, then starts
  the watcher and answers queries from SQLite.
- **`reindex`** — clear and rebuild the whole graph.
- **`remove`** — deregister from agents and optionally delete the cache.

## Freshness (watcher-driven)

A single mechanism keeps the graph current: a **filesystem watcher** (`watchfiles`), started
by `serve` (`Workspace.start_watching`) unless `--no-watch` is passed. On each change the
watcher calls `Workspace.reindex_connected`, which re-indexes the **connected set** of every
changed file — the file plus its importers (`get_importer_files`) and its imports
(`get_imported_files`) — with one full `analyze(files=…)`. Analyzing the set together lets the
resolver re-link calls *across* those files, so the affected region is a full graph, not a
single-file approximation. Deletions prune the file and refresh its importers. There is **no**
structure-only "skeleton" phase: every (re)index is a full analyze, so a file is `ok` or
(toolchain missing) `degraded`.

`Workspace.ensure_fresh` is the on-access backstop. And because an event-based watcher cannot
see changes made while it was not running, `serve` calls `Workspace.reconcile` once at startup
to catch up on files created/deleted/edited while down.

## Key invariants

1. **Stable node ids** come from the engine (`make_node_id`) — never positional. This is what
   lets a cross-file edge reconnect after its target file is re-indexed.
2. **Path normalization.** `_normalize_graph_paths` resolves every path to absolute before
   persisting, so the `files` table has one key per file regardless of the process cwd.
3. **File-owned writes.** `apply_patch` deletes/replaces only a file's own nodes and the edges
   sourced from them, so re-indexing one file never touches another.
4. **Fileless structural pass.** Project/module/boundary nodes (`file_path = NULL`) are
   persisted by `apply_structural`, since the per-file ownership filter cannot place them.
5. **Cross-language edges survive incremental.** `COMMUNICATES_WITH` is synthesized at full
   index only; `apply_patch` excludes it from its per-file edge delete so it does not erode.
6. **Dangling edges, no foreign keys.** An edge references its target by stable id, which may
   be momentarily absent during re-index; unresolved targets are filtered at read time.
7. **Cycle-safe traversal.** `get_callees/callers/neighbors` use recursive CTEs with a
   visited-path guard, so cyclic call graphs terminate without exponential blow-up.
8. **Atomic, rolled-back writes.** Every write runs under `SqliteStore._writing` (single-writer
   lock + commit-on-success / rollback-on-error), so a failed patch never leaves a partial
   transaction for the next writer.
9. **Resolver off the hot path.** One adapter is pooled per language for the `Workspace`
   lifetime; queries are served from SQLite, never by invoking a resolver synchronously.

## Storage

SQLite with `nodes`, `edges`, `deps`, `files`, `meta` and an FTS5 index over symbol names. A
dedicated **writer** connection serializes all writes behind a write lock, while a separate
read-only connection serves queries from the last committed WAL snapshot without queuing
behind an in-flight write.

## Cache, not system of record

The graph is a **regenerable cache** of the code on disk — it is never migrated. The store
records a schema *fingerprint* combining the engine's model `SCHEMA_VERSION` with a local
`LOCAL_SCHEMA_VERSION`. On mismatch the tables are dropped and rebuilt from scratch — which is
why there is no Alembic: migrations would be pure overhead for a cache you can rebuild in
seconds with `reindex`.
