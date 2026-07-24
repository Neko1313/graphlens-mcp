---
id: architecture
title: Architecture
sidebar_position: 7
---

# Architecture

`graphlens-mcp` is a stateful runtime over the stateless
[`graphlens`](https://github.com/Neko1313/graphlens) engine. The engine provides the
mechanisms (parsing, stable node identity, resolvers, cross-language linking); this product
owns all storage, freshness, and the agent-facing surface. Nothing stateful leaks into the
engine.

## Deployment modes

The same binary runs two ways, chosen entirely by environment:

- **Local (zero-infra).** No database DSN set: an embedded **Kuzu** graph + **Milvus Lite**
  vector store on disk, a single trusted user, no auth. `graphlens-mcp` serves stdio and the
  agent spawns it. Confirmations use MCP elicitation.
- **Server (multi-tenant).** Set `DB__GRAPH` (a `neo4j://` DSN) and `DB__VECTOR`: the same
  ports are backed by host **Neo4j** + **Milvus**, selected at runtime — no code change. The
  Neo4j backend applies a thin Cypher dialect shim so callers write one query. Run
  `graphlens-mcp --http` behind an ingress.

## Components

A feature-sliced layout under `src/` — one top-level package per concern:

```
src/
  app/                 # MCPServer wiring (server.py), argparse CLI (cli.py),
                       #   agent instructions, lifespan (store setup/teardown)
  features/
    search/            # each slice = an MCP tool: an adapter (tool + resources)
    info/              #   over a service, sharing the same store ports
    relations/
    projects/          # index / list_projects / remove_project + git clone
    navigation/        # the six slash-prompt workflows (no new storage)
  entities/            # request/result Pydantic models, project & commit types
  shared/common/
    db/graph|vector|registry   # store ports + Kuzu/Neo4j/Milvus backends
    indexing/          # pipeline, persist, temporal version log, embeddings
    setting/           # env config, platformdirs paths
```

## Tool & prompt surface

Six MCP tools, all flat-parameter:

- **Navigation** — `search`, `info`, `relations` (read the stored graph).
- **Projects** — `index`, `list_projects`, `remove_project`.

Plus six **navigation prompts** (`impact`, `find`, `trace`, `map`, `xflow`, `deadcode`) that
encode fixed methods over the navigation tools. See [Agent tools](./agent-tools.md).

## Indexing pipeline

`index_project_graph` (in `shared/common/indexing/pipeline.py`) runs, in order:

1. **Analyze** — the engine parses the whole repository and resolves cross-file edges. A
   project is a whole git repo; its identity is a hash of the git remote.
2. **Diff** — new nodes are compared to the stored ones by `content_hash`. Unchanged nodes
   skip both the graph write and the dominant-cost re-embedding.
3. **Embed** — changed nodes are embedded (before any destructive write, so a failure leaves
   the prior index intact).
4. **Persist** — vectors are upserted to Milvus; graph nodes and edges are written to Kuzu.
   Vanished nodes and their vectors are deleted; edges are replaced wholesale.
5. **Version** — the HEAD commit is appended to a temporal version log (below).

On Kuzu, node and edge writes use bulk **`COPY`** rather than per-row `UNWIND … MATCH`, which
keeps a whole-repo index O(n) instead of O(n²) — a superset-scale project that once took tens
of minutes now indexes in minutes. On Neo4j the `UNWIND` path is used (its index handles it).

## Storage

- **Code graph** — Kuzu (local) or Neo4j (server): a `CodeNode` table and a `Rel` edge table,
  scoped by `project_id`, plus a `GraphHead` record tracking the indexed commit.
- **Embeddings** — Milvus / Milvus Lite: a single `CODE_COLLECTION` collection, isolated per
  project by a `project_id` filter (not a separate collection per project).
- **Temporal log** — `NodeVersion` / `RelVersion` / `RefState` / `RefCommit` tables record an
  append-only history per ref, powering `ref` / `at` time-travel on `info` and `relations`. A
  past revision returns recorded metadata and neighbours, but not source bodies; `search` is
  always current.
- **Registry** — an embedded Kuzu `registry.db` mapping project ids to name / path / git url,
  independent of which backend serves the graph.

Locally these live under platformdirs `user_data_dir` (`$XDG_DATA_HOME/graphlens-mcp/` on
Linux) as `graph.db`, `vector.db`, `registry.db`.

## Key invariants

1. **Stable node ids** come from the engine — never positional — so a cross-file edge
   reconnects after its target file is re-indexed.
2. **Content-hash diff.** Only nodes whose `content_hash` changed are re-written and
   re-embedded; the stored hash is the last thing written, so an interrupted index self-heals
   on the next run.
3. **Whole-repo analyze, wholesale edge replace.** Every index re-analyzes the whole project
   and clears+rewrites the project's edges, so cross-file and cross-language links stay exact
   — there is no partial, connected-set re-link to drift out of date.
4. **Analyze + embed before destructive write.** A failure leaves the previous graph intact.
5. **Transactional multi-statement writes** on the graph store; the temporal append runs in
   one transaction, since orphaned rows at a seq would corrupt time-travel reads.
6. **Uneven per-language coverage is reported, not hidden.** `relations` returns `not_indexed`
   for groups a language analyzer never produces (Rust: implementors; Go: references) and
   `callees_unresolved` for calls it couldn't bind.

## Authorization (server mode)

Pushed outward, on two axes:

- **Write / index** is gated by the git token: no valid `ci_token` → no clone/pull → nothing
  written for that repo. That token *is* the write ACL.
- **Read / query** is regulated at the transport by an external OIDC gateway (e.g. Casdoor) in
  front of the server — a deployment concern, deliberately out of this project's scope. The
  server builds no session state.

## Cache, not system of record

The graph and embeddings are a **regenerable cache** of the code on disk. There are no
migrations: to change shape, delete the store and re-run `index` — it rebuilds in seconds to
minutes depending on repo size.

## Tool boundary

Each tool returns typed results from `entities/result`: `search` returns text lines (one per
hit in `concise` mode) or resource links; `info` returns a discriminated union
(symbol source / file outline / file source / ambiguity candidates / not-found); `relations`
returns the four navigation groups with `*_total`, `callees_unresolved`, and `not_indexed`.
Ambiguous names return candidates to narrow with `file`; unknown targets return an explicit
not-found rather than an empty success.
