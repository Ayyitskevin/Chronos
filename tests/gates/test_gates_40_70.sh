#!/usr/bin/env bash
# GATES-2 harness: pins the gate-script contract (gates/README.md) for 40/50/60/70.
# Gate 40 runs against a stub `make` first on PATH (the harness never runs the real `make gates`);
# gates 50/60/70 run in throwaway git repos (60 with a planted FAKE secret and a copy of the
# release security script) plus one read-only run each on this repo. Numbered by the GATES-2 contract.
set -u
ROOT=$(cd "$(dirname "$0")/../.." && pwd); G=$ROOT/gates; pass=0; fail=0
T=$(mktemp -d)
trap 'rm -rf "$T"' EXIT
ok() { pass=$((pass+1)); echo "ok       $1"; }; bad() { fail=$((fail+1)); echo "FAIL     $1"; }
check() { if eval "$2"; then ok "$1"; else bad "$1"; fi; }
run() { "$@" > "$T/out" 2> "$T/err"; echo $? > "$T/rc"; }  # a gate's stdout, stderr and exit code
one() { [ "$(wc -l < "$1")" = 1 ] && grep -qE "$2" "$1"; }   # exactly one line, matching
rc() { [ "$(cat "$T/rc")" = "$1" ]; }
passed() { rc 0 && one "$T/out" "^PASS: $1 — " && [ ! -s "$T/err" ]; }
failed() { rc 1 && one "$T/err" "^FAIL: $1 — $2" && [ ! -s "$T/out" ]; }
commit() { git -C "$1" add -A && git -C "$1" -c user.email=t@t -c user.name=t commit -qm "${2:-c}"; }
newrepo() { rm -rf "$1"; mkdir -p "$1"; git -C "$1" init -q; }
export BROKER_MODE=demo ALLOW_ORDER_TRANSMIT=false ALLOW_LIVE_TRADING=false PYTHONDONTWRITEBYTECODE=1
PY=$ROOT/.venv/bin/python

check "1 every gates/NN-*.sh for 40-70 exists and is executable" "[ -x $G/40-evidence-at-head.sh ] && [ -x $G/50-fixture-integrity.sh ] && [ -x $G/60-secrets-baseline.sh ] && [ -x $G/70-docs-claims-pinned.sh ]"

