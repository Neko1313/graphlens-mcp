---
id: agent-tools
title: Agent tools
sidebar_position: 4
---

# Agent tools

graphlens exposes exactly **three** MCP tools. Everything returned is a graph node — from a
real parse, not a text match — so an agent can trust a result instead of re-verifying it with
grep.

Every response carries `resolver_status` (`ok` | `degraded`, aggregated across every returned
node's file) and an `indexing` boolean. The server starts serving immediately and reconciles the
graph in the background, so `indexing=true` means a reindex is running and edges may still be
incomplete — don't conclude a symbol is unused while `indexing=true`. List-shaped results also
carry a `truncated` flag, and relation lists include a matching `*_total` field so a hidden tail
shows up as a number instead of a silent drop.

| Tool | Purpose |
|---|---|
| `search(query, limit=25, path_glob=None, exhaustive=False)` | Find code by NAME, CONTENT, or MEANING in one call — the entry point. Content is matched literally (not regex). Test files are excluded by default. `exhaustive=true` lists every matching file path for "list EVERY file that calls X" tasks. |
| `relations(symbol, depth=2, limit=25, file=None)` | A symbol's callers, callees, implementors, and non-call references in one call — the impact-analysis tool and the answer to "what implements/extends X?" |
| `info(target, limit=200, file=None, mode="outline", offset=1)` | Read a symbol's source/signature, or a file's outline (default) or actual line-numbered content (`mode="source"`, Read-equivalent, with `offset`/`limit` windowing and a `dependents` list). |

`relations` and `info` both accept a node ID **or** a bare symbol name — the name is resolved
internally, so no prior `search` call is required. If a name matches several definitions, pass
`file` (a path or suffix) to pin the one you mean.

## Searching effectively

`search` unifies three matching strategies — exact/near name match, literal content match, and
semantic (meaning) fallback — over the same node graph:

- **Don't search a bare common noun** (`Location`, `User`, `Config`) expecting the defining
  class to rank first among files, imports, and short names sharing the token.
- **Use the most distinctive identifier** you have: a compound name (`LocationRepository`) or
  qualify with the module path (`models.Location`).
- **Know the file? Skip search.** `info(path)` deterministically lists every node's outline.
- Content queries are **literal, not regex** — write `Request(` or `getErrorMap(` as-is.
- Scope with `path_glob` (e.g. `"tests/*"`, `"*.ts"`, `"!tests/*"`) instead of guessing a
  `file:`/`content:` query syntax that doesn't exist.

The semantic fallback embeds graph nodes with a bundled `model2vec` model (no extra install). If
the embedding model can't be fetched (offline first run), semantic matching is skipped and
`search` falls back to name/content matching only.

## Impact-analysis workflow

When asked *"what breaks if I change X?"*:

1. `relations("X", depth=3)` → callers, callees, implementors, and references in one call. Pass
   the name directly — a prior `search("X")` lookup is optional.
2. If a list's `*_total` exceeds what's shown, narrow with `search` and distinguishing terms
   rather than re-calling `relations` with a bigger `limit` — the cap reflects the graph's real
   size, not a page boundary.
3. Summarise the affected symbols — use `info` only for the ones that need elaboration, instead
   of reading every caller file.

## Respect `resolver_status` and `indexing`

- `resolver_status: "ok"` — full semantic graph, edges are trustworthy.
- `resolver_status: "degraded"` — calls/types not fully resolved (usually a missing language
  toolchain); treat edges as approximate and suggest `graphlens-mcp reindex` or installing the
  toolchain.
- `indexing: true` — a background reindex is still running, so missing callers/edges may simply
  not be indexed yet — don't conclude a symbol is unused until indexing has settled.

## Repeat guard

Calling `search`, `relations`, or `info` with identical arguments twice returns a `repeat_hint` —
the result is deterministic and won't change. A third identical call is blocked outright
(`error` field set, no data) to force a different query, tool, or a final answer instead of
looping.
