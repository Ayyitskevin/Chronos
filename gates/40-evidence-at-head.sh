#!/usr/bin/env bash
# 40-evidence-at-head.sh — test evidence must record the sha it ran on, and it must equal PR_HEAD_SHA.
# Runs the work of the existing `make gates` (adopted, not rewritten), split so no candidate code gets the
# network (reports/GATE40-SPLIT-PROPOSAL.md, the conductor's D2 call):
#   A. TRUSTED, network allowed, candidate bytes only as DATA — validate the requirements-*.lock files
#      (pinned lines + sha256 hashes + comments only), pre-fetch them as hash-pinned wheels (binary only) with
#      the trusted tools venv's pip, and run the trusted pip-audit on requirements-runtime.lock;
#   B. in the bwrap sandbox (gates/lib/sandbox.sh: no network, immutable exact-head snapshot, chronos
#      identity asserted) — `make lint format-check type type-worker test` each, `make release-gate` with
#      PIP_NO_INDEX + the read-only wheel cache, and the security gate's scans minus pip-audit. The target
#      DEFINITIONS (Makefile, the release/security scripts) are the TRUSTED base's, overlaid read-only on the
#      candidate snapshot inside the sandbox; the candidate's copies never run. `data/` is writable scratch.
# Afterwards the trusted gate compares every tracked snapshot file with PR_HEAD_SHA's blobs (the lane's
# object store) and checks the lane is untouched, then writes .gates/40-evidence-at-head.json (0700 dir, 0600
# file, never through a link) = {sha, exit, per-target exits, pytest counts, lock + cache digests, audit}.
# PASS iff sha == PR_HEAD_SHA == HEAD, every target exits 0, the audit is clean, and exactly one pytest summary
# shows passed > 0 with failed == errors == 0. Raw logs live only in the scratch dir.
set -uo pipefail
g=evidence-at-head
fail() { echo "FAIL: $g — $1" >&2; exit 1; }
top="$(git rev-parse --show-toplevel 2>/dev/null)" || fail "not inside a git work tree; run the gate from the repo root"
cd "$top" || fail "could not enter $top"
[[ "${PR_HEAD_SHA:-}" =~ ^[0-9a-f]{40}$ ]] || fail "PR_HEAD_SHA must be a full 40-hex sha; set it to the PR head"
head="$(git rev-parse HEAD)" || fail "could not read HEAD"
[ "$head" = "$PR_HEAD_SHA" ] || fail "checked-out HEAD $head is not PR_HEAD_SHA $PR_HEAD_SHA; check out the PR head and re-run"
[ -z "$(git status --porcelain --untracked-files=no)" ] || fail "tracked files are modified, so HEAD does not name the tested bytes; commit or stash, then re-run"
{ [ -L .gates ] || { [ -e .gates ] && [ ! -d .gates ]; }; } && fail ".gates is a symlink or not a directory; remove it, then re-run"
mkdir -p .gates && chmod 0700 .gates && [ "$(cd .gates && pwd -P)" = "$(pwd -P)/.gates" ] || fail "could not create a contained 0700 .gates/"
[ -z "$(git ls-files --others --exclude-standard -- ':!.gates')" ] || fail "untracked files in the tree (make security-gate refuses them: its scan enumerates git ls-files); add or remove them, then re-run"
. "$(dirname "$0")/lib/sandbox.sh" && . "$(dirname "$0")/lib/tools-venv.sh" || fail "could not load the trusted gate libraries"
mk="$(command -v make)" || fail "make is not on PATH"
py="${CHRONOS_PY:-.venv/bin/python}"
tools="$(tools_venv 2>&1)" || fail "$(tail -n 1 <<< "$tools")"
work="$(mktemp -d)" || fail "could not create a scratch dir"; trap 'rm -rf "$work"' EXIT
snap="$work/snap"
sandbox_snapshot "$work" || fail "could not build the exact-head snapshot at $PR_HEAD_SHA"
export SANDBOX_WRITABLE="dist .ruff_cache .mypy_cache .pytest_cache data"  # data/: untracked runtime state the tests write
# The target DEFINITIONS come from the trusted base, never the candidate: its Makefile and the three scripts
# the release/security targets run, overlaid read-only on the snapshot inside the sandbox only.
mkdir "$work/trusted" || fail "could not create the trusted dir"
for f in Makefile scripts/verify_release_security.py scripts/verify_release_artifact.py scripts/verify_pip_bootstrap.py; do
  trusted_show "$f" > "$work/trusted/${f##*/}" && [ -s "$work/trusted/${f##*/}" ] || fail "cannot read $f from the trusted base (${GATES_TRUSTED_REF:-origin/main})"