# --- 40 evidence-at-head (a stub make; a throwaway repo) --------------------------------------
# Gates 40/60/70 run candidate code under `env -i`, so a stub sees none of this harness's variables:
# stubs are controlled by files under $T/ctl (baked-in paths) and dump their environment for the probes.
mkdir "$T/bin" "$T/ctl"; C=$T/ctl; MAKE_LOG="$T/make.log"
ctl() { if [ "$#" -eq 2 ]; then printf '%s' "$2" > "$C/$1"; else rm -f "$C/$1"; fi; }
cat > "$T/bin/make" <<STUB
#!/usr/bin/env bash
printf '%s\n' "\$*" >> $MAKE_LOG; env > $T/make.env
echo "mypy: Success: no issues found in 12 source files"
if [ -f $C/summary ]; then cat $C/summary; echo; else echo "12 passed, 1 skipped, 3 warnings in 4.56s"; fi
[ -f $C/move_head ] && git -c user.email=t@t -c user.name=t commit -q --allow-empty -m moved
exit \$(cat $C/rc 2>/dev/null || echo 0)
STUB
chmod +x "$T/bin/make"
G40="$G/40-evidence-at-head.sh"; R40="$T/r40"; newrepo "$R40"; echo a > "$R40/a"; commit "$R40"
H=$(git -C "$R40" rev-parse HEAD); RCPT="$R40/.gates/40-evidence-at-head.json"
field() { python3 -c "import json,sys; r=json.load(open(sys.argv[1])); print($1)" "$RCPT"; }
: > "$MAKE_LOG"; (cd "$R40" && PATH="$T/bin:$PATH" PR_HEAD_SHA=$H run bash "$G40")
check "1 40 PASS: make gates exit 0 at PR_HEAD_SHA == HEAD → one PASS line, exit 0" "passed evidence-at-head"
check "1 40 adopts the existing target: make was called exactly once, as 'make gates'" "[ \"\$(cat $MAKE_LOG)\" = gates ]"
check "1 40 receipt on disk records {sha, exit, pytest counts} of that run" "[ \"\$(field \"r['sha'], r['exit'], r['pytest']['passed'], r['pytest']['failed'], r['pytest']['skipped']\")\" = \"$H 0 12 0 1\" ]"
check "1 40 PASS line names the sha and the counts" "grep -qF \"$H (12 passed, 0 failed, 1 skipped)\" $T/out"
(cd "$R40" && PATH="$T/bin:$PATH" DUMMY_CREDENTIAL=synthetic-probe-secret GH_TOKEN=synthetic-gh-token PR_HEAD_SHA=$H run bash "$G40")
ALLOW='ALLOW_LIVE_TRADING ALLOW_ORDER_TRANSMIT BROKER_MODE HOME LANG PATH PYTHONDONTWRITEBYTECODE '
envset() { cut -d= -f1 "$1" | grep -vxE 'PWD|SHLVL|_|OLDPWD' | sort | tr '\n' ' '; }
noleak() { [ -s "$1" ] && ! grep -qE '^(DUMMY_CREDENTIAL|GH_TOKEN)=' "$1" && [ "$(envset "$1")" = "$ALLOW" ] && grep -qx BROKER_MODE=demo "$1" && ! grep -qx "HOME=$HOME" "$1"; }
check "r1:2 P1-2 40 Daybreak's DUMMY_CREDENTIAL probe: make gates runs under env -i — no DUMMY_CREDENTIAL/GH_TOKEN, exactly the allowlist, demo forced, a throwaway HOME (positive control: PASS + env dumped)" "passed evidence-at-head && noleak $T/make.env"
: > "$MAKE_LOG"; ctl rc 2; ctl summary '3 failed, 9 passed in 1.00s'; (cd "$R40" && PATH="$T/bin:$PATH" PR_HEAD_SHA=$H run bash "$G40"); ctl rc; ctl summary
check "1 40 FAIL: make gates exit 2 → one FAIL line naming the exit, exit 1" "failed evidence-at-head 'make gates exited 2 at $H'"
check "1 40 the failing run still leaves its receipt (exit 2, 3 failed)" "[ \"\$(field \"r['exit'], r['pytest']['failed']\")\" = '2 3' ]"
ctl summary ''; (cd "$R40" && PATH="$T/bin:$PATH" PR_HEAD_SHA=$H run bash "$G40"); ctl summary
check "1 40 FAIL: a green make with no pytest summary records no counts → FAIL, never a PASS" "failed evidence-at-head '.*no pytest summary' && [ \"\$(field \"r['pytest']\")\" = None ]"
ctl move_head 1; (cd "$R40" && PATH="$T/bin:$PATH" PR_HEAD_SHA=$H run bash "$G40"); ctl move_head
check "1 40 FAIL: the receipt's sha (HEAD after make) is not PR_HEAD_SHA → FAIL, judged from the receipt on disk" "failed evidence-at-head 'receipt sha' && [ \"\$(field \"r['sha']\")\" != $H ]"
git -C "$R40" reset -q --hard "$H"
: > "$MAKE_LOG"; OTHER=$(printf '%040d' 0 | tr 0 a)
(cd "$R40" && PATH="$T/bin:$PATH" PR_HEAD_SHA=$OTHER run bash "$G40")
check "1 40 FAIL: PR_HEAD_SHA != checked-out HEAD → FAIL before make runs" "failed evidence-at-head 'checked-out HEAD $H is not PR_HEAD_SHA' && [ ! -s $MAKE_LOG ]"
for bad_sha in '' abc123 "${H}x"; do
  (cd "$R40" && PATH="$T/bin:$PATH" PR_HEAD_SHA="$bad_sha" run bash "$G40")
  check "1 40 FAIL: PR_HEAD_SHA '$bad_sha' is not a full 40-hex sha → FAIL, make never runs" "failed evidence-at-head 'PR_HEAD_SHA must be a full 40-hex sha' && [ ! -s $MAKE_LOG ]"
