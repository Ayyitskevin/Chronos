#!/usr/bin/env bash
# Runs every NN-*.sh gate in order. Exit 0 iff all pass.
# Trust model (gates/README.md): the lane runs THIS driver and the gates from a trusted checkout
# (GATES_TRUSTED_DIR, default this script's own directory) with the candidate tree as the working
# directory. A candidate gates/NN-*.sh absent from the trusted set is reported and never executed.
# A gate passes only if it exits 0 AND prints exactly one `PASS: ` line; anything else fails the run.
set -uo pipefail
dir="$(cd "${GATES_TRUSTED_DIR:-$(dirname "$0")}" 2>/dev/null && pwd)"
fail=0
if [ -z "$dir" ]; then
  echo "FAIL: run-all — GATES_TRUSTED_DIR '${GATES_TRUSTED_DIR:-}' is not a directory; point it at the trusted gates/" >&2
  echo "GATES FAILED" >&2
  exit 1
fi
if top="$(git rev-parse --show-toplevel 2>/dev/null)"; then
  for c in "$top"/gates/[0-9][0-9]-*.sh; do
    [ -e "$c" ] || continue
    if [ ! -e "$dir/${c##*/}" ]; then
      echo "FAIL: run-all — gates/${c##*/} is not in the trusted gate set; not run (it takes effect once merged to the trusted base)"
      fail=1
    fi
  done
fi
ran=0
for g in "$dir"/[0-9][0-9]-*.sh; do
  [ -e "$g" ] || continue
  ran=$((ran+1))
  if out="$("$g" 2>&1)" && [ "$(printf '%s\n' "$out" | wc -l)" = 1 ] && [[ "$out" == "PASS: "* ]]; then
    echo "$out"
  else
    echo "$out"
    [[ "$out" == "FAIL: "* ]] || echo "FAIL: run-all — ${g##*/} did not report exactly one PASS line with exit 0; treated as failed"
    fail=1
  fi
done
if [ "$ran" -eq 0 ]; then
  echo "FAIL: run-all — no gates found in $dir; an empty gate set is never a pass"
  fail=1
fi
if [ "$fail" -eq 0 ]; then
  echo "ALL GATES PASS"
else
  echo "GATES FAILED" >&2
  exit 1
fi
