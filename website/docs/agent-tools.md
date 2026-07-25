---
id: agent-tools
title: Agent tools
sidebar_position: 4
---

# Agent tools

graphlens exposes **six** MCP tools. Three are for navigation — `search`, `info`,
`relations` — and three manage projects — `index`, `list_projects`, `remove_project`.
Navigation results come from a real parse, not a text match, so an agent can trust a result
instead of re-verifying it with grep. All tools take **flat** parameters (not a nested
`params` object).

## Navigation

| Tool | Purpose |
|---|---|
| `search(query, project=None, limit=25, path_glob=None, verbosity="concise", exhaustive=False)` | Find code by NAME, CONTENT, or MEANING in one call — the entry point. Name matching runs first, then semantic. `concise` (default) returns one line per hit — `name · kind signature · path:line · id=…`, usually already the answer; `detailed` also inlines each hit's source. Content is literal (not regex). Test files are excluded by default. `exhaustive=true` lists every in-scope **file path** instead of ranked hits. |
| `relations(symbol, project=None, depth=1, limit=25, kinds="", file="", ref="", at="")` | A symbol's callers, callees, implementors, and references in one call — the impact-analysis tool. Default `depth=1` is the **direct** callers/callees; raise it only to trace transitively. Each group carries a `*_total`; `callees_unresolved` counts calls graphlens couldn't bind; `not_indexed` names groups this project's language analyzer never produces. |
| `info(target, project=None, mode="outline", limit=None, offset=0, file="", ref="", at="")` | Read a symbol's source + signature + metadata, or a file's `outline` of symbols (default) or full `source` (`mode="source"`, line-numbered with `offset`/`limit` windowing and an importers list). |

`relations` and `info` both accept a node id **or** a bare symbol name — the name is resolved
internally, so no prior `search` call is required. If a name matches several definitions, pass
`file` (a path or suffix) to pin the one you mean.

`project` is optional: omit it when only one project is indexed and the server resolves it.
When several are indexed, pass the id, the name, or an unambiguous id-prefix — never call
`list_projects` just to learn it.

## Project management

| Tool | Purpose |
|---|---|
| `index(directory=…` **or** `repo_url=…, ref=…, ci_token=…, name=…, description=…)` | Add a project to the graph — pass exactly one source. Re-running it refreshes in place (see [Freshness](./freshness.md)). Returns counts plus `resolver_status` per language. |
| `list_projects()` | List every indexed project (id, name, path, git url, description). |
| `remove_project(project=…)` | Delete a project's graph nodes, embeddings, and registry entry. |

## Searching effectively

`search` blends name/content/semantic matching over the same node graph:

- **Don't search a bare common noun** (`Location`, `User`, `Config`) expecting the defining
  class to rank first among files, imports, and short names sharing the token.
- **Use the most distinctive identifier** you have: a compound name (`LocationRepository`) or
  qualify with the module path (`models.Location`).
- **Know the file? Skip search.** `info("path/to/file.py")` lists that file's outline.
- Content queries are **literal, not regex** — write `Request(` or `getErrorMap(` as-is.
- Scope with `path_glob`, a literal pathlib glob (`src/**/*.py`) — `*` does not cross `/`, so
  nested files need `**`. There is no `!`-negation. Test files are already excluded by
  default; a glob that *names* tests (`**/*_test.go`) opts them back in.

The semantic pass embeds nodes with a small `model2vec` model that is fetched on first use and
cached; if it can't be fetched (offline first run) the `search` call errors rather than
silently degrading.

## Impact-analysis workflow

When asked *"what breaks if I change X?"* or *"which files call X?"*:

1. `relations("X")` → the `callers` group **is** the answer; the files those callers live in
   are the impact set. Pass the name directly — a prior `search("X")` is optional.
2. Size it by `callers_total` first; keep the default `depth=1` (a higher depth adds indirect
   callers and answers a different question). Do **not** pad the set with `search` hits —
   search also matches imports, mentions, and the definition, which are not calls.
3. Summarise the affected symbols — use `info` only where a caller needs elaboration.

State the completeness caveat where it matters: dynamic dispatch, DI, reflection,
cross-language, and un-indexed code can hide callers. Read `not_indexed` (a listed group is
*unknown*, not empty) and non-zero `callees_unresolved` as "may be incomplete", not "none".

## History (time-travel)

`info` and `relations` accept `ref` and `at` to answer from an indexed past commit instead of
now: `at` is a commit sha (a short prefix is fine) or a seq from the project's history. A past
revision returns a symbol's recorded metadata and its neighbours of the day, but **not** its
source body; `search` is always current. Use it for "when did this call disappear", "who
called X before the refactor", "did this symbol exist at release-1.2".

## Coverage signals

- `resolver_status` (on the `index` result) — `ok` per language, or `degraded` when a
  language server is missing (`gopls` for Go, `rust-analyzer` for Rust). Degraded means fewer
  resolved cross-file edges; re-run `index` after installing the toolchain.
- `not_indexed` / `*_unresolved` (on `relations`) — the honest "unknown, not none" signals
  described above.
