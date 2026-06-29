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

**Monorepo / workspace routing.** A repo can hold several independent packages of one
language (a uv / pnpm / cargo workspace). The full index lets each adapter discover those
per-package roots and keys every node id off the *package* name and its package-relative
module path. Incremental re-index must use the same roots, so `reindex_connected` groups
each changed file under its owning package root (`find_language_roots` →
`_nearest_root`) and analyzes per group. Passing the repo root with `files=` instead would
collapse the whole workspace into one project, re-keying a member's symbols under the wrong
name and breaking every cross-file edge into them. A plain single-package repo is one group
(the project root) and behaves exactly as before.

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
8. **Atomic, rolled-back writes.** Every write runs under `SqliteStore._writing` — the
   single-writer lock plus commit-on-success / rollback-on-error — so a failed multi-statement
   patch can never leave a partial transaction for the next writer.
9. **Resolver off the hot path.** One adapter (and resolver) is pooled per language for the
   `Workspace` lifetime; `Workspace.close()` shuts down resolver/LSP processes. Queries are
   served from SQLite, never by invoking a resolver synchronously.

## Storage

SQLite with `nodes`, `edges`, `deps`, `files`, `meta`, `clusters`, `node_clusters` and an
FTS5 index over symbol names. A dedicated **writer** connection serializes all writes behind
a write lock (so multi-statement patches are atomic), while a separate read-only connection
serves queries from the last committed WAL snapshot without queuing behind an in-flight
write. WAL is enabled for crash-safety and reader/writer concurrency.

## Semantic layer (optional `[semantic]` extra)

A bolt-on that lets agents search by *meaning* and by *content* — the cases that otherwise
send them back to `grep` — without adding weight to the base install. See
[docs/design/semantic-search.md](docs/design/semantic-search.md) for the full design.

- **Content search** (`search_code`) is the grep replacement: regex/text over file content
  via ripgrep with a pure-Python fallback. No semantic dependency.
- **Semantic search** (`search_semantic`, `find_related`) wraps [semble](https://github.com/MinishLab/semble)
  (static embeddings + BM25). Every hit is a `(file, line-range)` chunk that
  `SqliteStore.nodes_overlapping` bridges back to the graph's **node ids** (tightest
  enclosing symbol first), so a "found by meaning" result pivots straight into
  `get_callers`/`get_callees`.
- **Clusters** (`list_clusters`, `get_cluster`) embed each symbol node (model2vec, the same
  model semble uses) and group them with HDBSCAN into auto-labeled semantic zones. Sparse
  nodes are left unclustered; clusters describe dense zones, not a forced partition.

`indexer/semantic.py` imports every heavy package lazily and guarded: importing it never
fails on a base install, and a missing extra or unreachable embedding model degrades to a
structured `available=false` reason instead of breaking the graph server. The semble index
lives in a sidecar (`.graphlens/semble-index`), not in SQLite; clusters live in the
`clusters`/`node_clusters` tables and, like edges, carry **no foreign key** (a cluster row
may briefly outlive a node mid-reindex; unresolved members are filtered at read time).

### Unified index cycle & resume

`full_index` runs three phases in order — **graph → semantic → clusters** — recording a
resume checkpoint in `meta` after each (`index_phase`, with `index_root_hash` =
`files_fingerprint`). The semantic phases are best-effort: if the extra is absent or the
model is unreachable the graph index still completes and the checkpoint rests at the graph
phase. Incremental edits (`reindex_connected`) only *mark* the semantic index and clusters
stale — re-clustering per file save would be wasteful — and they rebuild lazily on the next
semantic/cluster query. `serve` calls `resume_pending_index` after `reconcile`: it finishes
a tail (clustering) that a prior crash interrupted when the fingerprint still matches, and
otherwise marks the layer stale for lazy rebuild. This is the checkpoint/resume the project
needs for expensive index work without taking on a durable-workflow framework (e.g. DBOS).

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
  nodes (and to full edge identity for a self-contained file). A new file an *unchanged*
  file already imports is covered by a second importer pass in `reindex_connected`: once the
  new file is indexed its importers resolve and are re-linked, so the dangling edge into it
  is rebuilt without a full reindex.
- **Cross-language edge erosion (mitigated):** `COMMUNICATES_WITH` is synthesized by the
  full-index link pass, never by single-file analysis, so an incremental patch preserves the
  edges it cannot re-emit. After each connected-set re-index, `_resynthesize_cross_language`
  rebuilds the pairwise edges for every boundary the re-indexed files touch (and prunes
  dangling ones), so a new or renamed exposer/consumer is linked immediately. A full
  `reindex` remains the exact escape hatch for a participant that leaves a boundary other
  files still use.
