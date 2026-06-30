---
id: agent-tools
title: Agent tools
sidebar_position: 4
---

# Agent tools

Each response carries a graph-quality status (`ok` | `degraded` | `skeleton`, aggregated across
every returned node's file) so the agent never mistakes a partial answer for a complete one.
List responses also carry a `truncated` flag and are capped at `MAX_RESULTS` (200).

Every response also carries an `indexing` boolean. The server starts serving immediately and
reconciles the graph in the background, so `indexing=true` means a reindex is running and edges
may still be incomplete — don't conclude a symbol is unused while `indexing=true`.

| Tool | Purpose |
|---|---|
| `search_symbols` | Full-text search over symbol names — **start here** to find a node ID |
| `explore` | One call for *"what is X / who uses it / what implements it"* — a symbol's source + signature plus its direct callers, callees, implementors and references. **The recommended entry point.** |
| `get_node_info` | Source snippet + signature + docstring + location for a node |
| `get_file_structure` | Symbol outline of a file |
| `get_callees` | What a function calls (outgoing, up to `max_depth`) |
| `get_callers` | Who calls a function — primary impact-analysis tool |
| `get_neighbors` | Nodes within N hops in any direction |
| `find_references` | Non-call usages (type annotations, assignments) |
| `get_implementors` | Subclasses / interface implementors / embedders — the tool for *"what implements/extends/subclasses X?"* (reverse `inherits_from` walk) |
| `get_cross_language_calls` | Connections across service boundaries (HTTP/gRPC/queues) |
| `search_code` | Regex/text over file **content** — the grep replacement (string literals, logs, comments, config) |
| `search_semantic` | Search by **meaning**; each hit is a graph node, so it pivots into `get_callers`/`get_callees` |
| `find_related` | Code semantically similar to a given symbol |
| `list_clusters` | Labeled semantic zones of the codebase (auth, serialization, …) |
| `get_cluster` | The cluster a symbol belongs to and its sibling members |

The relation tools (`explore`, `get_node_info`, `get_callees`, `get_callers`, `get_neighbors`,
`find_references`, `get_implementors`, `get_cross_language_calls`) accept a node ID **or** a bare
symbol name — the name is resolved internally, so no prior `search_symbols` call is required.
Wherever a `path_glob` is accepted, a bare directory expands to `dir/**`, and `max_depth` / `depth`
/ `limit` are clamped (not rejected) when they exceed their caps.

The last four embed graph nodes with a bundled `model2vec` model (no extra to install). If
the embedding model can't be fetched (offline first run), they return `available=false` with a
reason and the agent falls back to `search_symbols` / `search_code`.

## Searching effectively

`search_symbols` is FTS/BM25 over symbol names **and** qualified names — short, common tokens
rank badly because dozens of files, migrations and imports share them.

- **Don't search a bare common noun** (`Location`, `User`, `Config`). The defining
  class/function may stay buried under file and import nodes even at a high `limit`.
- **Use the most distinctive identifier** you have: a compound name (`LocationRepository`) or
  qualify with the module path (`models.Location`). The extra tokens discriminate.
- **Know the file? Skip search.** `get_file_structure(path)` deterministically lists every
  node with its ID — filter by `kind` (`class`/`function`/`method`).

## Impact-analysis workflow

When asked *"what breaks if I change X?"*:

1. `get_callers("X", max_depth=5)` → direct and transitive callers. Relation tools accept a
   symbol **name** directly, so the `search_symbols("X")` lookup is optional — pass the name and
   it's resolved internally. (`explore("X")` returns callers, callees, implementors and
   references in a single call if you want the whole picture at once.)
2. `find_references("X")` → non-call usages (type annotations, assignments).
3. `get_implementors("X")` → subclasses / implementors that override behaviour.
4. `get_cross_language_calls("X")` → cross-service consumers.
5. Summarise the affected symbols — use `get_node_info` only for the ones that need
   elaboration, instead of reading every caller file.

## Respect `resolver_status`

- `ok` — full semantic graph, edges are trustworthy.
- `degraded` — calls/types not fully resolved (usually a missing language toolchain); treat
  edges as approximate and suggest `graphlens-mcp reindex` or installing the toolchain.
- `skeleton` — structure only; relationship edges aren't available yet for these nodes.

Pair this with the `indexing` flag on every response: when `indexing=true` a background reindex
is still running, so missing callers/edges may simply not be indexed yet — don't conclude a
symbol is unused until indexing has settled.
