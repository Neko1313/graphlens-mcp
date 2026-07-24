"""
The agent engine: pydantic-ai + OpenRouter + one MCP toolset per arm.

Lifecycle (amortizes the heavy index load):

    async with ArmSession(arm, project) as session:   # MCP server started ONCE
        for model_key in models:
            for task in tasks:
                for seed in range(N_SEEDS):
                    out = await session.run(model_key, task.prompt)

The stdio MCP process stays warm for the whole (project, arm) sweep, so
graphlens's graph / codegraph's index is loaded a single time, not per task.

Cost is recorded two ways: `cost_exact` from OpenRouter usage accounting (the
real charge, when the provider returns it) and `cost_table` from live per-token
prices — see bench.models.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import dataclass, field
from time import perf_counter
from typing import TYPE_CHECKING, Any

from pydantic_ai import Agent, capture_run_messages
from pydantic_ai.mcp import MCPToolset, StdioTransport
from pydantic_ai.usage import UsageLimits

from bench import config
from bench.arms import CONTROL_SYSTEM_PROMPT, SYSTEM_PROMPT, Arm
from bench.models import MODELS, Price, fetch_pricing
from bench.scoring import (
    ERR_MAX_TURNS,
    ERR_NO_TOOLS,
    ERR_RATE_LIMITED,
    ERR_RUN_ERROR,
    ERR_TIMEOUT,
)

# --- OpenRouter model construction -----------------------------------------

try:  # provider was added in newer pydantic-ai; fall back to OpenAI-compatible base url
    from pydantic_ai.providers.openrouter import OpenRouterProvider

    def _provider() -> Any:
        return OpenRouterProvider(api_key=config.openrouter_api_key())

except ImportError:  # pragma: no cover
    from pydantic_ai.providers.openai import OpenAIProvider

    def _provider() -> Any:
        return OpenAIProvider(
            base_url="https://openrouter.ai/api/v1",
            api_key=config.openrouter_api_key(),
        )


from pydantic_ai.models.openai import (
    OpenAIChatModel,
    OpenAIChatModelSettings,
)

if TYPE_CHECKING:
    from bench.projects import Project


def build_model(model_key: str) -> OpenAIChatModel:
    """Build an OpenRouter-backed model for a registry key."""
    return OpenAIChatModel(MODELS[model_key], provider=_provider())


# --- Per-run result --------------------------------------------------------


@dataclass
class RunOutput:
    answer: str
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cost_exact: float | None = (
        None  # OpenRouter usage accounting, authoritative
    )
    cost_table: float = 0.0  # computed from live per-token prices
    num_turns: int = 0
    n_tool_calls: int = 0
    tool_names: list[str] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    wall_s: float = 0.0

    @property
    def is_error(self) -> bool:
        return self.answer.startswith("__")


def _usage_tokens(usage: Any) -> tuple[int, int, int]:
    """Read tokens across pydantic-ai versions (input/output/total)."""

    def g(*names: str) -> int:
        for n in names:
            v = getattr(usage, n, None)
            if v:
                return int(v)
        return 0

    inp = g("input_tokens", "request_tokens")
    out = g("output_tokens", "response_tokens")
    tot = g("total_tokens") or (inp + out)
    return inp, out, tot


def _exact_cost(usage: Any) -> float | None:
    """
    Return the real charged cost from OpenRouter usage accounting, or None.

    OpenRouter puts the charged cost in usage details when accounting is enabled.
    """
    details = getattr(usage, "details", None)
    if isinstance(details, dict):
        for k in ("cost", "total_cost", "upstream_inference_cost"):
            v = details.get(k)
            if isinstance(v, (int, float)) and v > 0:
                return float(v)
    return None


def _call_args(part: Any) -> Any:
    """Best-effort tool-call arguments as a plain dict (or the raw value)."""
    for attr in ("args_as_dict", "args"):
        val = getattr(part, attr, None)
        if callable(val):
            try:
                return val()
            except Exception:  # noqa: S112 — trace is best-effort
                continue
        if val is not None:
            if isinstance(val, str):
                try:
                    return json.loads(val)
                except (ValueError, TypeError):
                    return val
            return val
    return None


def _ret_size(part: Any) -> int:
    """Approx characters a tool return added to the agent's context."""
    content = getattr(part, "content", None)
    if content is None:
        return 0
    try:
        if isinstance(content, str):
            return len(content)
        return len(json.dumps(content, default=str))
    except Exception:
        return len(str(content))


