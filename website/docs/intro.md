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
`gopls`, the TypeScript compiler, Python analysis) — the LSP-grade tooling the industry
already trusts — for a *stable, real* picture of the project, not a bespoke approximation.

That stable foundation is the [`graphlens`](https://github.com/Neko1313/graphlens) engine
(parsing, stable node identity, resolvers). **`graphlens-mcp` is a smart, agent-facing layer
over it**: it persists the graph (so the whole thing isn't held in memory), adds a semantic
embedding layer for meaning-based search, refreshes incrementally when you re-run the `index`
tool, and exposes it to agents as navigation tools plus slash-prompt workflows. See
[Architecture](./architecture.md) for how the layer is built.

## Supported languages

Language support ships in the engine extra `graphlens[go,python,rust,typescript,php]`:

| Language | Full resolution needs | Out-of-box |
|---|---|---|
| Python | — | Full semantics immediately |
| TypeScript | Node.js | `degraded` without Node; full with Node installed |
| Go | `gopls` on `PATH` | `degraded` without it |
| Rust | `rust-analyzer` on `PATH` | `degraded` without it |
| PHP | — | Parsed structure |

The `index` tool's result reports the actual `resolver_status` per language. When a language
server is missing, that language is indexed in **degraded** mode (parsed structure, calls/types
not fully resolved) — it never blocks indexing; install the toolchain and re-run `index` for
full coverage.

Ready to try it? Head to [Getting started](./getting-started.md).
