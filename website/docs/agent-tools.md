---
id: agent-tools
title: Agent tools
sidebar_position: 4
---

# Agent tools

Each response carries a graph-quality status (`ok` | `degraded`, aggregated across every
returned node's file) so the agent never mistakes a partial answer for a complete one. List
responses also carry a `truncated` flag and are capped at `MAX_RESULTS` (200).

| Tool | Purpose |
|---|---|
| `search_symbols` | Full-text search over symbol names — **start here** |
| `get_node_info` | Source snippet + signature + docstring + location for a node |
| `get_file_structure` | Symbol outline of a file |
| `get_callees` | What a function calls (outgoing, up to `max_depth`) |
| `get_callers` | Who calls a function — primary impact-analysis tool |
| `get_neighbors` | Nodes within N hops in any direction |
| `find_references` | Non-call usages (type annotations, assignments) |
| `get_cross_language_calls` | Connections across service boundaries (HTTP/gRPC/queues) |

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

1. `search_symbols("X")` → get a node ID.
2. `get_callers(id, max_depth=5)` → direct and transitive callers.
3. `find_references(id)` → non-call usages (type annotations, assignments).
4. `get_cross_language_calls(id)` → cross-service consumers.
5. Summarise the affected symbols — use `get_node_info` only for the ones that need
   elaboration, instead of reading every caller file.

## Respect `resolver_status`

- `ok` — full semantic graph, edges are trustworthy.
- `degraded` — calls/types not fully resolved (usually a missing language toolchain); treat
  edges as approximate and suggest `graphlens-mcp reindex` or installing the toolchain.
