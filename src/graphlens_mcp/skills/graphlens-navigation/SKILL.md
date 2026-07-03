---
name: graphlens-navigation
description: >
  Navigate the code graph using graphlens MCP tools instead of reading files or grepping.
  Use when asked: "what calls X", "what breaks if I change X", "who uses this function",
  "what does this function depend on", "impact analysis", "find callers", "find references",
  "what implements/extends X", "what's in this file", "search for / find in the code",
  "where is this string/log/config", "find code that does X".
  Three tools: search(query) finds nodes by name, content, or meaning; relations(symbol)
  gives callers/callees/implementors/references; info(target) reads a symbol's source or
  a file's outline/content.
allowed-tools: Bash
---

# graphlens Navigation

graphlens exposes exactly three MCP tools — `search`, `relations`, `info` — that
replace grep/ls/file-reads for code navigation. Every result is a graph node
(from a real parse, not text matching), so trust it instead of re-verifying
with grep.

## Decision tree: how to start

```
Know the exact symbol name or a node id?
  YES → relations(name) for callers/callees/implementors/refs in one call
        info(name) for its source + signature
  NO  → Can you describe it as literal text (a name fragment, a code snippet)?
          YES → search(query)  # name, content, and meaning are unified here
          NO  → search(query) still works — it falls back to semantic (meaning) matching
```

`relations` and `info` both accept **a node id OR a plain symbol name** — no
separate lookup call needed. Pass either straight from a `search` result's
`id`/`qualified_name`, or a name you already know.

## The three tools

### search(query, limit=25, path_glob=None, exhaustive=False)
The one way in — unifies NAME, CONTENT, and MEANING matching:
- Content is matched **literally**, not as regex — write `Request(` or
  `getErrorMap(` as-is, no escaping.
- Returns graph nodes **with their signature** (`nodes`), plus non-symbol text
  hits (`text_matches`) for content with no enclosing symbol (config/comments).
- Scope with `path_glob` (e.g. `"tests/*"`, `"*.ts"`, `"!tests/*"` to exclude a
  subtree) — there is no `file:`/`content:` query syntax, use `path_glob`
  instead of guessing one.
- **Test files are excluded by default** unless `path_glob` is set explicitly
  or the query itself mentions "test" — pass `path_glob="tests/*"` to search
  them on purpose.
- Set `exhaustive=true` for "list EVERY file that calls/imports X" — returns
  every matching file path (no signatures), uncapped by the normal top-N limit
  (which can silently drop matches on a large fan-out).
- If the response's `note` field is set, your literal text matched nothing —
  every node came from a name/meaning guess, not a confirmed hit. Simplify the
  query instead of repeating it verbatim.

### relations(symbol, depth=2, limit=25, file=None)
A symbol's whole neighbourhood in one call: `callers`, `callees`,
`implementors`, `references` — each with its signature. THE tool for impact
analysis ("what breaks if I change X?") and "what implements/extends X".
- Each list is capped at `limit`; the matching `*_total` field is the true
  count, so a hidden tail shows as a number, not silently dropped. A bigger
  `limit` cannot reveal more — it's the graph's real size.
- Test-file callers/callees are excluded by default (unless `symbol` mentions
  "test"), so the cap fills with production code.
- If `symbol` matches several definitions, pass `file` (a path or suffix) to
  pin the one you mean — e.g. one `UserService` per service in a monorepo.

### info(target, limit=200, file=None, mode="outline", offset=1)
Read a specific target — a SYMBOL or a FILE:
- Symbol → source, signature, and location.
- File, default (`mode="outline"`) → its symbol outline (cheap structural
  overview, no full content).
- File, `mode="source"` → the file's actual current content, line-numbered
  (`<n>\t<line>`, the same shape `Read` gives you — safe to edit from),
  windowable with `offset`/`limit` just like `Read`, plus which files import
  it (`dependents`). Use this instead of opening the file yourself whenever
  you need the body, not just its symbol list.
- If `target` (a symbol name) matches several definitions, pass `file` (a
  path or suffix) to disambiguate, same as `relations`.

## Common replacements

| Old habit | graphlens equivalent |
|---|---|
| `grep -r "pattern" .` | `search("pattern")` |
| `grep -r "def foo\|class Foo"` | `search("foo")` |
| `cat file.py \| grep def` | `info("path/to/file.py")` (outline) |
| `grep -r "Interface"` to find implementations | `relations("Interface")` — never guess from grep |
| Read whole file to find a function | `info("path/to/file.py")` → `info("SymbolName")` |
| Read file to find callers | `relations("name")` — no file reads, no prior lookup |
| Read a file to edit it | `info("path", mode="source")` — line-numbered, Read-equivalent |

## Impact analysis workflow

When asked "what breaks if I change X?":
1. `relations("X", depth=3)` → callers, callees, implementors, and references
   in one call (pass the name directly; `search` first only if unsure of it).
2. If a list is truncated (its `*_total` exceeds what's shown), narrow with
   `search` and distinguishing terms rather than re-calling `relations` with a
   bigger `limit` — it won't reveal more.
3. Summarise affected symbols — use `info` only on the ones that need
   elaboration, not every caller.

## Respect resolver_status and indexing

Every response carries:
- `resolver_status`: `"ok"` (full graph, edges trustworthy) or `"degraded"`
  (calls/types not fully resolved, usually a missing language toolchain) —
  treat edges as approximate when degraded, and suggest `graphlens-mcp
  reindex` or installing the missing toolchain if it comes up.
- `indexing`: `true` means a (re)index is still running — the graph is
  **incomplete right now**. An empty `relations` result or a not-found
  symbol may simply be unindexed yet. Do **not** conclude a symbol is
  unused/dead/safe to delete while `indexing: true` — say the index is
  still building and retry shortly.

## Don't fight the repeat guard

If you call `search`, `relations`, or `info` with the exact same arguments
twice, the response carries a `repeat_hint` — the result is deterministic and
won't change. On the third identical call, the tool returns `BLOCKED` instead
of data. If the answer isn't in what you already have, change the query,
narrow with `path_glob`/`file`, switch tools, or answer with your best
current evidence — don't just repeat the call.

## Hard rules

- **Never** shell out to `grep`/`rg`/`find` for code navigation — use `search`
- **Never** read entire source files to find callers — use `relations`
- **Never** pass a directory to `info` — it expects a file or symbol
- **Don't assume** a list is complete when `resolver_status != "ok"`
- **Don't conclude "unused / dead / safe to delete"** when `indexing: true`
