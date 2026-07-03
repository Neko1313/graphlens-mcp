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
7. **Cycle-safe traversal.** The store's callers/callees/implementors walks use recursive CTEs
   with a visited-path guard, so cyclic call graphs terminate without exponential blow-up.
8. **Atomic, rolled-back writes.** Every write runs under `SqliteStore._writing` — the
   single-writer lock plus commit-on-success / rollback-on-error — so a failed multi-statement
   patch can never leave a partial transaction for the next writer.
9. **Resolver off the hot path.** One adapter (and resolver) is pooled per language for the
   `Workspace` lifetime; `Workspace.close()` shuts down resolver/LSP processes. Queries are
   served from SQLite, never by invoking a resolver synchronously.

## Storage

SQLite with `nodes`, `edges`, `deps`, `files`, `meta` and an FTS5 index over symbol names. A
dedicated **writer** connection serializes all writes behind a write lock (so multi-statement
patches are atomic), while a separate read-only connection serves queries from the last
committed WAL snapshot without queuing behind an in-flight write. WAL is enabled for
crash-safety and reader/writer concurrency.

## Semantic layer

Lets `search` fall back to matching by **meaning** when name/content matching comes up thin —
the case that otherwise sends an agent back to grep. It is part of the base install
(`model2vec` is a core dependency), not an optional extra.

- **Content matching** inside `search` is the grep replacement: literal text over file content
  via ripgrep with a pure-Python fallback. Uses no model at all.
- **Semantic matching** embeds the graph's **nodes** (functions/methods/classes) directly with
  the `model2vec` static model (`minishlab/potion-code-16M`) and ranks by in-process **cosine
  similarity** over a cached vector matrix. There is no file chunking and no chunk→node bridge
  — *each hit is already a graph node*, so a "found by meaning" result pivots straight into
  `relations`/`info`.

The float32 vectors are stored **in SQLite** alongside the graph (no sidecar index). The model
is imported at module top level — so the only graceful-degradation path that remains is a
**model-download failure** (offline, blocked HF egress): it is stored as a sticky reason and
surfaced via `available=false`, and the graph server keeps working with name/content matching
only.

### Unified index cycle & resume

`full_index` runs two phases in order — **graph → semantic** — recording a resume checkpoint
in `meta` after each (`index_phase`, with `index_root_hash` = `files_fingerprint`). The
semantic phase is best-effort: if the embedding model can't be fetched (offline) the graph
index still completes and the checkpoint rests at the graph phase. Incremental edits
(`reindex_connected`) only *mark* the semantic index stale — re-embedding per file save would
be wasteful — and it rebuilds lazily on the next semantic query. `serve` calls
`resume_pending_index` after `reconcile`: it finishes an embedding pass a prior crash
interrupted when the fingerprint still matches, and otherwise marks the layer stale for lazy
rebuild. This is the checkpoint/resume the project needs for expensive index work without
taking on a durable-workflow framework (e.g. DBOS).

## Cache, not system of record

The graph is a **regenerable cache** of the code on disk — it is never migrated. The
store records a schema *fingerprint* combining the engine's model `SCHEMA_VERSION` with a
local `LOCAL_SCHEMA_VERSION` (bumped on any `schema.sql` change). On mismatch the tables
are dropped and rebuilt from scratch. This is why there is no Alembic: migrations would be
pure overhead for a cache you can rebuild in seconds with `reindex`.

## Tool boundary

Every MCP tool returns a typed Pydantic model (`server/models.py`). List responses carry
`resolver_status` (`ok` | `degraded`, aggregated across every returned node's file — there is
no structure-only "skeleton" state, every index is a full analyze), an `indexing` flag (a
background reindex is in progress, so edges may be incomplete) and a `truncated` flag; results
are capped (`MAX_RESULTS`, 200), and an oversized `limit` / `depth` is clamped rather than
rejected. File-touching tools run the freshness check first; relative paths resolve against
the project root, not the server cwd.

## Known limitations

- **Connected-set, not whole-project, re-link:** a change re-analyzes the changed file with
  its direct importers and imports, so cross-file edges within that set are correct, but a
  change that ripples through several indirection layers may need a full `reindex` for an
  exact graph. A new file an *unchanged* file already imports is covered by a second importer
  pass in `reindex_connected`: once the new file is indexed its importers resolve and are
  re-linked, so the dangling edge into it is rebuilt without a full reindex.
- **Cross-language edge erosion (mitigated):** `COMMUNICATES_WITH` is synthesized by the
  full-index link pass, never by single-file analysis, so an incremental patch preserves the
  edges it cannot re-emit. After each connected-set re-index, `_resynthesize_cross_language`
  rebuilds the pairwise edges for every boundary the re-indexed files touch (and prunes
  dangling ones), so a new or renamed exposer/consumer is linked immediately. A full
  `reindex` remains the exact escape hatch for a participant that leaves a boundary other
  files still use.
