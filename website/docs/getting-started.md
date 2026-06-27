---
id: getting-started
title: Getting started
sidebar_position: 2
---

# Getting started

## Install

Requires **Python ≥ 3.13** (a constraint inherited from `graphlens`).

```bash
uv tool install graphlens-mcp      # or: pipx install graphlens-mcp
```

Python language analysis works out of the box (the `ty` type engine ships as a dependency).
Other languages parse immediately and unlock full cross-file semantics once their toolchain
is present (Node for TypeScript, the Go toolchain, etc.); without it that language is
reported as `degraded` rather than blocking `init`.

## Quickstart (two commands)

```bash
uv tool install graphlens-mcp        # 1. install
cd your-project && graphlens-mcp init  # 2. index + configure your agent
```

`init` detects the project's languages, indexes the code into a local graph, writes the MCP
server entry into your agent's config and installs the navigation skill. You do **not** run
`serve` yourself — your agent launches it from the config. Restart the agent and ask it
something like *"what breaks if I change the signature of `create_order`?"*.

## Configuring agents

`init` writes the MCP server entry into each selected agent's config. Pick agents
non-interactively with repeatable `--agent` flags, or let the interactive selector / agent
detection choose:

```bash
graphlens-mcp init --agent claude_code --agent cursor
graphlens-mcp init --yes          # accept detected agents, no prompt
graphlens-mcp init --no-agent     # index only, don't touch any agent config
```

Supported agents: Claude Code, Cursor, Windsurf, VS Code (Copilot), Codex CLI. Each stores
MCP config in its own format/location; `graphlens-mcp` knows them all and writes idempotently
without disturbing your other servers.

## Where the graph lives

The graph lives at `<project>/.graphlens/graph.db` (SQLite). It is a regenerable cache — safe
to delete; `reindex` rebuilds it. Add `.graphlens/` to your VCS ignore (the bundled `init`
flow assumes it is not committed).

## Detaching

```bash
graphlens-mcp remove              # deregister the server from your agents
graphlens-mcp remove --purge-db   # also delete the local .graphlens/ cache
```
