#!/usr/bin/env bash
# GATES-1 harness: pins the gate-script contract (gates/README.md) for 05/10/20/30 and run-all.sh.
# Checks are numbered by the GATES-1 contract (1-4) or, from GATES-1r1, `r1:<item> <finding>`.
# A stub `gh` sits first on PATH (it logs every call under the temp dir and never touches GitHub);
# gate 30 runs in a throwaway git repo whose `origin` is a local bare repo (no network); gate 10 runs
# in this repo and the trap restores docs/ whatever happens. Numbered by the GATES-1 contract.
set -u
ROOT=$(cd "$(dirname "$0")/../.." && pwd); G=$ROOT/gates; pass=0; fail=0
T=$(mktemp -d)
trap 'git -C "$ROOT" checkout -q -- docs 2>/dev/null; rm -rf "$T"' EXIT
ok() { pass=$((pass+1)); echo "ok       $1"; }; bad() { fail=$((fail+1)); echo "FAIL     $1"; }
check() { if eval "$2"; then ok "$1"; else bad "$1"; fi; }
run() { "$@" > "$T/out" 2> "$T/err"; echo $? > "$T/rc"; }  # a gate's stdout, stderr and exit code
one() { [ "$(wc -l < "$1")" = 1 ] && grep -qE "$2" "$1"; }   # exactly one line, matching
rc() { [ "$(cat "$T/rc")" = "$1" ]; }
passed() { rc 0 && one "$T/out" "^PASS: $1 — " && [ ! -s "$T/err" ]; }
failed() { rc 1 && one "$T/err" "^FAIL: $1 — $2" && [ ! -s "$T/out" ]; }

mkdir "$T/bin"; cat > "$T/bin/gh" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$STUB_LOG"
case "$1 $2 $*" in  # printf %b lets a case plant a newline in gh's output (K3 P2-1)
  "pr view "*headRefOid*) [ "${STUB_HEAD:-}" = ERROR ] && exit 1; printf '%b\n' "${STUB_HEAD:-}" ;;
  "pr view "*)    [ "${STUB_DRAFT:-}" = ERROR ] && exit 1; printf '%b\n' "${STUB_DRAFT:-}" ;;
  "repo view "*)  [ "${STUB_REPO-}" = ERROR ] && exit 1; printf '%b\n' "${STUB_REPO-Ayyitskevin/Chronos}" ;;
  "api graphql "*) [ "${STUB_BASE:-}" = ERROR ] && exit 1; printf '%b\n' "${STUB_BASE:-}" ;;
  *) echo "stub gh: unexpected call: $*" >&2; exit 97 ;;
esac
STUB
chmod +x "$T/bin/gh"; export PATH="$T/bin:$PATH" STUB_LOG="$T/gh.log"; : > "$STUB_LOG"

# --- 20 pr-ready -----------------------------------------------------------------------------
G20="$G/20-pr-ready.sh"
STUB_DRAFT=false PR_NUMBER=262 run bash "$G20"
check "1 20 PASS: not a draft → one PASS line on stdout, nothing on stderr, exit 0" "passed pr-ready"
STUB_DRAFT=true PR_NUMBER=262 run bash "$G20"
check "1 20 FAIL: a draft → one FAIL line on stderr, exit 1" "failed pr-ready '#262 is still a draft'"
check "2 20 FAIL remediation routes through lane authority (Muse's mark-ready), never instructs the seat" "grep -qF \"request Muse's mark-ready; seats never mark ready themselves\" $T/err"
for bad_state in ERROR garbage ''; do
  STUB_DRAFT="$bad_state" PR_NUMBER=262 run bash "$G20"
  check "1 20 FAIL: an unreadable draft state ('$bad_state') is a FAIL naming it, never a draft verdict" "failed pr-ready 'could not read #262' && ! grep -q 'still a draft' $T/err"
