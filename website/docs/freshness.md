---
id: freshness
title: Freshness model
sidebar_position: 6
---

# Freshness model

A single mechanism keeps the graph current: a **filesystem watcher** (`serve` starts it by
default; disable with `--no-watch`).

## Connected-set re-index

When a file changes on disk the server re-indexes the **connected set** — the changed file
plus the files that import it and the files it imports — with one full analyze. Analyzing the
set together lets the resolver re-link calls *across* those files, so cross-file edges are
rebuilt correctly rather than left partial. Deleting a file prunes its symbols and refreshes
its importers.

There is **no polling** and **no structure-only "skeleton" phase**: every (re)index produces
the full graph the resolver can give, so a file is `ok` or (toolchain missing) `degraded`.

## On-access backstop

A tool that touches a file the watcher hasn't processed yet triggers the same connected
re-index on access — so the freshness guarantee holds even with `--no-watch` or before the
first watch cycle.

## Startup reconcile

An event-based watcher cannot see files created, deleted or edited *while the server was
down*. So `serve` runs a one-shot **reconcile** at startup: it scans the project, indexes new
files, prunes vanished ones, and refreshes any that changed — then hands off to the watcher.

## Limitations

- **Connected-set, not whole-project, re-link:** a change re-analyzes the changed file with
  its direct importers and imports, so cross-file edges within that set are correct, but a
  rename that ripples through several indirection layers may need a full `reindex` for an
  exact graph.
- **Cross-language edges on incremental edits:** synthesized `COMMUNICATES_WITH` edges are
  rebuilt on a full `reindex`; the boundary-based query still resolves connections in between.
