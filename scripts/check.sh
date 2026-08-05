#!/usr/bin/env bash
# Full local gate. CI runs exactly these steps, in this order, so a green run
# here means a green run there.
#
#   scripts/check.sh          run everything
#   scripts/check.sh fast     skip the slow integration tests
set -Eeuo pipefail

cd "$(dirname "$0")/.."

PY="${PY:-.venv/bin/python}"
if [[ ! -x "$PY" ]]; then
  PY="$(command -v python3)"
  echo "note: .venv not found, falling back to ${PY}"
fi

MODE="${1:-full}"
FAILED=()

step() {
  local name="$1"; shift
  echo ""
  echo "=== ${name} ==="
  if "$@"; then
    echo "--- ${name}: ok"
  else
    echo "--- ${name}: FAILED"
    FAILED+=("${name}")
  fi
}

step "ruff format" "$PY" -m ruff format --check .
step "ruff lint" "$PY" -m ruff check .
step "mypy" "$PY" -m mypy
step "forbidden patterns" "$PY" scripts/check_forbidden.py
step "version sync" "$PY" scripts/check_versions.py
step "schema validity" "$PY" scripts/check_schemas.py

if [[ "$MODE" == "fast" ]]; then
  step "pytest (unit+contract)" "$PY" -m pytest tests/unit tests/contract
else
  step "pytest" "$PY" -m pytest
fi

if command -v luacheck >/dev/null 2>&1; then
  step "luacheck" luacheck pz-mod tests/lua --config .luacheckrc
else
  echo ""
  echo "=== luacheck ==="
  echo "--- luacheck: skipped (not installed); CI installs it"
fi

# The mod's pure logic is tested under a plain interpreter with no engine
# present. These prove the logic; they prove nothing about engine
# compatibility, which only tests/game-smoke/ against a live session can.
LUA="$(command -v lua5.4 || command -v lua || true)"
if [[ -n "$LUA" ]]; then
  run_lua_tests() {
    local failed=0
    for test in tests/lua/test_*.lua; do
      [[ -e "$test" ]] || continue
      if ! "$LUA" "$test"; then
        failed=1
      fi
    done
    return "$failed"
  }
  step "lua tests" run_lua_tests
else
  echo ""
  echo "=== lua tests ==="
  echo "--- lua tests: skipped (no lua interpreter); CI installs lua5.4"
fi

echo ""
if (( ${#FAILED[@]} )); then
  echo "FAILED: ${FAILED[*]}"
  exit 1
fi
echo "All checks passed."