done
trusted_sha="$(git -C "$(trusted_repo)" rev-parse "${GATES_TRUSTED_REF:-origin/main}" 2>/dev/null)" || fail "cannot resolve the trusted base ref"
MK="$work/trusted/Makefile=Makefile"
REL="$MK $work/trusted/verify_release_artifact.py=scripts/verify_release_artifact.py $work/trusted/verify_pip_bootstrap.py=scripts/verify_pip_bootstrap.py"
SEC="$work/trusted/verify_release_security.py=scripts/verify_release_security.py"
sandbox_run "$work" true 2>/dev/null; [ "$?" -ne 125 ] || fail "the bwrap sandbox is unavailable; candidate code never runs unsandboxed — install/enable bwrap"
sandbox_identity "$work" "$py" || fail "chronos does not import from the exact-head snapshot (an editable install or PYTHONPATH points elsewhere); refusing to judge"
# A. TRUSTED steps (network allowed; candidate bytes only as DATA): validate the locks, pre-fetch the wheel
#    cache, audit the runtime lock — every tool from the trusted tools venv, never candidate code.
LOCKS="bootstrap build runtime sbom"
why="$(python3 - "$snap" $LOCKS <<'PY' 2>&1
import re, sys
snap, names = sys.argv[1], sys.argv[2:]
ok = re.compile(r"(?:[A-Za-z0-9][A-Za-z0-9._-]*==[A-Za-z0-9._+!-]+|\s+--hash=sha256:[0-9a-f]{64})(?: \\)?|\s*(?:#.*)?")
for name in names:
    for n, line in enumerate(open(f"{snap}/requirements-{name}.lock", encoding="utf-8"), 1):
        if not ok.fullmatch(line.rstrip("\n")):
            sys.exit(f"requirements-{name}.lock:{n} is not a pinned requirement, a sha256 hash or a comment (options, URLs and paths are refused)")
PY
)" || fail "$(tail -n 1 <<< "$why"); the trusted steps read the locks only as pinned data"
cache="${XDG_CACHE_HOME:-$HOME/.cache}/chronos-gates/wheels"; mkdir -p "$cache" && chmod 0700 "$cache" || fail "could not create the wheel cache"
for l in $LOCKS; do
  "$tools/bin/python" -m pip download -q --disable-pip-version-check --no-deps --only-binary=:all: --require-hashes \
    -r "$snap/requirements-$l.lock" -d "$cache" > "$work/download-$l.log" 2>&1 \
    || fail "could not pre-fetch requirements-$l.lock as hash-pinned wheels (an sdist-only or yanked pin fails closed); see pip download"
done
"$tools/bin/python" -m pip_audit --require-hashes --disable-pip --progress-spinner off -r "$snap/requirements-runtime.lock" -f json -o "$work/audit.json" > "$work/audit.log" 2>&1
acode=$?
audit="$(python3 - "$work/audit.json" "$acode" <<'PY' 2>&1
import json, re, sys
try:
    deps = json.load(open(sys.argv[1]))["dependencies"]
except (OSError, ValueError, KeyError, TypeError):
    sys.exit(f"the trusted pip-audit exited {sys.argv[2]} without a readable report (its advisory service needs the network)")
vulns = [f"{d.get('name')}=={d.get('version')} {v.get('id')}" for d in deps for v in d.get("vulns", [])]
if vulns:
    sys.exit(f"{len(vulns)} known vulnerabilit(ies) in requirements-runtime.lock: " + "; ".join(re.sub(r"[^ -~]", "", x)[:80] for x in vulns[:5]))
