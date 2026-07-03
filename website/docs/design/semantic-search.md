---
id: semantic-search
title: Semantic search & clustering
sidebar_label: Semantic search
sidebar_position: 1
---

# Semantic search & clustering

Status: **semantic search implemented** (part of the base install — no optional extra);
**clustering removed**. This page captures the design behind the semantic layer so the
decisions are durable. An earlier draft explored composing the external
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

Both are folded into the single `search` tool today: content matching (literal, via
ripgrep) and semantic matching (meaning, via node embeddings) are two of the three ways
`search` can match a query, alongside name matching — the agent doesn't pick a different
tool for each, `search` tries all three and returns graph nodes either way.

## Direct node embeddings (no chunk bridge)

The key simplification over an external chunk retriever: graphlens already has
the units we care about — **graph nodes** (functions/methods/classes). So we
embed the nodes themselves rather than file chunks:

- Each node's embedding text is `qualified_name` + signature + a docstring
  summary, embedded with the `model2vec` static model (`minishlab/potion-code-16M`,
  CPU-only, ~ms queries). The float32 vectors are stored **in SQLite** next to
  the graph — there is no sidecar index.
- `search`'s semantic fallback loads the vectors into a cached matrix and does
  in-process **cosine similarity**. A hit is a node id directly — no
  `(file, line-range)` chunk, no overlap-to-node mapping step.

## Clustering — tried, then removed

An earlier iteration also ran the same node vectors through **HDBSCAN**
(scikit-learn) to auto-label dense semantic zones ("auth", "serialization", …),
exposed as `list_clusters`/`get_cluster` tools. It was removed:

- The engine consolidated the agent-facing surface down to three tools
  (`search`/`relations`/`info`) after benchmarking showed the smaller, denser
  surface was easier for weaker models to use correctly — clustering added two
  tools whose value (browsing "zones") didn't pay for the extra tool-choice
  surface or the `scikit-learn`/HDBSCAN dependency weight.
- `search`'s semantic fallback already covers "find code by meaning" without a
  clustering pass; clustering was additive browsing, not a required capability.

`scikit-learn` and HDBSCAN are no longer dependencies; the `clusters`/`node_clusters`
tables and their store methods no longer exist. This section is kept as a durable record
of the tradeoff, not as current behavior.

## Unified index cycle

One pipeline, two phases, driven by the existing index entry points:

```
full_index():
  graph     (graphlens analyze + persist)        ← existing
  semantic  (embed nodes → store vectors)         ← new
```

- **Incremental edits** (`reindex_connected`, watcher/on-access) mark the
  semantic vectors *stale* rather than rebuilding them per save — re-embedding
  on every keystroke would be wasteful. They rebuild lazily on the next
  semantic-matching `search` call.
- **`full_index`** runs both eagerly so init/reindex leave a complete,
  consistent cache.

## Checkpoint / resume (no DBOS)

The graph index and semantic embedding pass are expensive; a crash midway should not throw
the work away. We considered DBOS for durable workflows — it now has a SQLite
backend (`dbos[aiosqlite]`) — but it is marked dev-only for SQLite and pulls in
sqlalchemy/websockets/typer plus a second DB file. For a two-stage pipeline
that was disproportionate.

Instead we checkpoint in the graph's own `meta` table (the same regenerable
cache, zero new deps):

| key | value |
|---|---|
| `index_phase` | `indexing` → `graph` → `done` |
| `index_root_hash` | fingerprint of the indexed file set (`files_fingerprint`) |

`resume_pending_index()` runs at `serve` start (after `reconcile`): if the last
run completed the graph but died before the semantic embed pass — and the fingerprint
still matches — it finishes only the unfinished tail. If the tree changed while the
server was down, `reconcile` has already patched the graph, so the semantic
vectors are simply marked stale and rebuilt lazily.

## Dependencies & degradation

`model2vec` is a **core dependency** — there is no optional extra. `indexer/semantic.py`
imports it at module top level. The only graceful-degradation path that remains is a
**model-download failure** (offline, blocked HF egress, no `HF_TOKEN`): it is stored as a
sticky reason and the semantic fallback returns `available=false` with that reason,
steering `search` back to name/content matching only while the graph server keeps working.

## Testing under a blocked model host

The embedding model is fetched from HuggingFace at runtime, which may be
blocked (CI/sandboxed egress). Tests therefore:

- always run the pure logic (tokenizer, the store vectors/fingerprint, and content
  matching (ripgrep));
- exercise the embed path with a **monkeypatched model** so graceful degradation and
  the full checkpoint state machine are covered offline;
- gate any real-model end-to-end check behind model availability.
