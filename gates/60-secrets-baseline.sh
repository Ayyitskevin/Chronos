#!/usr/bin/env bash
# 60-secrets-baseline.sh — a fresh secrets scan of TRACKED files shows no finding beyond .secrets.baseline.
# NO candidate code runs: the TRUSTED tools venv's detect-secrets (gates/lib/tools-venv.sh, built from
# origin/main's requirements-dev.lock) scans the exact-head snapshot as DATA, with the same hook command
# the release security gate uses (`detect_secrets.pre_commit_hook --baseline <copy> --json -- <tracked>`),
# inside the bwrap sandbox (no network) against a temp COPY of the baseline, so .secrets.baseline is never
# written. The scanner CONFIGURATION (plugins, filters) is the trusted base's .secrets.baseline; the
# candidate baseline contributes only its reviewed results, and its configuration must equal main's. The scanner's stdout is piped to the trusted judge — no result
# file — and findings are reported as file:line type only.
set -uo pipefail
g=secrets-baseline
fail() { echo "FAIL: $g — $1" >&2; exit 1; }
top="$(git rev-parse --show-toplevel 2>/dev/null)" || fail "not inside a git work tree; run the gate from the repo root"
cd "$top" || fail "could not enter $top"
[ -f .secrets.baseline ] || fail ".secrets.baseline is missing"
inputs() { python3 - <<'PY'
import os, stat, subprocess
out = subprocess.run(["git", "ls-files", "-s", "-z"], capture_output=True, check=True).stdout
modes = {r.split(b"\t", 1)[1].decode(): r.split(b" ", 1)[0].decode() for r in out.split(b"\0") if r}
if modes.get(".secrets.baseline") not in ("100644", "100755") or os.path.islink(".secrets.baseline"):
    print(".secrets.baseline is not a tracked regular file inside the tree"); raise SystemExit
for path, mode in modes.items():  # every scanner input: no link or special file is ever followed
    if mode not in ("100644", "100755") or (os.path.lexists(path) and not stat.S_ISREG(os.lstat(path).st_mode)):
        print(f"{path} is a symlink or non-regular file"); raise SystemExit
PY
}
bad="$(inputs 2>&1)" || fail "could not inspect the tracked tree ($(tail -n 1 <<< "$bad"))"
[ -z "$bad" ] || fail "$bad; refusing to scan through it — replace it with a regular tracked file"
. "$(dirname "$0")/lib/sandbox.sh" && . "$(dirname "$0")/lib/tools-venv.sh" || fail "could not load the trusted gate libraries"
[ -z "$(git status --porcelain --untracked-files=no)" ] || fail "tracked files are modified, so HEAD's snapshot is not the tree you see; commit or stash, then re-run"
tools="$(tools_venv 2>&1)" || fail "$(tail -n 1 <<< "$tools")"
work="$(mktemp -d)" || fail "could not create a scratch dir"; trap 'rm -rf "$work"' EXIT
sandbox_snapshot "$work" || fail "could not build the exact-head snapshot"
sandbox_run "$work" true 2>/dev/null; [ "$?" -ne 125 ] || fail "the bwrap sandbox is unavailable; nothing runs unsandboxed — install/enable bwrap"
trusted_show .secrets.baseline > "$work/trusted.baseline" && [ -s "$work/trusted.baseline" ] || fail "cannot read .secrets.baseline from the trusted base (${GATES_TRUSTED_REF:-origin/main})"
# The scanner CONFIGURATION (version, plugins, filters) is the trusted base's; the candidate baseline
# contributes only its reviewed `results` as data, and its own configuration must equal the trusted one.
why="$(python3 - .secrets.baseline "$work/trusted.baseline" "$work/scan.baseline" <<'PY' 2>&1
import json, sys
try:
    cand, trusted = (json.load(open(p, encoding="utf-8")) for p in sys.argv[1:3])
    results = cand["results"]
    if not isinstance(results, dict):
        raise TypeError
except (OSError, ValueError, KeyError, TypeError):
    sys.exit(".secrets.baseline is not a readable detect-secrets baseline")
for key in ("plugins_used", "filters_used"):
    if cand.get(key) != trusted.get(key):
        sys.exit(f".secrets.baseline's {key} differs from the trusted base's; the scanner configuration is main's (change it there first) and only reviewed results are data")
scan = {k: trusted[k] for k in ("version", "plugins_used", "filters_used") if k in trusted}
scan["results"] = results
open(sys.argv[3], "w").write(json.dumps(scan, indent=2) + "\n")
PY
)" || fail "$(tail -n 1 <<< "$why")"
cp "$work/scan.baseline" "$work/io/.secrets.baseline" || fail "could not place the scan baseline in the output dir"
mapfile -d '' files < <(git ls-files -z | grep -zvx '.secrets.baseline')
[ "${#files[@]}" -gt 0 ] || fail "no tracked files; refusing an empty secret scan"
SANDBOX_RO="$tools" sandbox_run "$work" "$tools/bin/python" -m detect_secrets.pre_commit_hook --baseline "$work/io/.secrets.baseline" \
  --json -- "${files[@]}" > "$work/hook.out" 2>/dev/null
code=$?
judge() { python3 - "$work" "$code" "${#files[@]}" <<'PY'
import json, re, sys
work, code, tracked = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
before = open(f"{work}/scan.baseline", "rb").read()  # built by the trusted gate: main's config + reviewed results
stale = open(f"{work}/io/.secrets.baseline", "rb").read() != before  # written only by the trusted scanner
if code == 0 and not stale:
    print(f"{tracked} tracked files, no finding beyond .secrets.baseline (trusted detect-secrets)"); sys.exit(0)
reviewed = {(f, s["type"], s["hashed_secret"]) for f, ss in json.loads(before)["results"].items() for s in ss}
STALE = ".secrets.baseline is stale (a reviewed finding moved); hand-edit and review the baseline, never let detect-secrets rewrite it"
try:
    found = [s for ss in json.load(open(f"{work}/hook.out"))["results"].values() for s in ss]
except (ValueError, KeyError, AttributeError, TypeError):  # exit 3 = the scanner updated its (temp) baseline and printed a notice
    sys.exit(STALE if stale else f"the secret scan exited {code} with unreadable output; run make security-gate by hand")
new = sorted({(str(s["filename"]), int(s["line_number"]), str(s["type"])) for s in found if (s["filename"], s["type"], s["hashed_secret"]) not in reviewed})
if new:
    clean = lambda v: re.sub(r"[^ -~]", "", v)[:120]
    shown = "; ".join(f"{clean(f)}:{n} {clean(t)}" for f, n, t in new[:10]) + (f"; and {len(new) - 10} more" if len(new) > 10 else "")
    sys.exit(f"{len(new)} new finding(s) vs .secrets.baseline: {shown}; remove the secret, or review it and hand-edit the baseline")
sys.exit(STALE)
PY
}
if out="$(judge 2>&1)"; then echo "PASS: $g — $out"; else echo "FAIL: $g — $(tail -n 1 <<< "$out")" >&2; exit 1; fi
