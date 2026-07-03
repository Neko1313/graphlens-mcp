"""
graphlens-benchmarks: A/B harness comparing code-context MCP servers.

Two independent axes held against an identical pydantic-ai agent:

  - **arm**   — which single code-context MCP server provides tools
                (graphlens / semble / codegraph), plus a `none` control
  - **model** — which OpenRouter model answers
                (deepseek-chat-v3 / gemini-2.5-flash / qwen3-8b)

Evaluated across multiple real codebases (Go / Rust / Python / TypeScript), with
deterministic, oracle-verified grading — never an LLM judge, never the tool under
test grading its own output.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