done
PR_NUMBER= run bash "$G20"; check "1 20 FAIL: PR_NUMBER unset → one FAIL line" "failed pr-ready 'PR_NUMBER'"
PR_NUMBER='1;x' run bash "$G20"; check "1 20 FAIL: a non-numeric PR_NUMBER → one FAIL line" "failed pr-ready 'PR_NUMBER'"
check "2 gate 20 made only read queries (every gh call is 'pr view <N> --repo Ayyitskevin/Chronos --json isDraft …'; the log is non-empty)" "[ -s $STUB_LOG ] && ! grep -vE '^pr view [0-9]+ --repo Ayyitskevin/Chronos --json isDraft' $STUB_LOG"
STUB_DRAFT='true\nINJECTED' PR_NUMBER=262 run bash "$G20"
check "r1:1 P2-1 20 FAIL: newline-carrying gh output (K3 P2-1) is still exactly one FAIL line, never a draft verdict" "failed pr-ready 'could not read #262' && ! grep -q 'still a draft' $T/err"
check "2 no gate can mark ready or write to GitHub (no 'gh pr ready', --ready, -X/--method, gh pr edit/merge/close; ≥3 gates scanned)" "[ \"\$(ls $G/[0-9][0-9]-*.sh 2>/dev/null | wc -l)\" -ge 3 ] && ! grep -rnE 'gh pr (ready|edit|merge|close)|--ready|(^|[[:space:]])(-X|--method)[[:space:]]' $G"

# --- 30 base-fresh (a throwaway repo with a local bare origin) --------------------------------
git init -q --bare "$T/origin.git"; git clone -q "$T/origin.git" "$T/repo" 2>/dev/null
( cd "$T/repo" && git checkout -q -b main && git -c user.email=t@t -c user.name=t commit -q --allow-empty -m one && git push -q origin main \
  && git -c user.email=t@t -c user.name=t commit -q --allow-empty -m two && git push -q origin main )
OLD=$(git -C "$T/repo" rev-parse HEAD~1); NEW=$(git -C "$T/repo" rev-parse HEAD)
G30="$G/30-base-fresh.sh"
( cd "$T/repo" && STUB_BASE=$NEW PR_NUMBER=7 run bash "$G30" ); check "1 30 PASS: base == origin/main → one PASS line, exit 0" "passed base-fresh"
( cd "$T/repo" && STUB_BASE=$OLD PR_NUMBER=7 run bash "$G30" ); check "1 30 FAIL: base behind origin/main → one FAIL line naming both shas, exit 1" "failed base-fresh 'PR base $OLD is behind origin/main $NEW'"
for bad_base in ERROR not-a-sha ''; do
  ( cd "$T/repo" && STUB_BASE="$bad_base" PR_NUMBER=7 run bash "$G30" )
  check "1 30 FAIL: an unreadable base ('$bad_base') is a FAIL naming it" "failed base-fresh 'could not read #7'"
done
( cd "$T/repo" && STUB_BASE='abc123\nINJECTED' PR_NUMBER=7 run bash "$G30" )
check "r1:1 P2-1 30 FAIL: newline-carrying gh output (K3 P2-1) is still exactly one FAIL line" "failed base-fresh 'could not read #7'"
git -C "$T/repo" remote set-url origin "$T/missing.git"
( cd "$T/repo" && STUB_BASE=$NEW PR_NUMBER=7 run bash "$G30" ); check "1 30 FAIL: origin main cannot be fetched → one FAIL line" "failed base-fresh 'could not fetch origin main'"
check "r1:2 P1-1 30 queries the Chronos repository explicitly (graphql owner=Ayyitskevin name=Chronos, never the {owner}/{repo} inference)" "grep -q 'api graphql -F owner=Ayyitskevin -F name=Chronos' $STUB_LOG && ! grep -qF '{owner}' $STUB_LOG"
check "3 gate 30 leaves its repo's work tree unchanged (its only write is git fetch)" "[ -z \"\$(git -C $T/repo status --porcelain)\" ]"

