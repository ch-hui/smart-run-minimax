#!/usr/bin/env bash
# Convenience launcher — loads .env, then starts uvicorn.
set -euo pipefail

cd "$(dirname "$0")"

if [ ! -f .env ]; then
  echo "ERROR: .env not found. Copy .env.example to .env and set ANTHROPIC_API_KEY first." >&2
  exit 1
fi

# Pick a python that has the dependencies installed.
PY="${PYTHON:-python3}"

exec "$PY" -m uvicorn app:app --host "${HOST:-0.0.0.0}" --port "${PORT:-8000}" --reload