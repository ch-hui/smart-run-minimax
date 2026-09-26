#!/usr/bin/env bash
# Local regression smoke test — runs every endpoint once and fails on any
# non-2xx response. Assumes the FastAPI service is already running on
# $BASE (default http://127.0.0.1:8000).
#
# Usage:
#   chmod +x scripts/smoke.sh
#   ./scripts/smoke.sh                       # default base
#   BASE=http://47.121.29.106:8000 ./scripts/smoke.sh
#
# Requires: curl, jq (for pretty failure output)

set -euo pipefail

BASE="${BASE:-http://127.0.0.1:8000}"
PY="${PYTHON:-python3}"

red()    { printf "\033[31m%s\033[0m\n" "$*" >&2; }
green()  { printf "\033[32m%s\033[0m\n" "$*"; }
yellow() { printf "\033[33m%s\033[0m\n" "$*"; }

check() {
  local name="$1"; shift
  local body
  if body=$("$@" 2>&1); then
    green "✓ $name"
  else
    red   "✗ $name"
    echo "$body" | "$PY" -m json.tool 2>/dev/null || echo "$body"
    exit 1
  fi
}

echo "Smoke-testing $BASE"
echo

check "/healthz" \
  curl -fsS "$BASE/healthz"

check "POST /hello" \
  curl -fsS -X POST "$BASE/hello" \
    -H "Content-Type: application/json" \
    -d '{"text":"smoke test"}'

check "GET /hello" \
  curl -fsS --get "$BASE/hello" --data-urlencode "text=smoke"

check "GET /history?limit=1" \
  curl -fsS "$BASE/history?limit=1"

check "POST /race/batch" \
  curl -fsS -X POST "$BASE/race/batch" \
    -H "Content-Type: application/json" \
    -d '{"race_names":["2026南京马拉松","2026北京马拉松"],"concurrency":2}'

check "GET /race/batch" \
  curl -fsS --get "$BASE/race/batch" \
    --data-urlencode "race_names=2026武汉马拉松" \
    --data-urlencode "race_names=2026杭州马拉松"

echo
green "ALL GREEN ✓"