# --- 05 head-bound (a throwaway repo; the stub gh answers repo view + headRefOid) --------------
G05="$G/05-head-bound.sh"; R05="$T/r05"; mkdir "$R05"; git -C "$R05" init -q
git -C "$R05" -c user.email=t@t -c user.name=t commit -q --allow-empty -m one
H05=$(git -C "$R05" rev-parse HEAD); ZERO=$(printf '%040d' 0); OTHER=$(printf '%040d' 0 | tr 0 b)
g05() { (cd "$R05" && run bash "$G05"); }
check "r1:2 P1-1 05-head-bound.sh exists and is the FIRST gate in run order" "[ \"\$(ls $G/[0-9][0-9]-*.sh | head -n 1)\" = $G05 ]"
: > "$STUB_LOG"; STUB_HEAD=$H05 PR_NUMBER=7 PR_HEAD_SHA=$H05 g05
check "r1:2 P1-1 05 PASS: HEAD == PR_HEAD_SHA == headRefOid, repo resolves to Ayyitskevin/Chronos → one PASS line, exit 0" "passed head-bound"
check "r1:2 P1-1 05 made exactly the two read queries (repo view; pr view --repo Ayyitskevin/Chronos --json headRefOid)" "[ \"\$(cut -d' ' -f1-7 $STUB_LOG)\" = 'repo view --json nameWithOwner -q .nameWithOwner
pr view 7 --repo Ayyitskevin/Chronos --json headRefOid' ]"
STUB_HEAD=$ZERO PR_NUMBER=7 PR_HEAD_SHA=$ZERO g05
check "r1:2 P1-1 05 FAIL: Daybreak's PR_HEAD_SHA=000… probe — the checkout is not that head → one FAIL line" "failed head-bound 'checked-out HEAD $H05 is not PR_HEAD_SHA $ZERO'"
STUB_HEAD=$OTHER PR_NUMBER=7 PR_HEAD_SHA=$H05 g05
check "r1:2 P1-1 05 FAIL: the PR's head on GitHub is a different commit (borrowed metadata) → FAIL naming both" "failed head-bound \"#7's head on GitHub is $OTHER, not PR_HEAD_SHA $H05\""
STUB_REPO=someone/Fork STUB_HEAD=$H05 PR_NUMBER=7 PR_HEAD_SHA=$H05 g05
check "r1:2 P1-1 05 FAIL: the checkout resolves to another repository → FAIL naming it" "failed head-bound \"this checkout resolves to 'someone/Fork', not Ayyitskevin/Chronos\""
STUB_REPO=ERROR STUB_HEAD=$H05 PR_NUMBER=7 PR_HEAD_SHA=$H05 g05
check "r1:2 P1-1 05 FAIL: gh cannot resolve the repository → FAIL, never a pass" "failed head-bound \"this checkout resolves to '<gh error>'\""
for bad_head in ERROR 'abc\nINJECTED' ''; do
  STUB_HEAD="$bad_head" PR_NUMBER=7 PR_HEAD_SHA=$H05 g05
  check "r1:2 P1-1 05 FAIL: an unreadable headRefOid ('$bad_head') is one FAIL line, never a pass" "failed head-bound \"could not read #7's head sha\""
done
PR_NUMBER= PR_HEAD_SHA=$H05 g05; check "r1:2 P1-1 05 FAIL: PR_NUMBER unset → FAIL" "failed head-bound 'PR_NUMBER must be set'"
PR_NUMBER=7 PR_HEAD_SHA=abc123 g05; check "r1:2 P1-1 05 FAIL: PR_HEAD_SHA not a full 40-hex sha → FAIL" "failed head-bound 'PR_HEAD_SHA must be a full 40-hex sha'"

