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
def leaks(lane=None, extra=()):
    found = []
    if set(os.environ) - {"PWD", "SHLVL", "_", "OLDPWD"} != ALLOW | set(extra) or os.environ.get("BROKER_MODE") != "demo": found.append("env")
    if not os.environ.get("PYTHONPATH", "").split(":")[0].endswith("/snap/src") or os.environ.get("HOME") == "$HOME": found.append("env")
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
target = sys.argv[1:]
extra = ("PIP_NO_INDEX", "PIP_FIND_LINKS") if target == ["release-gate"] else ()
found = probe_lib.leaks(lane="$T/r40", extra=extra)
if len(target) != 1 or target[0] not in ("lint", "format-check", "type", "type-worker", "test", "release-gate"): found.append("argv")
if extra and (os.environ.get("PIP_NO_INDEX") != "1" or not os.path.isdir(os.environ.get("PIP_FIND_LINKS", "")) or os.access(os.environ["PIP_FIND_LINKS"], os.W_OK)):
    found.append("no-offline-cache")                                                          # release-gate: index off, cache read-only
for kind, attempt in (("naive", lambda: open("a", "a").write("changed")),                      # the snapshot is read-only
                      ("hostile", lambda: (os.chmod("a", 0o644), open("a", "a").write("changed"))),
                      ("commit", lambda: subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "moved"], check=True, capture_output=True))):
    try: attempt(); found.append("write-" + kind)
    except (OSError, subprocess.CalledProcessError): pass
if os.path.exists("$T/r40/a"): found.append("lane-visible")                                   # the lane's files are not visible at all
if "TRUSTED-MAKEFILE" not in open("Makefile").read(): found.append("untrusted-makefile")        # main's Makefile, never the candidate's
if target == ["release-gate"] and any("TRUSTED" not in open(f).read() for f in ("scripts/verify_release_artifact.py", "scripts/verify_pip_bootstrap.py")):
    found.append("untrusted-release-scripts")
if target == ["test"]:
    try: open("data/probe.db", "w").write("ok")                                              # data/: writable scratch
    except OSError: found.append("no-data")
    import shutil
    if shutil.which("age") != "$T/tooldir/age" or os.path.exists("$T/tooldir/sibling.txt"): found.append("binary-parity")
    if subprocess.run(["git", "rev-parse", "-q", "--verify", "refs/remotes/origin/main"], capture_output=True).returncode: found.append("no-origin-main")
try: open("dist/probe", "w").write("ok")                                                     # the declared outputs ARE writable
except OSError: found.append("no-dist")
if os.path.exists(".venv/pyvenv.cfg") and os.access(".venv", os.W_OK): found.append("venv-writable")
if found: print("LEAK " + " ".join(found)); sys.exit(7)                                        # a leak fails the target
if target == [ctl("rctarget", "test")] and ctl("netfail"): print("urllib3.exceptions.NameResolutionError: Temporary failure in name resolution"); sys.exit(2)
print("mypy: Success: no issues found in 12 source files")
if target == ["test"]: print(ctl("summary", "12 passed, 1 skipped, 3 warnings in 4.56s"))
sys.exit(int(ctl("rc", "0")) if target == [ctl("rctarget", "test")] else 0)
STUB
chmod +x "$T/bin/make"
# the trusted tools venv, stubbed (GATES_TOOLS_VENV): `pip download` and `pip_audit` on the HOST, driven by $C
mkdir -p "$T/tools/bin" "$T/xdg"; cat > "$T/tools/bin/python" <<STUB
#!/usr/bin/python3
import json, os, sys
C = "$C"; ctl = lambda k, d=None: open(os.path.join(C, k)).read() if os.path.exists(os.path.join(C, k)) else d
a = sys.argv[1:]
if a[:3] == ["-m", "pip", "download"]:
    open("$T/dl.log", "a").write(a[a.index("-r") + 1] + "\\n")
    open(os.path.join(a[a.index("-d") + 1], "stub-1.0-py3-none-any.whl"), "w").write("x")
    sys.exit(int(ctl("dl_rc", "0")))
if a[:2] == ["-m", "pip_audit"]:
    if ctl("audit") != "none":
        open(a[a.index("-o") + 1], "w").write(ctl("audit", json.dumps({"dependencies": [{"name": "x", "version": "1", "vulns": []}]})))
    sys.exit(int(ctl("audit_rc", "0")))
