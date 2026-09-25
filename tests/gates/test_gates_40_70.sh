#!/usr/bin/env bash
# GATES-2 harness: pins the gate-script contract (gates/README.md) for 40/50/60/70.
# Gate 40 runs against a stub `make` first on PATH (the harness never runs the real `make gates`);
# gates 50/60/70 run in throwaway git repos (60 with a planted FAKE secret and a copy of the
# release security script) plus one read-only run each on this repo. Numbered by the GATES-2 contract
# (1-4), GATES-1r1 (`r1:<item> <finding>`) and GATES-2r1 (`r2:<item> <finding>`).
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

# --- shared: the bwrap sandbox's view --------------------------------------------------------------
# Gates 10/40/60/70 run candidate code in the trusted bwrap sandbox (gates/lib/sandbox.sh). A stub sees
# only read-only binds ($T/bin, $T/ctl and the lane/this repo's venv + its toolchain, via GATES_SANDBOX_RO)
# and reports containment as its OWN verdict through $T/bin/probe_lib.py: a stub that sees a leak
# prints no evidence, so the gate FAILs. The network probe targets a loopback listener on the host.
mkdir "$T/bin" "$T/ctl"; C=$T/ctl
ctl() { if [ "$#" -eq 2 ]; then printf '%s' "$2" > "$C/$1"; else rm -f "$C/$1"; fi; }
RVENV=$(readlink -f "$ROOT/.venv"); UVROOT=$(dirname "$(dirname "$(sed -n 's/^home = //p' "$RVENV/pyvenv.cfg")")")
export GATES_SANDBOX_RO="$T/bin:$T/ctl:$RVENV:$UVROOT"
printf 'synthetic-probe-secret\n' > "$T/planted"
python3 -c 'import socket,time; s=socket.socket(); s.bind(("127.0.0.1",0)); s.listen(); open("'"$T"'/port","w").write(str(s.getsockname()[1])); time.sleep(1800)' & LPID=$!
for _ in $(seq 50); do [ -s "$T/port" ] && break; sleep 0.1; done
cat > "$T/bin/probe_lib.py" <<PROBE
import os, socket
ALLOW = {"ALLOW_LIVE_TRADING", "ALLOW_ORDER_TRANSMIT", "BROKER_MODE", "GATE_IO", "HOME", "LANG", "PATH", "PYTHONDONTWRITEBYTECODE", "PYTHONPATH"}
def leaks(lane=None):
    found = []
    if set(os.environ) - {"PWD", "SHLVL", "_", "OLDPWD"} != ALLOW or os.environ.get("BROKER_MODE") != "demo": found.append("env")
    if not os.environ.get("PYTHONPATH", "").endswith("/snap/src") or os.environ.get("HOME") == "$HOME": found.append("env")
    if any(os.path.exists(p) for p in ("$HOME/.ssh", "$HOME/.config/gh")): found.append("creds")
    try: open("$T/planted").read(); found.append("file")
    except OSError: pass
    try: socket.create_connection(("127.0.0.1", $(cat "$T/port")), 2).close(); found.append("net")
    except OSError: pass
    if lane and os.getcwd() == lane: found.append("cwd")
    return found
PROBE
# --- 40 evidence-at-head (a stub make inside the sandbox; a throwaway repo) ---------------------
cat > "$T/bin/make" <<STUB
#!/usr/bin/python3
import os, subprocess, sys; sys.path.insert(0, "$T/bin"); import probe_lib
C = "$C"; ctl = lambda k, d=None: open(os.path.join(C, k)).read() if os.path.exists(os.path.join(C, k)) else d
found = probe_lib.leaks(lane="$T/r40")
if sys.argv[1:] != ["gates"]: found.append("argv")
for kind, attempt in (("naive", lambda: open("a", "a").write("changed")),                      # the snapshot is read-only
                      ("hostile", lambda: (os.chmod("a", 0o644), open("a", "a").write("changed"))),
                      ("commit", lambda: subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "moved"], check=True, capture_output=True))):
    try: attempt(); found.append("write-" + kind)
    except (OSError, subprocess.CalledProcessError): pass
