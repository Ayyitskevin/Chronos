#!/usr/bin/env bash
# 70-docs-claims-pinned.sh — the doc-pinning contract tests pass (docs say only what the source proves).
# Runs the EXISTING tests by named selection (none rewritten, skipped or deselected):
# tests/unit/test_limitations_*_contract.py plus the three named files below (listed in gates/README.md).
# Each must be a TRACKED regular file inside tests/unit. The tests are candidate code: they run in the
# trusted bwrap sandbox (gates/lib/sandbox.sh) over an exact-head snapshot, after asserting `chronos`
# imports from it, writing only a junit report into the 0700 output dir. The trusted gate reads that report: PASS needs tests > 0, failures == errors == skipped == 0,
# every selected file executed, and no deselected/skipped/xfail in the summary. CHRONOS_PY overrides the python.
set -uo pipefail
g=docs-claims-pinned
fail() { echo "FAIL: $g — $1" >&2; exit 1; }
top="$(git rev-parse --show-toplevel 2>/dev/null)" || fail "not inside a git work tree; run the gate from the repo root"
cd "$top" || fail "could not enter $top"
py="${CHRONOS_PY:-.venv/bin/python}"
[ -x "$py" ] || fail "no python at $py; create the venv (make's PY) or set CHRONOS_PY"
shopt -s nullglob
set -- tests/unit/test_limitations_*_contract.py
[ "$#" -ge 1 ] || fail "no tests/unit/test_limitations_*_contract.py matched; restore the limitations contract tests"
for f in tests/unit/test_adr_point_in_time_claims.py tests/unit/test_docs_map_skill_contract.py tests/unit/test_vision_completion_plan_prose.py; do
  [ -f "$f" ] || fail "$f is missing; restore it or update the named set here and in gates/README.md"
  set -- "$@" "$f"
done
unit="$(cd tests/unit && pwd -P)" || fail "tests/unit is not a directory"
for f in "$@"; do
  git ls-files -s -- "$f" | grep -qE '^100(644|755) ' && [ ! -L "$f" ] && [ "$(cd "$(dirname "$f")" && pwd -P)" = "$unit" ] \
    || fail "$f is not a tracked regular file inside tests/unit; refusing to run it"
done
. "$(dirname "$0")/lib/sandbox.sh" || fail "could not load the trusted sandbox launcher"
[ -z "$(git status --porcelain --untracked-files=no)" ] || fail "tracked files are modified, so HEAD's snapshot is not the tree you see; commit or stash, then re-run"
work="$(mktemp -d)" || fail "could not create a scratch dir"; trap 'rm -rf "$work"' EXIT
sandbox_snapshot "$work" || fail "could not build the exact-head snapshot"
sandbox_run "$work" true 2>/dev/null; [ "$?" -ne 125 ] || fail "the bwrap sandbox is unavailable; candidate code never runs unsandboxed — install/enable bwrap"
sandbox_identity "$work" "$py" || fail "chronos does not import from the exact-head snapshot (an editable install or PYTHONPATH points elsewhere); refusing to judge"
out="$(sandbox_run "$work" "$py" -m pytest -q -p no:cacheprovider --junitxml="$work/io/junit.xml" "$@" 2>&1)"
code=$?
summary="$(grep -E ' in [0-9.]+s' <<< "$out" | tail -n 1 | sed -E 's/^=+ //; s/ =+$//' | tr -cd '[:print:]' | cut -c1-120)"
[ "$code" -ne 5 ] || fail "pytest collected no tests from the $# named files; the doc claims are unpinned"
[ "$code" -eq 0 ] || fail "pytest exited $code ($summary); run '$py -m pytest -q $*' and fix the doc or the claim"
! grep -qE '[0-9]+ (deselected|skipped|xfailed|xpassed)' <<< "$summary" || fail "pytest reported '$summary'; every doc-contract test must run and pass (none skipped or deselected)"
verdict="$(python3 - "$work/io/junit.xml" "$@" <<'PY' 2>&1
import os, sys, xml.etree.ElementTree as ET
path, files = sys.argv[1], sys.argv[2:]
if not os.path.isfile(path) or os.path.islink(path) or os.path.getsize(path) > 16 * 1024 * 1024:
    sys.exit("no usable junit report was written")
suites = [ET.parse(path).getroot()]; suites = suites[0].iter("testsuite") if suites[0].tag == "testsuites" else suites
tot = {k: sum(int(s.get(k, 0)) for s in suites) for k in ("tests", "failures", "errors", "skipped")} if (suites := list(suites)) else {}
if not tot or tot["tests"] <= 0 or tot["failures"] or tot["errors"] or tot["skipped"]:
    sys.exit(f"the junit report shows {tot or 'no testsuite'}; every selected test must run and pass")
ran = {c.get("classname", "") for s in suites for c in s.iter("testcase") if not any(k.tag in ("skipped", "failure", "error") for k in c)}
missing = [f for f in files if not any(n == f[:-3].replace("/", ".") or n.startswith(f[:-3].replace("/", ".") + ".") for n in ran)]
if missing:
    sys.exit(f"no test executed from {missing[0]}" + (f" (+{len(missing) - 1} more)" if len(missing) > 1 else ""))
print(tot["tests"])
PY
)" || fail "$(tail -n 1 <<< "$verdict"); fix the doc-contract tests, then re-run"
echo "PASS: $g — $# doc-contract files: $verdict tests executed and passed (junit); $summary"
