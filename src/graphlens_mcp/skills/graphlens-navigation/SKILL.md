---
name: graphlens-navigation
description: >
  Navigate the code graph using graphlens MCP tools instead of reading files or grepping.
  Use when asked: "what calls X", "what breaks if I change X", "who uses this function",
  "what does this function depend on", "impact analysis", "find callers", "find references",
  "what's in this file", "how do services communicate", "cross-language calls",
  "search for / find in the code", "where is this string/log/config", "find code that does X",
  "what is this codebase about", "group related code".
  Start with search_semantic (concept) or search_symbols (known name) or get_file_structure
  (known file). Use search_code only for raw text, never grep directly.
allowed-tools: Bash
---

# graphlens Navigation

graphlens MCP tools ARE your grep, ls, and file reader for code navigation.
**Never shell out to grep, find, or read entire files for structural questions.**
Every navigation question has a tool answer; raw file reads are for implementation
details only after you have a node ID.

## Decision tree: how to start

```
Do you know the exact symbol name?
  YES → Is it short/common (User, Config, get)?
          YES → get_file_structure(path) if you know the file, else search_symbols("pkg.ClassName")
          NO  → search_symbols("SymbolName")  # distinctive compound names work well
  NO  → Can you describe the behavior in words?
          YES → search_semantic("description of behavior")   # primary discovery tool
          NO  → list_clusters()  # orient in unfamiliar repo by semantic zones
```

## Replacing common tools

| Old habit | graphlens equivalent |
|---|---|
| `grep -r "pattern" .` | `search_code("pattern")` |
| `grep -r "def foo\|class Foo"` | `search_symbols("foo")` |
| `cat file.py \| grep def` | `get_file_structure("path/to/file.py")` |
| `ls src/authz/` | `search_symbols("authz.")` — qualified prefix |
| `grep -r "authentication"` to find auth code | `search_semantic("authentication")` |
| Read whole file to find a function | `get_file_structure(file)` → `get_node_info(id)` |
| Read file to find callers | `get_callers(id)` — no file reads needed |

## Searching effectively

### search_symbols — for known names
FTS5/BM25 over symbol names and qualified names.

- **Short/common names rank poorly** (`User`, `Config`, `get`, `handle`). Dozens of
  imports and file nodes match — the defining class may not appear in `limit=50`.
- **Use the most distinctive form**: compound name (`UserRepository`), qualified path
  (`models.User`, `authz.guard`), or wildcard prefix (`OAuth*`).
- **Know the file? Skip search.** `get_file_structure("path/to/file.py")` lists every
  node with its ID deterministically — faster and more reliable for common names.

### search_semantic — primary discovery tool
Use when you don't know the symbol name. Describe behavior in natural language:
- `"validate JWT token and extract claims"`
- `"retry failed HTTP request with exponential backoff"`
- `"check user has permission for resource"`
- Concept names also work well: `"authentication"`, `"rate limiting"`, `"caching"`

Each hit IS a graph node — `node_id` goes straight to `get_callers`/`get_node_info`.

### search_code — raw text patterns only
Use for things the symbol graph cannot answer: string literals, log messages, comments,
TODOs, config values, SQL fragments, URLs.

**Pattern is PCRE regex** — escape metacharacters for literal searches:
- Parentheses: `"foo\\(bar\\)"` not `"foo(bar)"`
- Dots: `"os\\.path"` not `"os.path"`
- When in doubt, use a distinctive substring without specials: `"needle-in-haystack"`

Use `path_glob` to scope: `search_code("def ", path_glob="*.py")`.

### get_file_structure — file outline (NOT directory)
- Takes a **file** path only — a directory path returns empty (`degraded` or empty list).
- For "what's in this directory": use `search_symbols("prefix.")` with the package prefix.
- Nodes with `file_path: null` are **external stubs** — the symbol is used here but
  defined in a dependency. Use `get_file_structure` on the importer or `search_symbols`
  with the qualified name to navigate to it.

## Tool quick reference

| Question | Tool | Input |
|---|---|---|
| Where is `create_order` defined? | `search_symbols` | `"create_order"` |
| Where is `Location` defined? (common noun) | `get_file_structure` | known file path |
| Find auth-related code (no name) | `search_semantic` | `"authentication flow"` |
| What symbols are in `order_service.py`? | `get_file_structure` | `"order_service.py"` |
| What's in the `authz/` directory? | `search_symbols` | `"authz."` |
| What does `create_order` call internally? | `get_callees` | node_id, depth=2 |
| Who calls `create_order`? (impact) | `get_callers` | node_id, depth=3 |
| What references `OrderService`? (type annotations) | `find_references` | node_id |
| Show source + signature of a symbol | `get_node_info` | node_id |
| Find a string literal / log / comment | `search_code` | escaped regex pattern |
| Find code similar to this symbol | `find_related` | node_id |
| How does Python service talk to TS client? | `get_cross_language_calls` | node_id |
| What's around this class in the graph? | `get_neighbors` | node_id, depth=2 |
| What is this codebase about? / orient | `list_clusters` | — |
| What's the semantic neighborhood of a symbol? | `get_cluster` | node_id |

## Impact analysis workflow

When asked "what breaks if I change X?":
1. `search_symbols("X")` or `search_semantic("X behavior")` → get node ID
2. `get_callers(id, max_depth=5)` → direct and transitive callers
3. `find_references(id)` → non-call usages (type annotations, assignments)
4. `get_cross_language_calls(id)` → cross-service consumers
5. Summarise affected symbols — do **not** read every caller file; use
   `get_node_info` only for ones that need elaboration.

## Understanding external symbols (file_path: null)

Any node with `file_path: null` is an **external stub** — it represents a symbol
from a library or another service, not something defined locally. You cannot read
its source via `get_node_info`. Instead:
- Use `get_callers(id)` to see where it is called in your codebase
- Use `search_symbols("pkg.SymbolName")` to find the local wrapper/adapter if any
- It still participates in the call graph — callers and edges are valid

## Respect resolver_status

Each response includes `resolver_status` across all returned nodes' files:
- `ok` — full semantic graph, edges are trustworthy
- `degraded` — calls/types not fully resolved (usually a missing language toolchain);
  treat edges as approximate, supplement with `search_code` for confirmation

When `degraded`, say so and suggest `graphlens-mcp reindex` or installing the
missing language toolchain (e.g. `pyright`, `typescript`).

## Hard rules

- **Never** shell out to `grep` or `rg` — use `search_code`
- **Never** read entire source files to find callers — use `get_callers`
- **Never** search a bare common noun and trust the result — use qualified name,
  `get_file_structure`, or `search_semantic`
- **Never** pass a directory to `get_file_structure` — it returns empty
- **Escape** regex metacharacters in `search_code` patterns
- **Don't assume** an edge list is complete when `resolver_status != ok`
