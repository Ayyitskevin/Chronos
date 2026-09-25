#!/usr/bin/env bash
# 30-base-fresh.sh — PR base must equal origin/main at verification time.
# baseRefOid is read through GitHub's GraphQL API: `gh pr view --json baseRefOid` is not
# available in every gh release (2.46.0 refuses it), and this is the same field.
# Its only write is `git fetch origin main`.
set -uo pipefail
clean() { printf '%s' "$1" | tr -cd '[:print:]' | cut -c1-80; }  # one printable line, whatever gh prints
if [[ ! "${PR_NUMBER:-}" =~ ^[0-9]+$ ]]; then
  echo "FAIL: base-fresh — PR_NUMBER must be set to the PR number by the lane runner" >&2
  exit 1
fi
if ! top="$(git rev-parse --show-toplevel 2>/dev/null)"; then
  echo "FAIL: base-fresh — not inside a git work tree; run the gate from the repo root" >&2
  exit 1
fi
cd "$top" || exit 1
if ! git fetch origin main --quiet 2>/dev/null; then
  echo "FAIL: base-fresh — could not fetch origin main; check the remote/network, then re-run" >&2
  exit 1
fi
query='query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){pullRequest(number:$number){baseRefOid}}}'
if ! base="$(gh api graphql -F owner=Ayyitskevin -F name=Chronos -F number="$PR_NUMBER" -f query="$query" -q .data.repository.pullRequest.baseRefOid 2>/dev/null)" \
    || [[ ! "$base" =~ ^[0-9a-f]{40}$ ]]; then
  echo "FAIL: base-fresh — could not read #$PR_NUMBER's base sha (got '$(clean "${base:-}")'); check gh auth/network, then re-run" >&2
  exit 1
fi
main="$(git rev-parse origin/main)"
if [ "$base" = "$main" ]; then
  echo "PASS: base-fresh — PR base $base == origin/main"
else
  echo "FAIL: base-fresh — PR base $base is behind origin/main $main; rebase and re-verify" >&2
  exit 1
fi
