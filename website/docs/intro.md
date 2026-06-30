---
id: intro
title: Introduction
slug: /
sidebar_position: 1
---

# graphlens-mcp

A free, MIT-licensed [MCP](https://modelcontextprotocol.io) server that gives coding agents
(Claude Code, Cursor, and compatible clients) a **semantic code graph** of your project —
symbols, cross-file calls, references, imports and cross-language boundaries. Instead of
grepping and reading files one at a time, the agent **navigates the structure**: *who calls
this function*, *what does it depend on*, *what breaks if I change its signature*.

## Why

The motivation is the same as every other code-context tool: **stop the agent from grepping.**
The *approach* is what sets `graphlens` apart. Most tools build their **own** ad-hoc model of
your code — every tool maps the codebase a little differently and nothing is authoritative.
`graphlens` instead builds on the **language's own real analysis engines** (`rust-analyzer`,
`gopls`, the TypeScript compiler, the bundled `ty` type engine) — the LSP-grade tooling the
industry already trusts — for a *stable, real* picture of the project, not a bespoke
approximation.

That stable foundation is the [`graphlens`](https://github.com/Neko1313/graphlens) engine
(parsing, stable node identity, resolvers). **`graphlens-mcp` is a smart, agent-facing layer
over it**: it persists the graph (so the whole thing isn't held in memory), adds a semantic +
clustering layer, keeps it fresh as you edit through a filesystem watcher, and exposes it to
agents as navigation tools plus a bundled skill. From that example it is growing into a
self-sufficient system — see [Architecture](./architecture.md) for how the layer is built.

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
