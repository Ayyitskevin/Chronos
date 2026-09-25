#!/usr/bin/env bash
# Runs every NN-*.sh gate in order. Exit 0 iff all pass.
set -uo pipefail
dir="$(cd "$(dirname "$0")" && pwd)"
fail=0
for g in "$dir"/[0-9][0-9]-*.sh; do
  if out="$("$g" 2>&1)"; then
    echo "$out"
  else
    echo "$out"
    fail=1
  fi
done
if [ "$fail" -eq 0 ]; then
  echo "ALL GATES PASS"
else
  echo "GATES FAILED" >&2
  exit 1
fi
