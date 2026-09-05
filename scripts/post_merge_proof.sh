#!/usr/bin/env bash
#
# The executable form of docs/AGENT_PROTOCOL.md §6 — "verify the result rather
# than the badge".
#
#   scripts/post_merge_proof.sh <pr-number> <reviewed-head-sha> <path>...
#
# §6 is a list of commands a human runs and reads. Read is the weak word: the
# phantom merge it was written after was caught because someone looked for the
# documents and they were absent, and every step since has depended on the next
# person looking just as hard at 2am. This script runs the same commands and
# fails on their results, so the proof does not depend on attention.
#
# The paths are passed explicitly, as §6 requires ("do not use unquoted command
# substitution") — and then reconciled against the PR's own file list, because a
# caller who passes too few paths gets a byte-equality proof that is narrower
# than it looks while reading exactly like a full one.
#
# Exits 0 only when every check below passed. Every failure is reported, not
# just the first: a proof that stops at the first bad branch tells you less than
# one that tells you which branches are bad.

set -uo pipefail

readonly SELF="${0##*/}"

if [[ $# -lt 3 ]]; then
  cat >&2 <<USAGE
usage: ${SELF} <pr-number> <reviewed-head-sha> <path>...

  <pr-number>          the merged PR, e.g. 172
  <reviewed-head-sha>  the SHA the reviewer verified (abbreviated is fine)
  <path>...            every path the PR changed, including any pre-rename name

example:
  ${SELF} 172 a4e4a95 docs/SHADOW_CAMPAIGN.md
USAGE
  exit 2
fi

PR="$1"
CANDIDATE="$2"
shift 2
PATHS=("$@")

FAILURES=0

pass() { printf '  ok    %s\n' "$*"; }
fail() { printf '  FAIL  %s\n' "$*"; FAILURES=$((FAILURES + 1)); }
step() { printf '\n== %s\n' "$*"; }

# --------------------------------------------------------------------------
step "0. preflight: fetch, and derive the default branch (§2)"

if ! git fetch origin --quiet; then
  fail "git fetch origin failed; every check below would read a stale remote"
  exit 1
fi

DEFAULT_REF="$(git ls-remote --symref origin HEAD | awk '/^ref:/ {print $2; exit}')"
DEFAULT_BRANCH="${DEFAULT_REF#refs/heads/}"
if [[ -z "${DEFAULT_BRANCH}" ]]; then
  fail "could not derive the default branch from git ls-remote --symref origin HEAD"
  exit 1
fi
pass "default branch is ${DEFAULT_BRANCH} (derived, not assumed)"

REPO="$(gh repo view --json nameWithOwner -q .nameWithOwner 2>/dev/null)"
if [[ -z "${REPO}" ]]; then
  fail "could not derive <owner>/<repo> from gh repo view"
  exit 1
fi
pass "repository is ${REPO}"

# --------------------------------------------------------------------------
step "1. the PR's own identities (§6.1)"

IDENTITIES="$(gh pr view "${PR}" --json baseRefName,headRefOid,mergeCommit,state \
  --jq '[.state, .baseRefName, .headRefOid, (.mergeCommit.oid // "null")] | @tsv' 2>/dev/null)"
if [[ -z "${IDENTITIES}" ]]; then
  fail "gh pr view ${PR} returned nothing; cannot prove anything about this PR"
  exit 1
fi
IFS=$'\t' read -r STATE BASE HEAD_OID MERGE_SHA <<<"${IDENTITIES}"

[[ "${STATE}" == "MERGED" ]] \
  && pass "state is MERGED" \
  || fail "state is ${STATE}, not MERGED"

[[ "${BASE}" == "${DEFAULT_BRANCH}" ]] \
  && pass "base is ${BASE}, the derived default" \
  || fail "base is ${BASE}, not the derived default ${DEFAULT_BRANCH}"

if [[ "${HEAD_OID}" == "${CANDIDATE}"* ]]; then
  pass "headRefOid ${HEAD_OID} matches the reviewed SHA ${CANDIDATE}"
else
  fail "headRefOid is ${HEAD_OID}, not the reviewed SHA ${CANDIDATE} — the merged candidate is not the one that was reviewed"
fi

if [[ "${MERGE_SHA}" == "null" || -z "${MERGE_SHA}" ]]; then
  fail "mergeCommit.oid is null; there is no merge result to prove"
  printf '\n%s: %d check(s) failed\n' "${SELF}" "${FAILURES}"
  exit 1
fi
pass "mergeCommit.oid is ${MERGE_SHA}"

# --------------------------------------------------------------------------
step "2. the merge result is on ${DEFAULT_BRANCH} (§6.2)"

if git merge-base --is-ancestor "${MERGE_SHA}" "origin/${DEFAULT_BRANCH}" 2>/dev/null; then
  pass "${MERGE_SHA} is an ancestor of origin/${DEFAULT_BRANCH}"
else
  fail "${MERGE_SHA} is NOT an ancestor of origin/${DEFAULT_BRANCH} — the result is stranded"
fi

# --------------------------------------------------------------------------
step "3. did the merge preserve the reviewed candidate's identity? (§6.3)"

# The PR's own file list, including pre-rename names, exactly as §6 derives it.
API_FILES="$(gh api "repos/${REPO}/pulls/${PR}/files" --paginate \
  --jq '.[] | [.filename, .status, (.previous_filename // "")] | @tsv' 2>/dev/null)"
if [[ -z "${API_FILES}" ]]; then
  fail "could not derive the PR's path set from the API; the byte-equality proof below would be unanchored"
fi

API_PATHS=()
while IFS=$'\t' read -r name status previous; do
  [[ -n "${name}" ]] && API_PATHS+=("${name}")
  [[ -n "${previous}" ]] && API_PATHS+=("${previous}")
done <<<"${API_FILES}"

MISSING=()
for api_path in "${API_PATHS[@]}"; do
  found=0
  for given in "${PATHS[@]}"; do
    [[ "${given}" == "${api_path}" ]] && found=1 && break
  done
  [[ "${found}" -eq 0 ]] && MISSING+=("${api_path}")
done
if [[ "${#MISSING[@]}" -eq 0 ]]; then
  pass "the ${#PATHS[@]} path(s) given cover all ${#API_PATHS[@]} path(s) the PR touched"
else
  fail "the PR touched paths that were not passed to this script, so any equality proved below is narrower than it reads: ${MISSING[*]}"
fi

if git merge-base --is-ancestor "${HEAD_OID}" "${MERGE_SHA}" 2>/dev/null; then
  pass "identity preserved: ${HEAD_OID} is an ancestor of the merge result (merge or fast-forward)"
  printf '        §6.3 asks for byte equality only where identity was rewritten, so it is not\n'
  printf '        checked here: a merge commit may legitimately carry other changes to these paths.\n'
else
  pass "identity rewritten (squash or rebase) — as §6.3 expects; proving final-state equality instead"
  if git diff --exit-code "${HEAD_OID}" "${MERGE_SHA}" -- "${PATHS[@]}" >/dev/null 2>&1; then
    pass "byte-for-byte equal on the given path(s): git diff --exit-code ${HEAD_OID} ${MERGE_SHA} -- ${PATHS[*]}"
  else
    fail "the merge result differs from the reviewed candidate on: $(git diff --name-only "${HEAD_OID}" "${MERGE_SHA}" -- "${PATHS[@]}" 2>/dev/null | tr '\n' ' ')"
  fi
fi

# --------------------------------------------------------------------------
step "4. CI at the merge result, not at the candidate (§6.4)"

# gh 2.46 has no `gh pr checks --json`; check-runs are read through the API by
# SHA, which is also the only form that can name the *merge result* rather than
# whatever the PR's head last ran.
CHECKS="$(gh api "repos/${REPO}/commits/${MERGE_SHA}/check-runs" --paginate \
  --jq '.check_runs[] | [.name, .status, (.conclusion // "null")] | @tsv' 2>/dev/null)"
if [[ -z "${CHECKS}" ]]; then
  fail "no check runs at ${MERGE_SHA}; a green run on the candidate is not proof about the result"
else
  while IFS=$'\t' read -r name status conclusion; do
    [[ -z "${name}" ]] && continue
    case "${conclusion}" in
      success | neutral | skipped)
        pass "check '${name}': ${conclusion}"
        ;;
      null)
        fail "check '${name}': not concluded (status ${status})"
        ;;
      *)
        fail "check '${name}': ${conclusion}"
        ;;
    esac
  done <<<"${CHECKS}"