if os.path.exists("$T/r40/a"): found.append("lane-visible")                                   # the lane's files are not visible at all
try: open("dist/probe", "w").write("ok")                                                     # the declared outputs ARE writable
except OSError: found.append("no-dist")
if os.path.exists(".venv/pyvenv.cfg") and os.access(".venv", os.W_OK): found.append("venv-writable")
if ctl("netfail"): print("urllib3.exceptions.NameResolutionError: Temporary failure in name resolution"); sys.exit(2)
print("mypy: Success: no issues found in 12 source files")
if found: print("LEAK " + " ".join(found)); sys.exit(0)                                        # no summary: the gate FAILs
print(ctl("summary", "12 passed, 1 skipped, 3 warnings in 4.56s"))
sys.exit(int(ctl("rc", "0")))
STUB
chmod +x "$T/bin/make"
G40="$G/40-evidence-at-head.sh"; R40="$T/r40"; newrepo "$R40"; echo a > "$R40/a"; mkdir -p "$R40/src/chronos"; : > "$R40/src/chronos/__init__.py"; commit "$R40"
H=$(git -C "$R40" rev-parse HEAD); RCPT="$R40/.gates/40-evidence-at-head.json"
field() { python3 -c "import json,sys; r=json.load(open(sys.argv[1])); print($1)" "$RCPT"; }
g40() { (cd "$R40" && PATH="$T/bin:$PATH" CHRONOS_PY=/usr/bin/python3 PR_HEAD_SHA="${1:-$H}" run bash "$G40"); }
g40
check "1 40 PASS: make gates exit 0 at PR_HEAD_SHA == HEAD → one PASS line, exit 0" "passed evidence-at-head"
check "1 40 receipt on disk records {sha, exit, pytest counts} of that run" "[ \"\$(field \"r['sha'], r['exit'], r['pytest']['passed'], r['pytest']['failed'], r['pytest']['skipped']\")\" = \"$H 0 12 0 1\" ]"
check "1 40 PASS line names the sha and the counts" "grep -qF \"$H (12 passed, 0 failed, 1 skipped)\" $T/out"
check "r2:1 P1-1 40 that PASS means the stub ran as exactly 'make gates' and saw no leak: exactly the env allowlist, no runner HOME/credential dirs, no planted host file, no network, not the lane's cwd; every write to the snapshot or its git was refused, the lane's files were not visible; dist/ writable" "passed evidence-at-head && [ \"\$(cat $R40/a)\" = a ] && [ \"\$(git -C $R40 rev-list --count HEAD)\" = 1 ]"
(cd "$R40" && PATH="$T/bin:$PATH" CHRONOS_PY=/usr/bin/python3 DUMMY_CREDENTIAL=synthetic-probe-secret GH_TOKEN=synthetic-gh-token PR_HEAD_SHA=$H run bash "$G40")
check "r2:1 P1-1 40 Daybreak's DUMMY_CREDENTIAL probe: with credential variables in the runner's env, make gates still sees none → PASS" "passed evidence-at-head"
(cd "$R40" && DUMMY_CREDENTIAL=x "$T/bin/make" gates > "$T/host.out" 2>&1)
check "r2:1 P1-1 positive control: the same stub run OUTSIDE the sandbox reports every leak (env creds file net cwd write-naive write-hostile write-commit lane-visible; and dist/ is absent there)" "grep -qx 'LEAK env env creds file net cwd write-naive write-hostile write-commit lane-visible no-dist' $T/host.out"
git -C "$R40" reset -q --hard "$H"; git -C "$R40" clean -qfd -e .gates
ctl rc 2; ctl summary '3 failed, 9 passed in 1.00s'; g40; ctl rc; ctl summary
check "1 40 FAIL: make gates exit 2 → one FAIL line naming the exit, exit 1" "failed evidence-at-head 'make gates exited 2 at $H'"
check "1 40 the failing run still leaves its receipt (exit 2, 3 failed)" "[ \"\$(field \"r['exit'], r['pytest']['failed']\")\" = '2 3' ]"
ctl summary ''; g40; ctl summary
check "1 40 FAIL: a green make with no pytest summary records no counts → FAIL, never a PASS" "failed evidence-at-head '.*no pytest summary' && [ \"\$(field \"r['pytest']\")\" = None ]"
ctl netfail 1; g40; ctl netfail
check "r2:1 P1-1 40 FAIL: make gates needing the network the sandbox denies → a named FAIL (never a fallback to an unsandboxed run)" "failed evidence-at-head 'make gates needs outbound network'"
for bad_sum in '3 failed, 9 passed in 1.00s' '5 skipped in 0.10s' '1 error, 5 passed in 0.10s' '0 passed in 0.10s'; do
  ctl summary "$bad_sum"; g40; ctl summary
  check "r2:1 P1-a 40 FAIL: exit 0 with a contradictory summary ('$bad_sum') is never a PASS" "failed evidence-at-head 'the pytest summary is contradictory'"
