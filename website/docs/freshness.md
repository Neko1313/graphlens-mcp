---
id: freshness
title: Freshness model
sidebar_position: 6
---

# Freshness model

Freshness is **explicit and incremental**: you (or the agent) re-run the `index` tool on the
repository, and graphlens updates only what changed. There is no filesystem watcher, no
background reconcile, and no on-access re-index — the navigation tools (`search`, `info`,
`relations`) only ever read the stored graph.

## Whole-repo analyze, diff-driven write

Every `index` run re-analyzes the **entire repository**. graphlens can't resolve cross-file
edges from a subset of files, so re-analyzing the whole project is what lets every call/type
edge re-link correctly — the blast radius comes for free. What's *written* is only the delta:

- **Unchanged symbols** (same `content_hash`) skip both the graph write and the
  dominant-cost re-embedding.
- **Added / changed symbols** are upserted and re-embedded.
- **Vanished symbols** and their vectors are deleted.
- **Edges are replaced wholesale** — they carry no embedding cost, so the whole edge set is
  cleared and rewritten each run, keeping cross-file and cross-language links exact.

Analysis and embedding run **before any destructive write**, so a failed or interrupted index
leaves the previous graph intact rather than half-updated.

## History is recorded, not overwritten

Each `index` run also appends the repository's HEAD commit to an append-only **temporal
version log**. That is what powers the `ref` / `at` time-travel parameters on `info` and
`relations`: you can read the graph as it was at an earlier indexed commit. A past revision
returns a symbol's recorded shape (name, kind, file, metadata) and its neighbours of the day,
but **not** its source text — only the latest graph stores bodies, and `search` is always
current. See [Navigation](./navigation.md).

## Practical notes

- To refresh after edits, re-run `index` on the same repo — there is no separate `reindex`
  tool; re-running `index` *is* the refresh.
- A no-op fast path skips work entirely when the repository's HEAD already matches what the
  graph reflects (used mainly by the server-side `repo_url` clone path).
- Missing language servers (`gopls`, `rust-analyzer`) lower coverage, not freshness: those
  languages index in `degraded` mode, reported per language in the `index` result's
  `resolver_status`.
