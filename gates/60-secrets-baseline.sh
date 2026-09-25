#!/usr/bin/env bash
# 60-secrets-baseline.sh — a fresh secrets scan of TRACKED files shows no finding beyond .secrets.baseline.
# Reuses the release security gate's own scan path (scripts/verify_release_security.py: its tool-version
# check, tracked-file list and "tracked-file secret scan" command) against a temp COPY of the baseline,
# so detect-secrets never writes .secrets.baseline. The hook's output is never echoed: findings are
# reported as file:line type only. The scan imports candidate code, so it runs in the trusted bwrap
# sandbox (gates/lib/sandbox.sh) over an exact-head snapshot; the TRUSTED gate judges its recorded
# result outside. CHRONOS_PY overrides the venv python (the Makefile's PY).
set -uo pipefail
g=secrets-baseline
fail() { echo "FAIL: $g — $1" >&2; exit 1; }
top="$(git rev-parse --show-toplevel 2>/dev/null)" || fail "not inside a git work tree; run the gate from the repo root"
cd "$top" || fail "could not enter $top"
py="${CHRONOS_PY:-.venv/bin/python}"
[ -x "$py" ] || fail "no python at $py; create the venv (make's PY) or set CHRONOS_PY"
[ -f .secrets.baseline ] || fail ".secrets.baseline is missing"
inputs() { python3 - <<'PY'
import os, stat, subprocess
out = subprocess.run(["git", "ls-files", "-s", "-z"], capture_output=True, check=True).stdout
modes = {r.split(b"\t", 1)[1].decode(): r.split(b" ", 1)[0].decode() for r in out.split(b"\0") if r}
for need in (".secrets.baseline", "scripts/verify_release_security.py"):  # the scan's own inputs: tracked, regular, inside
    if modes.get(need) not in ("100644", "100755") or os.path.commonpath([os.path.realpath("."), os.path.realpath(need)]) != os.path.realpath("."):
        print(f"{need} is not a tracked regular file inside the tree"); raise SystemExit
for path, mode in modes.items():  # every scanner input: no link or special file is ever followed
    if mode not in ("100644", "100755") or (os.path.lexists(path) and not stat.S_ISREG(os.lstat(path).st_mode)):
        print(f"{path} is a symlink or non-regular file"); raise SystemExit
PY
}
bad="$(inputs 2>&1)" || fail "could not inspect the tracked tree ($(tail -n 1 <<< "$bad"))"
[ -z "$bad" ] || fail "$bad; refusing to scan through it — replace it with a regular tracked file"
. "$(dirname "$0")/lib/sandbox.sh" || fail "could not load the trusted sandbox launcher"
[ -z "$(git status --porcelain --untracked-files=no)" ] || fail "tracked files are modified, so HEAD's snapshot is not the tree you see; commit or stash, then re-run"
work="$(mktemp -d)" || fail "could not create a scratch dir"; trap 'rm -rf "$work"' EXIT
sandbox_snapshot "$work" || fail "could not build the exact-head snapshot"
sandbox_run "$work" true 2>/dev/null; [ "$?" -ne 125 ] || fail "the bwrap sandbox is unavailable; candidate code never runs unsandboxed — install/enable bwrap"
cp .secrets.baseline "$work/io/.secrets.baseline" || fail "could not copy the baseline into the scan's output dir"
# In the sandbox (candidate code): load the release script, build its tracked-file scan, run it against the
# baseline COPY, and record the raw result in the output dir. Nothing is judged in there.
sandbox_run "$work" "$py" - > /dev/null 2>&1 <<'PY'
import importlib.util, json, os, subprocess, sys
from importlib import metadata
from pathlib import Path
io, rec = Path(os.environ["GATE_IO"]), {}
try:
    spec = importlib.util.spec_from_file_location("release_security", Path.cwd() / "scripts/verify_release_security.py")
    gate = sys.modules[spec.name] = importlib.util.module_from_spec(spec); spec.loader.exec_module(gate)
    gate._require_exact_tool_versions(metadata.version)
    tracked = tuple(p for p in gate._tracked_files(Path.cwd()) if p != ".secrets.baseline")
    (cmd,) = [c for c in gate.build_scan_commands(python=Path(sys.executable), baseline=io / ".secrets.baseline", tracked_files=tracked) if c.name == "tracked-file secret scan"]
    result = subprocess.run(cmd.argv, capture_output=True)
    rec = {"returncode": result.returncode, "stdout": result.stdout.decode("utf-8", "replace"), "tracked": len(tracked)}
except Exception as error:  # the trusted judge reports it; its text is never echoed raw
    rec = {"error": type(error).__name__ + ": " + str(error)}
(io / "result.json").write_text(json.dumps(rec))
PY
judge() { python3 - "$work/io" <<'PY'
import json, os, re, stat, sys
io = sys.argv[1]
def read(name, cap=64 * 1024 * 1024):  # never through a link, never unbounded
    fd = os.open(os.path.join(io, name), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as f:
        if not stat.S_ISREG(os.fstat(f.fileno()).st_mode) or os.fstat(f.fileno()).st_size > cap:
            sys.exit(f"{name} in the scan's output dir is not a bounded regular file")
        return f.read()
before = open(".secrets.baseline", "rb").read()  # the LANE's reviewed baseline, read by the trusted gate
try:
    rec = json.loads(read("result.json"))
except (OSError, ValueError):
    sys.exit("the sandboxed scan left no readable result; run make security-gate by hand")
if "error" in rec:
    sys.exit("the sandboxed scan could not run (" + re.sub(r"[^ -~]", "", str(rec["error"]))[:160] + ")")
stale = read(".secrets.baseline") != before
if rec["returncode"] == 0 and not stale:
    print(f"{rec['tracked']} tracked files, no finding beyond .secrets.baseline"); sys.exit(0)
reviewed = {(f, s["type"], s["hashed_secret"]) for f, ss in json.loads(before)["results"].items() for s in ss}
STALE = ".secrets.baseline is stale (a reviewed finding moved); hand-edit and review the baseline, never let detect-secrets rewrite it"
try:
    found = [s for ss in json.loads(rec["stdout"])["results"].values() for s in ss]
except (ValueError, KeyError, AttributeError, TypeError):  # exit 3 = the hook updated its (temp) baseline and printed a notice
    sys.exit(STALE if stale else f"the secret scan exited {rec['returncode']} with unreadable output; run make security-gate by hand")
new = sorted({(str(s["filename"]), int(s["line_number"]), str(s["type"])) for s in found if (s["filename"], s["type"], s["hashed_secret"]) not in reviewed})
if new:
    clean = lambda v: re.sub(r"[^ -~]", "", v)[:120]
    shown = "; ".join(f"{clean(f)}:{n} {clean(t)}" for f, n, t in new[:10]) + (f"; and {len(new) - 10} more" if len(new) > 10 else "")
    sys.exit(f"{len(new)} new finding(s) vs .secrets.baseline: {shown}; remove the secret, or review it and hand-edit the baseline")
sys.exit(STALE)
PY
}
if out="$(judge 2>&1)"; then echo "PASS: $g — $out"; else echo "FAIL: $g — $(tail -n 1 <<< "$out")" >&2; exit 1; fi