sys.exit(99)
STUB
chmod +x "$T/tools/bin/python"
G40="$G/40-evidence-at-head.sh"; R40="$T/r40"; newrepo "$R40"; echo a > "$R40/a"; mkdir -p "$R40/src/chronos" "$R40/scripts"; : > "$R40/src/chronos/__init__.py"
for l in bootstrap build runtime sbom; do printf '# locked by uv\nexample==1.0 \\\n    --hash=sha256:%064d\n' 0 > "$R40/requirements-$l.lock"; done
cat > "$R40/scripts/verify_release_security.py" <<FAKE
# a stand-in release security script: its pip-audit command is \`false\` (it must never run in the sandbox)
import os, sys; sys.path.insert(0, "$T/bin"); import probe_lib
class SecurityGateError(RuntimeError): pass
class _Cmd:
    def __init__(self, name, argv): self.name, self.argv = name, argv
def _require_exact_tool_versions(getter): pass
def _tracked_files(root): return ("a",)
def build_scan_commands(*, python, baseline, tracked_files):
    return (_Cmd("runtime dependency audit", ("false",)), _Cmd("Python static analysis", ("true",)), _Cmd("tracked-file secret scan", ("true",)))
def verify_git_history_secrets(root, baseline):
    found = probe_lib.leaks(lane="$T/r40")
    if found or os.path.exists("$C/histfail"): raise SecurityGateError("LEAK " + " ".join(found))
FAKE
printf '.venv/\n.gates/\ndata/\n' > "$R40/.gitignore"   # as in the repo: .gates/, the venv and data/ are ignored
printf '# TRUSTED-MAKEFILE (the trusted base)\n' > "$R40/Makefile"
printf '# TRUSTED release script\n' > "$R40/scripts/verify_release_artifact.py"; printf '# TRUSTED pip bootstrap\n' > "$R40/scripts/verify_pip_bootstrap.py"
commit "$R40"; git -C "$R40" branch -q trusted-main; git -C "$R40" update-ref refs/remotes/origin/main trusted-main
# the CANDIDATE on top: Daybreak's no-op Makefile and a gutted security script (its history scan checks nothing)
printf 'lint format-check type type-worker test release-gate:\n\t@echo "12 passed in 0.1s"\n' > "$R40/Makefile"
printf '# CANDIDATE\n' > "$R40/scripts/verify_release_artifact.py"; printf '# CANDIDATE\n' > "$R40/scripts/verify_pip_bootstrap.py"
cat > "$R40/scripts/verify_release_security.py" <<'GUT'
class SecurityGateError(RuntimeError): pass
class _Cmd:
    def __init__(self, name, argv): self.name, self.argv = name, argv
def _require_exact_tool_versions(getter): pass
def _tracked_files(root): return ()
def build_scan_commands(*, python, baseline, tracked_files): return ()
def verify_git_history_secrets(root, baseline): pass
GUT
commit "$R40"
mkdir -p "$T/tooldir"; printf '#!/bin/sh\necho age\n' > "$T/tooldir/age"; chmod +x "$T/tooldir/age"; echo secret > "$T/tooldir/sibling.txt"
H=$(git -C "$R40" rev-parse HEAD); RCPT="$R40/.gates/40-evidence-at-head.json"
field() { python3 -c "import json,sys; r=json.load(open(sys.argv[1])); print($1)" "$RCPT"; }
g40() { (cd "$R40" && PATH="$T/bin:$PATH" CHRONOS_PY=/usr/bin/python3 GATES_TOOLS_VENV="$T/tools" XDG_CACHE_HOME="$T/xdg" \
  GATES_TRUSTED_REPO="$R40" GATES_TRUSTED_REF=trusted-main GATES_SANDBOX_RO="$GATES_SANDBOX_RO:$T/tooldir/age" PR_HEAD_SHA="${1:-$H}" run bash "$G40"); }
