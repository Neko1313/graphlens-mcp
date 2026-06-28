# Market research & gap analysis — graphlens-mcp

> Internal strategy note (not part of the published docs site). Captures where
> `graphlens-mcp` sits in the 2026 "code intelligence for AI agents" landscape,
> what competitors ship that we don't, and a prioritized list of what to build
> next. Snapshot date: 2026-06. Star counts and benchmark figures are sourced
> from public write-ups (see [Sources](#sources)) and will drift.

## 1. What graphlens-mcp is today (v0.2.2)

A free, MIT, **local, read-only navigation** MCP server. It builds a semantic
code graph on top of the `graphlens` engine and exposes 8 query tools:
`search_symbols`, `get_node_info`, `get_file_structure`, `get_callees`,
`get_callers`, `get_neighbors`, `find_references`, `get_cross_language_calls`.

**Genuine strengths to defend:**

- **Real semantic resolution, not tree-sitter heuristics.** Python links go
  through the `ty` type engine; TS/Go/Rust/PHP use their real toolchains. This
  buys higher call/reference precision than the pure tree-sitter graph that most
  competitors ship — *when the toolchain is present*.
- **Cross-language edges** (`COMMUNICATES_WITH` over HTTP/gRPC/queue boundaries).
  Very few tools model service-to-service calls; this is a real differentiator.
- **Honest quality signal.** Every response carries `resolver_status`
  (`ok` | `degraded`), so the agent never treats a partial answer as complete.
  Most competitors return graphs with no confidence signal.
- **Robust freshness.** Watcher + startup reconcile + connected-set re-link is
  more carefully engineered than a naive "re-index changed file" approach.
- **Clean, single-file SQLite cache, regenerable, no migrations.** Easy to adopt.

## 2. The 2026 landscape

Code intelligence for agents went from niche to a breakout category in spring
2026. The recurring, benchmarked claim across vendors: indexed structural
retrieval cuts **~97% of input tokens** and **58–88% of tool calls** versus
grep+read, with measurable agent-quality gains. The market has settled on a few
recognizable shapes.

| Tool | Stars (≈) | Langs | Graph / impact | Editing? | Semantic search | Local? | Notable |
|---|---|---|---|---|---|---|---|
| **Serena** (oraios) | 25k | 20+ | refs, call hierarchy | **Yes — symbol-level edit/rename** | LSP symbol | Yes | The category default; editing is its moat |
| **CodeGraph** (codegraph-ai) | 47k | 38 | callers/callees, impact, circular deps | No | Curated context | Yes (SQLite) | 42 tools, persistent memory, VS Code ext + GH Action |
| **GitNexus** | 42k | many | **single-call confidence-scored blast radius** | No | dual graph | Yes (zero-server) | Hooks into agent PreToolUse to inject caller/callee context |
| **code-graph-mcp** (sdsrss) | <1k | 16 | call graph, impact, HTTP trace, dead code | No | **BM25 + vector** | Yes (Rust+SQLite) | Closest direct analog; BLAKE3 incremental |
| **Augment** | — | many | cross-repo context engine | (assist) | embeddings | Cloud | $252M-funded; spun engine out as MCP |
| **Sourcegraph Cody** | — | many | org-wide code graph | (assist) | embeddings | Cloud | Enterprise, multi-repo |
| **Aider repo map** | — | many | repo summary (~85% acc.) | Yes (edits) | — | Yes | Zero-config, misses function-body deps |
| **graphlens-mcp** | early | 5 | callers/callees, refs, **cross-language** | **No** | **Name FTS only** | Yes (SQLite) | Real resolvers + honest status |

**Closest competitor:** `code-graph-mcp` (sdsrss) — same idea (AST graph MCP,
call graph + impact + HTTP route tracing + find_references), but it adds dead-code
detection, hybrid BM25+vector semantic search, BLAKE3 Merkle incremental
indexing, 16 languages, and ships as a Claude Code plugin with slash commands.
**Biggest-mindshare competitors:** CodeGraph and GitNexus (40k+ stars each).
**Category leader on capability:** Serena, because it *edits*.

## 3. Gap analysis — what we're missing

Ordered roughly by impact on adoption.

### Tier 1 — adoption blockers / table stakes

1. **No editing tools (read-only).** The category leader (Serena) wins on
   symbol-level *editing* — `replace_symbol_body`, project-wide rename, insert.
   We give the agent perfect navigation and then make it drop back to raw
   line-edits to act on it. This is the single largest capability gap. Even one
   safe, graph-aware edit (rename-symbol with reference rewrite) would change the
   value proposition from "a smarter grep" to "an IDE for the agent."
2. **Python ≥3.13 hard requirement.** A real install barrier — most projects and
   environments aren't on 3.13. `uv tool install` isolates the runtime, but the
   constraint still scares off evaluators and breaks `pipx`/system-Python paths.
   At minimum: document the isolation story prominently; ideally relax the floor
   if `graphlens` allows it.
3. **No published benchmarks.** Every serious competitor leads with numbers
   ("58–88% fewer tool calls", "97% fewer tokens"). We have none. In 2026 this is
   how the category is evaluated; a reproducible token/tool-call benchmark vs
   grep+read on a known repo is high-leverage marketing *and* a regression guard.
4. **Narrow, often-degraded language coverage.** 5 languages, and 4 of them are
   `degraded` without a local toolchain, vs 16–38 for competitors. The honesty is
   good; the out-of-box footprint is small. A tree-sitter "structure + heuristic
   calls" fallback (clearly marked `degraded`) would widen reach without lying.

### Tier 2 — competitive parity

5. **No semantic / vector search** — only FTS5/BM25 over names. The SKILL even
   warns that common nouns rank badly. Competitors ship hybrid BM25 + embeddings
   (`sqlite-vec`). A "search by intent" tool closes a real usability hole.
6. **No project / architecture overview tool.** Competitors expose
   `project_map` / `module_overview` / `dependency_graph` — the agent's first
   "orient me" call. We have per-file structure but no top-down map.
7. **No single-call blast-radius tool.** GitNexus's pitch is one
   confidence-scored `impact` call instead of the agent chaining
   `get_callers` + `find_references` + `get_cross_language_calls` itself. We have
   the pieces; we don't compose them into one impact answer. Cheap, high-value.
8. **No dead-code / unused-symbol tool.** `find_dead_code` is a common,
   easy-to-explain win we already have the graph data to compute.

### Tier 3 — distribution & polish

9. **Distribution surface is thin.** No Claude Code plugin / slash commands, no
   VS Code extension, no GitHub Action / PR-review integration. Competitors lead
   with these. A Claude Code plugin (`/understand`, `/impact`, `/trace`) with an
   auto-index hook is the cheapest reach multiplier.
10. **No persistent memory / cross-session notes** (CodeGraph has this).
11. **No agent-hook context injection.** GitNexus injects caller/callee context
    at `PreToolUse` so the agent gets structure *without* explicitly asking. A
    `PreToolUse` hook that annotates file reads with callers/callees is a
    differentiated, low-token feature that fits our strengths.

## 4. Recommended next steps

**Defend the moat, then close the nearest gaps.** Lead with the two things only
we do well — *real resolvers* and *cross-language edges* — and borrow the table
stakes everyone else already has.

Suggested order:

1. **Benchmark harness + published numbers** (Tier 1.3). Smallest effort, biggest
   credibility; also guards against regressions. Do this first.
2. **`get_impact` composite tool** (Tier 2.7) — one call folding callers +
   references + cross-language consumers into a ranked, status-tagged blast
   radius. Reuses existing queries; immediately demoable.
3. **Claude Code plugin + slash commands** (Tier 3.9) — reach multiplier, low code.
4. **Tree-sitter `degraded` fallback** (Tier 1.4) — widen language reach honestly.
5. **One graph-aware edit: safe rename-symbol** (Tier 1.1) — the strategic bet
   that moves us into Serena's category. Largest effort; sequence after the
   quick wins prove traction. Gate edits behind `resolver_status == ok`.
6. **Semantic search via `sqlite-vec`** (Tier 2.5) and **`project_map`**
   (Tier 2.6) as parity follow-ups.

Explicitly *out of scope* (don't chase): cloud/enterprise multi-repo (Augment,
Sourcegraph) — that's a different, capital-intensive game. Stay local-first and
MIT; that's a positioning advantage against the funded incumbents.

## Sources

- Ry Walker — *Code Intelligence Tools for AI Agents Compared*: <https://rywalker.com/research/code-intelligence-tools>
- Serena (oraios): <https://github.com/oraios/serena>
- code-graph-mcp (sdsrss): <https://github.com/sdsrss/code-graph-mcp>
- CodeGraph: <https://claudemarketplaces.com/mcp/codegraph-ai/codegraph>
- GitNexus: <https://www.marktechpost.com/2026/04/24/meet-gitnexus-an-open-source-mcp-native-knowledge-graph-engine-that-gives-claude-code-and-cursor-full-codebase-structural-awareness/>
- Sourcegraph Cody: <https://sourcegraph.com/docs/cody>
- 2026 category overview: <https://www.amplifilabs.com/post/2026-round-up-the-top-10-ai-coding-assistants-compared-features-pricing-best-use-cases>
