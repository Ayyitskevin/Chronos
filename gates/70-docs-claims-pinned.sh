#!/usr/bin/env bash
# 70-docs-claims-pinned.sh — the doc-pinning contract tests pass (docs say only what the source proves).
# Runs the EXISTING tests by named selection (none rewritten, skipped or deselected):
# tests/unit/test_limitations_*_contract.py plus the three named files below (listed in gates/README.md).
# Each must be a TRACKED regular file inside tests/unit. The tests are candidate code: they run in the
# trusted bwrap sandbox (gates/lib/sandbox.sh) over an exact-head snapshot, after asserting `chronos`
# imports from it, with plugin autoload OFF, a trusted empty ini, --noconftest and the trusted reporter
# gates/lib/gates_nodes.py; the judge binds its node reports to the test functions a trusted AST parse
# of each selected file expects: PASS needs every expected function passed, no other outcome, no
# deselected/skipped/xfail. Two named tests are kept out (see EXCLUDED). CHRONOS_PY overrides the python.
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
# Trusted AST: the EXPECTED test functions of every selected file. Two tests are kept out of this gate by
# name (they spawn a nested `pytest --collect-only` that needs plugin autoload; they still run in gate 40):
EXCLUDED="tests/unit/test_limitations_autonomy_counters_contract.py::test_3b_the_injection_tests_exist_as_collected_pytest_items
tests/unit/test_limitations_autonomy_counters_contract.py::test_4c_the_release_is_guarded_by_not_counts_activity_attempt"
mkdir "$work/trusted" && printf '[pytest]\n' > "$work/trusted/pytest.ini" || fail "could not write the trusted pytest config"
mapfile -t args < <(python3 - "$work/trusted" "$EXCLUDED" "$@" <<'PY'
import ast, json, sys
out, excluded, files = sys.argv[1], set(sys.argv[2].split()), sys.argv[3:]
expected = {}
for f in files:
    tree = ast.parse(open(f, encoding="utf-8").read())
    names = [n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test")]
    names += [f"{c.name}::{m.name}" for c in tree.body if isinstance(c, ast.ClassDef) and c.name.startswith("Test")
              for m in c.body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)) and m.name.startswith("test")]
    keep = [n for n in names if f"{f}::{n}" not in excluded]
    expected[f] = keep
    print(*([f"{f}::{n}" for n in keep] if len(keep) != len(names) else [f]), sep="\n")
json.dump(expected, open(f"{out}/expected.json", "w"))
PY
)
[ "${#args[@]}" -gt 0 ] && [ -s "$work/trusted/expected.json" ] || fail "could not derive the expected doc-contract tests from the source"
out="$(SANDBOX_RO="$work/trusted $(cd "$(dirname "$0")/lib" && pwd)" SANDBOX_PYPATH="$(cd "$(dirname "$0")/lib" && pwd)" \
  SANDBOX_ENV="PYTEST_DISABLE_PLUGIN_AUTOLOAD=1" sandbox_run "$work" "$py" -m pytest -q -p no:cacheprovider -c "$work/trusted/pytest.ini" \
  --rootdir "$work/snap" --noconftest -p gates_nodes "${args[@]}" 2>&1)"
code=$?
summary="$(grep -E ' in [0-9.]+s' <<< "$out" | tail -n 1 | sed -E 's/^=+ //; s/ =+$//' | tr -cd '[:print:]' | cut -c1-120)"
[ "$code" -ne 5 ] || fail "pytest collected no tests from the $# named files; the doc claims are unpinned"
[ "$code" -eq 0 ] || fail "pytest exited $code ($summary); run '$py -m pytest -q $*' and fix the doc or the claim"
! grep -qE '[0-9]+ (deselected|skipped|xfailed|xpassed)' <<< "$summary" || fail "pytest reported '$summary'; every doc-contract test must run and pass (none skipped or deselected)"
verdict="$(python3 - "$work" <<'PY' 2>&1
import json, os, re, stat, sys
work = sys.argv[1]
expected = json.load(open(f"{work}/trusted/expected.json"))  # trusted: derived by AST before the run
try:
    fd = os.open(f"{work}/io/nodes.jsonl", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as f:
        st = os.fstat(f.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_size > 16 * 1024 * 1024:
            raise OSError
        reports = [json.loads(line) for line in f.read().decode("utf-8").splitlines() if line.strip()]
except (OSError, ValueError):
    sys.exit("no usable node report was written by the trusted reporter")
clean = lambda v: re.sub(r"[^ -~]", "", str(v))[:120]
passed = set()
for r in reports:
    if not isinstance(r, dict) or r.get("outcome") not in ("passed",) :
        sys.exit(f"{clean(r.get('nodeid') if isinstance(r, dict) else r)} reported {clean(r.get('outcome') if isinstance(r, dict) else '?')}; every doc-contract test must run and pass")
    base = re.sub(r"\[.*\]$", "", str(r.get("nodeid", "")))
    if r.get("when") == "call":
        passed.add(base)
    if base.split("::", 1)[0] not in expected:
        sys.exit(f"a report names {clean(base)}, which is not in the selected files")
for f, names in expected.items():
    missing = [n for n in names if f"{f}::{n}" not in passed]
    if missing or not any(p.startswith(f + "::") for p in passed):
        sys.exit(f"no passing test for {f}::{missing[0] if missing else '<any>'}" + (f" (+{len(missing) - 1} more)" if len(missing) > 1 else ""))
print(len(passed))
PY
)" || fail "$(tail -n 1 <<< "$verdict"); fix the doc-contract tests, then re-run"
echo "PASS: $g — $# doc-contract files: $verdict expected test functions passed (trusted AST × node reports); $summary"