g40
check "1 40 PASS: make gates exit 0 at PR_HEAD_SHA == HEAD → one PASS line, exit 0" "passed evidence-at-head"
check "r4:2 P3 40 the PASS ran the TRUSTED base's Makefile (the candidate's no-op Makefile never ran: the stub refuses any Makefile without the trusted marker) and the trusted release scripts" "passed evidence-at-head && grep -q echo $R40/Makefile"
check "r4:1 PB 40 that PASS also means: data/ was writable scratch inside, the lane's data/ untouched; the declared binary (age) was on PATH while its sibling file stayed invisible; origin/main resolved in the snapshot" "passed evidence-at-head && [ ! -e $R40/data ]"
check "r4:2 P3 40 the receipt names the trusted base it took the definitions from" "[ \"\$(field \"r['trusted_base']\")\" = \"\$(git -C $R40 rev-parse trusted-main)\" ]"
check "1 40 receipt on disk records {sha, exit, pytest counts} of that run" "[ \"\$(field \"r['sha'], r['exit'], r['pytest']['passed'], r['pytest']['failed'], r['pytest']['skipped']\")\" = \"$H 0 12 0 1\" ]"
check "1 40 PASS line names the sha and the counts" "grep -qF \"$H (12 passed, 0 failed, 1 skipped)\" $T/out"
check "r2:1 P1-1 40 that PASS means the stub ran as exactly 'make gates' and saw no leak: exactly the env allowlist, no runner HOME/credential dirs, no planted host file, no network, not the lane's cwd; every write to the snapshot or its git was refused, the lane's files were not visible; dist/ writable" "passed evidence-at-head && [ \"\$(cat $R40/a)\" = a ] && [ \"\$(git -C $R40 rev-list --count HEAD)\" = 2 ]"
DUMMY_CREDENTIAL=synthetic-probe-secret GH_TOKEN=synthetic-gh-token g40
check "r2:1 P1-1 40 Daybreak's DUMMY_CREDENTIAL probe: with credential variables in the runner's env, make gates still sees none → PASS" "passed evidence-at-head"
(cd "$R40" && DUMMY_CREDENTIAL=x "$T/bin/make" test > "$T/host.out" 2>&1)
check "r2:1 P1-1 positive control: the same stub run OUTSIDE the sandbox reports every leak (env creds file net cwd write-naive write-hostile write-commit lane-visible; the candidate's no-op Makefile; no data/ or dist/ there; the host's own age)" "grep -qx 'LEAK env env creds file net cwd write-naive write-hostile write-commit lane-visible untrusted-makefile no-data binary-parity no-dist' $T/host.out"
git -C "$R40" reset -q --hard "$H"; git -C "$R40" clean -qfd -e .gates
ctl rc 2; ctl summary '3 failed, 9 passed in 1.00s'; g40; ctl rc; ctl summary
check "1 40 FAIL: make gates exit 2 → one FAIL line naming the exit, exit 1" "failed evidence-at-head 'make gates exited 2 at $H'"
check "1 40 the failing run still leaves its receipt (exit 2, 3 failed)" "[ \"\$(field \"r['exit'], r['pytest']['failed']\")\" = '2 3' ]"
ctl summary ''; g40; ctl summary
check "1 40 FAIL: a green make with no pytest summary records no counts → FAIL, never a PASS" "failed evidence-at-head '.*no pytest summary' && [ \"\$(field \"r['pytest']\")\" = None ]"
ctl netfail 1; g40; ctl netfail
check "r2:1 P1-1 40 FAIL: a sandboxed target needing the network the sandbox denies → a named FAIL (never a fallback to an unsandboxed run)" "failed evidence-at-head 'the test target needs outbound network'"
for bad_sum in '3 failed, 9 passed in 1.00s' '5 skipped in 0.10s' '1 error, 5 passed in 0.10s' '0 passed in 0.10s'; do
  ctl summary "$bad_sum"; g40; ctl summary
  check "r2:1 P1-a 40 FAIL: exit 0 with a contradictory summary ('$bad_sum') is never a PASS" "failed evidence-at-head 'the pytest summary is contradictory'"