done
echo b >> "$R40/a"; (cd "$R40" && PATH="$T/bin:$PATH" PR_HEAD_SHA=$H run bash "$G40"); git -C "$R40" checkout -q -- a
check "1 40 FAIL: a modified tracked file means HEAD does not name the tested bytes → FAIL, make never runs" "failed evidence-at-head 'tracked files are modified' && [ ! -s $MAKE_LOG ]"
check "1 40 the receipt path is gitignored in this repo (so make security-gate's untracked check cannot trip on it)" "git -C $ROOT check-ignore -q .gates/40-evidence-at-head.json"

# --- 50 fixture-integrity (throwaway repos; one read-only run on this repo) --------------------
G50="$G/50-fixture-integrity.sh"; B50="$T/b50"
mk50() {  # a repo with one manifest.json pin, one meta.json (trace + pine pins), committed
  newrepo "$B50"; mkdir -p "$B50/tests/fixtures/cap" "$B50/tests/fixtures/tv" "$B50/research/pine"
  echo '{"x":1}' > "$B50/tests/fixtures/cap/capture.json"; echo 'p' > "$B50/research/pine/07_tool.pine"
  printf 'a,b\n1,2\n' > "$B50/tests/fixtures/tv/trace.csv"
  python3 - "$B50" <<'PY'
import hashlib, json, pathlib, sys
b = pathlib.Path(sys.argv[1]); h = lambda p: hashlib.sha256((b / p).read_bytes()).hexdigest()
(b / "tests/fixtures/cap/manifest.json").write_text(json.dumps({"files": {"capture.json": h("tests/fixtures/cap/capture.json")}}))
(b / "tests/fixtures/tv/trace.meta.json").write_text(json.dumps({"catalog_number": "07", "pine_sha256": h("research/pine/07_tool.pine"),
  "trace_sha256": h("tests/fixtures/tv/trace.csv"), "input_config_sha256": "0" * 64, "owner_attestation_sha256": None}))
PY
  commit "$B50"
}
edit_json() { python3 - "$1" "$2" <<'PY'
import json, sys
p = sys.argv[1]; d = json.load(open(p)); exec(sys.argv[2]); open(p, "w").write(json.dumps(d))
PY
}
g50() { (cd "$B50" && run bash "$G50"); }
mk50; g50; check "1 50 PASS: every pinned digest matches its bytes → one PASS line naming 3 pins in 2 manifests" "passed fixture-integrity && grep -qF '3 pinned files in 2 manifests' $T/out"
(cd "$ROOT" && run bash "$G50"); check "1 50 PASS on this repo: 4 pinned files in 2 manifests (rehearsal manifest ×2, five_tool_trace trace + pine)" "passed fixture-integrity && grep -qF '4 pinned files in 2 manifests' $T/out"
mk50; echo '{"x":2}' > "$B50/tests/fixtures/cap/capture.json"; g50
check "1 50 FAIL: a manifest.json file whose bytes changed → FAIL naming it" "failed fixture-integrity 'tests/fixtures/cap/capture.json sha256 mismatch'"
mk50; rm "$B50/tests/fixtures/cap/capture.json"; g50
check "1 50 FAIL: a pinned file missing → FAIL naming it" "failed fixture-integrity 'tests/fixtures/cap/capture.json is pinned but missing'"
mk50; echo '3,4' >> "$B50/tests/fixtures/tv/trace.csv"; g50
check "1 50 FAIL: a meta.json trace_sha256 whose sibling csv changed → FAIL" "failed fixture-integrity 'tests/fixtures/tv/trace.csv sha256 mismatch'"
mk50; echo 'q' >> "$B50/research/pine/07_tool.pine"; g50
check "1 50 FAIL: a meta.json pine_sha256 whose catalog pine source changed → FAIL" "failed fixture-integrity 'research/pine/07_tool.pine sha256 mismatch'"
mk50; cp "$B50/research/pine/07_tool.pine" "$B50/research/pine/07_other.pine"; commit "$B50"; g50
check "1 50 FAIL: two tracked pine sources for one catalog number → FAIL (ambiguous, never a pick)" "failed fixture-integrity '.*pine_sha256 needs exactly one tracked research/pine/07_\\*.pine, found 2'"
mk50; git -C "$B50" rm -q research/pine/07_tool.pine; commit "$B50"; g50
check "1 50 FAIL: no tracked pine source for the catalog number → FAIL" "failed fixture-integrity '.*found 0'"
mk50; edit_json "$B50/tests/fixtures/tv/trace.meta.json" 'd["extra_sha256"] = "0" * 64'; g50
check "1 50 FAIL: an unmapped *_sha256 key → FAIL (a new pin is never silently unchecked)" "failed fixture-integrity '.*unmapped pin extra_sha256'"
mk50; edit_json "$B50/tests/fixtures/tv/trace.meta.json" 'd["owner_attestation_sha256"] = "0" * 64'; g50
check "1 50 FAIL: a non-null owner_attestation_sha256 has no file binding → FAIL unmapped" "failed fixture-integrity '.*unmapped pin owner_attestation_sha256'"
mk50; edit_json "$B50/tests/fixtures/cap/manifest.json" 'd["files"] = {"../../x": "0" * 64}'; g50
check "1 50 FAIL: a manifest path escaping its directory → FAIL" "failed fixture-integrity '.*must be a relative path under'"
mk50; edit_json "$B50/tests/fixtures/cap/manifest.json" 'd["files"] = {}'; g50
check "1 50 FAIL: a manifest.json with an empty files map → FAIL (an empty check set is not green)" "failed fixture-integrity '.*has no files map'"
mk50; edit_json "$B50/tests/fixtures/cap/manifest.json" 'd["files"]["capture.json"] = "ABC"'; g50
check "1 50 FAIL: a digest that is not lowercase 64-hex → FAIL" "failed fixture-integrity '.*is not a lowercase sha256'"
mk50; git -C "$B50" rm -q -r tests/fixtures; commit "$B50"; g50
check "1 50 FAIL: zero pinned manifests → FAIL (an empty check set is not green)" "failed fixture-integrity 'no pinned fixture manifests'"
mk50; printf 'not json' > "$B50/tests/fixtures/cap/manifest.json"; g50
check "1 50 FAIL: an unreadable manifest → FAIL naming it" "failed fixture-integrity 'tests/fixtures/cap/manifest.json is not readable JSON'"

