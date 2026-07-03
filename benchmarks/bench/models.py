"""
OpenRouter model registry + pricing.

Three models span a wide capability/price band so we can see how robustly each
MCP server's tool surface holds up as the driving model gets weaker:

  - deepseek-chat-v3 : strong, cheap open MoE
  - gemini-2.5-flash : fast hosted mid-tier
  - qwen3-8b         : small open model — the stress test for tool-calling

Cost is captured two ways and the run records both:
  1. **exact** — OpenRouter usage accounting returns the real charged cost per
     request (we enable it in the runner); this is authoritative.
  2. **table** — a fallback computed from per-1M-token prices fetched live from
     the OpenRouter models API at run start (see `fetch_pricing`), with a
     hardcoded backstop if the API is unreachable.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

# key -> OpenRouter model id. The key becomes part of result filenames / chart labels.
# Four cheap flash-tier models from four different families (deepseek / google /
# qwen / z-ai) — diversity reduces single-family bias, and the weak-model tier is
# where good code-context tools should help most (largest lift over the `none`
# control). All verified to support function/tool calling on OpenRouter.
MODELS: dict[str, str] = {
    "deepseek-v4-flash": "deepseek/deepseek-v4-flash",
    "gemma-4-26b": "google/gemma-4-26b-a4b-it",
    "qwen3.5-flash": "qwen/qwen3.5-flash-02-23",
    "glm-4.7-flash": "z-ai/glm-4.7-flash",
    # Real (paid) — added for a 3-model comparison; the identical :free id
    # rate-limited too hard for a full sweep (see nemotron-super-120b-free).
    "nemotron-super-120b": "nvidia/nemotron-3-super-120b-a12b",
    # Replaces nemotron-super-120b as the weak-model slot: nemotron's OpenRouter
    # endpoint returned malformed responses (~35% of calls) even at
    # concurrency=1, an upstream provider issue unrelated to tool design.
    "gpt-oss-20b": "openai/gpt-oss-20b",
    # Free-tier variants: run at $0 but priced at their PAID twin's live rate
    # (see fetch_pricing) so the cost comparison stays honest. NOTE: OpenRouter
    # rate-limits :free models hard — expect slow sweeps / daily caps.
    "gpt-oss-120b": "openai/gpt-oss-120b:free",
    "nemotron-super-120b-free": "nvidia/nemotron-3-super-120b-a12b:free",
    "nemotron-ultra-550b": "nvidia/nemotron-3-ultra-550b-a55b:free",
    "gemma-4-31b": "google/gemma-4-31b-it:free",
}


@dataclass(frozen=True)
class Price:
    """USD per 1,000,000 tokens."""

    prompt: float
    completion: float

    def cost(self, input_tokens: int, output_tokens: int) -> float:
        return (
            input_tokens * self.prompt + output_tokens * self.completion
        ) / 1e6


# Backstop prices (USD / 1M tokens) used only if the live API fetch fails. These
# are approximate and provider-dependent — the live fetch overrides them, and the
# exact per-request cost from usage accounting is what we report as headline.
FALLBACK_PRICING: dict[str, Price] = {
    "deepseek/deepseek-v4-flash": Price(prompt=0.09, completion=0.18),
    "google/gemma-4-26b-a4b-it": Price(prompt=0.06, completion=0.33),
    "qwen/qwen3.5-flash-02-23": Price(prompt=0.065, completion=0.26),
    "z-ai/glm-4.7-flash": Price(prompt=0.06, completion=0.40),
    "nvidia/nemotron-3-super-120b-a12b": Price(prompt=0.085, completion=0.40),
    "openai/gpt-oss-20b": Price(prompt=0.029, completion=0.14),
    # :free models keyed by their :free id but priced at the PAID twin's rate
    # (fetched from OpenRouter 2026-07-01). Backstop only; fetch_pricing
    # refreshes these from the live paid-twin price.
    "openai/gpt-oss-120b:free": Price(prompt=0.03, completion=0.15),
    "nvidia/nemotron-3-super-120b-a12b:free": Price(
        prompt=0.085, completion=0.40
    ),
    "nvidia/nemotron-3-ultra-550b-a55b:free": Price(
        prompt=0.50, completion=2.20
    ),
    "google/gemma-4-31b-it:free": Price(prompt=0.12, completion=0.35),
}


def fetch_pricing(timeout: float = 15.0) -> dict[str, Price]:
    """
    Fetch current per-token prices from OpenRouter's public models API.

    Returns a map of model id -> Price for our registry. Falls back to
    FALLBACK_PRICING on any error so a run never blocks on pricing.
    """
    wanted = set(MODELS.values())
    # A :free model reports $0 on OpenRouter, which would zero out its cost in
    # the comparison. Price it at its PAID twin's live rate instead (same id
    # without the ":free" suffix), so free-tier runs are costed as if paid.
    paid_twin_of = {
        mid: mid.removesuffix(":free")
        for mid in wanted
        if mid.endswith(":free")
    }
    twin_ids = set(paid_twin_of.values())
    out: dict[str, Price] = dict(FALLBACK_PRICING)
    try:
        resp = httpx.get(
            "https://openrouter.ai/api/v1/models", timeout=timeout
        )
        resp.raise_for_status()
        for m in resp.json().get("data", []):
            mid = m.get("id")
            if mid not in wanted and mid not in twin_ids:
                continue
            p = m.get("pricing", {})
            # API prices are USD per *single* token (strings); scale to per-1M.
            price = Price(
                prompt=float(p.get("prompt", 0.0)) * 1e6,
                completion=float(p.get("completion", 0.0)) * 1e6,
            )
            # A real (non-free) wanted model: use its own live price.
            if mid in wanted and not mid.endswith(":free"):
                out[mid] = price
            # A paid twin: price every :free model that maps to it.
            for free_id, twin in paid_twin_of.items():
                if mid == twin:
                    out[free_id] = price
    except Exception:  # noqa: S110 — pricing is best-effort; fall back silently
        pass
    return out
