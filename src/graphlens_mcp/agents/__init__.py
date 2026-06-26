"""Per-agent MCP config registry (de/registration of the graphlens server)."""

from graphlens_mcp.agents.base import (
    SERVER_KEY,
    AgentSpec,
    configure,
    deregister,
)
from graphlens_mcp.agents.registry import REGISTRY

__all__ = ["REGISTRY", "SERVER_KEY", "AgentSpec", "configure", "deregister"]