done
ctl summary '5 passed in 0.1s
7 passed in 0.2s'; g40; ctl summary
check "r2:1 P1-a 40 FAIL: two pytest summaries are ambiguous → FAIL" "failed evidence-at-head 'make gates exited 0 but printed 2 pytest summaries'"
mkdir -p "$R40/.venv/bin"; echo 'home = /usr/bin' > "$R40/.venv/pyvenv.cfg"; g40
check "r2:2 P1-a 40 the lane .venv is bound read-only into the sandbox (the stub checks it cannot write it) and the lane's .venv stays writable on the host" "passed evidence-at-head && [ -w $R40/.venv/bin ]"
rm -rf "$R40/.venv"
(cd "$R40" && PATH="$T/bin:$PATH" CHRONOS_PY=/usr/bin/python3 PR_HEAD_SHA=$(printf '%040d' 0 | tr 0 a) run bash "$G40")
check "1 40 FAIL: PR_HEAD_SHA != checked-out HEAD → FAIL before make runs" "failed evidence-at-head 'checked-out HEAD $H is not PR_HEAD_SHA'"
for bad_sha in '' abc123 "${H}x"; do
  (cd "$R40" && PATH="$T/bin:$PATH" PR_HEAD_SHA="$bad_sha" run bash "$G40")
  check "1 40 FAIL: PR_HEAD_SHA '$bad_sha' is not a full 40-hex sha → FAIL before make runs" "failed evidence-at-head 'PR_HEAD_SHA must be a full 40-hex sha'"
