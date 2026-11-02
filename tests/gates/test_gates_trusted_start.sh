#!/usr/bin/env bash
# test_gates_trusted_start.sh — G70-FIX r2: TRUSTED Python the gates start inside the sandbox must start BEFORE any candidate
# bytes can run, and must not resolve candidate modules. Real python, real pytest, real bandit/detect-secrets from the real lane
# venv and tools venv, real bwrap; throwaway git repos under mktemp (never this repo's tree, never a live run).
#   S1 gate 70: a tracked src/sitecustomize.py (runs at interpreter startup, via the editable .pth / PYTHONPATH) must not forge a
#      PASS for a failing doc-contract test.   S2 sandbox_identity: the same hook must not make a wrong tree look identical.
#   S3 gate 40's security-offline step: the same hook must not skip the trusted driver, and root/src bandit.py and a root
#      detect_secrets/pre_commit_hook.py must not stand in for the scanners the driver's children run.
#   S4 tools-venv.sh: two simultaneous cold builders build ONCE and both get a complete venv.
# Exit 0 all ok, 1 otherwise.
set -u
pass=0; fail=0
ok() { pass=$((pass+1)); echo "ok       $1"; }; bad() { fail=$((fail+1)); echo "FAIL     $1"; [ -z "${DEBUG:-}" ] || { echo "  rc=$(cat "$T/rc" 2>/dev/null)"; head -c 600 "$T/out" 2>/dev/null | sed "s/^/  out: /"; head -c 900 "$T/err" 2>/dev/null | sed "s/^/  err: /"; }; }
check() { if eval "$2"; then ok "$1"; else bad "$1"; fi; }
HERE=$(cd "$(dirname "$0")" && pwd); ROOT=$(cd "$HERE/../.." && pwd); G=$ROOT/gates
T=$(mktemp -d)
run() { "$@" > "$T/out" 2> "$T/err"; echo $? > "$T/rc"; }
one() { [ "$(wc -l < "$1")" = 1 ] && grep -qE "$2" "$1"; }
rc() { [ "$(cat "$T/rc")" = "$1" ]; }
passed() { rc 0 && one "$T/out" "^PASS: $1 — " && [ ! -s "$T/err" ]; }
failed() { rc 1 && one "$T/err" "^FAIL: $1 — $2" && [ ! -s "$T/out" ]; }
commit() { git -C "$1" add -A && git -C "$1" -c user.email=t@t -c user.name=t commit -qm "${2:-c}"; }
newrepo() { rm -rf "$1"; mkdir -p "$1"; git -C "$1" init -q; }
export BROKER_MODE=demo ALLOW_ORDER_TRANSMIT=false ALLOW_LIVE_TRADING=false PYTHONDONTWRITEBYTECODE=1
RVENV=$(readlink -f "$ROOT/.venv"); PY=$RVENV/bin/python   # the real path: the sandbox binds the venv there, not through a symlinked .venv
[ -x "$PY" ] || { echo "FAIL     no lane venv at $PY (the real-python cases need it)"; exit 1; }
 UVROOT=$(dirname "$(dirname "$(sed -n 's/^home = //p' "$RVENV/pyvenv.cfg")")")
REALTOOLS=$(GATES_TRUSTED_REPO=$ROOT GATES_TRUSTED_REF=origin/main bash -c '. "$0/gates/lib/tools-venv.sh" && tools_venv' "$ROOT" 2>/dev/null) \
  || { echo "FAIL     no trusted tools venv (needs the cached tools-<digest> venv or the network once)"; exit 1; }
