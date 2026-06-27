# Architecture

`graphlens-mcp` is a thin, stateful runtime over the stateless [`graphlens`](https://pypi.org/project/graphlens/)
engine. The engine provides the mechanisms (parsing, stable node identity, resolvers,
cross-language linking); this product owns all storage, freshness and the agent-facing
surface. Nothing stateful leaks into the engine.

## Components

```
src/graphlens_mcp/
  cli.py            # init / serve / status / reindex / remove
  store/            # SQLite: schema, patches, graph queries (CTEs, FTS5)
  indexer/          # workspace orchestration, resolver lifecycle, concurrency
  server/           # FastMCP server, tools, Pydantic I/O models
  agents/           # per-agent MCP config registry (JSON + Codex TOML)
  skills/           # navigation skill installed into the agent
```

## Lifecycle

- **`init`** — detect languages → toolchain doctor → full index → persist → write agent
  config → install skill. Interactive agent selection (checkbox) or `--agent/--yes`.
- **`serve`** — FastMCP over stdio, launched by the agent. Answers queries from SQLite.
- **`reindex`** — clear and rebuild the whole graph.
- **`remove`** — deregister from agents and optionally delete the cache.

## Freshness (watcher-driven)

A single mechanism keeps the graph current: a **filesystem watcher** (`watchfiles`),
started by `serve` (`Workspace.start_watching`) unless `--no-watch` is passed. On each
change the watcher calls `Workspace.reindex_connected`, which re-indexes the **connected
set** of every changed file — the file plus its importers (`get_importer_files`) and its
imports (`get_imported_files`) — with one full `analyze(files=…)`. Analyzing the set
together lets the resolver re-link calls *across* those files, so the affected region is a
full graph, not a single-file approximation. Deletions prune the file and refresh its
importers. There is **no** structure-only "skeleton" phase: every (re)index is a full
analyze, so a file is `ok` or (toolchain missing) `degraded`.

`Workspace.ensure_fresh` is the on-access backstop: a tool that touches a changed file
before the watcher has processed it runs the same `reindex_connected` (deduped through
`InFlightRegistry`).

Because an event-based watcher cannot see changes made while it was not running, `serve`
calls `Workspace.reconcile` once at startup: it walks the project (`_discover_source_files`,
excluding `.graphlens`/VCS/build dirs), diffs disk against the `files` table, and feeds the
new/deleted/edited paths through `reindex_connected`. A wholesale rebuild remains `reindex`.

## Key invariants

1. **Stable node ids** come from the engine (`make_node_id`) — never positional. This is
   what lets a cross-file edge reconnect after its target file is re-indexed.
2. **Path normalization.** Adapters emit mixed `file_path` forms (FILE/MODULE nodes
   relative, symbol nodes absolute). `_normalize_graph_paths` resolves every path to
   absolute before persisting, so nothing is dropped and the `files` table has one key
   per file regardless of the process cwd.
3. **File-owned writes.** `apply_patch` deletes/replaces only a file's own nodes and the
   edges sourced from them, so re-indexing one file never touches another.
4. **Fileless structural pass.** Project/module/boundary nodes (`file_path = NULL`) and
   their `contains` edges are persisted by `apply_structural` (a separate full-index pass,
   like cross-language linking), since the per-file ownership filter cannot place them.
5. **Cross-language edges survive incremental.** `COMMUNICATES_WITH` is synthesized at
   full index only; `apply_patch` therefore excludes it from its per-file edge delete so
   it does not erode on incremental re-index. A full `reindex` rebuilds it exactly.
6. **Dangling edges, no foreign keys.** An edge references its target by stable id, which
   may be momentarily absent during re-index. There is **no** FK/CASCADE (it would reject
   such edges); unresolved targets are filtered at read time instead.
7. **Cycle-safe traversal.** `get_callees/callers/neighbors` use recursive CTEs with a
   visited-path guard, so cyclic call graphs terminate without exponential blow-up.
8. **Hash-versioned semantic patches.** A semantic re-index re-checks the file hash before
   applying; a result computed against stale content is discarded.
9. **Resolver off the hot path.** One adapter (and resolver) is pooled per language for the
   `Workspace` lifetime; `Workspace.close()` shuts down resolver/LSP processes. Queries are
   served from SQLite, never by invoking a resolver synchronously.

## Storage

SQLite with `nodes`, `edges`, `deps`, `files`, `meta` and an FTS5 index over symbol names.
A single aiosqlite connection serializes all operations; every write is additionally
guarded by a write lock so multi-statement patches are atomic. WAL is enabled for
crash-safety. (A dedicated reader connection is a possible future optimization; for a
single-user local server the serialized model is simpler and write bursts are short.)

## Cache, not system of record

The graph is a **regenerable cache** of the code on disk — it is never migrated. The
store records a schema *fingerprint* combining the engine's model `SCHEMA_VERSION` with a
local `LOCAL_SCHEMA_VERSION` (bumped on any `schema.sql` change). On mismatch the tables
are dropped and rebuilt from scratch. This is why there is no Alembic: migrations would be
pure overhead for a cache you can rebuild in seconds with `reindex`.

## Tool boundary

Every MCP tool returns a typed Pydantic model (`server/models.py`). List responses carry
`resolver_status` (`ok` | `degraded`, aggregated across every returned node's file) and a
`truncated` flag, results are capped (`MAX_RESULTS`), and input bounds (`limit`,
`max_depth`) are validated by pydantic. File-touching tools run the freshness check first;
relative paths resolve against the project root, not the server cwd.

## Known limitations

- **Connected-set, not whole-project, re-link:** a change re-analyzes the changed file with
  its direct importers and imports, so cross-file edges within that set are correct, but a
  change that ripples through several indirection layers may need a full `reindex` for an
  exact graph. The golden test scopes the `batch == incremental` invariant to file-bearing
  nodes (and to full edge identity for a self-contained file).