done
ctl summary '5 passed in 0.1s
7 passed in 0.2s'; g40; ctl summary
check "r2:1 P1-a 40 FAIL: two pytest summaries are ambiguous → FAIL" "failed evidence-at-head 'make gates exited 0 but printed 2 pytest summaries'"
# r3: the split — trusted steps over the locks as DATA, every target recorded, the receipt binds it all
: > "$T/dl.log"; g40
check "r3:1 40 PASS: every target (lint format-check type type-worker test release-gate security-offline) ran sandboxed and exited 0; the stub release-gate saw PIP_NO_INDEX and a READ-ONLY wheel cache; the fake security script's pip-audit command (\`false\`) was never run in the sandbox" "passed evidence-at-head && [ \"\$(field \"' '.join(f'{k}={v}' for k, v in sorted(r['targets'].items()))\")\" = 'format-check=0 lint=0 release-gate=0 security-offline=0 test=0 type=0 type-worker=0' ]"
check "r3:1 40 the receipt binds the four lock digests, the wheel-cache digest and the trusted audit" "[ \"\$(field \"sorted(r['locks']) == ['bootstrap', 'build', 'runtime', 'sbom'] and all(len(v) == 64 for v in r['locks'].values()) and len(r['cache_sha256']) == 64 and r['audit']['dependencies'] == 1\")\" = True ]"
check "r3:1 40 the trusted wheel pre-fetch ran on the HOST over the SNAPSHOT's four locks (never the lane's files, never in the sandbox)" "[ \"\$(sed -E 's#^.*/snap/##' $T/dl.log | sort | tr '\\n' ' ')\" = 'requirements-bootstrap.lock requirements-build.lock requirements-runtime.lock requirements-sbom.lock ' ]"
for inj in '--index-url https://example.invalid/simple' 'https://example.invalid/pkg.whl' '-r other.txt' '-e .'; do
  printf '%s\n' "$inj" >> "$R40/requirements-build.lock"; commit "$R40"; g40 "$(git -C "$R40" rev-parse HEAD)"; git -C "$R40" reset -q --hard "$H"
  check "r3:1 40 FAIL: an option/URL/path line in a lock ('$inj') is refused before any trusted network step reads it" "failed evidence-at-head 'requirements-build.lock:4 is not a pinned requirement'"
done
ctl dl_rc 1; g40; ctl dl_rc
check "r3:1 40 FAIL: a lock that cannot be pre-fetched as hash-pinned wheels → FAIL, never an online install" "failed evidence-at-head 'could not pre-fetch requirements-bootstrap.lock'"
ctl audit '{"dependencies": [{"name": "urllib3", "version": "1.0", "vulns": [{"id": "GHSA-xxxx"}]}]}'; ctl audit_rc 1; g40; ctl audit; ctl audit_rc
check "r3:1 40 FAIL: the trusted pip-audit reports a vulnerability → FAIL naming it" "failed evidence-at-head '1 known vulnerabilit\\(ies\\) in requirements-runtime.lock: urllib3==1.0 GHSA-xxxx'"
ctl audit none; ctl audit_rc 1; g40; ctl audit; ctl audit_rc
check "r3:1 40 FAIL: the trusted pip-audit leaves no report (e.g. its advisory service is unreachable) → FAIL" "failed evidence-at-head 'the trusted pip-audit exited 1 without a readable report'"
ctl rctarget type; ctl rc 1; g40; ctl rc; ctl rctarget
check "r3:2 40 FAIL: one failing target is reported by name, and the others still ran (recorded)" "failed evidence-at-head 'make gates exited 1 at $H \\(target type\\)' && [ \"\$(field \"len(r['targets'])\")\" = 7 ]"
ctl histfail 1; g40; ctl histfail
check "r4:2 P3 40 FAIL: with the candidate's GUTTED security script in the tree, the TRUSTED base's script still runs, so a failing history scan still FAILs (Daybreak's probe)" "failed evidence-at-head '.*target security-offline'"
echo x > "$R40/untracked.txt"; g40; rm -f "$R40/untracked.txt"
check "r3:2 40 FAIL: an untracked file (make security-gate's precheck, done by the trusted gate) → FAIL" "failed evidence-at-head 'untracked files in the tree'"
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
g60() { (cd "$R60" && run bash "$G60"); }
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
# Daybreak's forgery probe: a candidate release script that would write a PASS result — it is never run now
mk60; printf 'import json, os\nopen(os.path.join(os.environ.get("GATE_IO", "."), "result.json"), "w").write(json.dumps({"returncode": 0, "stdout": "{}", "tracked": 1}))\n' > "$R60/scripts/verify_release_security.py"
printf 'x = 1\naws_key = "%s"\n' "$FAKE" > "$R60/leak.py"; commit "$R60"; g60
check "r3:1 P5 60 FAIL: Daybreak's forged-result probe — the candidate release script is never executed, so a planted tracked secret still FAILs" "failed secrets-baseline '1 new finding\\(s\\) vs .secrets.baseline: leak.py:2 AWS Access Key'"
mk60 "d['plugins_used'].append({'name': 'EvilDetector', 'path': 'file://./evil.py'})"; g60
check "r4:1 P6 60 FAIL: a baseline whose plugins_used differs from the trusted base's (an added path plugin) → FAIL before the scanner runs" "failed secrets-baseline \".secrets.baseline's plugins_used differs from the trusted base's\""
mk60 "d['filters_used'].append({'path': 'file://./evil.py::f'})"; g60
check "r4:1 P6 60 FAIL: a baseline whose filters_used differs from the trusted base's → FAIL" "failed secrets-baseline \".secrets.baseline's filters_used differs from the trusted base's\""
mk60 "d['plugins_used'] = []"; printf 'x = 1\naws_key = "%s"\n' "$FAKE" > "$R60/leak.py"; commit "$R60"; g60
check "r4:1 P6 60 FAIL: Daybreak's empty-plugins probe (a baseline with no plugins + a planted AWS-shaped secret) → FAIL, never a clean scan" "failed secrets-baseline \".secrets.baseline's plugins_used differs from the trusted base's\""
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
mk70() { newrepo "$R70"; mkdir -p "$R70/tests/unit" "$R70/src/chronos"; : > "$R70/src/chronos/__init__.py"; for f in tests/unit/test_limitations_a_contract.py tests/unit/test_limitations_b_contract.py $NAMED; do printf 'def test_a():\n    pass\n\n\ndef test_b():\n    pass\n' > "$R70/$f"; done; commit "$R70"; }
cat > "$T/bin/fakepy" <<STUB
#!/usr/bin/python3
# fake pytest inside the sandbox: runs -c (the identity assert) for real; otherwise checks its own argv
# against \$C/argv, its containment and the autoload-off env, prints \$C/pysum, and writes node reports to
# \$GATE_IO/nodes.jsonl the way the trusted gates_nodes reporter does, per \$C/nodes (default: all pass).
import json, os, re, sys; sys.path.insert(0, "$T/bin"); import probe_lib
if len(sys.argv) > 1 and sys.argv[1] == "-c" and not sys.argv[2].startswith("/"):
    os.execv("/usr/bin/python3", ["python3", *sys.argv[1:]])
