#!/usr/bin/env bash
# 10-current-state-fresh.sh — docs/generated/CURRENT_STATE.md must be fresh.
# Regenerates the page from its inputs and requires no diff. A fresh tree is left exactly as found.
# Every failure, including `make` itself failing, is one FAIL line on stderr and exit 1.
set -uo pipefail
if ! top="$(git rev-parse --show-toplevel 2>/dev/null)"; then
  echo "FAIL: current-state-fresh — not inside a git work tree; run the gate from the repo root" >&2
  exit 1
fi
cd "$top" || exit 1
if ! make current-state >/dev/null 2>&1; then
  echo "FAIL: current-state-fresh — 'make current-state' failed; run it by hand, fix the error, then re-run the gate" >&2
  exit 1
fi
if git diff --quiet -- docs/generated/CURRENT_STATE.md; then
  echo "PASS: current-state-fresh — generated page matches state inputs"
else
  echo "FAIL: current-state-fresh — docs/generated/CURRENT_STATE.md is stale; run 'make current-state' and commit it" >&2
  exit 1
fi
