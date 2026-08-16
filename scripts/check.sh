#!/usr/bin/env bash
# Full local gate. CI runs exactly these steps, in this order, over the commit
# it checked out. This script runs them over whatever is on disk, which is the
# same thing only when the tree is clean — so every run says which tree it
# judged, first and last. See scripts/check_tree_identity.py.
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

# Said before the four minutes of tests as well as after them, because a run
# started against the wrong tree is worth abandoning early.
"$PY" scripts/check_tree_identity.py --prefix "about to check"

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
# Asks the historical questions no test run can: did the proof a task names
# exist at the commit the task says proved it. Two seconds over 400 claims, and
# it needs the full history — a shallow clone makes it exit 2 rather than pass,
# which is the honest answer when no historical question is answerable.
step "pass audit" "$PY" scripts/audit_pass.py --quiet
step "playbook in sync" "$PY" scripts/generate_playbook.py --check
step "knowledge docs in sync" "$PY" scripts/generate_knowledge_docs.py --check

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
# Not a bare "All checks passed": a verdict with no subject was read as one
# about the commit that followed, and the commit went red on CI.
"$PY" scripts/check_tree_identity.py --prefix "All checks passed"