C = "$C"; ctl = lambda k, d=None: open(os.path.join(C, k)).read() if os.path.exists(os.path.join(C, k)) else d
found = probe_lib.leaks(lane="$T/r70", extra=("PYTEST_DISABLE_PLUGIN_AUTOLOAD",))
if os.environ.get("PYTEST_DISABLE_PLUGIN_AUTOLOAD") != "1" or not os.environ.get("PYTHONPATH", "").endswith("/gates/lib"): found.append("autoload")
argv = re.sub(r"-c \S*/trusted/pytest\.ini --rootdir \S*/snap ", "-c TRUSTED --rootdir SNAP ", " ".join(sys.argv[1:]))
if ctl("argv") is not None and argv != ctl("argv"): found.append("argv")
if found: print("LEAK " + " ".join(found)); sys.exit(8)
print(ctl("pysum", "5 passed in 0.10s"))
files = [a for a in sys.argv[1:] if a.endswith(".py")]; mode = ctl("nodes", "pass")
rows = {"pass": [(f"{f}::{t}", "passed") for f in files for t in ("test_a", "test_b")],
        "skipped": [(f"{f}::{t}", "skipped") for f in files for t in ("test_a", "test_b")],
        "onefile": [(f"{files[0]}::{t}", "passed") for t in ("test_a", "test_b")],
        "missingfn": [(f"{f}::test_a", "passed") for f in files],
        "foreign": [(f"{f}::{t}", "passed") for f in files for t in ("test_a", "test_b")] + [("tests/other/test_x.py::test_x", "passed")],
        "none": []}.get(mode, [])
if mode == "junitonly":  # Daybreak's forged zero-test JUnit: a report the gate no longer reads
    open(os.path.join(os.environ["GATE_IO"], "junit.xml"), "w").write('<testsuites><testsuite tests="0" failures="0" errors="0" skipped="0"/></testsuites>')