done
echo b >> "$R40/a"; g40; git -C "$R40" checkout -q -- a
check "1 40 FAIL: a modified tracked file means HEAD does not name the tested bytes → FAIL before make runs" "failed evidence-at-head 'tracked files are modified'"
check "1 40 the receipt path is gitignored in this repo (so make security-gate's untracked check cannot trip on it)" "git -C $ROOT check-ignore -q .gates/40-evidence-at-head.json"
printf 'import os, sys\nsys.path.insert(0, os.path.join(os.getcwd(), "vendor"))\n' > "$R40/src/sitecustomize.py"; mkdir -p "$R40/vendor/chronos"; : > "$R40/vendor/chronos/__init__.py"; git -C "$R40" add -A; commit "$R40"; H2=$(git -C "$R40" rev-parse HEAD); g40 "$H2"
check "r2:1 P1-2 40 FAIL: chronos resolving outside <snapshot>/src (sitecustomize + a vendored copy) → the identity assert refuses before make runs" "failed evidence-at-head 'chronos does not import from the exact-head snapshot'"
git -C "$R40" reset -q --hard "$H"; git -C "$R40" clean -qfd -e .gates; g40
check "r2:2 P1-b 40 .gates is a 0700 dir and the receipt a 0600 file" "[ \"\$(stat -c %a $R40/.gates)\" = 700 ] && [ \"\$(stat -c %a $RCPT)\" = 600 ]"
# P1-b: Daybreak's .gates symlink, and a planted link/FIFO at the receipt path
gates_link_refused() { failed evidence-at-head '.gates is a symlink or not a directory' && [ -z "$(ls -A "$T/outside-receipts")" ]; }
receipt_link_refused() { failed evidence-at-head 'could not verify the snapshot or write the receipt' && [ "$(cat "$T/outside-receipt.json")" = outside ]; }
R40L="$T/r40link"; newrepo "$R40L"; echo a > "$R40L/a"; mkdir "$T/outside-receipts"; ln -s "$T/outside-receipts" "$R40L/.gates"
git -C "$R40L" add a; git -C "$R40L" add -f .gates; commit "$R40L"; HL=$(git -C "$R40L" rev-parse HEAD)
(cd "$R40L" && PATH="$T/bin:$PATH" CHRONOS_PY=/usr/bin/python3 PR_HEAD_SHA=$HL run bash "$G40")
check "r2:2 P1-b 40 FAIL: Daybreak's committed .gates symlink → FAIL before make runs; nothing written outside" "gates_link_refused"
printf 'outside\n' > "$T/outside-receipt.json"; rm -f "$RCPT"; ln -s "$T/outside-receipt.json" "$RCPT"; g40
check "r2:2 P1-b 40 FAIL: a symlink planted at the receipt path is refused, never written through" "receipt_link_refused"
rm -f "$RCPT"; mkfifo "$RCPT"; g40; rm -f "$RCPT"
check "r2:2 P1-b 40 FAIL: a FIFO planted at the receipt path is refused" "failed evidence-at-head 'could not verify the snapshot or write the receipt'"

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
# P1-b / K3 P2-1 / P2-2: pinned paths are tracked regular files inside their root, never followed
OUTSIDE_SUM=$(printf 'safe external fixture\n' | tee "$T/outside-fixture.bin" | sha256sum | cut -d' ' -f1)
mk50; ln -s "$T/outside-fixture.bin" "$B50/tests/fixtures/cap/escape.bin"; edit_json "$B50/tests/fixtures/cap/manifest.json" "d['files']['escape.bin'] = '$OUTSIDE_SUM'"; commit "$B50"; g50
check "r2:2 P1-b 50 FAIL: Daybreak's tracked escape.bin symlink with the matching digest → FAIL, never a PASS" "failed fixture-integrity 'tests/fixtures/cap/escape.bin is not a tracked regular file'"
HOSTSUM=$(sha256sum /etc/hostname | cut -d' ' -f1)
mk50; ln -s /etc/hostname "$B50/tests/fixtures/cap/link.json"; edit_json "$B50/tests/fixtures/cap/manifest.json" "d['files']['link.json'] = '0' * 64"; commit "$B50"; g50
check "r2:2 P2-1 50 FAIL: K3's link to /etc/hostname with a wrong digest → FAIL, and the outside file's sha256 is never printed (hash oracle closed)" "failed fixture-integrity 'tests/fixtures/cap/link.json is not a tracked regular file' && ! grep -qF $HOSTSUM $T/out $T/err"
mk50; echo '{"y":1}' > "$B50/tests/fixtures/cap/untracked.json"; edit_json "$B50/tests/fixtures/cap/manifest.json" "d['files']['untracked.json'] = '0' * 64"; g50
check "r2:2 P1-b 50 FAIL: a pinned file that exists but is untracked → FAIL" "failed fixture-integrity 'tests/fixtures/cap/untracked.json is not a tracked regular file'"
mk50; rm "$B50/tests/fixtures/cap/capture.json"; mkfifo "$B50/tests/fixtures/cap/capture.json"; g50
check "r2:2 P1-b 50 FAIL: a FIFO in the work tree at a tracked pinned path → FAIL, never a blocking read" "failed fixture-integrity 'tests/fixtures/cap/capture.json is not a regular file'"
mk50; mv "$B50/tests/fixtures/cap/capture.json" "$T/cap-outside.json"; ln -s "$T/cap-outside.json" "$B50/tests/fixtures/cap/capture.json"; g50
check "r2:2 P1-b 50 FAIL: a tracked pinned file swapped for an outside symlink in the work tree → FAIL" "failed fixture-integrity 'tests/fixtures/cap/capture.json resolves outside tests/fixtures'"
mk50; printf '[1,2]' > "$B50/tests/fixtures/cap/manifest.json"; g50
check "r2:1 P2-2 50 FAIL: K3's valid-JSON non-object manifest → exactly one FAIL line" "failed fixture-integrity 'tests/fixtures/cap/manifest.json is not a JSON object'"

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
cat > "$T/bin/probe60" <<STUB
#!/usr/bin/python3
import json, os, sys; sys.path.insert(0, "$T/bin"); import probe_lib
sys.stdin.read(); found = probe_lib.leaks(lane="$T/r60")
rec = {"error": "LEAK " + " ".join(found)} if found else {"returncode": 0, "stdout": "{}", "tracked": 1}
open(os.path.join(os.environ.get("GATE_IO", "/nonexistent"), "result.json"), "w").write(json.dumps(rec))
STUB
chmod +x "$T/bin/probe60"
(cd "$R60" && DUMMY_CREDENTIAL=synthetic-probe-secret GH_TOKEN=synthetic-gh-token CHRONOS_PY="$T/bin/probe60" run bash "$G60")
check "r2:1 P1-1 60 the sandboxed scan (it imports candidate code) sees no credential variable, runner HOME, planted file or network, and its recorded result is judged OUTSIDE → PASS" "passed secrets-baseline"
(cd "$R60" && CHRONOS_PY="$T/nopython" run bash "$G60")
check "1 60 FAIL: no python at CHRONOS_PY → one FAIL line" "failed secrets-baseline 'no python at'"
mk60; ln -s clean.py "$R60/link.py"; commit "$R60"; g60
check "r2:2 P1-b 60 FAIL: a tracked symlink among the scanner inputs → FAIL before the scan" "failed secrets-baseline 'link.py is a symlink or non-regular file'"
mk60; mv "$R60/.secrets.baseline" "$T/outside.baseline"; ln -s "$T/outside.baseline" "$R60/.secrets.baseline"; commit "$R60"; g60
check "r2:2 P1-b 60 FAIL: .secrets.baseline as a tracked symlink → FAIL" "failed secrets-baseline '.secrets.baseline is not a tracked regular file inside the tree'"
r0=$(bsum "$ROOT"); (cd "$ROOT" && run bash "$G60")
check "1 60 PASS on this repo (tracked files vs the reviewed baseline)" "passed secrets-baseline"
check "2 60 this repo's .secrets.baseline is byte-identical after the gate" "[ \"\$(bsum $ROOT)\" = \"$r0\" ]"
check "2 60 gate source never prints the hook's captured output (no echo/print of stdout/stderr)" "! grep -nE 'print\\((r|result|proc)\\.(stdout|stderr)|sys\\.std(out|err)\\.write\\((r|result|proc)\\.' $G60"