def _extract_tool_calls(
    messages: list[Any],
) -> tuple[int, list[str], int, list[dict[str, Any]]]:
    """
    (n_calls, names, n_responses, trace) from the message history.

    ``trace`` is one ``{tool, args, ret_chars}`` per call in order, so we can
    see exactly what the agent fetched AND how big each response was — where the
    tokens actually go (e.g. info drilling returning big source each time).
    """
    n_calls = 0
    names: list[str] = []
    n_responses = 0
    trace: list[dict[str, Any]] = []
    returns: dict[str, int] = {}
    for msg in messages:
        parts = getattr(msg, "parts", None)
        if parts is None:
            continue
        if msg.__class__.__name__ == "ModelResponse":
            n_responses += 1
        for part in parts:
            cls = part.__class__.__name__
            if cls == "ToolCallPart":
                n_calls += 1
                name = getattr(part, "tool_name", "?")
                names.append(name)
                trace.append(
                    {
                        "tool": name,
                        "args": _call_args(part),
                        "id": getattr(part, "tool_call_id", None),
                    }
                )
            elif cls == "ToolReturnPart":
                tcid = getattr(part, "tool_call_id", None)
                if tcid is not None:
                    returns[tcid] = _ret_size(part)
    for entry in trace:
        entry["ret_chars"] = returns.get(entry.pop("id", None), 0)
    return n_calls, names, n_responses, trace


def _model_settings(seed: int) -> OpenAIChatModelSettings:
    """Deterministic (temp 0) + per-repeat seed + OpenRouter real-cost accounting."""
    return OpenAIChatModelSettings(
        temperature=0.0,
        seed=seed,
        extra_body={"usage": {"include": True}},
    )


