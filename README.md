# graphlens-mcp

<!-- mcp-name: io.github.Neko1313/graphlens-mcp -->

[![CI](https://github.com/Neko1313/graphlens-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/Neko1313/graphlens-mcp/actions/workflows/ci.yml)
[![Docs](https://img.shields.io/badge/docs-github%20pages-blue)](https://neko1313.github.io/graphlens-mcp/)
[![Python](https://img.shields.io/badge/python-%E2%89%A53.13-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

A free, MIT-licensed [MCP](https://modelcontextprotocol.io) server that gives coding
agents (Claude Code, Cursor, and compatible clients) a **semantic code graph** of your
project — symbols, cross-file calls, references, imports and cross-language boundaries.

Instead of reading files top-to-bottom or grepping for names, the agent **navigates the
structure**: *who calls this function*, *what does it depend on*, *what breaks if I change
its signature*. It is a thin runtime layer over the
[`graphlens`](https://github.com/Neko1313/graphlens) analysis engine: `graphlens` provides
the mechanisms (parsing, stable node identity, resolvers); `graphlens-mcp` owns the storage,
freshness and the agent-facing surface.

📖 **Documentation:** <https://neko1313.github.io/graphlens-mcp/>

> Status: early. The core navigation works; see [Known limitations](#known-limitations).

## Why

Coding agents discover structure the slow way — grep, glob, read one file at a time —
rebuilding call paths by hand before the real work even starts. The motivation is the same
as every other code-context tool: **stop the agent from grepping.** The *approach* is what
sets `graphlens` apart.

Most tools answer this by building their **own** model of your code — an ad-hoc graph
stitched from heuristics, where every tool maps the codebase a little differently and
nothing is authoritative. `graphlens` takes the opposite bet: it builds on the **language's
own real analysis engines** — `rust-analyzer`, `gopls`, the TypeScript compiler, the bundled
`ty` type engine — the LSP-grade tooling the industry already trusts. That yields a *stable,
real* picture of the project (who actually calls what, across files and languages), not a
bespoke approximation. And a stable foundation is something you can build on: attach context
to the parts of a change that matter, auto-extract semantic clusters, answer impact
questions reliably.

That foundation is the [`graphlens`](https://github.com/Neko1313/graphlens) engine —
parsing, stable node identity, and the resolvers. **`graphlens-mcp` is a smart, agent-facing
layer over it**, and — honestly — a worked example of how to *use* the engine: it persists
the graph (so the whole thing isn't held in memory), adds a semantic + clustering layer on
top, keeps it fresh as you edit, and exposes it to agents as navigation tools plus a bundled
skill. From that example it is growing into a **self-sufficient system** — one that, measured
against the market's giants, aims for **stable, reproducible** results: better in some places,
worse in others, but honest about which (see [How it compares](#how-it-compares)).

## How it compares

`graphlens-mcp` ships with a reproducible **A/B benchmark** ([`benchmarks/`](benchmarks/README.md))
that drives the same agent against four interchangeable code-context MCP servers —
`graphlens`, `semble` (semantic search), `codegraph` (graph index), and a `filesystem`
(grep + read) baseline — plus a **no-tools control** that measures how much each server adds
over the model's own memory. It runs across real **Go / Rust / Python / TypeScript** codebases
and grades answers **deterministically against oracle gold** (no LLM judge),
stratified into SIMPLE lookups vs HARD impact / cross-file questions, and reports accuracy
**alongside** token / tool-call / dollar cost — because a cheaper arm at equal accuracy wins.

<!-- BENCHMARK-RESULTS:START -->
> 📊 **Results: _coming soon._** A headline table (accuracy + tokens + cost per language,
> vs the `filesystem` baseline and the `none` control) lands here once the current run
> completes. Until then, see [`benchmarks/README.md`](benchmarks/README.md) for the full
> methodology and to reproduce it yourself.
<!-- BENCHMARK-RESULTS:END -->

## Install

Requires **Python ≥ 3.13** (a constraint inherited from `graphlens`).

```bash
uv tool install graphlens-mcp      # or: pipx install graphlens-mcp
```

Python language analysis works out of the box (the `ty` type engine ships as a
dependency). Other languages parse immediately and unlock full cross-file semantics once
their toolchain is present (Node for TypeScript, the Go toolchain, etc.); without it that
language is reported as `degraded` rather than blocking `init`.

## Quickstart (two commands)

```bash
uv tool install graphlens-mcp        # 1. install
cd your-project && graphlens-mcp init  # 2. index + configure your agent
```

`init` detects the project's languages, indexes the code into a local graph, writes the
MCP server entry into your agent's config and installs the navigation skill. You do **not**
run `serve` yourself — your agent launches it from the config. Restart the agent and ask
it something like *"what breaks if I change the signature of `create_order`?"*.

## Commands

| Command | What it does |
|---|---|
| `graphlens-mcp init` | Detect languages → toolchain doctor → full index → configure agents → install skill |
| `graphlens-mcp serve` | Start the MCP server over stdio. **Launched by the agent**, not by you |
| `graphlens-mcp status` | Show detected languages, toolchain status, and graph size/freshness |
| `graphlens-mcp reindex` | Force a full rebuild (e.g. after installing a new toolchain) |
| `graphlens-mcp remove` | Deregister from agents and (with `--purge-db`) delete the local graph |

Useful `init` flags: `--root <dir>`, `--agent claude_code --agent cursor` (repeatable),
`--no-agent`, `--no-skills`, `--db <path>`.

The graph lives at `<project>/.graphlens/graph.db` (SQLite). It is a regenerable cache —
safe to delete; `reindex` rebuilds it. Add `.graphlens/` to your VCS ignore (the bundled
`init` flow assumes it is not committed).

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

## Agent tools

Each response carries a graph-quality status (`ok` | `degraded`) so the agent never mistakes
a partial answer for a complete one, plus an `indexing` flag (`true` when a background reindex
is running, so edges may be temporarily incomplete).

| Tool | Purpose |
|---|---|
| `explore` | One call: a symbol's source + signature plus its direct callers, callees, implementors and references — **the first call for "what is X / who uses it / what implements it"** |
| `search_symbols` | Full-text search over symbol names — **start here** |
| `get_node_info` | Source snippet + signature + location for a node |
| `get_file_structure` | Symbol outline of a file |
| `get_callees` | What a function calls (outgoing, up to `max_depth`) |
| `get_callers` | Who calls a function — primary impact-analysis tool |
| `get_neighbors` | Nodes within N hops in any direction |
| `find_references` | Non-call usages (type annotations, assignments) |
| `get_implementors` | Subclasses / interface implementors / embedders of a symbol (reverse `inherits_from` walk) — "what implements / extends / subclasses X?" |
| `get_cross_language_calls` | Connections across service boundaries (HTTP/gRPC/queues) |
| `search_code` | Regex/text over file **content** — the grep replacement (string literals, logs, comments, config) |
| `search_semantic` | Search by **meaning**; each hit carries the graph node ids it overlaps |
| `find_related` | Find code semantically similar to a symbol |
| `list_clusters` | Labeled semantic zones of the codebase (auth, serialization, …) |
| `get_cluster` | The cluster a symbol belongs to and its sibling members |

The relation tools (`get_callers`, `get_callees`, `get_neighbors`, `find_references`,
`get_implementors`, `get_node_info`, `get_cross_language_calls`) accept either a symbol
**name** or a node id directly — you don't need to look up a node id first.

The four semantic tools (`search_semantic`, `find_related`, `list_clusters`,
`get_cluster`) ship in the box — no extra to install. If the embedding model can't be
fetched (e.g. a first run with no network), they return `available=false` with a reason
and the agent falls back to `search_symbols` / `search_code` rather than failing.

## Freshness model

A single mechanism keeps the graph current: a **filesystem watcher** (`serve` starts it by
default; disable with `--no-watch`). When a file changes on disk the server re-indexes the
**connected set** — the changed file plus the files that import it and the files it imports —
with one full analyze, so cross-file edges are rebuilt correctly rather than left partial.
Deleting a file prunes its symbols and refreshes its importers. There is no polling and no
structure-only "skeleton" phase: every (re)index produces the full graph the resolver can
give. As a backstop, a tool that touches a file the watcher hasn't processed yet triggers the
same connected re-index on access.

Files created, deleted or edited *while the server was down* are invisible to an event-based
watcher, so `serve` runs a one-shot **reconcile** at startup: it scans the project, indexes
new files, prunes vanished ones, and refreshes any that changed — then hands off to the
watcher.

## Known limitations

- **Connected-set re-link, deep ripples:** the watcher re-links the *connected set* of a
  change (the changed file plus its direct importers and imports), not the entire project. A
  rename that ripples through many indirection layers may need a full `reindex` for an exact
  graph. Creating a file that an *unchanged* file already imports is handled — a second
  importer pass re-links that importer once the new file is indexed.
- **Cross-language edges on incremental edits:** synthesized `COMMUNICATES_WITH` edges are
  re-synthesized for every boundary a re-indexed file touches, so a new or moved
  exposer/consumer is linked without a full `reindex`. A change that leaves a boundary
  entirely (a file that stops exposing an endpoint others still consume) may still need a
  full `reindex` for an exact cross-language view; the boundary-based query resolves
  connections regardless.

## Uninstall

`graphlens-mcp remove` deregisters the server from your agents; add `--purge-db` to also
delete the local `.graphlens/` cache.

## Development

```bash
uv sync --all-groups   # install lint + test tooling
task check             # ruff + format-check + ty + bandit + pytest (the CI gate)
task docs:serve        # preview the docs site locally (needs Node + pnpm)
```

See the [Architecture](https://neko1313.github.io/graphlens-mcp/architecture) and
[Semantic search](https://neko1313.github.io/graphlens-mcp/design/semantic-search) pages on the
[documentation site](https://neko1313.github.io/graphlens-mcp/) for the design and invariants.

## License

MIT — see [LICENSE](LICENSE).
