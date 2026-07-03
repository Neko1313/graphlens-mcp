#!/usr/bin/env bash
# Stop a detached run started by run_all.sh and clean up stray MCP servers.
set -uo pipefail
cd "$(dirname "$0")/.."

if [[ -f .bench.pgid ]]; then
  pgid="$(cat .bench.pgid)"
  echo "killing process group $pgid"
  kill -- -"$pgid" 2>/dev/null || true
  rm -f .bench.pgid
fi

# Belt-and-suspenders: reap stray arm servers this benchmark may have spawned.
for pat in "graphlens-mcp serve" "codegraph serve" "mcp-server-filesystem" "semble mcp"; do
  pkill -f "$pat" 2>/dev/null || true
done
echo "stopped"