fi

# --------------------------------------------------------------------------
step "5. the claimed files are as claimed, at the merge result (§6.5)"

while IFS=$'\t' read -r name status previous; do
  [[ -z "${name}" ]] && continue
  if [[ "${status}" == "removed" ]]; then
    if git cat-file -e "${MERGE_SHA}:${name}" 2>/dev/null; then
      fail "${name} was reported deleted but is present at ${MERGE_SHA}"
    else
      pass "${name} is absent (deleted), as claimed"
    fi
  else
    if git cat-file -e "${MERGE_SHA}:${name}" 2>/dev/null; then
      pass "${name} is present (${status}), as claimed"
    else
      fail "${name} was reported ${status} but is ABSENT at ${MERGE_SHA} — the phantom-merge signature"
    fi
  fi
  if [[ -n "${previous}" ]]; then
    if git cat-file -e "${MERGE_SHA}:${previous}" 2>/dev/null; then
      fail "${previous} was renamed to ${name} but still exists at ${MERGE_SHA}"
    else
      pass "${previous} is absent, as a rename to ${name} requires"
    fi
  fi
done <<<"${API_FILES}"

# --------------------------------------------------------------------------
step "6. duplicated row IDs in the table documents (§6.6)"

for table in DECISIONS.md RISK_REGISTER.md; do
  if ! git cat-file -e "${MERGE_SHA}:${table}" 2>/dev/null; then
    fail "${table} is absent at ${MERGE_SHA}; the duplicate-ID scan cannot run"
    continue
  fi
done

# One combined stream over both documents, matching the full ID cell — a looser
# pattern false-positives on the historical `R-nn-orig` rows.
DUPES="$( { git show "${MERGE_SHA}:DECISIONS.md" 2>/dev/null; git show "${MERGE_SHA}:RISK_REGISTER.md" 2>/dev/null; } \
  | grep -oE '^\| [DR]-[0-9]+[^ |]*' | sort | uniq -d )"
if [[ -z "${DUPES}" ]]; then
  pass "no duplicated row IDs in DECISIONS.md or RISK_REGISTER.md at ${MERGE_SHA}"
else
  fail "duplicated row IDs at ${MERGE_SHA}: $(echo "${DUPES}" | tr '\n' ' ')"
fi

# --------------------------------------------------------------------------
printf '\n'
if [[ "${FAILURES}" -eq 0 ]]; then
  printf '%s: PR #%s merge proof PASSED — candidate %s, merge result %s on %s\n' \
    "${SELF}" "${PR}" "${HEAD_OID}" "${MERGE_SHA}" "${DEFAULT_BRANCH}"
  exit 0
fi
printf '%s: PR #%s merge proof FAILED — %d check(s) failed\n' "${SELF}" "${PR}" "${FAILURES}"
exit 1