# --- 60 secrets-baseline (a throwaway repo with a FAKE planted secret; one run on this repo) ----
G60="$G/60-secrets-baseline.sh"; R60="$T/r60"; FAKE="AKIA$(printf Z%.0s 1 2 3 4)GATESPROBE77"  # assembled at run time: no key-shaped literal in a tracked file
mk60() {  # $1 = python statement editing baseline dict d (the results-empty copy of this repo's baseline)
  newrepo "$R60"; mkdir -p "$R60/scripts"; cp "$ROOT/scripts/verify_release_security.py" "$R60/scripts/"
  echo 'x = 1' > "$R60/clean.py"
  python3 - "$ROOT/.secrets.baseline" "$R60/.secrets.baseline" "${1:-pass}" <<'PY'
import json, sys
d = json.load(open(sys.argv[1])); d["results"] = {}; d.pop("history_results", None); exec(sys.argv[3])
open(sys.argv[2], "w").write(json.dumps(d, indent=2) + "\n")
PY
  commit "$R60"
}
bsum() { sha256sum < "$1/.secrets.baseline"; }
g60() { (cd "$R60" && CHRONOS_PY=$PY run bash "$G60"); }
mk60; s0=$(bsum "$R60"); g60
check "1 60 PASS: no finding beyond the baseline → one PASS line, exit 0" "passed secrets-baseline"
check "2 60 PASS run leaves .secrets.baseline byte-identical" "[ \"\$(bsum $R60)\" = \"$s0\" ]"
printf 'x = 1\naws_key = "%s"\n' "$FAKE" > "$R60/leak.py"; g60
check "1 60 an UNTRACKED secret is out of scope (tracked files only) → PASS" "passed secrets-baseline"
commit "$R60"; HASHED=$(printf '%s' "$FAKE" | sha1sum | cut -d' ' -f1); g60
check "1 60 FAIL: a new tracked finding → one FAIL line as file:line type, exit 1" "failed secrets-baseline '1 new finding\\(s\\) vs .secrets.baseline: leak.py:2 AWS Access Key'"
check "2 60 never prints the secret value or its hashed_secret (positive control: the FAIL line names leak.py:2)" "grep -qF leak.py:2 $T/err && ! grep -qF '$FAKE' $T/out $T/err && ! grep -qF $HASHED $T/out $T/err"
check "2 60 FAIL run leaves .secrets.baseline byte-identical" "[ \"\$(bsum $R60)\" = \"$s0\" ] && [ -z \"\$(git -C $R60 status --porcelain)\" ]"
ENTRY="{'type': 'AWS Access Key', 'filename': 'leak.py', 'hashed_secret': '$HASHED', 'is_verified': False, 'line_number': LINE}"
mk60 "d['results'] = {'leak.py': [${ENTRY/LINE/2}]}"; printf 'x = 1\naws_key = "%s"\n' "$FAKE" > "$R60/leak.py"; commit "$R60"; s1=$(bsum "$R60"); g60
check "1 60 PASS: a finding already reviewed in the baseline is not new" "passed secrets-baseline"
mk60 "d['results'] = {'leak.py': [${ENTRY/LINE/5}]}"; printf 'x = 1\naws_key = "%s"\n' "$FAKE" > "$R60/leak.py"; commit "$R60"; s2=$(bsum "$R60"); g60
check "1 60 FAIL: a reviewed finding whose line moved → FAIL 'baseline stale', never a rewrite" "failed secrets-baseline '.secrets.baseline is stale'"
check "2 60 the stale case leaves .secrets.baseline byte-identical (the hook only ever saw a temp copy)" "[ \"\$(bsum $R60)\" = \"$s2\" ]"
printf '#!/bin/sh\ncat > /dev/null; env > %s; echo "stub scan"\n' "$T/py60.env" > "$T/bin/envpy60"; chmod +x "$T/bin/envpy60"
(cd "$R60" && DUMMY_CREDENTIAL=synthetic-probe-secret GH_TOKEN=synthetic-gh-token CHRONOS_PY="$T/bin/envpy60" run bash "$G60")
check "r1:2 P1-2 60 DUMMY_CREDENTIAL probe: the scan (it imports candidate code) runs under env -i — no credential variables, exactly the allowlist" "passed secrets-baseline && noleak $T/py60.env"
(cd "$R60" && CHRONOS_PY="$T/nopython" run bash "$G60")
check "1 60 FAIL: no python at CHRONOS_PY → one FAIL line" "failed secrets-baseline 'no python at'"
r0=$(bsum "$ROOT"); (cd "$ROOT" && run bash "$G60")
check "1 60 PASS on this repo (tracked files vs the reviewed baseline)" "passed secrets-baseline"
check "2 60 this repo's .secrets.baseline is byte-identical after the gate" "[ \"\$(bsum $ROOT)\" = \"$r0\" ]"
check "2 60 gate source never prints the hook's captured output (no echo/print of stdout/stderr)" "! grep -nE 'print\\((r|result|proc)\\.(stdout|stderr)|sys\\.std(out|err)\\.write\\((r|result|proc)\\.' $G60"

