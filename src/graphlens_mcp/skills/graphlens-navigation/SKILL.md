---
name: graphlens-navigation
description: >
  Navigate the code graph using graphlens MCP tools instead of reading files or grepping.
  Use when asked: "what calls X", "what breaks if I change X", "who uses this function",
  "what does this function depend on", "impact analysis", "find callers", "find references",
  "what's in this file", "how do services communicate", "cross-language calls".
  Start with search_symbols (use a distinctive name) or get_file_structure, then narrow with get_callers/get_callees/find_references.
allowed-tools: Bash
---

# graphlens Navigation

You have a semantic code graph via graphlens MCP tools. Use them instead of reading files
or grepping when answering questions about code structure, call relationships, or impact analysis.

## Core strategy

1. **Locate the symbol** — by distinctive name via `search_symbols`, or — if you
   know the file — deterministically via `get_file_structure` (see below). This is
   how you get a node ID.
2. **Narrow with graph traversal** — once you have a node ID, use graph tools
   rather than reading source files to understand relationships.
3. **Read source only for implementation details** — use `get_node_info` to get
   the source snippet; read full files only when you genuinely need surrounding context.

## Searching effectively

`search_symbols` is FTS/BM25 over symbol names AND qualified names — short, common
tokens rank badly because dozens of files, migrations, and imports share them.

- **Don't search a bare common noun** (`Location`, `User`, `Device`, `Config`). The
  defining class/function may not appear even at `limit=50` — it stays buried under
  file and migration nodes.
- **Use the most distinctive identifier you have**: a compound name
  (`LocationRepository`, `get_location_dc`) or qualify with the module path
  (`models.Location`, `repository.LocationRepository`). The extra tokens discriminate.
- **Know the file? Skip search.** `get_file_structure(path)` deterministically lists
  every node with its ID — filter by `kind` (`class`/`function`/`method`). This is the
  reliable way to get the *defining* node with source, not a ranking lottery.
- **Watch for stub nodes.** A name search may return an `external_symbol` (with
  `file_path: null`) or `import` nodes instead of the defining `class`/`function`.
  For source + bases, get the real node via `get_file_structure` or follow to the def.
- **Grep is a fine fallback** for discovery by a too-common name; once you have a file
  or a node ID, pivot back into the graph.

## Tool guide

| Question | Tool |
|---|---|
| Where is `create_order` defined? | `search_symbols("create_order")` (distinctive name) |
| Where is `Location` defined? (common noun) | `get_file_structure(path)` or `search_symbols("models.Location")` |
| What does `create_order` call? | `get_callees(id, max_depth=2)` |
| Who calls `create_order`? | `get_callers(id, max_depth=3)` |
| What references `OrderService`? | `find_references(id)` |
| What symbols are in `order_service.py`? | `get_file_structure("order_service.py")` |
| Show source + signature of a symbol | `get_node_info(id)` |
| How does this Python service talk to the TS client? | `get_cross_language_calls(id)` |
| What's around this class in the graph? | `get_neighbors(id, depth=2)` |

## Impact analysis workflow

When asked "what breaks if I change X?":
1. `search_symbols("X")` → get node ID
2. `get_callers(id, max_depth=5)` → direct and transitive callers
3. `find_references(id)` → non-call usages (type annotations, assignments)
4. `get_cross_language_calls(id)` → cross-service consumers
5. Summarise affected symbols — do NOT read every caller file; use
   `get_node_info` only for ones that need elaboration.

## Respect resolver_status

Each tool response includes `resolver_status` (aggregated across every returned node's file):
- `ok` — full semantic graph, edges are trustworthy
- `degraded` — calls/types not fully resolved (usually a missing language toolchain); treat
  edges as approximate

When status is `degraded`, say so explicitly and suggest `graphlens-mcp reindex` or
installing the missing toolchain.

## Do NOT

- Read entire source files to find callers — use `get_callers`
- Search a bare common noun and trust the result — it gets drowned by file/migration
  nodes; use a distinctive/qualified name, `get_file_structure`, or grep as fallback
- Assume an edge list is complete when `resolver_status != ok`
