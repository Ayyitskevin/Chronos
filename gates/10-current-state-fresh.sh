#!/usr/bin/env bash
# 10-current-state-fresh.sh — docs/generated/ must match its state inputs.
# Read-only: runs `scripts/build_current_state.py --check` (never regenerates, so it writes nothing),
# after refusing any tracked symlink/gitlink or non-regular path and any docs/generated output that is
# not a regular file inside the tree. The check is candidate code: it runs in the trusted bwrap sandbox
# (gates/lib/sandbox.sh) over an immutable snapshot of HEAD — no network, no runner home, one writable
# 0700 dir — after asserting that `chronos` imports from that snapshot. CHRONOS_PY overrides the python.
set -uo pipefail
g=current-state-fresh
fail() { echo "FAIL: $g — $1" >&2; exit 1; }
top="$(git rev-parse --show-toplevel 2>/dev/null)" || fail "not inside a git work tree; run the gate from the repo root"
cd "$top" || fail "could not enter $top"
. "$(dirname "$0")/lib/sandbox.sh" || fail "could not load the trusted sandbox launcher"
contained() { python3 - <<'PY'
import os, stat, subprocess
top = os.path.realpath(".")
out = subprocess.run(["git", "ls-files", "-s", "-z"], capture_output=True, check=True).stdout
for rec in filter(None, out.split(b"\0")):
    mode, path = rec.split(b" ", 1)[0].decode(), rec.split(b"\t", 1)[1].decode()
    st = os.lstat(path) if os.path.lexists(path) else None
    if mode not in ("100644", "100755") or (st is not None and not stat.S_ISREG(st.st_mode)):
        print(f"{path} is a symlink or non-regular file"); break
else:
    for path, want in (("docs/generated", stat.S_ISDIR), ("docs/generated/CURRENT_STATE.md", stat.S_ISREG),
                       ("docs/generated/capability-matrix.json", stat.S_ISREG)):
        real = os.path.realpath(path)
        if not os.path.lexists(path) or not want(os.lstat(path).st_mode) or os.path.commonpath([top, real]) != top:
            print(f"{path} must be a real {'directory' if want is stat.S_ISDIR else 'file'} inside the tree"); break
PY
}
bad="$(contained 2>&1)" || fail "could not inspect the tracked tree ($(tail -n 1 <<< "$bad"))"
[ -z "$bad" ] || fail "$bad; refusing to run the generator over it — replace it with a regular tracked file"
[ -z "$(git status --porcelain --untracked-files=no)" ] || fail "tracked files are modified, so HEAD's snapshot is not the tree you see; commit or stash, then re-run"
py="${CHRONOS_PY:-.venv/bin/python}"
[ -x "$py" ] || fail "no python at $py; create the venv (make's PY) or set CHRONOS_PY"
work="$(mktemp -d)" || fail "could not create a scratch dir"; trap 'rm -rf "$work"' EXIT
sandbox_snapshot "$work" || fail "could not build the exact-head snapshot"
sandbox_run "$work" true 2>/dev/null; [ "$?" -ne 125 ] || fail "the bwrap sandbox is unavailable; candidate code never runs unsandboxed — install/enable bwrap"
sandbox_identity "$work" "$py" || fail "chronos does not import from the exact-head snapshot (an editable install or PYTHONPATH points elsewhere); refusing to judge"
out="$(sandbox_run "$work" "$py" scripts/build_current_state.py --check 2>&1)"
code=$?
[ "$code" -eq 0 ] && { echo "PASS: $g — generated page matches state inputs"; exit 0; }
stale="$(grep -oE '^generated artifact (stale|missing): docs/generated/[A-Za-z0-9._-]+$' <<< "$out" | head -n 1 | sed -E 's/^generated artifact (stale|missing): //')"
[ -n "$stale" ] && fail "$stale is stale; run 'make current-state' and commit it"
fail "the current-state check could not run (exit $code); run '$py scripts/build_current_state.py --check' by hand, fix the error, then re-run the gate"