class ArmSession:
    """Holds one warm MCP toolset for a (project, arm) and runs tasks through it."""

    def __init__(
        self, arm: Arm, project: Project, pricing: dict[str, Price]
    ) -> None:
        self.arm = arm
        self.project = project
        self.pricing = pricing
        if arm.is_control:
            # No tools — the model answers from parametric memory.
            self._toolset = None
            self._agent = Agent(system_prompt=CONTROL_SYSTEM_PROMPT, retries=2)
        else:
            spec = arm.serve(project)
            env = dict(spec.env) if spec.env else None
            transport = StdioTransport(
                command=spec.command,
                args=spec.args,
                cwd=spec.cwd,
                env=env,
                # Warmth comes from holding the agent context open for the whole
                # sweep (see __aenter__), NOT from keep_alive — keep_alive=True
                # would leave the subprocess running after the context closes and
                # leak one server per (project, arm).
                keep_alive=False,
            )
            self._toolset = MCPToolset(
                transport,
                id=f"{project.key}:{arm.name}",
                read_timeout=config.MCP_TOOL_TIMEOUT_S,
                # Whether the server's MCP `instructions` are injected into the
                # agent. OFF by default: it must match how the archived rival
                # arms (semble/codegraph) were measured, or the comparison is no
                # longer apples-to-apples — and injecting ~2 KB of guidance
                # measurably destabilises weak models' tool-calling (gpt-oss
                # leaks Harmony channel tokens into tool names; completion fell
                # from 0.98 to 0.40), penalising exactly the weak-model tier the
                # benchmark stresses. graphlens's real gains (flat params, the
                # test filter, direct-caller default, "relations is the answer"
                # in the tool docstrings, compact search output) travel in the
                # tool schema itself, which is always sent regardless. Set
                # BENCH_INCLUDE_INSTRUCTIONS=1 to measure the with-instructions
                # deployment instead.
                include_instructions=config.INCLUDE_INSTRUCTIONS,
                # Generous: a big graph (superset's 182MB) takes a while to load
                # before the server answers the MCP handshake.
                init_timeout=300,
            )
            addendum = arm.instructions(project)
            system_prompt = (
                f"{SYSTEM_PROMPT}\n\n{addendum}" if addendum else SYSTEM_PROMPT
            )
            self._agent = Agent(
                toolsets=[self._toolset],
                system_prompt=system_prompt,
                retries=2,
            )
        self._models: dict[str, OpenAIChatModel] = {}
        self._stack: contextlib.AsyncExitStack | None = None

    async def __aenter__(self) -> ArmSession:
        self._stack = contextlib.AsyncExitStack()
        # Entering the agent starts and keeps the MCP server warm for the sweep.
        await self._stack.enter_async_context(self._agent)
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._stack is not None:
            await self._stack.aclose()
            self._stack = None

    def _model(self, model_key: str) -> OpenAIChatModel:
        if model_key not in self._models:
            self._models[model_key] = build_model(model_key)
        return self._models[model_key]

    async def run(
        self, model_key: str, prompt: str, seed: int = 0
    ) -> RunOutput:
        model = self._model(model_key)
        t0 = perf_counter()
        # capture_run_messages keeps the partial history even when the run raises
        # (e.g. MAX_TURNS), so an errored run still reports its real tool-call count
        # instead of a misleading zero.
        with capture_run_messages() as messages:
            try:
                result = await asyncio.wait_for(
                    self._agent.run(
                        prompt,
                        model=model,
                        model_settings=_model_settings(seed),
                        usage_limits=UsageLimits(
                            request_limit=config.MAX_TURNS
                        ),
                    ),
                    timeout=config.RUN_TIMEOUT_S,
                )
            except Exception as exc:
                n_calls, names, n_resp, trace = _extract_tool_calls(messages)
                return RunOutput(
                    answer=_classify_error(exc),
                    num_turns=n_resp,
                    n_tool_calls=n_calls,
                    tool_names=names,
                    tool_calls=trace,
                    wall_s=perf_counter() - t0,
                )

        wall = perf_counter() - t0
        usage = result.usage
        inp, out, tot = _usage_tokens(usage)
        n_calls, names, n_resp, trace = _extract_tool_calls(
            result.all_messages()
        )
        answer = (result.output or "").strip()
        if not answer:
            answer = ERR_RUN_ERROR
        elif _is_provider_error(answer):
            # OpenRouter sometimes returns an upstream error *as the model's
            # text* ("Connect timeout, please try again later.") on an
            # otherwise-successful call. Scored literally it is a 0 that looks
            # like a wrong answer; caught here it is an error row, excluded
            # from accuracy and re-run on the next pass like any other failure.
            answer = f"{ERR_RUN_ERROR} provider: {answer}"[:200]
        elif n_calls == 0 and not self.arm.is_control:
            # A real arm answered without ever touching a tool — likely from memory,
            # or the MCP server exposed no tools. Either way the comparison is void.
            # (For the control arm, zero tool calls is the whole point — not an error.)
            answer = f"{ERR_NO_TOOLS} {answer}"[:200]

        model_id = MODELS[model_key]
        price = self.pricing.get(model_id)
        cost_table = price.cost(inp, out) if price else 0.0
        # A :free model is charged $0, so its exact cost is meaningless for the
        # comparison — drop it and let the report fall back to cost_table, which
        # is priced at the paid twin's rate (see bench.models.fetch_pricing).
        cost_exact = None if model_id.endswith(":free") else _exact_cost(usage)
        return RunOutput(
            answer=answer,
            input_tokens=inp,
            output_tokens=out,
            total_tokens=tot,
            cost_exact=cost_exact,
            cost_table=cost_table,
            num_turns=n_resp,
            n_tool_calls=n_calls,
            tool_names=names,
            tool_calls=trace,
            wall_s=wall,
        )


# Upstream-error phrasings OpenRouter passes through as model text. Matched
# only against a short output — a real answer is symbol names and paths, never
# a full "please try again later" sentence — so a legitimate answer that
# happens to contain one of these words is not misread as a failure.
_PROVIDER_ERROR_PHRASES = (
    "connect timeout",
    "please try again later",
    "upstream error",
    "internal server error",
    "service unavailable",
    "no endpoints found",
    "rate limit",
)
_PROVIDER_ERROR_MAX_LEN = 200


def _is_provider_error(answer: str) -> bool:
    low = answer.lower()
    return len(answer) <= _PROVIDER_ERROR_MAX_LEN and any(
        phrase in low for phrase in _PROVIDER_ERROR_PHRASES
    )


def _classify_error(exc: Exception) -> str:
    name = exc.__class__.__name__
    text = f"{name}: {exc}".lower()
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return ERR_TIMEOUT
    if (
        "usagelimit" in name.lower()
        or "request_limit" in text
        or "max" in name.lower()
    ):
        return ERR_MAX_TURNS
    if "429" in text or "rate" in text or "quota" in text:
        return ERR_RATE_LIMITED
    return f"{ERR_RUN_ERROR} {name}: {exc}"[:200]


def make_pricing() -> dict[str, Price]:
    return fetch_pricing()