# --- 70 docs-claims-pinned (a stub python in a throwaway repo; one real run on this repo) -------
G70="$G/70-docs-claims-pinned.sh"; R70="$T/r70"
NAMED="tests/unit/test_adr_point_in_time_claims.py tests/unit/test_docs_map_skill_contract.py tests/unit/test_vision_completion_plan_prose.py"
mk70() { newrepo "$R70"; mkdir -p "$R70/tests/unit"; for f in tests/unit/test_limitations_a_contract.py tests/unit/test_limitations_b_contract.py $NAMED; do echo '' > "$R70/$f"; done; commit "$R70"; }
PY_LOG="$T/py.log"; cat > "$T/bin/fakepy" <<STUB
#!/usr/bin/env bash
printf '%s\n' "\$*" >> $PY_LOG; env > $T/py.env
if [ -f $C/pysum ]; then cat $C/pysum; echo; else echo "5 passed in 0.10s"; fi
exit \$(cat $C/pyrc 2>/dev/null || echo 0)
STUB
chmod +x "$T/bin/fakepy"
g70() { : > "$PY_LOG"; (cd "$R70" && CHRONOS_PY="$T/bin/fakepy" run bash "$G70"); }
mk70; g70
check "1 70 PASS: the named doc-contract set passes → one PASS line, exit 0" "passed docs-claims-pinned"
DUMMY_CREDENTIAL=synthetic-probe-secret GH_TOKEN=synthetic-gh-token g70
check "r1:2 P1-2 70 DUMMY_CREDENTIAL probe: the doc-contract tests run under env -i (no credential variables, exactly the allowlist)" "passed docs-claims-pinned && noleak $T/py.env"
check "1 70 adopts the existing tests: pytest ran once over exactly the glob + the three named files, nothing skipped or deselected" "[ \"\$(cat $PY_LOG)\" = \"-m pytest -q tests/unit/test_limitations_a_contract.py tests/unit/test_limitations_b_contract.py $NAMED\" ]"
ctl pyrc 1; ctl pysum '1 failed, 4 passed in 0.10s'; g70
check "1 70 FAIL: a failing doc-contract test → one FAIL line with the pytest summary, exit 1" "failed docs-claims-pinned 'pytest exited 1 \\(1 failed, 4 passed'"
ctl pyrc 5; ctl pysum 'no tests ran in 0.01s'; g70; ctl pyrc; ctl pysum
check "1 70 FAIL: pytest collected nothing (exit 5) → FAIL, never green" "failed docs-claims-pinned 'pytest collected no tests'"
mk70; git -C "$R70" rm -q tests/unit/test_docs_map_skill_contract.py; commit "$R70"; g70
check "1 70 FAIL: a named contract file missing → FAIL naming it, pytest never runs" "failed docs-claims-pinned 'tests/unit/test_docs_map_skill_contract.py is missing' && [ ! -s $PY_LOG ]"
mk70; git -C "$R70" rm -q 'tests/unit/test_limitations_*'; commit "$R70"; g70
check "1 70 FAIL: the limitations contract glob matches nothing → FAIL" "failed docs-claims-pinned 'no tests/unit/test_limitations_\\*_contract.py'"
check "1 70 the README lists the named set it runs" "grep -qF 'test_limitations_*_contract.py' $G/README.md && for f in $NAMED; do grep -qF \"\${f#tests/unit/}\" $G/README.md || exit 1; done"
(cd "$ROOT" && run bash "$G70")
check "1 70 PASS on this repo (the real doc-contract tests, real pytest)" "passed docs-claims-pinned"

# --- contract 4: the harness never edits existing tests or source ------------------------------
check "4 this repo's tracked tree is unchanged by the harness (git status empty)" "[ -z \"\$(git -C $ROOT status --porcelain --untracked-files=no)\" ]"

echo "test_gates_40_70: $pass ok, $fail failed"
[ "$fail" -eq 0 ]