elif mode != "none":
    with open(os.path.join(os.environ["GATE_IO"], "nodes.jsonl"), "w") as out:
        for nodeid, outcome in rows:
            for when in ("setup", "call", "teardown"):
                out.write(json.dumps({"nodeid": nodeid, "when": when, "outcome": outcome if when == "call" else "passed"}) + "\\n")
sys.exit(int(ctl("pyrc", "0")))
STUB
chmod +x "$T/bin/fakepy"
g70() { (cd "$R70" && CHRONOS_PY="$T/bin/fakepy" run bash "$G70"); }
mk70; g70
check "1 70 PASS: the named doc-contract set passes → one PASS line, exit 0" "passed docs-claims-pinned"
DUMMY_CREDENTIAL=synthetic-probe-secret GH_TOKEN=synthetic-gh-token g70
check "r2:1 P1-1 70 with credential variables in the runner's env, the sandboxed tests see no credential variable, runner HOME, planted file or network → PASS" "passed docs-claims-pinned"
ctl argv "-m pytest -q -p no:cacheprovider -c TRUSTED --rootdir SNAP --noconftest -p gates_nodes tests/unit/test_limitations_a_contract.py tests/unit/test_limitations_b_contract.py $NAMED"; g70; ctl argv
check "1 70 adopts the existing tests: the sandboxed pytest ran over exactly the glob + the three named files, with plugin autoload OFF, a trusted empty ini, --noconftest and the trusted reporter (the stub refuses any other argv/env)" "passed docs-claims-pinned"
ctl pyrc 1; ctl pysum '1 failed, 4 passed in 0.10s'; g70
check "1 70 FAIL: a failing doc-contract test → one FAIL line with the pytest summary, exit 1" "failed docs-claims-pinned 'pytest exited 1 \\(1 failed, 4 passed'"
ctl pyrc 5; ctl pysum 'no tests ran in 0.01s'; g70; ctl pyrc; ctl pysum
check "1 70 FAIL: pytest collected nothing (exit 5) → FAIL, never green" "failed docs-claims-pinned 'pytest collected no tests'"
ctl pysum '5 skipped in 0.10s'; g70; ctl pysum
check "r2:1 P1-c 70 FAIL: Daybreak's all-skipped summary with exit 0 → FAIL" "failed docs-claims-pinned \"pytest reported '5 skipped in 0.10s'\""
ctl pysum '4 passed, 1 deselected in 0.10s'; g70; ctl pysum
check "r2:1 P1-c 70 FAIL: a deselected test in the summary → FAIL" "failed docs-claims-pinned \"pytest reported '4 passed, 1 deselected\""
ctl nodes skipped; g70; ctl nodes
check "r3:1 P6 70 FAIL: a clean summary but node reports showing skipped outcomes → FAIL naming the node" "failed docs-claims-pinned 'tests/unit/test_limitations_a_contract.py::test_a reported skipped'"
ctl nodes none; g70; ctl nodes
check "r3:1 P6 70 FAIL: no node report written → FAIL" "failed docs-claims-pinned 'no usable node report was written by the trusted reporter'"
ctl nodes junitonly; g70; ctl nodes
check "r3:1 P6 70 FAIL: Daybreak's zero-test forged JUnit (exit 0, a clean summary) → FAIL, the gate no longer reads JUnit" "failed docs-claims-pinned 'no usable node report was written'"
ctl nodes onefile; g70; ctl nodes
check "r3:1 P6 70 FAIL: a selected file with no passing test (the others pass) → FAIL naming it" "failed docs-claims-pinned 'no passing test for tests/unit/test_limitations_b_contract.py::test_a'"
ctl nodes missingfn; g70; ctl nodes
check "r3:1 P6 70 FAIL: a report missing one test function the trusted AST expects (test_b) → FAIL naming it" "failed docs-claims-pinned 'no passing test for tests/unit/test_limitations_a_contract.py::test_b'"
ctl nodes foreign; g70; ctl nodes
check "r3:1 P6 70 FAIL: a report naming a test outside the selected files → FAIL" "failed docs-claims-pinned 'a report names tests/other/test_x.py::test_x'"
check "r2:2 P1-c 70 the node report lands in the gate's output dir, outside the tree (nothing new in the repo)" "[ -z \"\$(git -C $R70 status --porcelain --untracked-files=all)\" ]"
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
