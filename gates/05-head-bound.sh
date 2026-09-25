#!/usr/bin/env bash
# 05-head-bound.sh — the checked-out bytes ARE the PR head, in the Chronos repository.
# Runs first: git rev-parse HEAD == PR_HEAD_SHA == the PR's headRefOid on GitHub, and gh resolves
# this checkout to Ayyitskevin/Chronos. Later gates may then trust PR_NUMBER/PR_HEAD_SHA as one PR.
# Read-only: `gh repo view` and `gh pr view --json headRefOid`, nothing else.
set -uo pipefail
g=head-bound; repo=Ayyitskevin/Chronos
fail() { echo "FAIL: $g — $1" >&2; exit 1; }
clean() { printf '%s' "$1" | tr -cd '[:print:]' | cut -c1-80; }  # an external value is never more than one printable line
[[ "${PR_NUMBER:-}" =~ ^[0-9]+$ ]] || fail "PR_NUMBER must be set to the PR number by the lane runner"
[[ "${PR_HEAD_SHA:-}" =~ ^[0-9a-f]{40}$ ]] || fail "PR_HEAD_SHA must be a full 40-hex sha set by the lane runner"
top="$(git rev-parse --show-toplevel 2>/dev/null)" || fail "not inside a git work tree; run the gate from the repo root"
cd "$top" || fail "could not enter $top"
head="$(git rev-parse HEAD 2>/dev/null)" || fail "could not read HEAD"
[ "$head" = "$PR_HEAD_SHA" ] || fail "checked-out HEAD $head is not PR_HEAD_SHA $PR_HEAD_SHA; check out the PR head and re-run"
resolved="$(gh repo view --json nameWithOwner -q .nameWithOwner 2>/dev/null)" || resolved="<gh error>"
[ "$resolved" = "$repo" ] || fail "this checkout resolves to '$(clean "$resolved")', not $repo; run the gates in a Chronos clone"
remote="$(gh pr view "$PR_NUMBER" --repo "$repo" --json headRefOid -q .headRefOid 2>/dev/null)" || remote="<gh error>"
[[ "$remote" =~ ^[0-9a-f]{40}$ ]] || fail "could not read #$PR_NUMBER's head sha (got '$(clean "$remote")'); check gh auth/network, then re-run"
[ "$remote" = "$PR_HEAD_SHA" ] || fail "#$PR_NUMBER's head on GitHub is $remote, not PR_HEAD_SHA $PR_HEAD_SHA; re-run on the PR's current head"
echo "PASS: $g — HEAD == PR_HEAD_SHA == #$PR_NUMBER headRefOid $head in $repo"
