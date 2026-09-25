#!/usr/bin/env bash
# 70-docs-claims-pinned.sh — the doc-pinning contract tests pass (docs say only what the source proves).
# Runs the EXISTING tests by named selection (none rewritten, skipped or deselected):
# tests/unit/test_limitations_*_contract.py plus the three named files below (listed in gates/README.md).
# A missing named file, an empty glob, or pytest collecting nothing is a FAIL, never green.
# CHRONOS_PY overrides the venv python (the Makefile's PY).
set -uo pipefail
g=docs-claims-pinned
fail() { echo "FAIL: $g — $1" >&2; exit 1; }
top="$(git rev-parse --show-toplevel 2>/dev/null)" || fail "not inside a git work tree; run the gate from the repo root"
cd "$top" || fail "could not enter $top"
py="${CHRONOS_PY:-.venv/bin/python}"
[ -x "$py" ] || fail "no python at $py; create the venv (make's PY) or set CHRONOS_PY"
shopt -s nullglob
set -- tests/unit/test_limitations_*_contract.py
[ "$#" -ge 1 ] || fail "no tests/unit/test_limitations_*_contract.py matched; restore the limitations contract tests"
for f in tests/unit/test_adr_point_in_time_claims.py tests/unit/test_docs_map_skill_contract.py tests/unit/test_vision_completion_plan_prose.py; do
  [ -f "$f" ] || fail "$f is missing; restore it or update the named set here and in gates/README.md"
  set -- "$@" "$f"
done
out="$("$py" -m pytest -q "$@" 2>&1)"
code=$?
summary="$(grep -E ' in [0-9.]+s' <<< "$out" | tail -n 1 | sed -E 's/^=+ //; s/ =+$//')"
[ "$code" -ne 5 ] || fail "pytest collected no tests from the $# named files; the doc claims are unpinned"
[ "$code" -eq 0 ] || fail "pytest exited $code ($summary); run '$py -m pytest -q $*' and fix the doc or the claim"
echo "PASS: $g — $# doc-contract files: $summary"
