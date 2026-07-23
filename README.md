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
the graph (so the whole thing isn't held in memory), adds a semantic search layer on top,
re-indexes incrementally on demand, and exposes it to agents as navigation tools plus
workflow prompts. From that example it is growing into a **self-sufficient system** — one that, measured
against the market's giants, aims for **stable, reproducible** results: better in some places,
worse in others, but honest about which (see [How it compares](#how-it-compares)).

## How it compares

`graphlens-mcp` ships with a reproducible **A/B benchmark** ([`benchmarks/`](benchmarks/README.md))
that drives the same agent against three interchangeable code-context MCP servers —
`graphlens`, `semble` (semantic search), and `codegraph` (graph index) — plus a **no-tools
control** that measures how much each server adds over the model's own memory. It runs across
real **Go / Rust / Python / TypeScript** codebases and grades answers **deterministically
against oracle gold** (no LLM judge), stratified into SIMPLE lookups vs HARD impact /
cross-file questions, and reports accuracy **alongside** token / tool-call / dollar cost —
because a cheaper arm at equal accuracy wins.

<!-- BENCHMARK-RESULTS:START -->
> 📊 **Results** (10 repos · 3 models, strong → genuinely weak · ~2,400 graded runs —
> full breakdown, significance tests and reproduction steps at
> [**docs: Benchmarks**](https://neko1313.github.io/graphlens-mcp/benchmarks)):
>
> | | SIMPLE accuracy | HARD accuracy | HARD tokens (median) | HARD completion |
> |---|---|---|---|---|
> | **graphlens** | 0.980 – 1.000 | 0.899 – 0.921 | **22.4k – 34.1k** | **≥ 0.959 on every model** |
> | codegraph | 0.912 – 0.990 | 0.655 – 0.939 | 23.2k – 70.0k | drops to 0.765 on the weakest model |
> | semble | 0.647 – 0.961 | 0.555 – 0.850 | 21.6k – 74.9k | drops to 0.688 on the weakest model |
> | none (control) | 0.366 – 0.681 | 0.453 – 0.685 | 0.1k – 0.9k | — |
>
> Accuracy alone hides the number that matters to a bill: **tokens paid per task**. graphlens's
> HARD-tier token spend stays flat (22k–34k) whether the driving model is strong or weak;
> codegraph's and semble's balloon past 70k on the weakest model — more than double
> graphlens's ceiling — for a *worse* answer, not a better one. graphlens is the only arm that
> stays clearly ahead of the no-tools control **and** keeps completion above 0.95 at every
> model tier: on the weakest model tested (gpt-oss-20b) it holds 0.900 HARD accuracy at
> roughly **half the token cost** of codegraph. Pairwise Wilcoxon signed-rank tests (matched by
> task) confirm the gap is statistically significant on the weaker models, not an artifact of a
> few outlier tasks — see the notebook for per-model p-values and effect sizes.
<!-- BENCHMARK-RESULTS:END -->

## Install

Requires **Python ≥ 3.13** (a constraint inherited from `graphlens`).

```bash
uv tool install graphlens-mcp      # or: pipx install graphlens-mcp
```

Python language analysis works out of the box (the `ty` type engine ships as a
dependency). Other languages parse immediately and unlock full cross-file semantics once
their toolchain is present (Node for TypeScript, the Go toolchain, etc.); without it that
language is reported as `degraded` rather than blocking indexing.

The hosted graph/vector backends (Neo4j) are an optional extra — only for the multi-tenant
server mode: `uv tool install "graphlens-mcp[neo4j]"`.

## Quickstart

`graphlens-mcp` is an MCP server your **agent launches** — you don't run it yourself. Point
your MCP client at it over stdio:

```jsonc
// e.g. Claude Code / Cursor MCP config
{ "mcpServers": { "graphlens": { "command": "graphlens-mcp" } } }
```

Then, from your agent:

1. **`index`** the project — a git repo **with a remote** (identity is derived from the
   remote, so the same repo maps to one project across clones and CI). Re-running `index`
   refreshes it in place; only the changed symbols are re-embedded.
2. Ask structural questions — *"what breaks if I change the signature of `create_order`?"* —
   and the agent answers from the graph via **`search` / `relations` / `info`** (and the
   `/impact`, `/trace`, `/deadcode`, … prompts) instead of grepping.

## Running the server

| Invocation | Mode |
|---|---|
| `graphlens-mcp` | **stdio** — the local default; your agent spawns it |
| `graphlens-mcp --http --host 0.0.0.0 --port 8000` | **Streamable HTTP** — for a hosted deployment |

Project management is done through MCP **tools**, not subcommands: **`index`** (add or
refresh — a local `directory` or a remote `repo_url` to clone), **`list_projects`**, and
**`remove_project`**.

## Storage

Local mode keeps everything embedded — a **Kuzu** code graph and a **Milvus Lite** vector
index — under the platform data directory (e.g. `~/.local/share/graphlens-mcp/`), alongside a
small project registry. It is a regenerable cache: safe to delete, and re-running `index`
rebuilds it. The server mode swaps these for hosted **Neo4j** + **Milvus** behind the same
code (see [Server deployment](#server-deployment)).

## Supported languages

| Language | Engine | Out-of-box |
|---|---|---|
| Python | `ty` (bundled) | Full semantics immediately |
| TypeScript | Node bridge | `degraded` without Node; full semantics with Node installed |
| Go | Go toolchain | `degraded` without toolchain |
| Rust | SCIP / rust-analyzer | `degraded` without toolchain |
| PHP | PHP parser | `degraded` without toolchain |

Every `index` result reports the resolver status per language. When a toolchain is missing,
that language is reported as **degraded** (parsed structure, calls/types not fully resolved)
rather than blocking indexing.

## Agent tools

Three **query** tools — everything a symbol or file needs comes back as a navigable graph
**node**, not a dead grep line. Each response carries a graph-quality status (`ok` |
`degraded`) so the agent never mistakes a partial answer for a complete one.

| Tool | Purpose |
|---|---|
| `search` | Find code by NAME, CONTENT, or MEANING — **the one way in**. Returns graph nodes with their signature (often enough to answer without a follow-up call). Content is matched literally, not as a regex. Scope with `path_glob` (e.g. `"tests/*"`, `"*.ts"`, `"!tests/*"` to exclude a subtree); set `exhaustive=true` to list every matching file (no cap, no signatures) instead of the ranked top-N |
| `relations` | A symbol's neighbourhood in one call: callers, callees, implementors/subclasses, and non-call references — each with its signature. **The** impact-analysis tool ("what breaks if I change X?", "what implements X?") |
| `info` | Read a specific target: a symbol (node id or name) → source + signature + location; a file path → its symbol outline |

`search` and `relations` accept either a symbol **name** or a node id directly — you don't
need to look up a node id first. Both cap their response size (a large hit set is ranked by
relevance via a small bundled embedding model, not just truncated) and surface true counts
(`callers_total`, `references_total`, …) when a list is capped, so the agent sees "15 shown
of 22" instead of guessing. If the embedding model can't be fetched (e.g. a first run with no
network), search transparently falls back to name/content matching.

Three **management** tools — `index` (add or refresh a project), `list_projects`, and
`remove_project` — round out the surface, plus six **workflow prompts** (`/impact`, `/find`,
`/trace`, `/map`, `/xflow`, `/deadcode`) that drive the query tools through a fixed method so
the agent doesn't have to improvise one.

## Indexing & freshness

Indexing is **on demand** — the agent (or CI) calls `index`; there is no filesystem watcher.
Every run does one **full analyze** of the project, so cross-file edges are resolved
correctly rather than left partial, and then writes only the **delta** against what's stored:
unchanged symbols keep their graph row and embedding, changed ones are re-analyzed and
re-embedded, and vanished ones are pruned (from the graph *and* the vector index). Re-running
`index` after edits is therefore cheap — the dominant cost, re-embedding, is paid only for
what actually changed, and the server can skip a clone entirely when the remote HEAD is
already the last-indexed commit.

Each index run is also recorded in an append-only **temporal log** keyed by the commit it
captured, so a ref's history is retained for future time-travel / branch-diff reads (the
query side of that is still landing).

## Server deployment

The same binary serves the local zero-infra case and a multi-tenant deployment; only the
backends and auth differ, and both sit behind the same ports:

- **Backends by DSN.** Set `DB__GRAPH` to a `neo4j://` DSN (and `DB__VECTOR` to a Milvus
  host) to swap the embedded stores for hosted **Neo4j** + **Milvus** — no code change. The
  Neo4j backend applies a thin Cypher dialect shim; every non-trivial query is verified
  against a real Neo4j in the test suite. Unset, it stays on embedded Kuzu + Milvus Lite.
- **Write / index auth is the git token.** `index(repo_url, ref, ci_token)` clones over the
  token; no valid token → no clone → nothing written, so the token *is* the write ACL. The
  clone is hardened: the token travels via `GIT_ASKPASS` (never the URL, argv, or
  `.git/config`), transports are allow-listed (no `ext::` command execution), and the
  checkout lands in a per-uid `0700` directory.
- **Read auth is an external gateway.** Reads are not checked by the server; front it with an
  OIDC gateway (e.g. Casdoor). The server keeps no session state.

Run the hosted server with `graphlens-mcp --http`.

## Known limitations

> Status: early — the runtime is being rebuilt on the MCP 2.0 SDK. The core index/query/
> incremental paths work; a few edges are still landing.

- **A git remote is required.** Project identity is `hash(remote + subpath)`, so an un-pushed
  or non-git directory is not indexable — add a remote (or push) first.
- **One snapshot per project, latest wins.** Identity is ref-independent, so indexing two
  refs of one repo into the same project leaves the live graph reflecting whichever was
  indexed last (the temporal log keeps both). Per-ref materialized views aren't wired up yet.
- **Reads need the working tree.** `info` source and content search read files from disk; the
  server retains a remote project's checkout for this, and local mode uses your own directory.
- **Rebuild on upgrade.** The store is a cache with no schema migrations — after upgrading
  across a schema change, delete the data directory and re-index rather than upgrading in
  place.

## Development

```bash
uv sync --all-groups                          # lint + test tooling (add --extra neo4j for the parity tests)
uv run ruff check . && uv run ty check src    # lint + types
uv run pytest                                 # tests (the Neo4j parity tests skip without Docker)
```

See the [Architecture](https://neko1313.github.io/graphlens-mcp/architecture) and
[Semantic search](https://neko1313.github.io/graphlens-mcp/design/semantic-search) pages on the
[documentation site](https://neko1313.github.io/graphlens-mcp/) for the design and invariants.

## License

MIT — see [LICENSE](LICENSE).
