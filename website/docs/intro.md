---
id: intro
title: Introduction
slug: /
sidebar_position: 1
---

# graphlens-mcp

A free, MIT-licensed [MCP](https://modelcontextprotocol.io) server that gives coding agents
(Claude Code, Cursor, and compatible clients) a **semantic code graph** of your project —
symbols, cross-file calls, references, imports and cross-language boundaries.

Instead of reading files top-to-bottom or grepping for names, the agent **navigates the
structure**: *who calls this function*, *what does it depend on*, *what breaks if I change
its signature*. It is a thin runtime layer over the
[`graphlens`](https://github.com/Neko1313/graphlens) analysis engine: `graphlens` provides
the mechanisms (parsing, stable node identity, resolvers); `graphlens-mcp` owns the storage,
freshness and the agent-facing surface.

## Why

A `filesystem`/grep MCP makes the agent read whole files and match text — slow, noisy, and
blind to which of three modules actually calls `OrderService.create`. Bare tree-sitter gives
single-file syntax but cannot resolve links *between* files. `graphlens-mcp` answers the
cross-file questions — call graphs and impact analysis — and keeps the graph fresh as you
edit through a filesystem watcher, then teaches the agent to use it via a bundled navigation
skill.

## Supported languages

| Language | Engine | Out-of-box |
|---|---|---|
| Python | `ty` (bundled) | Full semantics immediately |
| TypeScript | Node bridge | `degraded` without Node; full semantics with Node installed |
| Go | Go toolchain | `degraded` without toolchain |
| Rust | SCIP / rust-analyzer | `degraded` without toolchain |
| PHP | PHP parser | `degraded` without toolchain |

`graphlens-mcp status` reports the actual resolver status per language. When a toolchain is
missing, that language is reported as **degraded** (parsed structure, calls/types not fully
resolved) with an install hint — it never blocks `init`.

Ready to try it? Head to [Getting started](./getting-started.md).
