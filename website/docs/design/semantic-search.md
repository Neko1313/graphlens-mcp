---
id: semantic-search
title: Semantic search & clustering
sidebar_label: Semantic search
sidebar_position: 1
---

# Semantic search & clustering

Status: **implemented** (part of the base install — no optional extra). This page
captures the design behind the semantic layer so the decisions are durable. An
earlier draft explored composing the external
[semble](https://github.com/MinishLab/semble) retriever with a chunk→node bridge;
the implementation took the simpler path below — embedding graph nodes directly —
so semble is **not** a dependency.

## Goal

Make `graphlens-mcp` complete enough that an agent can stop reaching for raw
`grep` and file reads. The structural graph already answers *who calls X / what
breaks if I change it*. Two gaps pushed agents back to `grep`:

1. **No content search.** The FTS index covers only symbol *names*
   (`name`, `qualified_name`) — not function bodies, string literals,
   comments, logs, or config. Anything inside a body was invisible.
2. **No search by meaning.** "find the retry-with-backoff logic" has no entry
   point when you don't know the symbol name.

## New tools (5)

| Tool | Adds | Notes |
|---|---|---|
| `search_code` | grep replacement | regex/text over file content via ripgrep (+ pure-Python fallback). Uses no model. |
| `search_semantic` | search by meaning | ranks graph nodes by cosine similarity to the query; **each hit is already a graph node**, so the agent pivots straight into `get_callers`/`get_callees`. |
| `find_related` | "code like this symbol" | nearest nodes to a given node's vector. |
| `list_clusters` | labeled semantic zones | "auth", "serialization", … |
| `get_cluster` | a symbol's cluster + siblings | — |

`search_code` is model-free, so the primary grep replacement works even when the
embedding model can't be fetched. The other four use the embedding model (see
below).

## Direct node embeddings (no chunk bridge)

The key simplification over an external chunk retriever: graphlens already has
the units we care about — **graph nodes** (functions/methods/classes). So we
embed the nodes themselves rather than file chunks:

- Each node's embedding text is `qualified_name` + signature + a docstring
  summary, embedded with the `model2vec` static model (`minishlab/potion-code-16M`,
  CPU-only, ~ms queries). The float32 vectors are stored **in SQLite** next to
  the graph — there is no sidecar index.
- `search_semantic` / `find_related` load the vectors into a cached matrix and do
  in-process **cosine similarity**. A hit is a node id directly — no
  `(file, line-range)` chunk, no overlap-to-node mapping step.
- **Clustering** runs the same node vectors through **HDBSCAN** (scikit-learn) and
  auto-labels each cluster from its members' identifier tokens. Sparse nodes are
  left unclustered (HDBSCAN noise) — clusters describe dense zones, not a forced
  partition of everything.

## Unified index cycle

One pipeline, three phases, driven by the existing index entry points:

```
full_index():
  graph     (graphlens analyze + persist)        ← existing
  semantic  (embed nodes → store vectors)         ← new
  clusters  (HDBSCAN over node vectors → store)   ← new
```

- **Incremental edits** (`reindex_connected`, watcher/on-access) mark the
  semantic vectors and clusters *stale* rather than rebuilding them per save —
  re-clustering on every keystroke would be wasteful. They rebuild lazily on the
  next semantic/cluster query.
- **`full_index`** runs all three eagerly so init/reindex leave a complete,
  consistent cache.

## Checkpoint / resume (no DBOS)

Clustering and the graph index are expensive; a crash midway should not throw
the work away. We considered DBOS for durable workflows — it now has a SQLite
backend (`dbos[aiosqlite]`) — but it is marked dev-only for SQLite and pulls in
sqlalchemy/websockets/typer plus a second DB file. For a three-stage pipeline
that was disproportionate.

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
vectors and clusters are simply marked stale and rebuilt lazily.

## Dependencies & degradation

`model2vec`, `numpy` and `scikit-learn` are **core dependencies** — there is no
optional extra. `indexer/semantic.py` imports them at module top level. The only
graceful-degradation path that remains is a **model-download failure** (offline,
blocked HF egress, no `HF_TOKEN`): it is stored as a sticky reason and the
semantic tools return `available=false` with that reason, steering the agent back
to `search_symbols` / `search_code` while the graph server keeps working.

## Schema additions

```sql
clusters(id, label, size, terms)          -- terms = JSON array of top tokens
node_clusters(node_id, cluster_id, score) -- node ∈ ≤1 cluster; score = centroid cosine
```

Node embedding vectors are stored in SQLite (written by `store_embeddings`).
None of these carry foreign keys (same dangling-tolerance as `edges`): a cluster
row may briefly outlive a node mid-reindex; unresolved members are filtered at
read time. `LOCAL_SCHEMA_VERSION` is bumped so the cache rebuilds on upgrade.

## Testing under a blocked model host

The embedding model is fetched from HuggingFace at runtime, which may be
blocked (CI/sandboxed egress). Tests therefore:

- always run the pure logic (tokenizer, label derivation, cluster assembly), the
  store vectors/clusters/fingerprint, and `search_code` (ripgrep);
- exercise the embed/cluster paths with a **monkeypatched model** so graceful
  degradation and the full checkpoint state machine are covered offline;
- gate any real-model end-to-end check behind model availability.
