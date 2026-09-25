#!/usr/bin/env bash
# 40-evidence-at-head.sh — test evidence must record the sha it ran on, and it must equal PR_HEAD_SHA.
# Runs the existing `make gates` (adopted, not rewritten) — but inside a read-only exact-head snapshot
# (a local clone at PR_HEAD_SHA in a 0700 scratch dir; only the gitignored outputs dist/ and the tool
# caches are writable), under `env -i` with a fixed allowlist and a throwaway HOME. Afterwards the
# trusted gate proves every tracked byte of the snapshot still equals PR_HEAD_SHA's blobs (the lane's
# object store) and the lane is untouched, then writes .gates/40-evidence-at-head.json (0700 dir, 0600
# file, never through a link) = {sha, exit, pytest counts, tree_verified}. PASS iff that receipt shows
# sha == PR_HEAD_SHA == HEAD, exit 0, exactly one pytest summary with passed > 0, failed == errors == 0.
# The raw make log lives only in the scratch dir; it is never kept.
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
mk="$(command -v make)" || fail "make is not on PATH"
work="$(mktemp -d)" || fail "could not create a scratch dir"; trap 'chmod -R u+w "$work" 2>/dev/null; rm -rf "$work"' EXIT
snap="$work/snap"; mkdir "$work/home" || fail "could not create a temp HOME"
git clone -q --no-checkout "$top" "$snap" 2>/dev/null && git -C "$snap" checkout -q --detach "$PR_HEAD_SHA" 2>/dev/null \
  || fail "could not build the exact-head snapshot at $PR_HEAD_SHA"
if [ -d .venv ] && [ ! -L .venv ]; then  # the lane's venv, seen from the snapshot (the editable install is overridden by PYTHONPATH)
  mkdir "$snap/.venv" && cp .venv/pyvenv.cfg "$snap/.venv/" || fail "could not shim .venv into the snapshot"
  for d in bin lib lib64 include; do [ -e ".venv/$d" ] && ln -s "$top/.venv/$d" "$snap/.venv/$d"; done
fi
mkdir -p "$snap/dist" "$snap/.ruff_cache" "$snap/.mypy_cache" "$snap/.pytest_cache" || fail "could not create the snapshot's output dirs"
find "$snap" \( -path "$snap/.git" -o -path "$snap/.venv" -o -path "$snap/dist" -o -path "$snap/.*_cache" \) -prune -o ! -type l -exec chmod a-w {} + 2>/dev/null
(cd "$snap" && env -i PATH=/usr/local/bin:/usr/bin:/bin HOME="$work/home" LANG=C.UTF-8 BROKER_MODE=demo ALLOW_ORDER_TRANSMIT=false \
  ALLOW_LIVE_TRADING=false PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$snap/src" "$mk" gates) > "$work/make.log" 2>&1
code=$?
out="$(python3 - "$top" "$snap" "$PR_HEAD_SHA" "$code" "$work/make.log" <<'PY' 2>&1
import json, os, re, stat, subprocess, sys
top, snap, sha, code, log = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4]), sys.argv[5]
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
receipt = {"sha": snap_head, "exit": code, "pytest": pytest, "summaries": len(summaries), "tree_verified": bad is None and not lane_dirty}
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
[ "$rexit" = 0 ] || fail "make gates exited $rexit at $rsha; re-run 'make gates' by hand at that head, fix, then re-run"
[ "$rsha" = "$PR_HEAD_SHA" ] && [ "$rsha" = "$head" ] || fail "receipt sha $rsha is not PR_HEAD_SHA $PR_HEAD_SHA (HEAD moved during make gates); re-run at a fixed head"
[ "$nsum" = 1 ] || fail "make gates exited 0 but printed $nsum pytest summaries (no pytest summary is ambiguous too); the evidence must carry exactly one set of test counts"
IFS=/ read -r np nf ns ne <<< "$counts"
[ "$nf" = 0 ] && [ "$ne" = 0 ] && [ "$np" -gt 0 ] || fail "the pytest summary is contradictory ($np passed, $nf failed, $ne errors) for exit 0; fix the failures, then re-run"
echo "PASS: $g — make gates exit 0 at $rsha ($np passed, $nf failed, $ns skipped); receipt .gates/40-evidence-at-head.json"
