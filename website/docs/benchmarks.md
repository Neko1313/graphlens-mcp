---
id: benchmarks
title: Benchmarks
sidebar_position: 8
---

# Benchmarks

`graphlens-mcp` ships with a reproducible A/B benchmark ([`benchmarks/`](https://github.com/Neko1313/graphlens-mcp/tree/main/benchmarks))
that drives an identical [`pydantic-ai`](https://ai.pydantic.dev) agent against four
interchangeable code-context arms — **graphlens** (this project), **codegraph** (graph index),
**semble** (semantic search), and a **no-tools control** that measures how much any of them
adds over the model's own memory — across ten real open-source repositories
(Go / Rust / Python / TypeScript), graded deterministically against oracle gold answers
(no LLM judge).

## Scope

- **10 projects**: gin, echo, ripgrep, clap, fastapi, click, httpx, hono, zod, superset.
- **3 models**, spanning strong to genuinely weak: `deepseek-v4-flash`, `gemma-4-26b`,
  `gpt-oss-20b`.
- **2 difficulty tiers**: `SIMPLE` (symbol/definition lookups, single-hop) and `HARD`
  (impact analysis, disambiguation, multi-hop cross-file questions).
- **~2,400 graded runs**, each with token/tool-call/dollar cost recorded alongside accuracy.

## Headline result

| | SIMPLE accuracy | HARD accuracy | HARD tokens (median) | HARD completion |
|---|---|---|---|---|
| **graphlens** | 0.980 – 1.000 | 0.899 – 0.921 | **22.4k – 34.1k** | **≥ 0.959 on every model** |
| codegraph | 0.912 – 0.990 | 0.655 – 0.939 | 23.2k – 70.0k | drops to 0.765 on the weakest model |
| semble | 0.647 – 0.961 | 0.555 – 0.850 | 21.6k – 74.9k | drops to 0.688 on the weakest model |
| none (control) | 0.366 – 0.681 | 0.453 – 0.685 | 0.1k – 0.9k | — |

Accuracy alone hides the number that maps directly to a bill: **tokens paid per task**.
graphlens's HARD-tier token spend stays flat (22k–34k) across the whole model range; codegraph's
and semble's balloon past 70k on the weakest model — more than double graphlens's ceiling — for
a *worse* answer, not a better one. Accuracy and token cost are both worth reading, and reading
together: a tool that's marginally more accurate but burns 2× the tokens per task isn't
obviously the better deal once that scales to a real workload.

graphlens is the only arm that stays clearly ahead of the no-tools control **and** keeps
completion above 0.95 at every model tier. The gap over codegraph widens, not narrows, as the
driving model gets weaker: on the weakest model tested (`gpt-oss-20b`), graphlens holds
**0.900** HARD accuracy at **0.959** completion, while codegraph drops to **0.655** accuracy
with completion falling to **0.765** — roughly 1 in 4 of its runs never produce an answer at
all, most often by exhausting its own output-token budget mid-answer. graphlens gets there at
**roughly half the token cost** (34k vs 70k median tokens per HARD task).

### Full breakdown

<details>
<summary>SIMPLE tasks — accuracy · completion · median tokens · median cost</summary>

| arm | model | accuracy | completion | tokens | cost |
|---|---|---|---|---|---|
| graphlens | deepseek-v4-flash | 0.980 | 1.000 | 8,396 | $0.00077 |
| graphlens | gemma-4-26b | 1.000 | 1.000 | 4,060 | $0.00026 |
| graphlens | gpt-oss-20b | 0.980 | 1.000 | 4,033 | $0.00014 |
| codegraph | deepseek-v4-flash | 0.990 | 1.000 | 9,662 | $0.00090 |
| codegraph | gemma-4-26b | 0.912 | 0.941 | 7,588 | $0.00048 |
| codegraph | gpt-oss-20b | 0.971 | 1.000 | 11,150 | $0.00035 |
| semble | deepseek-v4-flash | 0.961 | 0.961 | 12,934 | $0.00121 |
| semble | gemma-4-26b | 0.647 | 0.696 | 5,800 | $0.00038 |
| semble | gpt-oss-20b | 0.824 | 0.843 | 12,038 | $0.00041 |
| none | deepseek-v4-flash | 0.681 | 1.000 | 243 | $0.00004 |
| none | gemma-4-26b | 0.366 | 0.901 | 115 | $0.00001 |
| none | gpt-oss-20b | 0.549 | 0.961 | 466 | $0.00005 |

</details>

<details>
<summary>HARD tasks — accuracy · completion · median tokens · median cost</summary>

| arm | model | accuracy | completion | tokens | cost |
|---|---|---|---|---|---|
| graphlens | deepseek-v4-flash | 0.921 | 1.000 | 23,436 | $0.00226 |
| graphlens | gemma-4-26b | 0.899 | 1.000 | 22,430 | $0.00138 |
| graphlens | gpt-oss-20b | 0.900 | 0.959 | 34,064 | $0.00106 |
| codegraph | deepseek-v4-flash | 0.939 | 1.000 | 23,212 | $0.00214 |
| codegraph | gemma-4-26b | 0.847 | 0.959 | 25,287 | $0.00156 |
| codegraph | gpt-oss-20b | 0.655 | 0.765 | 69,991 | $0.00223 |
| semble | deepseek-v4-flash | 0.850 | 0.908 | 60,804 | $0.00582 |
| semble | gemma-4-26b | 0.615 | 0.724 | 21,555 | $0.00136 |
| semble | gpt-oss-20b | 0.555 | 0.688 | 74,937 | $0.00244 |
| none | deepseek-v4-flash | 0.685 | 1.000 | 246 | $0.00003 |
| none | gemma-4-26b | 0.453 | 0.898 | 133 | $0.00001 |
| none | gpt-oss-20b | 0.529 | 0.917 | 944 | $0.00011 |

</details>

## Why report accuracy *and* completion separately

A run that never finishes — a turn-limit cutoff, a timeout, the model exhausting its own
output-token budget — is graded as wrong, same as a run that answered confidently and
incorrectly. Reporting only accuracy hides *why* a tool scored low: a wide gap between accuracy
and completion means the tool isn't being out-reasoned so much as it's failing to finish at all.
This is exactly the failure mode that dominates codegraph's and semble's weak-model numbers
above, and it's invisible in a bare accuracy column.

## Statistical significance

Every arm answers the identical task set, so this is a repeated-measures design — the right
significance test is a **paired**, non-parametric one, not an unpaired comparison of pooled
means. The benchmark's [`metrics.ipynb`](https://github.com/Neko1313/graphlens-mcp/blob/main/benchmarks/metrics.ipynb)
runs:

- a **Friedman test** (omnibus): are the four arms different at all, ranked within each task?
- a **Wilcoxon signed-rank test** (pairwise): for graphlens vs. each rival specifically, matched
  by `task_id`, with a rank-biserial effect size alongside the p-value.

The pairwise gap between graphlens and codegraph is **not statistically significant** on
`deepseek-v4-flash` (the strongest model — the two are a real tie there) but becomes
significant, with a large effect size, on both weaker models — exactly where the headline table
above shows the widest gap. A per-project heatmap in the same notebook confirms graphlens ranks
first among all four arms on HARD accuracy in **all ten projects**, so the result isn't one
favorable repo carrying the average.

## Why `none` (no tools) is in the comparison

`none` answers from the model's own parametric memory alone, with zero tools available. It's
not a competitor — it's the contamination-robust floor every real arm needs to clear. A popular
open-source repo a model already memorized during training would inflate *every* arm's raw
accuracy equally; each arm's **lift over `none`** is what isolates the tool's actual
contribution from that noise.

## Reproducing this

```bash
cd benchmarks
uv sync
cp .env.example .env   # add your OPENROUTER_API_KEY
uv run main.py                       # full sweep (slow, costs real API spend)
uv run main.py --projects gin echo   # a quick subset
uv run scripts/report.py             # headline table + Friedman test, from the CLI
jupyter notebook metrics.ipynb       # the fuller analysis on this page
```

See [`benchmarks/README.md`](https://github.com/Neko1313/graphlens-mcp/blob/main/benchmarks/README.md)
for the full harness design: task authoring, oracle grading, cost accounting, and the exact
arm/model registry.

## Data hygiene note

A benchmark cell that times out or hits its turn limit gets retried; the retry is appended to
that project's result file rather than replacing the failed attempt in place. Any analysis over
this data must dedupe on `(project, arm, model, task_id, seed)`, keeping the **last** row, before
computing an aggregate — `scripts/report.py`'s `load()` does this automatically. Skipping it
double-counts every retried failure and measurably distorts the result: an earlier, un-deduped
pass of this data reported codegraph's `gpt-oss-20b` / HARD accuracy at 0.373 (apparently *worse*
than the no-tools control); properly deduped, it's 0.655.