# --- 10 current-state-fresh (read-only --check; this repo, and a throwaway clone for the probes) --
export BROKER_MODE=demo ALLOW_ORDER_TRANSMIT=false ALLOW_LIVE_TRADING=false PYTHONDONTWRITEBYTECODE=1
G10="$G/10-current-state-fresh.sh"
docs_sum() { (cd "$ROOT" && git status --porcelain -- docs; find docs/generated -type f -exec sha256sum {} + | sort); }
check "3 precondition: docs/ is clean before gate 10 runs" "[ -z \"\$(git -C $ROOT status --porcelain -- docs)\" ]"
before=$(docs_sum); (cd "$ROOT" && run bash "$G10")
check "1 10 PASS: a fresh page → one PASS line, exit 0" "passed current-state-fresh"
check "3 10 leaves the tree exactly as it found it when fresh (docs/ status and docs/generated bytes unchanged)" "[ \"\$(docs_sum)\" = \"\$before\" ]"
printf '\nnoise\n' >> "$ROOT/docs/generated/CURRENT_STATE.md"; before=$(docs_sum); (cd "$ROOT" && run bash "$G10")
check "r1:2 P1-3 10 FAIL: a dirtied page is stale (the read-only --check never regenerates it) → one FAIL line naming the page" "failed current-state-fresh 'docs/generated/CURRENT_STATE.md is stale'"
check "r1:2 P1-3 10 wrote nothing on that FAIL path (docs/ status and bytes exactly as the case left them)" "[ \"\$(docs_sum)\" = \"\$before\" ]"
git -C "$ROOT" checkout -q -- docs
printf '\nstale input\n' >> "$ROOT/docs/VISION_COMPLETION_PLAN.md"; before=$(docs_sum); (cd "$ROOT" && run bash "$G10")
check "1 10 FAIL: a changed page INPUT with no regeneration → one FAIL line naming the page, exit 1" "failed current-state-fresh 'docs/generated/CURRENT_STATE.md is stale'"
check "r1:2 P1-3 10 the stale-input FAIL leaves docs/generated untouched (K3 P2-2: only the planted input edit remains)" "[ \"\$(docs_sum)\" = \"\$before\" ] && [ \"\$(git -C $ROOT status --porcelain -- docs)\" = ' M docs/VISION_COMPLETION_PLAN.md' ]"
git -C "$ROOT" checkout -q -- docs
printf '#!/bin/sh\nexit 2\n' > "$T/bin/failpy"; chmod +x "$T/bin/failpy"; (cd "$ROOT" && CHRONOS_PY="$T/bin/failpy" run bash "$G10")
check "1 10 FAIL: the check itself failing → one FAIL line, never a silent exit" "failed current-state-fresh 'the current-state check could not run \\(exit 2\\)'"
printf '#!/bin/sh\nenv > %s\n' "$T/envpy.env" > "$T/bin/envpy"; chmod +x "$T/bin/envpy"
(cd "$ROOT" && DUMMY_CREDENTIAL=synthetic-probe-secret GH_TOKEN=synthetic-gh-token CHRONOS_PY="$T/bin/envpy" run bash "$G10")
ALLOW='ALLOW_LIVE_TRADING ALLOW_ORDER_TRANSMIT BROKER_MODE HOME LANG PATH PYTHONDONTWRITEBYTECODE '
envset() { cut -d= -f1 "$1" | grep -vxE 'PWD|SHLVL|_|OLDPWD' | sort | tr '\n' ' '; }
check "r1:2 P1-2 10 Daybreak's DUMMY_CREDENTIAL probe: the candidate check does not see DUMMY_CREDENTIAL or GH_TOKEN (positive control: it ran and dumped its env)" "passed current-state-fresh && [ -s $T/envpy.env ] && ! grep -qE '^(DUMMY_CREDENTIAL|GH_TOKEN)=' $T/envpy.env"
check "r1:2 P1-2 10 the candidate's environment is exactly the allowlist (plus the shell's own PWD/SHLVL/_), demo flags forced, HOME not the runner's" "[ \"\$(envset $T/envpy.env)\" = \"$ALLOW\" ] && grep -qx BROKER_MODE=demo $T/envpy.env && grep -qx ALLOW_ORDER_TRANSMIT=false $T/envpy.env && ! grep -qx \"HOME=\$HOME\" $T/envpy.env"
check "r1:2 P1-2 10 the throwaway HOME is removed afterwards" "h=\$(sed -n 's/^HOME=//p' $T/envpy.env); [ -n \"\$h\" ] && [ ! -e \"\$h\" ]"

