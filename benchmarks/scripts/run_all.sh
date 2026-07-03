#!/usr/bin/env bash
# Launch the full benchmark detached, auto-resuming across rate-limit / crash
# windows. Logs to bench_overnight.log; stop with scripts/stop.sh.
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ -z "${OPENROUTER_API_KEY:-}" ]] && [[ ! -f .env ]]; then
  echo "OPENROUTER_API_KEY not set and no .env present. Aborting." >&2
  exit 1
fi

ARGS="$*"
setsid bash -c "until uv run main.py ${ARGS}; do echo '--- resume in 60s ---'; sleep 60; done" \
  > bench_overnight.log 2>&1 &
echo $! > .bench.pgid
echo "started (pgid $(cat .bench.pgid)); tail -f bench_overnight.log"
