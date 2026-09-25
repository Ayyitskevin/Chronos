#!/usr/bin/env bash
# 20-pr-ready.sh — PR must not be a draft.
# The gate REPORTS the state; it never marks ready (Muse's ruling: "request Muse's mark-ready;
# seats never mark ready themselves"). It passes only on a literal `false` from GitHub; an
# unreadable state is its own FAIL, never read as a draft verdict.
set -uo pipefail
if [[ ! "${PR_NUMBER:-}" =~ ^[0-9]+$ ]]; then
  echo "FAIL: pr-ready — PR_NUMBER must be set to the PR number by the lane runner" >&2
  exit 1
fi
if ! draft="$(gh pr view "$PR_NUMBER" --json isDraft -q .isDraft 2>/dev/null)"; then
  draft="<gh error>"
fi
case "$draft" in
  false)
    echo "PASS: pr-ready — #$PR_NUMBER is not a draft" ;;
  true)
    echo "FAIL: pr-ready — #$PR_NUMBER is still a draft — request Muse's mark-ready; seats never mark ready themselves" >&2
    exit 1 ;;
  *)
    echo "FAIL: pr-ready — could not read #$PR_NUMBER's draft state (got '$draft'); check gh auth/network, then re-run" >&2
    exit 1 ;;
esac