C10="$T/c10"; git clone -q "$ROOT" "$C10"; ln -s "$ROOT/.venv" "$C10/.venv"
reset10() { git -C "$C10" reset -q --hard; git -C "$C10" clean -qfd -e .venv; rm -rf "$T/gen-out"; }
g10c() { (cd "$C10" && run bash "$G10"); }
g10c; check "r1:2 P1-3 10 positive control: a clean throwaway clone PASSes" "passed current-state-fresh"
printf 'outside-sentinel\n' > "$T/outside.md"; rm "$C10/docs/generated/CURRENT_STATE.md"; ln -s "$T/outside.md" "$C10/docs/generated/CURRENT_STATE.md"
git -C "$C10" add docs/generated/CURRENT_STATE.md; g10c
check "r1:2 P1-3 10 FAIL: Daybreak's staged output symlink → one FAIL line naming it" "failed current-state-fresh 'docs/generated/CURRENT_STATE.md is a symlink or non-regular file'"
check "r1:2 P1-3 10 nothing was written outside the tree (the external sentinel is byte-unchanged)" "[ \"\$(cat $T/outside.md)\" = outside-sentinel ]"
reset10; rm "$C10/docs/generated/CURRENT_STATE.md"; ln -s "$T/outside.md" "$C10/docs/generated/CURRENT_STATE.md"; g10c
check "r1:2 P1-3 10 FAIL: an UNSTAGED output symlink (lstat, not the index) → FAIL" "failed current-state-fresh 'docs/generated/CURRENT_STATE.md is a symlink or non-regular file'"
reset10; rm "$C10/docs/generated/capability-matrix.json"; mkfifo "$C10/docs/generated/capability-matrix.json"; g10c
check "r1:2 P1-3 10 FAIL: a FIFO at an output path → FAIL, never a blocking read" "failed current-state-fresh 'docs/generated/capability-matrix.json is a symlink or non-regular file'"
reset10; mv "$C10/docs/generated" "$T/gen-out"; ln -s "$T/gen-out" "$C10/docs/generated"; g10c
check "r1:2 P1-3 10 FAIL: docs/generated itself a symlink out of the tree → FAIL" "failed current-state-fresh 'docs/generated must be a real directory inside the tree'"
reset10; cp "$C10/docs/VISION_COMPLETION_PLAN.md" "$T/vcp.md"; rm "$C10/docs/VISION_COMPLETION_PLAN.md"; ln -s "$T/vcp.md" "$C10/docs/VISION_COMPLETION_PLAN.md"; git -C "$C10" add docs/VISION_COMPLETION_PLAN.md; g10c
check "r1:2 P1-3 10 FAIL: a tracked INPUT symlinked out of the tree → FAIL (every tracked path is lstat-checked)" "failed current-state-fresh 'docs/VISION_COMPLETION_PLAN.md is a symlink or non-regular file'"
reset10

