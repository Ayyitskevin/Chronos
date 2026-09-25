#!/usr/bin/env bash
# 40-evidence-at-head.sh — test evidence must record the sha it ran on, and it must equal PR_HEAD_SHA.
# Runs the existing `make gates` (adopted, not rewritten) at a clean HEAD, writes the receipt
# .gates/40-evidence-at-head.json = {sha, exit, pytest counts} (gitignored), then judges the receipt
# as read back from disk: PASS iff receipt.sha == PR_HEAD_SHA == HEAD, exit == 0 and counts recorded.
# `make gates` is candidate code: it runs under `env -i` with a fixed allowlist and a throwaway HOME.
set -uo pipefail
g=evidence-at-head
fail() { echo "FAIL: $g — $1" >&2; exit 1; }
top="$(git rev-parse --show-toplevel 2>/dev/null)" || fail "not inside a git work tree; run the gate from the repo root"
cd "$top" || fail "could not enter $top"
[[ "${PR_HEAD_SHA:-}" =~ ^[0-9a-f]{40}$ ]] || fail "PR_HEAD_SHA must be a full 40-hex sha; set it to the PR head"
head="$(git rev-parse HEAD)" || fail "could not read HEAD"
[ "$head" = "$PR_HEAD_SHA" ] || fail "checked-out HEAD $head is not PR_HEAD_SHA $PR_HEAD_SHA; check out the PR head and re-run"
[ -z "$(git status --porcelain --untracked-files=no)" ] || fail "tracked files are modified, so HEAD does not name the tested bytes; commit or stash, then re-run"
mkdir -p .gates || fail "could not create .gates/"
mk="$(command -v make)" || fail "make is not on PATH"
home="$(mktemp -d)" || fail "could not create a temp HOME"; trap 'rm -rf "$home"' EXIT
env -i PATH=/usr/local/bin:/usr/bin:/bin HOME="$home" LANG=C.UTF-8 BROKER_MODE=demo ALLOW_ORDER_TRANSMIT=false ALLOW_LIVE_TRADING=false PYTHONDONTWRITEBYTECODE=1 "$mk" gates > .gates/make-gates.log 2>&1
code=$?
after="$(git rev-parse HEAD)" || fail "could not read HEAD after make gates"
python3 - "$after" "$code" .gates/make-gates.log > .gates/40-evidence-at-head.json <<'PY' || fail "could not write the receipt .gates/40-evidence-at-head.json"
import json, re, sys
sha, code, log = sys.argv[1], int(sys.argv[2]), open(sys.argv[3], errors="replace").read()
pytest = None
for line in log.splitlines():  # pytest's final summary, e.g. "5967 passed, 1 skipped, 28 warnings in 312.4s"
    m = re.fullmatch(r"=*\s*(\d+ \w+(?:, \d+ \w+)*) in [\d.]+s\b.*", line.strip())
    if m:
        n = {w: int(c) for c, w in (p.split(" ", 1) for p in m.group(1).split(", "))}
        pytest = {k: n.get(k, 0) for k in ("passed", "failed", "skipped")}
print(json.dumps({"sha": sha, "exit": code, "pytest": pytest}))
PY
read -r rsha rexit counts < <(python3 -c 'import json,sys; r=json.load(open(sys.argv[1])); p=r["pytest"]
print(r["sha"], r["exit"], "none" if p is None else "%d/%d/%d" % (p["passed"], p["failed"], p["skipped"]))' .gates/40-evidence-at-head.json) \
  || fail "could not read back the receipt .gates/40-evidence-at-head.json"
[ "$rexit" = 0 ] || fail "make gates exited $rexit at $rsha; see .gates/make-gates.log, fix, then re-run"
[ "$rsha" = "$PR_HEAD_SHA" ] && [ "$rsha" = "$head" ] || fail "receipt sha $rsha is not PR_HEAD_SHA $PR_HEAD_SHA (HEAD moved during make gates); re-run at a fixed head"
[ "$counts" != none ] || fail "make gates exited 0 but no pytest summary was recorded in .gates/make-gates.log; the evidence must carry test counts"
IFS=/ read -r np nf ns <<< "$counts"
echo "PASS: $g — make gates exit 0 at $rsha ($np passed, $nf failed, $ns skipped); receipt .gates/40-evidence-at-head.json"
