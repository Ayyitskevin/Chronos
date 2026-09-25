#!/usr/bin/env bash
# 60-secrets-baseline.sh — a fresh secrets scan of TRACKED files shows no finding beyond .secrets.baseline.
# Reuses the release security gate's own scan path (scripts/verify_release_security.py: its tool-version
# check, tracked-file list and "tracked-file secret scan" command) against a temp COPY of the baseline,
# so detect-secrets never writes .secrets.baseline. The hook's output is never echoed: findings are
# reported as file:line type only. The scan imports candidate code, so it runs under `env -i` with a
# fixed allowlist and a throwaway HOME. CHRONOS_PY overrides the venv python (the Makefile's PY).
set -uo pipefail
g=secrets-baseline
fail() { echo "FAIL: $g — $1" >&2; exit 1; }
top="$(git rev-parse --show-toplevel 2>/dev/null)" || fail "not inside a git work tree; run the gate from the repo root"
cd "$top" || fail "could not enter $top"
py="${CHRONOS_PY:-.venv/bin/python}"
[ -x "$py" ] || fail "no python at $py; create the venv (make's PY) or set CHRONOS_PY"
[ -f .secrets.baseline ] || fail ".secrets.baseline is missing"
home="$(mktemp -d)" || fail "could not create a temp HOME"; trap 'rm -rf "$home"' EXIT
scan() { env -i PATH=/usr/local/bin:/usr/bin:/bin HOME="$home" LANG=C.UTF-8 BROKER_MODE=demo ALLOW_ORDER_TRANSMIT=false ALLOW_LIVE_TRADING=false PYTHONDONTWRITEBYTECODE=1 "$py" - <<'PY'
import importlib.util, json, shutil, subprocess, sys, tempfile
from importlib import metadata
from pathlib import Path
root, baseline = Path.cwd(), Path(".secrets.baseline")
spec = importlib.util.spec_from_file_location("release_security", root / "scripts/verify_release_security.py")
gate = sys.modules[spec.name] = importlib.util.module_from_spec(spec); spec.loader.exec_module(gate)
try:
    gate._require_exact_tool_versions(metadata.version)
except gate.SecurityGateError as error:
    sys.exit(f"{error}; install the pinned scanner versions")
before = baseline.read_bytes()
tracked = tuple(p for p in gate._tracked_files(root) if p != ".secrets.baseline")
if not tracked:
    sys.exit("no tracked files; refusing an empty secret scan")
with tempfile.TemporaryDirectory(prefix="chronos-gate60-") as temp:
    copy = Path(temp) / baseline.name
    shutil.copyfile(baseline, copy)
    (cmd,) = [c for c in gate.build_scan_commands(python=Path(sys.executable), baseline=copy, tracked_files=tracked) if c.name == "tracked-file secret scan"]
    result = subprocess.run(cmd.argv, cwd=root, capture_output=True)
    stale = copy.read_bytes() != before
if baseline.read_bytes() != before:
    sys.exit(".secrets.baseline changed during the scan; restore it with git checkout -- .secrets.baseline")
if result.returncode == 0 and not stale:
    print(f"{len(tracked)} tracked files, no finding beyond .secrets.baseline"); sys.exit(0)
reviewed = {(f, s["type"], s["hashed_secret"]) for f, ss in json.loads(before)["results"].items() for s in ss}
STALE = ".secrets.baseline is stale (a reviewed finding moved); hand-edit and review the baseline, never let detect-secrets rewrite it"
try:
    found = [s for ss in json.loads(result.stdout)["results"].values() for s in ss]
except (ValueError, KeyError, AttributeError):  # exit 3 = the hook updated its (temp) baseline and printed a notice
    sys.exit(STALE if stale else f"the secret scan exited {result.returncode} with unreadable output; run make security-gate by hand")
new = sorted({(s["filename"], s["line_number"], s["type"]) for s in found if (s["filename"], s["type"], s["hashed_secret"]) not in reviewed})
if new:
    shown = "; ".join(f"{f}:{n} {t}" for f, n, t in new[:10]) + (f"; and {len(new) - 10} more" if len(new) > 10 else "")
    sys.exit(f"{len(new)} new finding(s) vs .secrets.baseline: {shown}; remove the secret, or review it and hand-edit the baseline")
sys.exit(STALE)
PY
}
if out="$(scan 2>&1)"; then echo "PASS: $g — $out"; else echo "FAIL: $g — $(tail -n 1 <<< "$out")" >&2; exit 1; fi