if int(sys.argv[2]) != 0:
    sys.exit(f"the trusted pip-audit exited {sys.argv[2]}")
print(len(deps))
PY
)" || fail "$(tail -n 1 <<< "$audit")"
# B. In the bwrap sandbox, network denied: every other `make gates` target, each recorded separately.
code=0; first=""
target() {  # $1 name, rest = the sandboxed command; records the exit, keeps the first failure
  local name="$1"; shift
  "$@" > "$work/$name.log" 2>&1; local rc=$?
  printf '%s %s\n' "$name" "$rc" >> "$work/targets"
  if [ "$rc" -ne 0 ] && [ "$code" -eq 0 ]; then code=$rc; first=$name; fi
}
for t in lint format-check type type-worker test; do SANDBOX_OVERLAY="$MK" target "$t" sandbox_run "$work" "$mk" "$t"; done
SANDBOX_OVERLAY="$REL" SANDBOX_RO="$cache" SANDBOX_ENV="PIP_NO_INDEX=1 PIP_FIND_LINKS=$cache" target release-gate sandbox_run "$work" "$mk" release-gate
cp "$snap/.secrets.baseline" "$work/io/.secrets.baseline" 2>/dev/null
SANDBOX_OVERLAY="$SEC" target security-offline sandbox_run "$work" "$py" - <<'PY'
# the security gate's own scans minus pip-audit (run by the trusted step above), from the TRUSTED base's script
import importlib.util, os, subprocess, sys
from importlib import metadata
from pathlib import Path
root, copy = Path.cwd(), Path(os.environ["GATE_IO"]) / ".secrets.baseline"
spec = importlib.util.spec_from_file_location("release_security", root / "scripts/verify_release_security.py")
gate = sys.modules[spec.name] = importlib.util.module_from_spec(spec); spec.loader.exec_module(gate)
gate._require_exact_tool_versions(metadata.version)
tracked = tuple(p for p in gate._tracked_files(root) if p != ".secrets.baseline")
for command in gate.build_scan_commands(python=Path(sys.executable), baseline=copy, tracked_files=tracked):
    if command.name != "runtime dependency audit":
        subprocess.run(command.argv, cwd=root, check=True)
