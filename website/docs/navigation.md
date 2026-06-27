---
id: navigation
title: Navigating the graph
sidebar_position: 5
---

# Navigating the graph

The bundled **navigation skill** teaches the agent to answer structural questions with graph
tools instead of reading files or grepping. The core strategy:

1. **Locate the symbol** — by distinctive name via `search_symbols`, or deterministically via
   `get_file_structure(path)` when you know the file. Either way you get a node ID.
2. **Narrow with graph traversal** — once you have a node ID, use `get_callers` / `get_callees`
   / `find_references` rather than reading source files to understand relationships.
3. **Read source only for implementation detail** — `get_node_info` returns the source
   snippet for a node; read whole files only when you genuinely need surrounding context.

## Question → tool

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

## Do not

- Read entire source files to find callers — use `get_callers`.
- Search a bare common noun and trust the result — it gets drowned by file/import nodes; use
  a distinctive/qualified name, `get_file_structure`, or grep as a fallback.
- Assume an edge list is complete when `resolver_status != ok`.
