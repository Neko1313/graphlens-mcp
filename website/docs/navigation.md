---
id: navigation
title: Navigating the graph
sidebar_position: 5
---

# Navigating the graph

The bundled **navigation skill** teaches the agent to answer structural questions with graph
tools instead of reading files or grepping. The core strategy:

1. **Start with `explore`** — `explore("X")` is the recommended one-call entry point. It takes a
   bare symbol name and returns the symbol's source + signature plus its direct callers,
   callees, implementors and references in a single call — no prior `search_symbols` needed.
2. **Locate the symbol (optional)** — relation tools resolve a bare symbol name internally, so
   you can call them directly. When a name is ambiguous, narrow first by distinctive name via
   `search_symbols`, or deterministically via `get_file_structure(path)` when you know the file.
   Both also give you a node ID.
3. **Narrow with graph traversal** — pass a symbol **name** (or a node ID) to `get_callers` /
   `get_callees` / `find_references` / `get_implementors` rather than reading source files to
   understand relationships.
4. **Read source only for implementation detail** — `get_node_info` returns the source
   snippet for a node; read whole files only when you genuinely need surrounding context.

The relation tools — `get_callers`, `get_callees`, `find_references`, `get_implementors`,
`get_node_info`, `get_neighbors` — accept either a node ID **or** a bare symbol name (resolved
internally), so the locate-first step is optional.

## Question → tool

| Question | Tool |
|---|---|
| What is `X` and who uses it / implements it? | `explore("X")` (one call) |
| Where is `create_order` defined? | `search_symbols("create_order")` (distinctive name) |
| Where is `Location` defined? (common noun) | `get_file_structure(path)` or `search_symbols("models.Location")` |
| What does `create_order` call? | `get_callees(id, max_depth=2)` |
| Who calls `create_order`? | `get_callers(id, max_depth=3)` |
| What references `OrderService`? | `find_references(id)` |
| What subclasses / implements `X`? | `get_implementors("X")` |
| What symbols are in `order_service.py`? | `get_file_structure("order_service.py")` |
| Show source + signature of a symbol | `get_node_info(id)` |
| How does this Python service talk to the TS client? | `get_cross_language_calls(id)` |
| What's around this class in the graph? | `get_neighbors(id, depth=2)` |
| Find text in bodies/strings/config (the grep case) | `search_code("regex")` |
| Find code by meaning when you don't know the name | `search_semantic("retry with backoff")` |
| Code similar to this symbol? | `find_related(id)` |
| What are the major zones of this codebase? | `list_clusters()` |
| What's inside a specific zone? | `get_cluster(id)` |

## Do not

- Read entire source files to find callers — use `get_callers`.
- Search a bare common noun and trust the result — it gets drowned by file/import nodes; use
  a distinctive/qualified name, `get_file_structure`, or `search_semantic` as a fallback.
- Shell out to `grep` — `search_code` is the content-search replacement and keeps you on the
  graph (each hit maps back to nodes).
- Assume an edge list is complete when `resolver_status != ok`.
- Conclude a symbol is unused when the response has `indexing: true` — that flag means a
  background reindex is still running, so edges may be incomplete. Re-check once indexing
  settles.