export GATES_SANDBOX_RO="$REALTOOLS"   # NOT the lane venv: the sandbox binds <repo>/.venv itself and overlays its editable .pth; a second bind would mask the overlay
case "$UVROOT" in /|/usr|/usr/*) ;; *) GATES_SANDBOX_RO="$GATES_SANDBOX_RO:$UVROOT" ;; esac

# --- S1 gate 70 --------------------------------------------------------------------------------------------------------
G70=$G/70-docs-claims-pinned.sh; R70=$T/r70
NAMED="tests/unit/test_adr_point_in_time_claims.py tests/unit/test_docs_map_skill_contract.py tests/unit/test_vision_completion_plan_prose.py"
mk70() {  # one doc-contract test FAILS for real; every other one passes
  newrepo "$R70"; mkdir -p "$R70/tests/unit" "$R70/src/chronos"; : > "$R70/src/chronos/__init__.py"
  for f in tests/unit/test_limitations_a_contract.py $NAMED; do printf 'def test_a():\n    pass\n' > "$R70/$f"; done
  printf 'def test_a():\n    assert False, "the doc claim is false"\n' > "$R70/tests/unit/test_limitations_a_contract.py"
}
HOOK70='import json, os
gio = os.environ.get("GATE_IO", "")
exp = os.path.join(os.path.dirname(gio), "trusted", "expected.json")   # the trusted AST list, bound read-only in the gate-70 sandbox
if gio and os.path.exists(exp):
    with open(os.path.join(gio, "nodes.jsonl"), "w") as out:
        for f, names in json.load(open(exp)).items():
            for n in names:
                for w in ("setup", "call", "teardown"):
                    out.write(json.dumps({"nodeid": f + "::" + n, "when": w, "outcome": "passed"}) + "\n")
    print("4 passed in 0.10s", flush=True)   # a convincing summary line too: only a hook that never runs defeats the forgery
    os._exit(0)
'
g70() { (cd "$R70" && ln -sfn "$RVENV" .venv && CHRONOS_PY="$PY" run bash "$G70"); }
mk70; printf '.venv\n' > "$R70/.gitignore"; commit "$R70"; g70
check "s1:0 control: a really failing doc-contract test FAILs gate 70 (real pytest)" "failed docs-claims-pinned 'pytest exited 1'"
mk70; printf '.venv\n' > "$R70/.gitignore"; printf '%s' "$HOOK70" > "$R70/src/sitecustomize.py"; commit "$R70"; g70
check "s1:1 a tracked src/sitecustomize.py that forges the node reports and exits 0 at interpreter startup does NOT turn the failing test into a PASS" "failed docs-claims-pinned ."

# --- S2 sandbox_identity -----------------------------------------------------------------------------------------------
RID=$T/rid
ident() {  # the identity check of the repo in $RID; prints nothing, returns its status
  ( cd "$RID" && ln -sfn "$RVENV" .venv && . "$G/lib/sandbox.sh" && w=$(mktemp -d "$T/w.XXXXXX") && sandbox_snapshot "$w" && sandbox_identity "$w" "$PY" )
}
newrepo "$RID"; printf '.venv\n' > "$RID/.gitignore"; mkdir -p "$RID/src/chronos"; : > "$RID/src/chronos/__init__.py"; commit "$RID"
check "s2:0 control: chronos under <snap>/src → the identity check passes" "ident"
newrepo "$RID"; printf '.venv\n' > "$RID/.gitignore"; mkdir -p "$RID/src"; printf 'import os, sys\nif sys.argv[:1] == ["-c"]:\n    os._exit(0)\n' > "$RID/src/sitecustomize.py"; commit "$RID"
check "s2:1 NO chronos in the snapshot plus a src/sitecustomize.py that exits 0 for -c: the identity check still REFUSES (the hook never gets to vouch)" "! ident"

# --- S3 gate 40 security-offline (real lane venv, real tools venv, real scanners; pip steps stubbed) ---------------------
G40=$G/40-evidence-at-head.sh; R40=$T/r40
mkdir -p "$T/toolsx/bin" "$T/xdgx"; cat > "$T/toolsx/bin/python" <<STUB
#!/usr/bin/python3
# the trusted tools venv for the NETWORK steps only (pip download / pip_audit); everything else is the REAL tools venv
import json, os, sys
a = sys.argv[1:]
if a[:2] != ["-I", "-B"]: sys.exit(98)
a = a[2:]
if a[:3] == ["-m", "pip", "download"]:
    open(os.path.join(a[a.index("-d") + 1], "stub-1.0-py3-none-any.whl"), "w").write("x"); sys.exit(0)
if a[:2] == ["-m", "pip_audit"]:
    open(a[a.index("-o") + 1], "w").write(json.dumps({"dependencies": [{"name": "x", "version": "1", "vulns": []}]})); sys.exit(0)
os.execv("$REALTOOLS/bin/python", ["$REALTOOLS/bin/python", "-I", "-B", *a])
STUB
chmod +x "$T/toolsx/bin/python"
mk40() {
  newrepo "$R40"; mkdir -p "$R40/src/chronos" "$R40/scripts" "$R40/worker"; : > "$R40/src/chronos/__init__.py"; : > "$R40/worker/__init__.py"
  for l in bootstrap build runtime sbom dev; do printf '# locked by uv\nexample==1.0 \\\n    --hash=sha256:%064d\n' 0 > "$R40/requirements-$l.lock"; done   # dev: gate 40 validates and audits the dev lock too (DEV-AUDIT)
  # the TRUSTED base's script as it was BEFORE it carried -I -B itself (the flags stripped), so these cases pin the DRIVER's own
  # child isolation independently of the script's (that one is pinned by tests/unit/test_release_security.py)
  python3 -c "import re,sys; s=open(sys.argv[1]).read(); open(sys.argv[2],'w').write(re.sub(r'\n\s+\"-I\",\n\s+\"-B\",', '', s))" "$ROOT/scripts/verify_release_security.py" "$R40/scripts/verify_release_security.py"
  ! grep -q '"-I",' "$R40/scripts/verify_release_security.py" || { echo "FAIL     fixture: the script still carries -I"; exit 1; }
  python3 -c "import json,sys; d=json.load(open(sys.argv[1])); d['results']={}; d['history_results']={}; json.dump(d, open(sys.argv[2],'w'), indent=2)" "$ROOT/.secrets.baseline" "$R40/.secrets.baseline"
  printf '.venv\n.gates/\ndata/\ndist/\n' > "$R40/.gitignore"
  printf '# TRUSTED-MAKEFILE\nlint format-check type type-worker test release-gate:\n\t@echo "12 passed in 0.1s"\n' > "$R40/Makefile"
  printf '# TRUSTED pip bootstrap\n' > "$R40/scripts/verify_pip_bootstrap.py"
  # D-47(b) re-stack: gate 40's trusted migration-chain step (gates/lib/migration_chain.py) runs before the scanners, so the
  # trusted base needs main's chain 0001 → 0002 and the one _MIGRATION_HEAD line it overlays — the same shape as
  # test_gates_40_70.sh's mig40/lit40 fixture. The candidate inherits both unchanged, so S3 reaches the security-offline step.
  mkdir -p "$R40/src/chronos/persistence/migrations/versions"
  for m in '0001 None base' '0002 "0001" next'; do set -- $m
    printf 'from __future__ import annotations\n\nrevision = "%s"\ndown_revision = %s\nbranch_labels = None\ndepends_on = None\n' "$1" "$2" \
      > "$R40/src/chronos/persistence/migrations/versions/$1_$3.py"
  done
  printf '# TRUSTED release script\n_MIGRATION_HEAD: Final[str] = "0002"\n' > "$R40/scripts/verify_release_artifact.py"
  commit "$R40"; git -C "$R40" branch -q trusted-main; git -C "$R40" update-ref refs/remotes/origin/main trusted-main
}
g40() {  # the gate against the CURRENT HEAD of $R40, lane venv = the real one
  H40=$(git -C "$R40" rev-parse HEAD)
  (cd "$R40" && ln -sfn "$RVENV" .venv && GATES_TOOLS_VENV="$T/toolsx" XDG_CACHE_HOME="$T/xdgx" GATES_TRUSTED_REPO="$R40" GATES_TRUSTED_REF=trusted-main \
    PR_HEAD_SHA="$H40" run bash "$G40")
}
FAKE="AKIA$(printf Z%.0s 1 2 3 4)GATESPROBE77"   # assembled at run time: no key-shaped literal in a tracked file
BANDIT_HIT='exec("1")   # bandit B102 (medium severity), a real finding in the CANDIDATE tree\n'
SHADOW0='import sys\nsys.exit(0)\n'
mk40; g40
check "s3:0 control: a clean candidate tree → gate 40 PASSes with the real scanners" "passed evidence-at-head"
mk40; printf "$BANDIT_HIT" > "$R40/src/chronos/probe.py"; commit "$R40"; g40
check "s3:1 control: a real bandit finding in src/chronos is REPORTED (the scan still covers the candidate's tree)" "failed evidence-at-head '.*target security-offline'"
mk40; printf "$BANDIT_HIT" > "$R40/src/chronos/probe.py"; printf "$SHADOW0" > "$R40/bandit.py"; commit "$R40"; g40
check "s3:2 a tracked ROOT bandit.py (exits 0) does NOT stand in for bandit: the finding is still reported" "failed evidence-at-head '.*target security-offline'"
mk40; printf "$BANDIT_HIT" > "$R40/src/chronos/probe.py"; printf "$SHADOW0" > "$R40/src/bandit.py"; commit "$R40"; g40
check "s3:3 a tracked src/bandit.py (exits 0) does NOT stand in for bandit: the finding is still reported" "failed evidence-at-head '.*target security-offline'"
mk40; printf 'x = 1\naws_key = "%s"\n' "$FAKE" > "$R40/leak.py"; commit "$R40"; g40
check "s3:4 control: a tracked secret is REPORTED by the trusted tracked-file scan" "failed evidence-at-head '.*target security-offline'"
mk40; mkdir "$R40/detect_secrets"; : > "$R40/detect_secrets/__init__.py"; printf 'import sys\nsys.exit(3)\n' > "$R40/detect_secrets/pre_commit_hook.py"; commit "$R40"; g40
check "s3:5 a tracked ROOT detect_secrets/pre_commit_hook.py that FAILS (exit 3) on a clean tree never runs: the scanner child resolved the real detect_secrets, so the gate PASSes (the history scan would also report a planted secret, so a poisoned shadow is the only way to see which module the child imported)" "passed evidence-at-head"
mk40; printf "$BANDIT_HIT" > "$R40/src/chronos/probe.py"
printf 'import os, sys\nif os.path.basename(sys.argv[0]) in ("-", "security_offline.py"):   # the trusted driver, however it is started\n    os._exit(0)\n' > "$R40/src/sitecustomize.py"; commit "$R40"; g40
check "s3:6 a tracked src/sitecustomize.py that exits 0 for the stdin driver does NOT skip the trusted driver: the finding is still reported" "failed evidence-at-head '.*target security-offline'"

# --- S4 tools-venv.sh --------------------------------------------------------------------------------------------------
TV=$T/tvrepo; newrepo "$TV"; mkdir -p "$TV/.venv" "$T/fakebase"
printf '# lock\nexample==1.0 \\\n    --hash=sha256:%064d\n' 0 > "$TV/requirements-dev.lock"; commit "$TV"
printf 'home = %s\n' "$T/fakebase" > "$TV/.venv/pyvenv.cfg"
cat > "$T/fakebase/python3" <<'FAKE'
#!/bin/bash
# a fake base interpreter: `-I -B -m venv DIR` makes DIR/bin/python, a fake whose `-m pip install` takes 1.5 s
echo build >> "$TV_LOG"
dir="${@: -1}"; mkdir -p "$dir/bin" && cat > "$dir/bin/python" <<'PYFAKE'
#!/bin/bash
sleep 1.5; mkdir -p "$(dirname "$0")/../installed" && echo ok > "$(dirname "$0")/../installed/pkg"
PYFAKE
chmod +x "$dir/bin/python"
FAKE
chmod +x "$T/fakebase/python3"
tv() { ( export TV_LOG="$T/tv.log" XDG_CACHE_HOME="$T/tvcache" GATES_TRUSTED_REPO="$TV" GATES_TRUSTED_REF=HEAD; unset GATES_TOOLS_VENV; . "$G/lib/tools-venv.sh"; tools_venv ) ; }
: > "$T/tv.log"; tv > "$T/tv1.out" 2> "$T/tv1.err" & p1=$!; sleep 0.3; tv > "$T/tv2.out" 2> "$T/tv2.err" & p2=$!; wait $p1; r1=$?; wait $p2; r2=$?
check "s4:1 two simultaneous COLD builders both succeed and name the same venv" "[ $r1 = 0 ] && [ $r2 = 0 ] && [ -s $T/tv1.out ] && cmp -s $T/tv1.out $T/tv2.out"
check "s4:2 ...the venv was built ONCE (a digest lock, not two racing rm -rf builds)" "[ \"\$(wc -l < $T/tv.log)\" = 1 ]"
check "s4:3 ...and the published venv is complete: its package is installed and its lock marker matches the digest" "d=\$(cat $T/tv1.out); [ -f \$d/installed/pkg ] && [ \"\$(cat \$d/.gates-lock-sha256)\" = \"\$(git -C $TV show HEAD:requirements-dev.lock | sha256sum | cut -c1-64)\" ]"
: > "$T/tv.log"; tv > "$T/tv3.out" 2>&1
check "s4:4 a warm start reuses it (no rebuild)" "[ \"\$(wc -l < $T/tv.log)\" = 0 ]"

echo "test_gates_trusted_start: $pass ok, $fail failed"; [ $fail = 0 ]