# --- 70 docs-claims-pinned (a stub python in a throwaway repo; one real run on this repo) -------
G70="$G/70-docs-claims-pinned.sh"; R70="$T/r70"
NAMED="tests/unit/test_adr_point_in_time_claims.py tests/unit/test_docs_map_skill_contract.py tests/unit/test_vision_completion_plan_prose.py"
mk70() { newrepo "$R70"; mkdir -p "$R70/tests/unit" "$R70/src/chronos"; : > "$R70/src/chronos/__init__.py"; for f in tests/unit/test_limitations_a_contract.py tests/unit/test_limitations_b_contract.py $NAMED; do echo '' > "$R70/$f"; done; commit "$R70"; }
cat > "$T/bin/fakepy" <<STUB
#!/usr/bin/python3
# fake pytest inside the sandbox: runs -c (the identity assert) for real; otherwise checks its own argv
# against \$C/argv and its containment, prints \$C/pysum, and writes a junit report per \$C/junit.
import os, re, sys; sys.path.insert(0, "$T/bin"); import probe_lib
if len(sys.argv) > 1 and sys.argv[1] == "-c":
    os.execv("/usr/bin/python3", ["python3", *sys.argv[1:]])
C = "$C"; ctl = lambda k, d=None: open(os.path.join(C, k)).read() if os.path.exists(os.path.join(C, k)) else d
found = probe_lib.leaks(lane="$T/r70")
argv = re.sub(r"--junitxml=\S*/io/junit\.xml", "--junitxml=J", " ".join(sys.argv[1:]))
if ctl("argv") is not None and argv != ctl("argv"): found.append("argv")
if found: print("LEAK " + " ".join(found)); sys.exit(8)
print(ctl("pysum", "5 passed in 0.10s"))
files = [a for a in sys.argv[1:] if a.endswith(".py")]; mode = ctl("junit", "pass")
junit = next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--junitxml=")), None)
if junit and mode != "none":
    body = "".join(f'<testcase classname="{f[:-3].replace("/", ".")}" name="t">' + ("<skipped/>" if mode == "skipped" else "") + "</testcase>"
                   for f in (files[:1] if mode == "onefile" else files))
    n = len(files[:1] if mode == "onefile" else files)
    open(junit, "w").write(f'<testsuites><testsuite tests="{n}" failures="0" errors="0" skipped="{n if mode == "skipped" else 0}">{body}</testsuite></testsuites>')
