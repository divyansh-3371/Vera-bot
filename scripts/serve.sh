#!/usr/bin/env bash
# Restart the bot in the background (single worker; state is in memory). Logs: .cache/logs/server.log
cd "$(dirname "$0")/.." || exit 1
mkdir -p .cache/logs
if [ -f .cache/server.pid ]; then kill "$(cat .cache/server.pid)" 2>/dev/null; sleep 1; fi
VERA_DISK_CACHE=.cache/llm nohup .venv/bin/uvicorn bot:app --host 0.0.0.0 --port "${PORT:-8080}" --workers 1 \
  > .cache/logs/server.log 2>&1 &
echo $! > .cache/server.pid
for _ in $(seq 1 20); do curl -sf "localhost:${PORT:-8080}/v1/healthz" >/dev/null && { echo "bot up (pid $(cat .cache/server.pid))"; exit 0; }; sleep 0.5; done
echo "bot failed to start"; tail -20 .cache/logs/server.log; exit 1