gate.verify_git_history_secrets(root, copy)
PY
if [ "$code" -ne 0 ] && cat "$work"/*.log | grep -qE 'Temporary failure in name resolution|Network is unreachable|NameResolutionError|NewConnectionError'; then
  fail "the $first target needs outbound network, which the sandbox denies (the split moved pip-audit and the wheel downloads to trusted steps); FAILing closed"
fi
cachedigest="$(cd "$cache" && find . -maxdepth 1 -type f -name '*.whl' -printf '%f\n' | LC_ALL=C sort | xargs -r sha256sum | sha256sum | cut -c1-64)"
lockdigests="$(for l in $LOCKS; do printf '%s=%s ' "$l" "$(sha256sum < "$snap/requirements-$l.lock" | cut -c1-64)"; done)"
out="$(python3 - "$top" "$snap" "$PR_HEAD_SHA" "$code" "$work/test.log" "$work/targets" "$lockdigests" "$cachedigest" "$audit" "$trusted_sha" <<'PY' 2>&1
import json, os, re, stat, subprocess, sys
top, snap, sha, code, log = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4]), sys.argv[5]
targets = dict(line.split() for line in open(sys.argv[6]))
locks = dict(kv.split("=", 1) for kv in sys.argv[7].split())
def git(*a, cwd=top):
    return subprocess.run(["git", *a], cwd=cwd, capture_output=True, check=True).stdout
snap_head = git("rev-parse", "HEAD", cwd=snap).decode().strip()
bad, paths, blobs = None, [], []
for rec in filter(None, git("ls-tree", "-r", "-z", sha).split(b"\0")):  # the lane's object store, not the snapshot's
    meta, path = rec.split(b"\t", 1); mode, _, blob = meta.decode().split(); path = path.decode()
    full = os.path.join(snap, path)
    if mode not in ("100644", "100755") or not os.path.lexists(full) or not stat.S_ISREG(os.lstat(full).st_mode):
        bad = f"{path} is not a regular file in the snapshot"; break
    paths.append(full); blobs.append((path, blob))
if bad is None:
    got = subprocess.run(["git", "hash-object", "--no-filters", "--stdin-paths"], cwd=top, input="\n".join(paths).encode(),
                         capture_output=True, check=True).stdout.decode().split()
    bad = next((f"{path} changed during make gates" for (path, blob), h in zip(blobs, got) if h != blob), None)
    if bad is None and len(got) != len(blobs): bad = "the snapshot could not be hashed completely"
lane_dirty = git("status", "--porcelain", "--untracked-files=no") != b"" or git("rev-parse", "HEAD").decode().strip() != sha
summaries = [m.group(1) for m in (re.fullmatch(r"=*\s*(\d+ \w+(?:, \d+ \w+)*) in [\d.]+s\b.*", l.strip()) for l in open(log, errors="replace")) if m]
pytest = None
if len(summaries) == 1:
    n = {}
    for part in summaries[0].split(", "):
        c, w = part.split(" ", 1); n[{"error": "errors"}.get(w, w)] = int(c)
    pytest = {k: n.get(k, 0) for k in ("passed", "failed", "skipped", "errors")}
receipt = {"sha": snap_head, "exit": code, "pytest": pytest, "summaries": len(summaries), "tree_verified": bad is None and not lane_dirty,
           "targets": {k: int(v) for k, v in targets.items()}, "locks": locks, "cache_sha256": sys.argv[8],
           "audit": {"tool": "pip-audit (trusted tools venv)", "dependencies": int(sys.argv[9]), "vulns": 0},
           "trusted_base": sys.argv[10]}
path = os.path.join(top, ".gates", "40-evidence-at-head.json")
if os.path.lexists(path):
    if not stat.S_ISREG(os.lstat(path).st_mode): sys.exit(f"{path} is a link or non-regular file; remove it")
    os.unlink(path)
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
os.write(fd, (json.dumps(receipt) + "\n").encode()); os.close(fd)
print(bad or ("the lane tree changed during make gates" if lane_dirty else "ok"))
PY
)" || fail "could not verify the snapshot or write the receipt ($(tail -n 1 <<< "$out"))"
[ "$out" = ok ] || fail "$out; the evidence does not name PR_HEAD_SHA's bytes — re-run on an untouched head"
read -r rsha rexit nsum counts < <(python3 -c 'import json,os,sys; r=json.load(os.fdopen(os.open(sys.argv[1], os.O_RDONLY|os.O_NOFOLLOW))); p=r["pytest"]
print(r["sha"], r["exit"], r["summaries"], "none" if p is None else "%d/%d/%d/%d" % (p["passed"], p["failed"], p["skipped"], p["errors"]))' .gates/40-evidence-at-head.json) \
  || fail "could not read back the receipt .gates/40-evidence-at-head.json"
[ "$rexit" = 0 ] || fail "make gates exited $rexit at $rsha (target $first); re-run 'make $first' by hand at that head, fix, then re-run"
[ "$rsha" = "$PR_HEAD_SHA" ] && [ "$rsha" = "$head" ] || fail "receipt sha $rsha is not PR_HEAD_SHA $PR_HEAD_SHA (HEAD moved during make gates); re-run at a fixed head"
[ "$nsum" = 1 ] || fail "make gates exited 0 but printed $nsum pytest summaries (no pytest summary is ambiguous too); the evidence must carry exactly one set of test counts"
IFS=/ read -r np nf ns ne <<< "$counts"
[ "$nf" = 0 ] && [ "$ne" = 0 ] && [ "$np" -gt 0 ] || fail "the pytest summary is contradictory ($np passed, $nf failed, $ne errors) for exit 0; fix the failures, then re-run"
echo "PASS: $g — make gates exit 0 at $rsha ($np passed, $nf failed, $ns skipped); receipt .gates/40-evidence-at-head.json"