sys.exit(int(ctl("pyrc", "0")))
STUB
chmod +x "$T/bin/fakepy"
g70() { (cd "$R70" && CHRONOS_PY="$T/bin/fakepy" run bash "$G70"); }
mk70; g70
check "1 70 PASS: the named doc-contract set passes → one PASS line, exit 0" "passed docs-claims-pinned"
DUMMY_CREDENTIAL=synthetic-probe-secret GH_TOKEN=synthetic-gh-token g70
check "r2:1 P1-1 70 with credential variables in the runner's env, the sandboxed tests see no credential variable, runner HOME, planted file or network → PASS" "passed docs-claims-pinned"
ctl argv "-m pytest -q -p no:cacheprovider --junitxml=J tests/unit/test_limitations_a_contract.py tests/unit/test_limitations_b_contract.py $NAMED"; g70; ctl argv
check "1 70 adopts the existing tests: the sandboxed pytest ran over exactly the glob + the three named files, nothing skipped or deselected (the stub refuses any other argv)" "passed docs-claims-pinned"
ctl pyrc 1; ctl pysum '1 failed, 4 passed in 0.10s'; g70
check "1 70 FAIL: a failing doc-contract test → one FAIL line with the pytest summary, exit 1" "failed docs-claims-pinned 'pytest exited 1 \\(1 failed, 4 passed'"
ctl pyrc 5; ctl pysum 'no tests ran in 0.01s'; g70; ctl pyrc; ctl pysum
check "1 70 FAIL: pytest collected nothing (exit 5) → FAIL, never green" "failed docs-claims-pinned 'pytest collected no tests'"
ctl pysum '5 skipped in 0.10s'; g70; ctl pysum
check "r2:1 P1-c 70 FAIL: Daybreak's all-skipped summary with exit 0 → FAIL" "failed docs-claims-pinned \"pytest reported '5 skipped in 0.10s'\""
ctl pysum '4 passed, 1 deselected in 0.10s'; g70; ctl pysum
check "r2:1 P1-c 70 FAIL: a deselected test in the summary → FAIL" "failed docs-claims-pinned \"pytest reported '4 passed, 1 deselected\""
ctl junit skipped; g70; ctl junit
check "r2:1 P1-c 70 FAIL: a clean summary but a junit report showing skipped tests → FAIL (the trusted gate reads the report)" "failed docs-claims-pinned 'the junit report shows'"
ctl junit none; g70; ctl junit
check "r2:1 P1-c 70 FAIL: no junit report written → FAIL" "failed docs-claims-pinned 'no usable junit report was written'"
ctl junit onefile; g70; ctl junit
check "r2:1 P1-c 70 FAIL: a selected file with no executed test (the others pass) → FAIL naming it" "failed docs-claims-pinned 'no test executed from tests/unit/test_limitations_b_contract.py'"
check "r2:2 P1-c 70 the junit report lands in the gate's output dir, outside the tree (nothing new in the repo)" "[ -z \"\$(git -C $R70 status --porcelain --untracked-files=all)\" ]"
mk70; ln -s /etc/hostname "$R70/tests/unit/test_limitations_c_contract.py"; git -C "$R70" add tests; commit "$R70"; g70
check "r2:2 P1-b 70 FAIL: a named contract file that is a tracked symlink → FAIL, pytest never runs" "failed docs-claims-pinned 'tests/unit/test_limitations_c_contract.py is not a tracked regular file inside tests/unit'"
mk70; echo '' > "$R70/tests/unit/test_limitations_z_contract.py"; g70
check "r2:2 P1-b 70 FAIL: an UNTRACKED file matching the glob → FAIL, pytest never runs" "failed docs-claims-pinned 'tests/unit/test_limitations_z_contract.py is not a tracked regular file'"
mk70; git -C "$R70" rm -q tests/unit/test_docs_map_skill_contract.py; commit "$R70"; g70
check "1 70 FAIL: a named contract file missing → FAIL naming it, pytest never runs" "failed docs-claims-pinned 'tests/unit/test_docs_map_skill_contract.py is missing'"
mk70; git -C "$R70" rm -q 'tests/unit/test_limitations_*'; commit "$R70"; g70
check "1 70 FAIL: the limitations contract glob matches nothing → FAIL" "failed docs-claims-pinned 'no tests/unit/test_limitations_\\*_contract.py'"
check "1 70 the README lists the named set it runs" "grep -qF 'test_limitations_*_contract.py' $G/README.md && for f in $NAMED; do grep -qF \"\${f#tests/unit/}\" $G/README.md || exit 1; done"
(cd "$ROOT" && run bash "$G70")
check "1 70 PASS on this repo (the real doc-contract tests, real pytest)" "passed docs-claims-pinned"

# --- contract 4: the harness never edits existing tests or source ------------------------------
check "4 this repo's tracked tree is unchanged by the harness (git status empty)" "[ -z \"\$(git -C $ROOT status --porcelain --untracked-files=no)\" ]"

check "r2:3 no .github change on this branch (working tree vs origin/main)" "git -C $ROOT diff --quiet origin/main -- .github"
kill "$LPID" 2>/dev/null
echo "test_gates_40_70: $pass ok, $fail failed"
[ "$fail" -eq 0 ]