# --- run-all.sh (trusted driver; trivial gates) ------------------------------------------------
mkdir "$T/ra"; cp "$G/run-all.sh" "$T/ra/"
printf '#!/bin/sh\necho "PASS: a — ok"\n' > "$T/ra/10-a.sh"; printf '#!/bin/sh\necho "PASS: b — ok"\n' > "$T/ra/20-b.sh"; chmod +x "$T/ra/"*.sh
RA0="$T/ra-cand"; mkdir "$RA0"; git -C "$RA0" init -q   # a candidate tree with no gates/ of its own
ra() { (cd "${1:-$RA0}" && run bash "${2:-$T/ra/run-all.sh}"); }
ra; check "1 run-all: every gate passes → each PASS line then ALL GATES PASS on stdout, exit 0" "rc 0 && [ \"\$(cat $T/out)\" = 'PASS: a — ok
PASS: b — ok
ALL GATES PASS' ] && [ ! -s $T/err ]"
printf '#!/bin/sh\necho "FAIL: b — broken; fix it" >&2\nexit 1\n' > "$T/ra/20-b.sh"; ra
check "1 run-all: one gate fails → its FAIL line is shown, GATES FAILED on stderr, exit 1" "rc 1 && grep -qx 'FAIL: b — broken; fix it' $T/out && [ \"\$(cat $T/err)\" = 'GATES FAILED' ]"
printf '#!/bin/sh\necho "PASS: b — ok"\n' > "$T/ra/20-b.sh"
RC="$T/cand"; mkdir -p "$RC/gates"; git -C "$RC" init -q; cp "$T/ra/"*.sh "$RC/gates/"
printf '#!/bin/sh\nprintf "%%s" "$DUMMY_CREDENTIAL" > "$CAPTURE_PATH"\necho "PASS: repository-supplied — executed"\n' > "$RC/gates/15-repository-supplied.sh"; chmod +x "$RC/gates/"*.sh
(cd "$RC" && DUMMY_CREDENTIAL=synthetic-probe-secret CAPTURE_PATH="$T/captured" GATES_TRUSTED_DIR="$T/ra" run bash "$T/ra/run-all.sh")
check "r1:2 P1-2 run-all: Daybreak's candidate-added gates/15-*.sh is reported, never executed (no capture file), and the run fails" "rc 1 && grep -qx 'FAIL: run-all — gates/15-repository-supplied.sh is not in the trusted gate set; not run (it takes effect once merged to the trusted base)' $T/out && [ ! -e $T/captured ] && grep -qx 'GATES FAILED' $T/err"
check "r1:2 P1-2 run-all: the trusted gates still ran and reported" "grep -qx 'PASS: a — ok' $T/out && grep -qx 'PASS: b — ok' $T/out && ! grep -q 'PASS: repository-supplied' $T/out"
(cd "$RC" && DUMMY_CREDENTIAL=synthetic-probe-secret CAPTURE_PATH="$T/captured" run bash "$T/ra/run-all.sh")
check "r1:2 P1-2 run-all: GATES_TRUSTED_DIR unset → the driver's own directory is the trusted set; the probe gate is still refused" "rc 1 && grep -q 'gates/15-repository-supplied.sh is not in the trusted gate set' $T/out && [ ! -e $T/captured ]"
(cd "$RC" && DUMMY_CREDENTIAL=synthetic-probe-secret CAPTURE_PATH="$T/captured" run bash "$RC/gates/run-all.sh")
check "r1:2 P1-2 positive control: the CANDIDATE's own driver copy does execute the probe (why the lane must invoke the trusted copy — README trust model)" "[ \"\$(cat $T/captured 2>/dev/null)\" = synthetic-probe-secret ]"
(cd "$RA0" && GATES_TRUSTED_DIR="$T/nonexistent" run bash "$T/ra/run-all.sh")
check "r1:2 run-all: a GATES_TRUSTED_DIR that is not a directory → FAIL + GATES FAILED, exit 1" "rc 1 && grep -q '^FAIL: run-all — GATES_TRUSTED_DIR' $T/err && grep -qx 'GATES FAILED' $T/err"
for bad_gate in 'exit 0' 'echo "PASS: c — one"; echo "PASS: c — two"' 'echo "PASS: c — ok"; exit 1' 'echo "FAIL: c — says fail" >&2; exit 0'; do
  printf '#!/bin/sh\n%s\n' "$bad_gate" > "$T/ra/30-c.sh"; chmod +x "$T/ra/30-c.sh"; ra
  check "r1:2 run-all: a gate that does not report exactly one PASS line with exit 0 ('$bad_gate') fails the run" "rc 1 && grep -qx 'GATES FAILED' $T/err && ! grep -qx 'ALL GATES PASS' $T/out"
done
printf '#!/bin/sh\necho "PASS: c — ok"\n' > "$T/ra/30-c.sh"; chmod -x "$T/ra/30-c.sh"; ra
check "r1:2 run-all: a gate that cannot run (not executable) fails the run" "rc 1 && grep -q '30-c.sh did not report exactly one PASS line' $T/out"
rm "$T/ra/30-c.sh"; mkdir "$T/empty"; cp "$T/ra/run-all.sh" "$T/empty/"; ra "$RA0" "$T/empty/run-all.sh"
check "r1:2 run-all: an empty trusted gate set is never a pass" "rc 1 && grep -q '^FAIL: run-all — no gates found' $T/out && grep -qx 'GATES FAILED' $T/err"
check "1 every gates/NN-*.sh is executable (run-all runs them directly)" "for g in $G/[0-9][0-9]-*.sh; do [ -x \"\$g\" ] || exit 1; done"
check "r1:3 no .github change on this branch (working tree vs origin/main)" "git -C $ROOT diff --quiet origin/main -- .github"

echo "test_gates_10_30: $pass ok, $fail failed"; [ "$fail" = 0 ]
