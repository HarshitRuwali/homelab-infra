#!/bin/bash
# Daily memory ingestion.
#
# Runs scripts/ingest_daily_data.py against the memory API, logs the run, and
# reports a one-line result on stdout so a scheduler can deliver it.
#
# Locates the repository from its own path, so the checkout can live anywhere
# and a scheduler can invoke this script directly:
#   /path/to/open-memory-stack/scripts/ingest_daily.sh
#
# Override the interpreter with OPEN_MEMORY_PYTHON if the app venv is elsewhere.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${OPEN_MEMORY_PYTHON:-$REPO_ROOT/api-service/.venv/bin/python}"
INGEST_SCRIPT="$REPO_ROOT/scripts/ingest_daily_data.py"
LOG_DIR="$REPO_ROOT/logs"
LOG_FILE="$LOG_DIR/ingest_$(date +%Y-%m-%d_%H-%M-%S).log"

if [ ! -x "$PYTHON_BIN" ]; then
    echo "No interpreter at $PYTHON_BIN — run 'uv sync' in api-service/, or set OPEN_MEMORY_PYTHON" >&2
    exit 1
fi

mkdir -p "$LOG_DIR"

# `|| EXIT_CODE=$?` keeps `set -e` from aborting here before the checks below,
# which is the whole point of running the pipeline under a timeout.
EXIT_CODE=0
timeout 300 "$PYTHON_BIN" "$INGEST_SCRIPT" > "$LOG_FILE" 2>&1 || EXIT_CODE=$?

if [ "$EXIT_CODE" -eq 0 ]; then
    echo "Daily memory ingestion completed successfully"
    tail -1 "$LOG_FILE"   # the JSON result line
    exit 0
elif [ "$EXIT_CODE" -eq 124 ]; then
    echo "Daily memory ingestion timed out (>5 min)"
    echo "Log: $LOG_FILE"
    tail -20 "$LOG_FILE"
    exit 1
else
    echo "Daily memory ingestion failed with code $EXIT_CODE"
    echo "Log: $LOG_FILE"
    tail -20 "$LOG_FILE"
    exit 1
fi
