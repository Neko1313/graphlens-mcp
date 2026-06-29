# Design: semantic search & clustering

Status: implemented (optional `[semantic]` extra). This document captures the
design discussion behind the semantic layer so the decisions are durable.

## Goal

Make `graphlens-mcp` complete enough that an agent can stop reaching for raw
`grep` and file reads. The structural graph already answers *who calls X / what
breaks if I change it*. Two gaps pushed agents back to `grep`:

1. **No content search.** The FTS index covers only symbol *names*
   (`name`, `qualified_name`) — not function bodies, string literals,
   comments, logs, or config. Anything inside a body was invisible.
2. **No search by meaning.** "find the retry-with-backoff logic" has no entry
   point when you don't know the symbol name.

[semble](https://github.com/MinishLab/semble) solves (2) well (static
embeddings + BM25, CPU-only, ~ms queries) but has nothing graph-shaped — no
callers/callees, no clusters. graphlens has the graph but no semantics. The
design composes the two and adds clustering on top.

## New tools (5)

| Tool | Replaces / adds | Notes |
|---|---|---|
| `search_code` | `grep` | regex/text over file content via ripgrep (+ pure-Python fallback). No semantic deps. |
| `search_semantic` | semble `search` | search by meaning; **each hit carries the graph node ids it overlaps** so the agent pivots into `get_callers`/`get_callees`. |
| `find_related` | semble `find_related` | "code like this symbol", bridged back to nodes. |
| `list_clusters` | — (new) | labeled semantic zones of the codebase ("auth", "serialization"). |
| `get_cluster` | — (new) | the cluster a symbol belongs to + its sibling members. |

`search_code` is deliberately dependency-free so the primary grep replacement
works on every install; the other four require the `[semantic]` extra.

## The graph bridge

The unique value over plain semble: a semantic hit is a `(file_path,
start_line, end_line)` chunk, which `SqliteStore.nodes_overlapping` maps back
to the graph nodes whose source span overlaps it (tightest enclosing symbol
first). So "found by meaning" flows straight into "understand its structure".

Clustering is the inverse: embed each symbol node (`qualified_name` +
signature + docstring summary) with the same model semble uses
(`potion-code-16M` via model2vec), cluster the vectors with HDBSCAN, and
auto-label each cluster from its members' identifier tokens. Sparse nodes are
left unclustered (HDBSCAN noise) — clusters describe dense zones, not a forced
partition of everything.

## Unified index cycle

One pipeline, three phases, driven by the existing index entry points:

```
full_index():
  graph     (graphlens analyze + persist)      ← existing
  semantic  (build/persist semble index)       ← new
  clusters  (embed nodes → HDBSCAN → store)     ← new
```

- **Incremental edits** (`reindex_connected`, watcher/on-access) mark the
  semantic index and clusters *stale* rather than rebuilding them per save —
  the watcher cannot patch semble's index in place, and re-clustering on every
  keystroke would be wasteful. They rebuild lazily on the next semantic/cluster
  query.
- **`full_index`** runs all three eagerly so init/reindex leave a complete,
  consistent cache.

## Checkpoint / resume (no DBOS)

Clustering and the graph index are expensive; a crash midway should not throw
the work away. We considered DBOS for durable workflows — it now has a SQLite
backend (`dbos[aiosqlite]`) — but it is marked dev-only for SQLite and pulls in
sqlalchemy/websockets/typer plus a second DB file, and its launch model needs
checking against the stdio FastMCP loop. For a three-stage pipeline that was
disproportionate.

Instead we checkpoint in the graph's own `meta` table (the same regenerable
cache, zero new deps):

| key | value |
|---|---|
| `index_phase` | `indexing` → `graph` → `semantic` → `done` |
| `index_root_hash` | fingerprint of the indexed file set (`files_fingerprint`) |

`resume_pending_index()` runs at `serve` start (after `reconcile`): if the last
run completed the graph but died before clusters — and the fingerprint still
matches — it finishes only the unfinished tail. If the tree changed while the
server was down, `reconcile` has already patched the graph, so the semantic
index and clusters are simply marked stale and rebuilt lazily.

## Optional dependency strategy

The base install stays dependency-light. The extra:

```toml
[project.optional-dependencies]
semantic = ["scikit-learn>=1.3", "semble>=0.4"]
```

`indexer/semantic.py` imports every heavy package lazily and guarded.
Importing the module never fails on a base install. `semantic_availability()`
reports the install hint when the extra is absent; a build/query reports a
structured reason when the embedding model can't be fetched (offline, blocked
egress, no `HF_TOKEN`). The graph server keeps working regardless — semantic
tools just return `available=false` with a reason, steering the agent back to
`search_symbols` / `search_code`.

## Schema additions

```sql
clusters(id, label, size, terms)          -- terms = JSON array of top tokens
node_clusters(node_id, cluster_id, score) -- node ∈ ≤1 cluster; score = centroid cosine
```

No foreign keys (same dangling-tolerance as `edges`): a cluster row may briefly
outlive a node mid-reindex; unresolved members are filtered at read time. The
semble retrieval index is **not** in SQLite — it lives in a sidecar
(`.graphlens/semble-index`) written by semble's own `save()`. `LOCAL_SCHEMA_VERSION`
is bumped so the cache rebuilds on upgrade.

## Testing under a blocked model host

The embedding model is fetched from HuggingFace at runtime, which may be
blocked (CI/sandboxed egress). Tests therefore:

- always run the pure logic (tokenizer, label derivation, cluster assembly),
  the store bridge/clusters/fingerprint, and `search_code` (ripgrep);
- exercise the build/cluster paths with a **monkeypatched model** so graceful
  degradation, the chunk→node bridge, and the full checkpoint state machine are
  covered offline;
- gate any real-model end-to-end check behind model availability.
