# graphlens-benchmarks

An A/B benchmark that measures the **token economics, accuracy, and tool-call
efficiency** of code-context MCP servers driving an *identical* agent. The only
variable is which MCP server provides the agent's code-navigation tools.

It is the spiritual successor to `agent-context-bench`, with four changes:

| | agent-context-bench | this benchmark |
|---|---|---|
| Engine | Claude Code CLI (`claude -p`) | **pydantic-ai** agent |
| Models | Claude haiku/sonnet/opus | **OpenRouter** flash tier: deepseek-v4-flash, gemma-4-26b, qwen3.5-flash, glm-4.7-flash |
| Arms | filesystem, graphlens, serena, codegraph | **graphlens**, **semble**, codegraph, **+ `none` control** |
| Targets | apache/superset (Py+TS) | **10 repos** across Go / Rust / Python / TS + superset (polyglot) |

## The two axes

Held against one agent, system prompt, and task set:

- **arm** — the single code-context MCP server: `graphlens` (this repo's working
  copy), `semble` (semantic search), `codegraph` (graph index), plus `none` — a
  **control arm with no tools** that answers from model memory (see Methodology).
  A `filesystem` (read + name search) baseline was dropped: it never competed on
  accuracy with any real arm, so its presence added cost without signal.
- **model** — `deepseek-v3` (strong cheap MoE), `gemini-flash` (fast hosted),
  `qwen3-8b` (small — the tool-calling stress test).

The matrix is **projects × arms × models × tasks × seeds**.

## Targets

| project | repo @ tag | languages |
|---|---|---|
| `gin` | gin-gonic/gin @ v1.10.0 | Go |
| `echo` | labstack/echo @ v4.12.0 | Go |
| `ripgrep` | BurntSushi/ripgrep @ 14.1.1 | Rust |
| `clap` | clap-rs/clap @ v4.5.20 | Rust |
| `fastapi` | fastapi/fastapi @ 0.115.0 | Python |
| `click` | pallets/click @ 8.1.7 | Python |
| `httpx` | encode/httpx @ 0.27.2 | Python |
| `hono` | honojs/hono @ v4.6.0 | TypeScript |
| `zod` | colinhacks/zod @ v3.23.8 | TypeScript |
| `superset` | apache/superset @ 6.0.0 | Python + TS — **cross-language** (TS hook → Py route) |

Every repo carries **10 hand-/oracle-verified tasks (5 SIMPLE + 5 HARD)** —
superset's 10 include the cross-language set. **100 tasks total**, equal weight.

## How it works

1. **setup** (`main.py`, measured into `data/index_costs.jsonl`): shallow-clone
   each target at its pinned tag, then build each arm's index up front so the MCP
   server loads a warm index instead of indexing during the agent's first turn.
2. **run**: for each `(project, arm)` a single MCP stdio server is started and
   **kept warm** for the whole sweep (pydantic-ai holds the process open — no
   per-task reload, no SSE proxy). Every `(model, task, seed)` runs through it.
3. **grade**: deterministic, oracle-verified, **no LLM judge** (see below).
4. **report**: `scripts/report.py` aggregates `data/*.jsonl`, stratified by regime.

### Methodology (why the numbers are trustworthy)

Aligned with current agent-benchmark practice:

- **No LLM judge** — deterministic, oracle-verified grading sidesteps judge bias
  / preference leakage.
- **Cost-controlled** — accuracy is always reported next to tokens / tool-calls /
  cost / wall; a cheaper arm at equal accuracy wins. (`scripts/report.py`)
- **Contamination control** — popular OSS is likely in the models' training data,
  so a model may "know" answers without using a tool. Defenses: (1) the `none`
  control arm gives the pure-memory baseline and we report each arm's **lift**
  over it; (2) `ungrounded` = share of answers given with **zero tool calls**
  (high = answering from memory); (3) because the model is held constant across
  arms, memory is a constant offset that the **arm ranking** mostly cancels —
  and the **HARD** regime (exact impact sets at a pinned tag) is far less
  memorizable, which is where the real signal lives.
- **Reliability** — 2 seeds per cell (set `BENCH_SEEDS=5` for higher
  confidence); the report shows per-task accuracy spread (`acc_sd`), not just the
  mean.
- **Significance** — a Friedman test ranks the arms within each regime.
- **Toolchain fairness** — graphlens/codegraph resolve Go/Rust via gopls /
  rust-analyzer. Missing them silently halves the graph (measured: gin indexed
  2,023 nodes degraded vs **5,715** with gopls). `bench/config.py` auto-adds
  `~/go/bin` & `~/.cargo/bin` to PATH, and `main.py` prints a preflight warning
  for any language whose server is absent.

### Why the grading is trustworthy

Gold answers come from a source **other than the arms under test** — otherwise an
arm is graded against its own output. Two task shapes, both deterministic:

- `answer_contains`: required substrings → fraction present (pinpoint lookups).
- `answer_set`: an unordered gold set → **F1** (precision penalizes a grep-dump
  that over-lists; recall penalizes misses). Used for impact / overrides.

Gold is produced by an **independent oracle** (`bench/oracle/`, ripgrep/ast-grep
+ language toolchains) and **spot-checked by hand**. `scripts/check_gold.py`
re-derives each answer and flags disagreements:

```
uv run scripts/check_gold.py --projects gin
#  ✓ AGREE   — oracle confirms the gold
#  ⚠ REVIEW  — look (oracle is recall-biased; usually it over-proposes)
#  · MANUAL  — no oracle spec; verified by hand (gold_src records how)
```

## Setup

```bash
cd benchmarks
uv sync                                   # python deps (pydantic-ai, etc.)
uv run scripts/setup_arms.py --install    # semble, ast-grep, gopls, rust-analyzer
echo "OPENROUTER_API_KEY=sk-or-..." > .env
```

Arm binaries expected on PATH: `codegraph`, `uvx` (for `semble`). `graphlens`
runs from this repo's working copy via `uv run --project .. graphlens-mcp serve`.

> **Toolchains matter for fairness.** graphlens/codegraph resolve Go via `gopls`
> and Rust via `rust-analyzer`. Without them those languages index in *degraded*
> mode. Install them before a headline run (`setup_arms.py --install`).

## Run

```bash
# Validate the wiring first — connect every arm, list its tools (no LLM, no cost):
uv run scripts/smoke_one.py --probe --project gin

# One cell end-to-end (needs OPENROUTER_API_KEY):
uv run scripts/smoke_one.py --project gin --arm graphlens --model qwen3-8b

# Clone + index only:
uv run main.py --setup-only

# Full matrix (resumable — re-running skips completed (task_id, seed) rows):
uv run main.py
uv run main.py --projects gin hono --arms graphlens codegraph --models qwen3-8b
uv run main.py --only impact_set overrides_count --seeds 1

# Detached overnight with auto-resume across rate limits:
scripts/run_all.sh           # stop with scripts/stop.sh

# Analyze:
uv run scripts/report.py
```

## Output

`data/<project>__<arm>__<model>.jsonl`, one row per run:

```jsonc
{
  "project": "gin", "arm": "graphlens", "model": "qwen3-8b", "seed": 0,
  "task_id": "gin_def_engine", "kind": "where_defined", "regime": "SIMPLE",
  "answer": "gin.go", "accuracy": 1.0,
  "input_tokens": 1234, "output_tokens": 12, "total_tokens": 1246,
  "cost_exact": 0.00009, "cost_table": 0.00010,      // OpenRouter real cost / table estimate
  "num_turns": 2, "n_tool_calls": 1, "tool_names": ["search"],
  "wall_s": 3.2, "verified": "v1.10.0", "gold_src": "oracle:go"
}
```

Plus `data/index_costs.jsonl` (upfront, amortizable indexing cost — a different
currency from per-query token cost, never blended) and `data/run_manifest.json`.

## Task kinds

`SIMPLE` (lookups): `where_defined`, `inherits_from`, `implements`,
`abstract_methods`, `signature`, `route_handler`, `route_call`.
`HARD` (multi-hop): `callers`, `impact_set`, `overrides_count`, `implementors`,
`disambiguate`, `xlang_link`. Regimes are **reported separately, never pooled.**

## Status

- ✅ Harness: engine (pydantic-ai 2.x + OpenRouter), 4 arms incl. `none` control,
  deterministic scoring, oracle, orchestration, resume, contamination-aware
  reporting (lift / ungrounded / Friedman), toolchain preflight + PATH fix — all
  built and validated. The `filesystem` baseline was dropped after enough runs
  showed it never competed on accuracy with any real arm.
- ✅ Task sets: **100 tasks, all verified, equal weight** — 10 repos × 10
  (gin, echo, ripgrep, clap, fastapi, click, httpx, hono, zod, superset).
  `check_gold.py`: oracle-AGREE on every callers/where-defined task, the rest
  hand-verified via full-repo grep, 0 unresolved. Caller/impact golds stay in
  sync with the oracle via `scripts/sync_oracle_gold.py` (comment-stripped, so
  Rust doc-example mentions aren't miscounted as call sites).
- ✅ Quality gate green: `ruff` (project config) + `ty` + `pytest` all pass.
- Cost scales with the matrix: 100 tasks × arms × models × seeds. A single
  model with 2 seeds across all 4 arms is a few dollars; each extra model or
  seed multiplies it linearly. Levers to cut it: lower `BENCH_MAX_TURNS` (caps
  a runaway semble tail), drop a model, or `--arms` to a subset.

## Authoring a new project's tasks

```bash
uv run main.py --setup-only --projects ripgrep   # clone + index
# inspect the repo, fix gold from the source, write tasks/ripgrep.jsonl
uv run scripts/check_gold.py --projects ripgrep  # confirm against the oracle
uv run scripts/smoke_one.py --probe --project ripgrep
```

## Quality gate

Mirrors the project's tooling (`ruff.toml` / `ty.toml` copied from the engine,
line-length 79). All green:

```bash
uv run ruff format --check && uv run ruff check && uv run ty check && uv run pytest
```

## Layout

```
bench/        config, projects, arms, models, runner, scoring, dataset, setup, oracle/
tasks/        <project>.jsonl task sets
data/         result JSONL (committed) + index_costs + manifest
scripts/      setup_arms, smoke_one, check_gold, report, run_all.sh, stop.sh
tests/        scoring unit tests
main.py       orchestrator
```
