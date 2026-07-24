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
[semble](https://github.com/MinishLab/semble) retriever with a chunk→node bridge; the
implementation took the simpler path below — embedding graph nodes directly — so semble is
**not** a dependency.

## Goal

Make `graphlens-mcp` complete enough that an agent can stop reaching for raw `grep` and file
reads. The structural graph already answers *who calls X / what breaks if I change it*. Two
gaps otherwise pushed agents back to `grep`:

1. **No content search.** Name matching covers only symbol names — not function bodies,
   string literals, comments, logs, or config.
2. **No search by meaning.** "find the retry-with-backoff logic" has no entry point when you
   don't know the symbol name.

Both are folded into the single `search` tool: name matching, literal content matching (via
ripgrep), and semantic matching (via node embeddings) are tried together and all return graph
nodes, so the agent doesn't pick a different tool for each.

## Direct node embeddings (no chunk bridge)

The key simplification over an external chunk retriever: graphlens already has the units we
care about — **graph nodes** (functions/methods/classes). So we embed the nodes themselves
rather than file chunks:

- Each node's embedding text (`embed_text` in `shared/common/indexing/embed.py`) combines the
  node **kind** + `qualified_name`, identifier tokens split from the qualified name, the
  **signature**, the **docstring**, and — described in code as the strongest signal — a
  **source-body snippet** sliced from the node's span, truncated to ~1500 chars. It is
  embedded with the `model2vec` static model (`minishlab/potion-code-16M`, CPU-only, ~ms
  queries).
- The vectors live in **Milvus** — Milvus Lite on disk locally, a Milvus server when
  `DB__VECTOR` is set — as a single collection (`CODE_COLLECTION`) isolated per project by a
  `project_id` filter. This is a dedicated vector index alongside the Kuzu code graph, not
  vectors stuffed into the graph store.
- `search` encodes the query and issues a **vector search against the Milvus collection**
  (scoped by the project filter); Milvus returns ranked node ids. A hit is a node id directly
  — no `(file, line-range)` chunk, no overlap-to-node mapping — so a "found by meaning" result
  pivots straight into `relations` / `info`.

## Clustering — tried, then removed

An earlier iteration also ran the same node vectors through **HDBSCAN** (scikit-learn) to
auto-label dense semantic zones ("auth", "serialization", …), exposed as
`list_clusters`/`get_cluster` tools. It was removed:

- The navigation surface was kept small and dense — `search` / `relations` / `info` — after
  benchmarking showed weaker models use a smaller surface more reliably. Clustering added two
  tools whose value (browsing "zones") didn't pay for the extra tool-choice surface or the
  `scikit-learn`/HDBSCAN dependency weight.
- `search`'s semantic matching already covers "find code by meaning" without a clustering
  pass; clustering was additive browsing, not a required capability.

`scikit-learn` and HDBSCAN are no longer dependencies. This section is kept as a durable
record of the tradeoff, not as current behavior. (The full tool surface today is six tools —
these three navigation tools plus `index` / `list_projects` / `remove_project` for project
management.)

## Index cycle

Embedding is a step of the one indexing pipeline (`index_project_graph`), not a separate
phase to resume:

- The pipeline diffs the freshly analyzed graph against what's stored by `content_hash` and
  **re-embeds only nodes whose content changed** — unchanged nodes are skipped, which is the
  dominant cost saving on a re-index. This happens eagerly within the index run; there is no
  lazy on-search rebuild and no stale-marking.
- Embedding runs **before any destructive write**: vectors and graph are computed, then the
  old vectors/nodes are replaced. So a crash mid-index leaves the previous, consistent cache
  in place — crash-safety by ordering, not by a checkpoint table.

## Dependencies & degradation

`model2vec` is a **core dependency** — there is no optional extra. The model is loaded at the
top of `shared/common/indexing/embed.py` and fetched from HuggingFace on first use, then
cached. Note the current limitation: `search` calls `encode(...)` inline with no fallback, so
if the model cannot be fetched (offline first run, blocked HF egress) the `search` call errors
rather than silently degrading to name/content matching. Pre-warming the model cache avoids
this on air-gapped hosts.

## Testing under a blocked model host

The embedding model is fetched from HuggingFace at runtime, which may be blocked
(CI/sandboxed egress). Tests therefore run the pure logic (tokenizer, store round-trips,
content matching) unconditionally, exercise the embed path with a monkeypatched model offline,
and gate any real-model end-to-end check behind model availability.
