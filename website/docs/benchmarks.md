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
- **2 models**, a strong one and a weaker one: `deepseek-v4-flash` and `glm-4.7-flash`. The
  model is held constant across arms, so within a model the only variable is the tool surface.
  (A third weak model, `gpt-oss-20b`, was dropped after it was verified to fail at *synthesising*
  answers from tool results — a model limitation, not a tool-surface one — which added noise.)
- **2 difficulty tiers**: `SIMPLE` (symbol/definition lookups, single-hop) and `HARD`
  (impact analysis, disambiguation, multi-hop cross-file questions).
- **~1,600 graded runs** (4 arms × 2 models × 10 projects × 10 tasks × 2 seeds), each with
  token/tool-call/dollar cost recorded alongside accuracy.

## Headline result

Ranges span the strong model ↔ the weaker model (`deepseek-v4-flash` ↔ `glm-4.7-flash`):

| | SIMPLE accuracy | HARD accuracy | HARD tokens (median) | HARD completion |
|---|---|---|---|---|
| **graphlens** | 0.990 – 1.000 | 0.937 – 0.971 | 21.9k – 34.0k | 0.827 – 0.990 |
| codegraph | 0.990 – 1.000 | 0.963 – 0.968 | 23.2k – 29.7k | 0.816 – 1.000 |
| semble | 0.984 – 1.000 | 0.952 – 0.960 | 17.9k – 60.8k | **0.306** – 0.908 |
| none (control) | 0.600 – 0.639 | 0.665 – 0.702 | 0.3k – 0.8k | — |

On the **strong** model, graphlens leads: SIMPLE 1.000, HARD 0.971 at the lowest median token
cost of the real arms (21.9k HARD, vs codegraph 23.2k and semble 60.8k). On the **weaker**
model the picture is a close race between the two graph-based arms — codegraph nudges ahead on
HARD accuracy (0.968 vs 0.937), graphlens on completion (0.827 vs 0.816) — so the old claim
that graphlens's lead *widens* on weaker models does not hold here; it's a genuine tie.

What does separate the arms on the weaker model is **completion**: `semble`'s HARD completion
collapses to **0.306** — two runs in three never finish, looping on semantic hits the weak
model can't synthesise into an answer — while both graph-based arms hold near 0.82. The robust,
model-independent finding is that *graph-structured context degrades gracefully as the driving
model weakens; semantic-only search does not*.

Every real arm clears the no-tools control by a wide margin — graphlens's lift is **+0.31–0.40
HARD**. graphlens's own cost tail is a few *impact/enumeration* tasks (e.g. "which files
construct `Request(...)`") where the model spirals in `search`; these inflate its *mean* token
spend but not its median. Upgrading the engine to `graphlens 0.8.2` (which added Rust
`implementors`, Go `references`, and TypeScript barrel/type-annotation edges the previous
version missed) cut those tails sharply — e.g. `hono_impact_getpath` fell from 486k to 111k
tokens once `getPath`'s callers resolved through the barrel re-export.

### Full breakdown

<details>
<summary>SIMPLE tasks — accuracy · completion · median tokens · median cost</summary>

| arm | model | accuracy | completion | tokens | cost |
|---|---|---|---|---|---|
| graphlens | deepseek-v4-flash | 1.000 | 1.000 | 11,426 | $0.00114 |
| graphlens | glm-4.7-flash | 0.990 | 1.000 | 16,996 | $0.00112 |
| codegraph | deepseek-v4-flash | 0.990 | 1.000 | 9,662 | $0.00090 |
| codegraph | glm-4.7-flash | 1.000 | 0.990 | 10,363 | $0.00074 |
| semble | deepseek-v4-flash | 1.000 | 0.961 | 12,934 | $0.00121 |
| semble | glm-4.7-flash | 0.984 | 0.627 | 9,316 | $0.00070 |
| none | deepseek-v4-flash | 0.600 | 0.980 | 190 | $0.00003 |
| none | glm-4.7-flash | 0.639 | 0.706 | 560 | $0.00019 |

</details>

<details>
<summary>HARD tasks — accuracy · completion · median tokens · median cost</summary>

| arm | model | accuracy | completion | tokens | cost |
|---|---|---|---|---|---|
| graphlens | deepseek-v4-flash | 0.971 | 0.990 | 23,436 | $0.00218 |
| graphlens | glm-4.7-flash | 0.937 | 0.827 | 33,952 | $0.00230 |
| codegraph | deepseek-v4-flash | 0.963 | 1.000 | 23,212 | $0.00214 |
| codegraph | glm-4.7-flash | 0.968 | 0.816 | 29,674 | $0.00198 |
| semble | deepseek-v4-flash | 0.952 | 0.908 | 60,804 | $0.00582 |
| semble | glm-4.7-flash | 0.960 | 0.306 | 17,942 | $0.00132 |
| none | deepseek-v4-flash | 0.665 | 0.969 | 285 | $0.00005 |
| none | glm-4.7-flash | 0.702 | 0.643 | 826 | $0.00029 |

</details>

## Why report accuracy *and* completion separately

A run that never finishes — a turn-limit cutoff, a timeout, the model exhausting its own
output-token budget — is graded as wrong, same as a run that answered confidently and
incorrectly. Reporting only accuracy hides *why* a tool scored low: a wide gap between accuracy
and completion means the tool isn't being out-reasoned so much as it's failing to finish at all.
`semble` on the weaker model is the clearest case: its HARD *accuracy* looks fine (0.960) but
its *completion* is **0.306** — that 0.960 is only over the third of runs that finished, while
two in three looped on a semantic search the model couldn't turn into an answer. A bare accuracy
column would hide that collapse entirely; accuracy and completion have to be read together.

## Statistical significance

Every arm answers the identical task set, so this is a repeated-measures design — the right
significance test is a **paired**, non-parametric one, not an unpaired comparison of pooled
means. The benchmark's [`metrics.ipynb`](https://github.com/Neko1313/graphlens-mcp/blob/main/benchmarks/metrics.ipynb)
runs:

- a **Friedman test** (omnibus): are the four arms different at all, ranked within each task?
- a **Wilcoxon signed-rank test** (pairwise): for graphlens vs. each rival specifically, matched
  by `task_id`, with a rank-biserial effect size alongside the p-value.

graphlens and codegraph are statistically a **tie** on HARD accuracy for both models — the
pairwise Wilcoxon gap is small and not significant in either direction (graphlens edges ahead on
`deepseek-v4-flash`, codegraph on `glm-4.7-flash`). Where the tests bite is graphlens/codegraph
vs. `semble` on the weaker model and every arm vs. the `none` control: those gaps are large and
significant. So the defensible claim from this data is *graph-structured context (graphlens or
codegraph) beats semantic-only search and beats no tools*, not that graphlens beats codegraph —
on these two models they trade the lead. Re-run the notebook for the exact p-values and effect
sizes on your own sweep.

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
echo "OPENROUTER_API_KEY=sk-or-..." > .env   # your OpenRouter key
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
