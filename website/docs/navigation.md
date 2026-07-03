---
id: navigation
title: Navigating the graph
sidebar_position: 5
---

# Navigating the graph

The bundled **navigation skill** teaches the agent to answer structural questions with graph
tools instead of reading files or grepping. The core strategy:

1. **Know the name? Skip straight to `relations`/`info`.** Both accept a bare symbol name or a
   node ID — resolved internally, no prior `search` call needed.
2. **Don't know the name?** `search(query)` unifies name, content, and semantic matching over
   the same node graph, and each hit is a node you can pass straight into `relations`/`info`.
3. **Narrow with graph traversal, not source reading.** `relations("X")` returns callers,
   callees, implementors, and references in one call — use it instead of reading files to infer
   relationships.
4. **Read source only for implementation detail.** `info(target)` returns a symbol's source or a
   file's outline/content; read whole files only when you genuinely need surrounding context
   `info` didn't already give you (`mode="source"` returns the full line-numbered body).

## Question → tool

| Question | Tool |
|---|---|
| What is `X`, who uses it, what implements it? | `relations("X")` |
| Where is `create_order` defined? | `search("create_order")` (distinctive name) |
| Where is `Location` defined? (common noun) | `info(path)` or `search("models.Location")` |
| What does `create_order` call? | `relations("create_order")` → `callees` |
| Who calls `create_order`? | `relations("create_order")` → `callers` |
| What references `OrderService`? | `relations("OrderService")` → `references` |
| What subclasses / implements `X`? | `relations("X")` → `implementors` |
| What symbols are in `order_service.py`? | `info("order_service.py")` (outline) |
| Show source + signature of a symbol | `info(id)` |
| Read a file's actual content (to edit it) | `info(path, mode="source")` |
| List every file that calls/imports `X` | `search("X", exhaustive=True)` |
| Find text in bodies/strings/config (the grep case) | `search("literal text")` |
| Find code by meaning when you don't know the name | `search("retry with backoff")` |

## Do not

- Read entire source files to find callers — use `relations`.
- Search a bare common noun and trust the result — it gets drowned by file/import nodes; use
  a distinctive/qualified name, or `info(path)` when you know the file.
- Shell out to `grep`/`rg`/`find` — `search` is the content-search replacement and keeps you on
  the graph (each hit maps back to nodes).
- Assume a list is complete when `resolver_status != "ok"`.
- Conclude a symbol is unused when the response has `indexing: true` — that flag means a
  background reindex is still running, so edges may be incomplete. Re-check once indexing
  settles.
- Repeat the exact same `search`/`relations`/`info` call expecting a different result — the
  third identical call is blocked outright; change the query, tool, or answer with what you have.
