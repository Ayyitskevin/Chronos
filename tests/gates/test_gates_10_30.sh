#!/usr/bin/env bash
# GATES-1 harness: pins the gate-script contract (gates/README.md) for 10/20/30 and run-all.sh.
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
case "$1 $2" in
  "pr view")    [ "${STUB_DRAFT:-}" = ERROR ] && exit 1; printf '%s\n' "${STUB_DRAFT:-}" ;;
  "api graphql") [ "${STUB_BASE:-}" = ERROR ] && exit 1; printf '%s\n' "${STUB_BASE:-}" ;;
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
check "2 gate 20 made only read queries (every gh call is 'pr view <N> --json isDraft …'; the log is non-empty)" "[ -s $STUB_LOG ] && ! grep -vE '^pr view [0-9]+ --json isDraft' $STUB_LOG"
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
git -C "$T/repo" remote set-url origin "$T/missing.git"
( cd "$T/repo" && STUB_BASE=$NEW PR_NUMBER=7 run bash "$G30" ); check "1 30 FAIL: origin main cannot be fetched → one FAIL line" "failed base-fresh 'could not fetch origin main'"
check "3 gate 30 leaves its repo's work tree unchanged (its only write is git fetch)" "[ -z \"\$(git -C $T/repo status --porcelain)\" ]"

# --- 10 current-state-fresh (this repo; the trap restores docs/) -------------------------------
export BROKER_MODE=demo ALLOW_ORDER_TRANSMIT=false ALLOW_LIVE_TRADING=false PYTHONDONTWRITEBYTECODE=1
G10="$G/10-current-state-fresh.sh"
docs_sum() { (cd "$ROOT" && git status --porcelain -- docs; find docs/generated -type f -exec sha256sum {} + | sort); }
check "3 precondition: docs/ is clean before gate 10 runs" "[ -z \"\$(git -C $ROOT status --porcelain -- docs)\" ]"
before=$(docs_sum); (cd "$ROOT" && run bash "$G10")
check "1 10 PASS: a fresh page → one PASS line, exit 0" "passed current-state-fresh"
check "3 10 leaves the tree exactly as it found it when fresh (docs/ status and docs/generated bytes unchanged)" "[ \"\$(docs_sum)\" = \"\$before\" ]"
printf '\nnoise\n' >> "$ROOT/docs/generated/CURRENT_STATE.md"; (cd "$ROOT" && run bash "$G10")
check "1 10 PASS: a merely dirtied page is regenerated back from its inputs (the page matches them)" "passed current-state-fresh"
git -C "$ROOT" checkout -q -- docs
printf '\nstale input\n' >> "$ROOT/docs/VISION_COMPLETION_PLAN.md"; (cd "$ROOT" && run bash "$G10")
check "1 10 FAIL: a changed page INPUT with no regeneration → one FAIL line naming the page, exit 1" "failed current-state-fresh 'docs/generated/CURRENT_STATE.md is stale'"
git -C "$ROOT" checkout -q -- docs
printf '#!/bin/sh\nexit 2\n' > "$T/bin/make"; chmod +x "$T/bin/make"; (cd "$ROOT" && run bash "$G10"); rm "$T/bin/make"
check "1 10 FAIL: make current-state itself failing → one FAIL line, never a silent exit" "failed current-state-fresh \"'make current-state' failed\""

# --- run-all.sh (the proposal's aggregator, on two trivial gates) -----------------------------
mkdir "$T/ra"; cp "$G/run-all.sh" "$T/ra/"
printf '#!/bin/sh\necho "PASS: a — ok"\n' > "$T/ra/10-a.sh"; printf '#!/bin/sh\necho "PASS: b — ok"\n' > "$T/ra/20-b.sh"; chmod +x "$T/ra/"*.sh
run bash "$T/ra/run-all.sh"
check "1 run-all: every gate passes → each PASS line then ALL GATES PASS on stdout, exit 0" "rc 0 && [ \"\$(cat $T/out)\" = 'PASS: a — ok
PASS: b — ok
ALL GATES PASS' ] && [ ! -s $T/err ]"
printf '#!/bin/sh\necho "FAIL: b — broken; fix it" >&2\nexit 1\n' > "$T/ra/20-b.sh"
run bash "$T/ra/run-all.sh"
check "1 run-all: one gate fails → its FAIL line is shown, GATES FAILED on stderr, exit 1" "rc 1 && grep -qx 'FAIL: b — broken; fix it' $T/out && [ \"\$(cat $T/err)\" = 'GATES FAILED' ]"
check "1 every gates/NN-*.sh is executable (run-all runs them directly)" "for g in $G/[0-9][0-9]-*.sh; do [ -x \"\$g\" ] || exit 1; done"

echo "test_gates_10_30: $pass ok, $fail failed"; [ "$fail" = 0 ]
