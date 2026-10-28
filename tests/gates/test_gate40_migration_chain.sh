#!/usr/bin/env bash
# D-47(b) harness: pins gates/lib/migration_chain.py, the TRUSTED step of gate 40 that derives the expected
# migration head from the candidate's revision files read as DATA (git objects + ast.parse, never imported).
# Each case builds a candidate commit on a synthetic `main` (0001 → 0002, _MIGRATION_HEAD "0002") and runs the
# trusted script exactly as gate 40 does (python3 -I -B, cwd = the candidate tree). Gate 40's own wiring
# (the overlay reaching `make release-gate`, the #309 failure on the unfixed gate) is pinned in
# tests/gates/test_gates_40_70.sh. Numbered `d47b:<invariant>`.
set -u
ROOT=$(cd "$(dirname "$0")/../.." && pwd); G=${GATES_DIR_UNDER_TEST:-$ROOT/gates}; pass=0; fail=0
T=$(mktemp -d)
trap 'rm -rf "$T"' EXIT
ok() { pass=$((pass+1)); echo "ok       $1"; }; bad() { fail=$((fail+1)); echo "FAIL     $1"; }
check() { if eval "$2"; then ok "$1"; else bad "$1"; fi; }
export BROKER_MODE=demo ALLOW_ORDER_TRANSMIT=false ALLOW_LIVE_TRADING=false PYTHONDONTWRITEBYTECODE=1
M=$T/mc; V=src/chronos/persistence/migrations/versions; S=scripts/verify_release_artifact.py
commit() { git -C "$M" add -A && git -C "$M" -c user.email=t@t -c user.name=t commit -qm "${1:-c}" --allow-empty; }
mig() {  # mig <rev> <down: None or "NNNN"> [file name stem]: a well-formed revision file
  printf '"""Synthetic revision %s."""\n\nfrom __future__ import annotations\n\nrevision = "%s"\ndown_revision = %s\nbranch_labels = None\ndepends_on = None\n\n\ndef upgrade() -> None:\n    pass\n' \
    "$1" "$1" "$2" > "$M/$V/$1_${3:-step}.py"
}
lit() { printf 'from typing import Final\n\n_MIGRATION_HEAD: Final[str] = "%s"\n\n\ndef main() -> None:\n    print(_MIGRATION_HEAD)\n' "$1" > "$M/$S"; }
rm -rf "$M"; mkdir -p "$M/$V" "$M/scripts"; git -C "$M" init -q
mig 0001 None base; mig 0002 '"0001"' next; lit 0002; commit main; git -C "$M" branch -q trusted-main
MAIN=$(git -C "$M" rev-parse trusted-main); git -C "$M" show "$MAIN:$S" > "$T/overlay.orig"
fresh() { git -C "$M" checkout -q -f --detach "$MAIN"; git -C "$M" clean -qfdx; }   # a candidate starts at main
run_chain() {  # exactly gate 40's call: isolated, cwd = the candidate tree; $1 = trusted commit (default main)
  cp "$T/overlay.orig" "$T/overlay"
  (cd "$M" && python3 -I -B "$G/lib/migration_chain.py" "$M" "${1:-$MAIN}" "$M" "$(git -C "$M" rev-parse HEAD)" "$T/overlay" > "$T/out" 2> "$T/err"; echo $? > "$T/rc")
}
passes() { [ "$(cat "$T/rc")" = 0 ] && [ "$(cat "$T/out")" = "$1" ] && [ ! -s "$T/err" ]; }
refused() { [ "$(cat "$T/rc")" = 1 ] && [ ! -s "$T/out" ] && [ "$(wc -l < "$T/err")" = 1 ] && grep -qE "^migration chain $1" "$T/err" && cmp -s "$T/overlay" "$T/overlay.orig"; }
overlay_took() {  # the overlay differs from main's in exactly the one _MIGRATION_HEAD line, now naming $1
  [ "$(diff "$T/overlay.orig" "$T/overlay" | grep -c '^[<>]')" = 2 ] && grep -qx "_MIGRATION_HEAD: Final\\[str\\] = \"$1\"" "$T/overlay"
}
case_() {  # case_ <label> <expected: PASS:NNNN or a FAIL regex> <shell edits applied to a fresh candidate>
  fresh; eval "$3"; commit cand; run_chain
  case "$2" in PASS:*) check "$1" "passes ${2#PASS:}" ;; *) check "$1" "refused '$2'" ;; esac
}

# --- the legitimate shapes -------------------------------------------------------------------------
fresh; commit same; run_chain
check "d47b:5 main itself (no new migration) passes: the derived head is main's 0002 ..." "passes 0002"
check "d47b:5 ...and the trusted overlay stays byte-identical to main's script (no write)" "cmp -s $T/overlay $T/overlay.orig"
case_ "d47b:1 a candidate appending 0003 (and bumping its literal) passes with head 0003 ..." PASS:0003 'mig 0003 "\"0002\""; lit 0003'
check "d47b:1 ...and the trusted overlay took exactly its one _MIGRATION_HEAD line, now 0003; every other byte is main's" "overlay_took 0003"
case_ "d47b:1 two appended revisions (0003, 0004) pass with head 0004" PASS:0004 'mig 0003 "\"0002\""; mig 0004 "\"0003\""; lit 0004'
case_ "d47b:header an annotated literal header (revision: str = \"0003\") is a literal assignment too" PASS:0003 \
  'mig 0003 "\"0002\""; sed -i "s/^revision = /revision: str = /" $M/$V/0003_step.py; lit 0003'
mark=$T/payload-ran
case_ "d47b:data a revision whose upgrade() body runs code (writes a marker, os.system) passes on its literal header ..." PASS:0003 \
  "mig 0003 '\"0002\"'; printf '    open(\"%s\", \"w\").write(\"ran\")\n    import os\n    os.system(\"touch %s.sys\")\n    raise SystemExit(3)\n' $mark $mark >> $M/$V/0003_step.py; lit 0003"
check "d47b:data ...and the trusted step never imported or executed it (no marker from open() or os.system)" "[ ! -e $mark ] && [ ! -e $mark.sys ]"
case_ "d47b:data the same payload at module level is refused by the module whitelist ..." '\[module\] versions/0003_step.py line 13: a module-level Expr statement is not allowed' \
  "mig 0003 '\"0002\"'; printf 'open(\"%s\", \"w\").write(\"ran\")\nimport os\nos.system(\"touch %s.sys\")\nraise SystemExit(3)\n' $mark $mark >> $M/$V/0003_step.py; lit 0003"
check "d47b:data ...and the trusted step never executed it either (no marker)" "[ ! -e $mark ] && [ ! -e $mark.sys ]"

# --- I1: versions/ holds only regular NNNN_lowercase.py blobs -------------------------------------
for name in zz.py __init__.py 0003_X.py 0003.py 0003_x.pyc README 0003-x.py; do
  case_ "d47b:I1 a tracked versions/$name (Alembic could load it; the release glob would not see it) is refused by name" '\[I1\] versions/.* is not a revision file' \
    "mig 0003 '\"0002\"'; lit 0003; printf 'x = 1\n' > '$M/$V/$name'"
done
case_ "d47b:I1 a subdirectory in versions/ is refused (a tree, not a blob)" '\[I1\] versions/sub is not a regular file \(mode 040000\)' \
  'mkdir $M/$V/sub; mig 0003 "\"0002\""; mv $M/$V/0003_step.py $M/$V/sub/; lit 0003'
case_ "d47b:I1 a NEW revision that is a symlink (to main's 0002) is refused" '\[I1\] versions/0003_step.py is not a regular file \(mode 120000\)' \
  'ln -s 0002_next.py $M/$V/0003_step.py; lit 0003'
case_ "d47b:I1 main's 0001 replaced by a symlink to an outside file is refused" '\[I1\] versions/0001_base.py is not a regular file \(mode 120000\)' \
  'printf "x\n" > $T/outside.py; rm $M/$V/0001_base.py; ln -s $T/outside.py $M/$V/0001_base.py'
case_ "d47b:I1 a candidate without a versions/ directory is refused" '\[I1\] src/chronos/persistence/migrations/versions is not a directory' \
  'git -C $M rm -rq src/chronos/persistence/migrations'
case_ "d47b:I1 versions/ as a FILE is refused" '\[I1\] src/chronos/persistence/migrations/versions is not a directory' \
  'git -C $M rm -rq $V; mkdir -p $(dirname $M/$V); printf "x\n" > $M/$V'
case_ "d47b:I1 more than 256 entries in versions/ is refused before any is read" '\[I1\] versions/ holds more than 256 entries' \
  'for i in $(seq 3 259); do mig $(printf %04d $i) "\"$(printf %04d $((i-1)))\""; done; lit 0259'

# --- I2: ids are 4 digits, match the file name, unique --------------------------------------------
case_ "d47b:I2 0003_step.py declaring revision 0004 (filename/revision mismatch) is refused" '\[I2\] versions/0003_step.py declares revision 0004, its file name says 0003' \
  'mig 0003 "\"0002\""; sed -i "s/^revision = \"0003\"/revision = \"0004\"/" $M/$V/0003_step.py; lit 0003'
case_ "d47b:I2 two files with revision 0003 (0003_a.py, 0003_b.py: a second head under one id) are refused" '\[I2\] revision 0003 is declared twice' \
  'mig 0003 "\"0002\"" a; mig 0003 "\"0002\"" b; lit 0003'
case_ "d47b:I2 an int revision (revision = 3) is refused" '\[I2\] versions/0003_step.py: `revision` must be a 4-digit string' \
  'mig 0003 "\"0002\""; sed -i "s/^revision = \"0003\"/revision = 3/" $M/$V/0003_step.py; lit 0003'
case_ "d47b:I2 a non-4-digit revision string (\"3\") is refused" '\[I2\] versions/0003_step.py: `revision` must be a 4-digit string' \
  'mig 0003 "\"0002\""; sed -i "s/^revision = \"0003\"/revision = \"3\"/" $M/$V/0003_step.py; lit 0003'

# --- I3: append-only ------------------------------------------------------------------------------
case_ "d47b:I3 main's 0001 rewritten (one byte appended) is refused" "\\[I3\\] main.s versions/0001_base.py was changed" \
  'printf "# edited\n" >> $M/$V/0001_base.py'
case_ "d47b:I3 main's 0002 header rewritten (down_revision now None) is refused" "\\[I3\\] main.s versions/0002_next.py was changed" \
  'sed -i "s/^down_revision = .*/down_revision = None/" $M/$V/0002_next.py'
case_ "d47b:I3 main's 0002 deleted (and the literal lowered to 0001) is refused" "\\[I3\\] main.s versions/0002_next.py is missing" \
  'git -C $M rm -q $V/0002_next.py; lit 0001'
case_ "d47b:I3 main's 0002 renamed (0002_next.py → 0002_other.py) is refused" "\\[I3\\] main.s versions/0002_next.py is missing" \
  'git -C $M mv $V/0002_next.py $V/0002_other.py'
case_ "d47b:I3 main's 0001 made executable (mode 100755, same bytes) is refused" "\\[I3\\] main.s versions/0001_base.py was changed" \
  'chmod +x $M/$V/0001_base.py'
case_ "d47b:I3 a replace ref cannot launder a rewritten main migration (the trusted step reads objects with replacements off)" "\\[I3\\] main.s versions/0001_base.py was changed" \
  'printf "# laundered\\n" >> $M/$V/0001_base.py; git -C $M add -A; git -C $M replace "$(git -C $M rev-parse :$V/0001_base.py)" "$(git -C $M rev-parse $MAIN:$V/0001_base.py)"'
git -C "$M" replace -l | xargs -r git -C "$M" replace -d > /dev/null

# --- I4: one root, no dangling parent, no cycle, no branch, one head ------------------------------
case_ "d47b:I4 a second head with distinct ids (0003 and 0004 both revise 0002) is refused" '\[I4\] revision 0002 has 2 children \(0003, 0004\); the chain is linear' \
  'mig 0003 "\"0002\""; mig 0004 "\"0002\""; lit 0004'
case_ "d47b:I4 a down_revision cycle (0003 → 0004 → 0003) is refused" '\[I4\] down_revision cycle through 0003' \
  'mig 0003 "\"0004\""; mig 0004 "\"0003\""; lit 0004'
case_ "d47b:I4 a self-revising revision (0003 revises 0003) is refused" '\[I4\] down_revision cycle through 0003' \
  'mig 0003 "\"0003\""; lit 0003'
case_ "d47b:I4 a second root (0003 with down_revision None) is refused" '\[I4\] 2 roots \(0001, 0003\); the chain has exactly one' \
  'mig 0003 None; lit 0003'
case_ "d47b:I4 a dangling parent (0003 revises 0099) is refused" '\[I4\] revision 0003 revises 0099, which does not exist' \
  'mig 0003 "\"0099\""; lit 0003'

# --- I6: numbering continues by one ---------------------------------------------------------------
case_ "d47b:I6 0005 revising 0002 (a numbering gap) is refused" '\[I6\] revision 0005 revises 0002; the numbering continues by one' \
  'mig 0005 "\"0002\""; lit 0005'

# --- the literal-header rule (dynamic or duplicated headers never reach the chain) -----------------
hdr() {  # hdr <label> <regex> <sed on 0003_step.py or an appended line>
  case_ "d47b:header $1" "$2" "mig 0003 '\"0002\"'; $3; lit 0003"
}
NL='is not exactly one literal module-level assignment'
hdr "revision = compute() is refused" '\[header\] versions/0003_step.py: `revision` is not a literal' \
  "sed -i 's/^revision = .*/revision = str(3).zfill(4)/' $M/$V/0003_step.py"
hdr "revision = \"00\" + \"03\" (a constant expression, not a literal) is refused" '\[header\] versions/0003_step.py: `revision` is not a literal' \
  "sed -i 's/^revision = .*/revision = \"00\" + \"03\"/' $M/$V/0003_step.py"
hdr "an f-string revision is refused" '\[header\] versions/0003_step.py: `revision` is not a literal' \
  "sed -i 's/^revision = .*/revision = f\"{3:04d}\"/' $M/$V/0003_step.py"
hdr "a header assigned under if True: is refused" "\\[header\\] versions/0003_step.py: \`revision\` $NL" \
  "sed -i 's/^revision = \\(.*\\)/if True:\\n    revision = \\1/' $M/$V/0003_step.py"
hdr "a second assignment of revision is refused" "\\[header\\] versions/0003_step.py: \`revision\` $NL" \
  "printf 'revision = \"0009\"\n' >> $M/$V/0003_step.py"
hdr "a tuple-unpacked header is refused" "\\[header\\] versions/0003_step.py: \`revision\` $NL" \
  "sed -i 's/^revision = .*/revision, _x = \"0003\", 1/' $M/$V/0003_step.py"
hdr "an import rebinding revision is refused" "\\[header\\] versions/0003_step.py: \`revision\` $NL" \
  "printf 'import os as revision\n' >> $M/$V/0003_step.py"
hdr "a def named down_revision is refused" "\\[header\\] versions/0003_step.py: \`down_revision\` $NL" \
  "printf 'def down_revision() -> None:\n    pass\n' >> $M/$V/0003_step.py"
hdr "del revision at module level is refused" "\\[header\\] versions/0003_step.py: \`revision\` $NL" \
  "printf 'del revision\n' >> $M/$V/0003_step.py"
hdr "a walrus rebinding revision is refused" "\\[header\\] versions/0003_step.py: \`revision\` $NL" \
  "printf '_y = (revision := \"0009\")\n' >> $M/$V/0003_step.py"
hdr "a for-loop target named revision is refused" "\\[header\\] versions/0003_step.py: \`revision\` $NL" \
  "printf 'for revision in (\"0009\",):\n    pass\n' >> $M/$V/0003_step.py"
hdr "a missing depends_on is refused" "\\[header\\] versions/0003_step.py: \`depends_on\` $NL" \
  "sed -i '/^depends_on = /d' $M/$V/0003_step.py"
hdr "an annotation-only second binding (revision: str) is refused" "\\[header\\] versions/0003_step.py: \`revision\` $NL" \
  "printf 'revision: str\n' >> $M/$V/0003_step.py"
hdr "an int down_revision (2) is refused" '\[header\] versions/0003_step.py: `down_revision` must be a 4-digit string or None' \
  "sed -i 's/^down_revision = .*/down_revision = 2/' $M/$V/0003_step.py"
hdr "a 4-digit int down_revision (2000) is refused as a non-string, not left to the dangling-parent check (reader F2, M10)" \
  '\[header\] versions/0003_step.py: `down_revision` must be a 4-digit string or None' \
  "sed -i 's/^down_revision = .*/down_revision = 2000/' $M/$V/0003_step.py"
hdr "branch_labels as a tuple is refused" '\[header\] versions/0003_step.py: `branch_labels` is not a literal' \
  "sed -i 's/^branch_labels = .*/branch_labels = (\"x\",)/' $M/$V/0003_step.py"
hdr "branch_labels as a string is refused" '\[header\] versions/0003_step.py: `branch_labels` must be None' \
  "sed -i 's/^branch_labels = .*/branch_labels = \"x\"/' $M/$V/0003_step.py"
hdr "depends_on naming a revision is refused" '\[header\] versions/0003_step.py: `depends_on` must be None' \
  "sed -i 's/^depends_on = .*/depends_on = \"0001\"/' $M/$V/0003_step.py"
check "d47b:header a function-local variable named revision is NOT a module-level binding (no false refusal)" \
  "fresh; mig 0003 '\"0002\"'; printf 'def downgrade() -> None:\n    revision = 1\n    del revision\n' >> $M/$V/0003_step.py; lit 0003; commit cand; run_chain; passes 0003"

# --- [module]: module level is data and imports only (D47B-FIX1, review P1-1) ---------------------------
# A literal header can be rebound when the module body runs (globals()[...] store, exec, star import); the
# runtime graph then forks at main's head and merges back to the declared head. The trusted step refuses every
# module-level statement outside a whitelist instead, judged on the text the interpreter runs ([encoding] below);
# a module the file imports can still rebind its header (owner residual, not caught here). mig() writes 12 lines,
# so an appended payload starts at line 13. Payloads live in files so no shell quoting reaches the Python text.
P=$T/payloads; mkdir "$P"
pl() { cat > "$P/$1"; }  # pl <name> <<'EOF' ... EOF
app() { cat "$P/$1" >> "$M/$V/${2:-0003_step.py}"; }
pl v1a <<'EOF'
globals()["down_revision"] = "0002"
EOF
pl v1b <<'EOF'
globals()["down_revision"] = ("0003", "0004")
EOF
pl v3a <<'EOF'
exec('down_revision = "0002"')
EOF
pl v3b <<'EOF'
exec('down_revision = ("0003", "0004")')
EOF
pl v8a <<'EOF'
from chronos_fork import *
EOF
pl v8b <<'EOF'
from chronos_merge import *
EOF
pl v2a <<'EOF'
import sys as _s, types as _t
class _M(_t.ModuleType):
    down_revision = property(lambda self: "0002")
_s.modules[__name__].__class__ = _M
EOF
pl v2b <<'EOF'
import sys as _s, types as _t
class _M(_t.ModuleType):
    down_revision = property(lambda self: ("0003", "0004"))
_s.modules[__name__].__class__ = _M
EOF
fork() {  # fork <payload 0004> <payload 0005>: honest literal headers 0003 → 0004 → 0005 whose bodies fork at 0002 and merge
  mig 0003 '"0002"'; mig 0004 '"0003"' b; app "$1" 0004_b.py; mig 0005 '"0004"' c; app "$2" 0005_c.py; lit 0005
}
MF='\[module\] versions/0004_b.py line'
case_ "d47b:module V1 globals()[\"down_revision\"] = ... (runtime fork at 0002, merge at 0005) is refused at the trusted step" \
  "$MF 13: an assignment binds one plain name to a literal value" 'fork v1a v1b'
case_ "d47b:module V3 exec('down_revision = ...') at module level is refused at the trusted step" \
  "$MF 13: a module-level Expr statement is not allowed" 'fork v3a v3b'
case_ "d47b:module V8 a star import of a helper on the sandbox PYTHONPATH (src/) is refused at the trusted step" \
  "$MF 13: a star import is not allowed" \
  'printf "down_revision = \"0002\"\n" > $M/src/chronos_fork.py; printf "down_revision = (\"0003\", \"0004\")\n" > $M/src/chronos_merge.py; fork v8a v8b'
case_ "d47b:module V2 a module __class__ swap (class body + attribute store) is refused at the trusted step" \
  "$MF 14: a module-level ClassDef statement is not allowed" 'fork v2a v2b'

M3='\[module\] versions/0003_step.py'
FUT="$M3: \`from __future__ import annotations\` must open the module \\(after the docstring\\)"
case_ "d47b:module a revision without \`from __future__ import annotations\` is refused (def annotations would run at def time)" "$FUT" \
  "mig 0003 '\"0002\"'; sed -i '/^from __future__/d' $M/$V/0003_step.py; lit 0003"
case_ "d47b:module \`from __future__ import annotations\` after another import is refused" "$FUT" \
  "mig 0003 '\"0002\"'; sed -i 's/^from __future__ import annotations\$/import os\\nfrom __future__ import annotations/' $M/$V/0003_step.py; lit 0003"
case_ "d47b:module a first __future__ import naming more than annotations is refused" "$FUT" \
  "mig 0003 '\"0002\"'; sed -i 's/^from __future__ import annotations\$/from __future__ import annotations, generator_stop/' $M/$V/0003_step.py; lit 0003"
case_ "d47b:module a first import of \`annotations\` from another module is not the future import (annotations would run)" "$FUT" \
  "mig 0003 '\"0002\"'; sed -i 's/^from __future__ import annotations\$/from typing import annotations/' $M/$V/0003_step.py; lit 0003"
case_ "d47b:module a relative \`from .__future__ import annotations\` is not the future import" "$FUT" \
  "mig 0003 '\"0002\"'; sed -i 's/^from __future__ import annotations\$/from .__future__ import annotations/' $M/$V/0003_step.py; lit 0003"
case_ "d47b:module a second __future__ import is refused" "$M3 line 13: a second \`from __future__\` import is not allowed" \
  "mig 0003 '\"0002\"'; printf 'from __future__ import annotations\n' >> $M/$V/0003_step.py; lit 0003"
case_ "d47b:module a revision without a docstring (the future import first) passes" PASS:0003 \
  "mig 0003 '\"0002\"'; sed -i '1d' $M/$V/0003_step.py; lit 0003"

mw() {  # mw <label> <line: reason regex, or PASS> <payload text on stdin> : one payload appended to 0003_step.py
  local name="mw$((pass + fail))"; pl "$name"
  case "$2" in
    PASS) case_ "d47b:module $1" PASS:0003 "mig 0003 '\"0002\"'; app $name; lit 0003" ;;
    *) case_ "d47b:module $1" "$M3 line $2" "mig 0003 '\"0002\"'; app $name; lit 0003" ;;
  esac
}
ASG='an assignment binds one plain name to a literal value'
mw "a bare string after the docstring is refused (only the first statement may be a docstring)" "13: a module-level Expr statement is not allowed" <<'EOF'
"""not a docstring"""
EOF
mw "an attribute store (sys.modules[__name__].down_revision = ...) is refused" "14: $ASG" <<'EOF'
import sys
sys.modules[__name__].down_revision = "0002"
EOF
mw "a chained assignment (_a = _b = 1) is refused" "13: $ASG" <<'EOF'
_a = _b = 1
EOF
mw "an annotation without a value (_x: int) is refused" "13: $ASG" <<'EOF'
_x: int
EOF
mw "a lambda value is refused" "13: $ASG" <<'EOF'
_l = lambda: exec('down_revision = "0002"')
EOF
mw "a bare-name call value (_g = globals()) is refused" "13: $ASG" <<'EOF'
_g = globals()
EOF
mw "a call rooted outside sqlalchemy (types.SimpleNamespace()) is refused" "14: $ASG" <<'EOF'
import types
_ns = types.SimpleNamespace()
EOF
mw "a sqlalchemy alias rebound by a second import (sa = types) is not sqlalchemy" "15: $ASG" <<'EOF'
import sqlalchemy as sa
import types as sa
_ns = sa.SimpleNamespace()
EOF
mw "a lowercase sqlalchemy function (sa.text) is refused; only CamelCase constructors" "14: $ASG" <<'EOF'
import sqlalchemy as sa
_t = sa.text("select 1")
EOF
mw "a negative number (_D = -1, a UnaryOp) is refused: benign but not a literal (R2, reader F3)" "13: $ASG" <<'EOF'
_D = -1
EOF
mw "an attribute alias of an imported module (_T = schema.SOME_TABLE) is refused (R2, reader F3)" "14: $ASG" <<'EOF'
from chronos.persistence import schema
_T = schema.SOME_TABLE
EOF
mw "a dotted sqlalchemy path (sa.types.Integer) is refused; only sa.<Name>" "14: $ASG" <<'EOF'
import sqlalchemy as sa
_i = sa.types.Integer()
EOF
mw "a non-literal constructor argument is refused" "14: $ASG" <<'EOF'
import sqlalchemy as sa
_c = sa.Column(exec('down_revision = "0002"'))
EOF
mw "a non-literal constructor keyword is refused" "14: $ASG" <<'EOF'
import sqlalchemy as sa
_c = sa.Column("x", default=exec('down_revision = "0002"'))
EOF
mw "a ** argument (even of a literal) is refused" "14: $ASG" <<'EOF'
import sqlalchemy as sa
_c = sa.Column("x", **())
EOF
mw "a non-literal tuple element is refused" "13: $ASG" <<'EOF'
_t = ("x", exec('down_revision = "0002"'))
EOF
mw "a decorated def is refused (the decorator runs at import; reported at the def line)" "14: a decorated def is not allowed" <<'EOF'
@(lambda f: f)
def _f() -> None:
    pass
EOF
mw "a def with type parameters is refused" "13: a def with type parameters is not allowed" <<'EOF'
def _f[T]() -> None:
    pass
EOF
mw "a non-constant positional default is refused (defaults run at def time)" "13: a def default must be a constant" <<'EOF'
def _f(_x=exec('down_revision = "0002"')) -> None:
    pass
EOF
mw "a non-constant keyword-only default is refused" "13: a def default must be a constant" <<'EOF'
def _f(*, _x=exec('down_revision = "0002"')) -> None:
    pass
EOF
for kind in "ClassDef|class _C:\n    pass" "If|if True:\n    pass" "Try|try:\n    pass\nexcept Exception:\n    pass" \
  "With|with open('/dev/null'):\n    pass" "For|for _i in ():\n    pass" "While|while False:\n    pass" \
  "Assert|assert True" "Pass|pass" "TypeAlias|type _A = int" "Raise|raise SystemExit(0)"; do
  mw "a module-level ${kind%%|*} is refused" "13: a module-level ${kind%%|*} statement is not allowed" < <(printf '%b\n' "${kind#*|}")
done
mw "an augmented assignment is refused" "14: a module-level AugAssign statement is not allowed" <<'EOF'
_n = 1
_n += 1
EOF
mw "a module-level del of a private name is refused" "14: a module-level Delete statement is not allowed" <<'EOF'
_n = 1
del _n
EOF
mw "main's constant shapes pass: sqlalchemy CamelCase constructors on literals inside tuples (0005/0013), strings, lists" PASS <<'EOF'
import sqlalchemy as sa
_NEW_COLUMNS = (
    sa.Column("delivered_at", sa.DateTime(), nullable=True),
    sa.Column("delivery_attempts", sa.Integer(), nullable=False, server_default="0"),
)
_COLUMNS = (("order_ref", sa.Column("order_ref", sa.String(length=120), nullable=True)),)
_INDEX = "ix_fills_permanent_id"
_NAMES = ["a", 1, None]
EOF
mw "an unaliased import sqlalchemy, other imports, constant defaults and async defs pass" PASS <<'EOF'
import os.path
import sqlalchemy
from datetime import UTC, datetime
from alembic import op
_c = sqlalchemy.Integer()


def _helper(_x=1, *, _y=None) -> None:
    pass


async def _later() -> None:
    pass
EOF
pl badmain <<'EOF'
exec('pass')
EOF
fresh; app badmain 0002_next.py; commit bad-module-main; BADMOD=$(git -C "$M" rev-parse HEAD); git -C "$M" show "$BADMOD:$S" > "$T/overlay.orig"
fresh; mig 0003 '"0002"'; lit 0003; commit cand; run_chain "$BADMOD"
check "d47b:module the whitelist reads main's files too: a trusted base failing it is a trusted-base defect" \
  "refused '\\[module\\] versions/0002_next.py line 13: a module-level Expr statement is not allowed \\(trusted base: a trusted-base defect; fix main first\\)'"
git -C "$M" show "$MAIN:$S" > "$T/overlay.orig"
REAL=$(git -C "$ROOT" rev-parse HEAD); git -C "$ROOT" show "$REAL:$S" > "$T/real-overlay"; cp "$T/real-overlay" "$T/real-overlay.orig"
REALHEAD=$(sed -n 's/^_MIGRATION_HEAD: Final\[str\] = "\([0-9]\{4\}\)"$/\1/p' "$T/real-overlay")
(cd "$ROOT" && python3 -I -B "$G/lib/migration_chain.py" "$ROOT" "$REAL" "$ROOT" "$REAL" "$T/real-overlay" > "$T/out" 2> "$T/err"; echo $? > "$T/rc")
check "d47b:module positive control: this repo's own revision files (at HEAD, $(git -C "$ROOT" ls-tree --name-only "$REAL:$V" | grep -c '\.py$') of them, head $REALHEAD) pass the whitelist unchanged" \
  "[ -n '$REALHEAD' ] && passes $REALHEAD && cmp -s $T/real-overlay $T/real-overlay.orig"

# --- parse bombs: size cap, encoding, null bytes, nesting, syntax -----------------------------------
hdr "a revision file over 64 KiB is refused before it is parsed" '\[parse\] versions/0003_step.py is larger than 65536 bytes' \
  "python3 -c 'print(\"#\" * 70000)' >> $M/$V/0003_step.py"
hdr "invalid UTF-8 is refused" '\[parse\] versions/0003_step.py cannot be parsed as data \(UnicodeDecodeError\)' \
  "printf '# \\377\\376\n' >> $M/$V/0003_step.py"
hdr "a null byte is refused" '\[parse\] versions/0003_step.py cannot be parsed as data \((SyntaxError|ValueError)\)' \
  "printf 'x = 1\\000\n' >> $M/$V/0003_step.py"
hdr "a nesting bomb (300 parentheses) is refused" '\[parse\] versions/0003_step.py cannot be parsed as data \(SyntaxError\)' \
  "python3 -c 'print(\"x = \" + \"(\" * 300 + \"1\" + \")\" * 300)' >> $M/$V/0003_step.py"
hdr "a unary-operator stack bomb (60000 minus signs, under the cap) is refused" '\[parse\] versions/0003_step.py cannot be parsed as data \((MemoryError|RecursionError|SyntaxError)\)' \
  "python3 -c 'print(\"x = \" + \"-\" * 60000 + \"1\")' >> $M/$V/0003_step.py"
hdr "a syntax error is refused" '\[parse\] versions/0003_step.py cannot be parsed as data \(SyntaxError\)' \
  "printf 'def (\n' >> $M/$V/0003_step.py"

# --- [encoding]: the trusted step parses exactly the text the interpreter runs (D47B-FIX2, reader F1) -------
# CPython decodes a source file with its BOM or PEP 263 coding cookie, so a parse of the same bytes as plain
# UTF-8 can see a different program: under `# coding: utf-7`, `+AAo-` is a newline to the interpreter and
# comment text to a UTF-8 reader, so a header rebinding or a call hides inside a "comment". The trusted step
# refuses any file the interpreter would not read as plain UTF-8 (a BOM, a non-utf-8 cookie, an undecidable
# encoding) and any coding cookie at all; every variant's payload is shown live by the interpreter's own parse.
cat > "$T/enc.py" <<'EOF'
import sys
path, kind, arg = sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else ""
data = open(path, "rb").read()
def hide(code):  # ASCII "comment" text that a utf-7 decoder reads as: newline, `code`, newline
    return b"# x +AAo-" + code.encode() + b"+AAo-\n"
if kind == "utf7":       # cookie on line 1, `arg` hidden at the end
    data = b"# coding: utf-7\n" + data + hide(arg)
elif kind == "cr-utf7":  # the cookie after a lone CR on line 1, `arg` hidden at the end
    data = b"# note\r# coding: utf-7\n" + data + hide(arg)
elif kind == "line1":    # `arg` as line 1
    data = arg.encode() + b"\n" + data
elif kind == "line2":    # `arg` as line 2, after a comment line
    data = b"# synthetic\n" + arg.encode() + b"\n" + data
elif kind == "bom":
    data = b"\xef\xbb\xbf" + data
elif kind == "utf16":    # the whole file re-encoded as UTF-16 (with its BOM), its cookie saying so
    data = ("# coding: utf-16\n" + data.decode()).encode("utf-16")
open(path, "wb").write(data)
EOF
enc() { python3 -I "$T/enc.py" "$@"; }
seen() {  # seen <file> <name>: how many module-level stores of <name> the INTERPRETER's own parse (of the bytes) sees
  python3 -I -c 'import ast, sys; t = ast.parse(open(sys.argv[1], "rb").read()); print(sum(1 for n in t.body if isinstance(n, (ast.Assign, ast.AnnAssign)) and getattr(n.targets[0] if isinstance(n, ast.Assign) else n.target, "id", None) == sys.argv[2]))' "$1" "$2"
}
E3='\[encoding\] versions/0003_step.py'
DEC='the interpreter would decode it as'
UND='the interpreter cannot determine its encoding \(SyntaxError\)'
COOK='a coding cookie is not allowed'
case_ "d47b:encoding F1 a utf-7 cookie hiding down_revision rebindings (runtime fork at 0001, merge at 0004: the reader's probe) is refused" \
  "$E3: $DEC utf-7" \
  "mig 0003 '\"0002\"'; enc $M/$V/0003_step.py utf7 'down_revision = \"0001\"'; mig 0004 '\"0003\"'; enc $M/$V/0004_step.py utf7 'down_revision = (\"0002\", \"0003\")'; lit 0004"
check "d47b:encoding F1 ...positive control: the interpreter's parse of 0003's bytes sees TWO down_revision stores (the payload is live)" \
  "[ \"\$(seen $M/$V/0003_step.py down_revision)\" = 2 ]"
mark7=$T/utf7-ran
case_ "d47b:encoding F1 a utf-7 cookie hiding a module-level call is refused ..." "$E3: $DEC utf-7" \
  "mig 0003 '\"0002\"'; enc $M/$V/0003_step.py utf7 'open(\"$mark7\", \"w\").write(\"ran\")'; lit 0003"
check "d47b:encoding F1 ...and the trusted step never ran it (no marker)" "[ ! -e $mark7 ]"
(cd "$T" && python3 -I -B "$M/$V/0003_step.py")
check "d47b:encoding F1 ...positive control: the interpreter running that file executes the hidden call (marker written)" "[ -e $mark7 ]"
case_ "d47b:encoding a utf-7 cookie after a lone CR on line 1 is refused" "$E3: $DEC utf-7" \
  "mig 0003 '\"0002\"'; enc $M/$V/0003_step.py cr-utf7 'down_revision = \"0001\"'; lit 0003"
case_ "d47b:encoding a latin-1 cookie on an otherwise honest ASCII revision is refused" "$E3: $DEC iso-8859-1" \
  "mig 0003 '\"0002\"'; enc $M/$V/0003_step.py line1 '# -*- coding: latin-1 -*-'; lit 0003"
case_ "d47b:encoding a utf-16 cookie on ASCII text is refused" "$E3: $UND" \
  "mig 0003 '\"0002\"'; enc $M/$V/0003_step.py line1 '# coding: utf-16'; lit 0003"
case_ "d47b:encoding a revision encoded as UTF-16 with its BOM is refused" "$E3: $UND" \
  "mig 0003 '\"0002\"'; enc $M/$V/0003_step.py utf16; lit 0003"
case_ "d47b:module V4 a UTF-8 BOM (the interpreter decodes utf-8-sig) is refused by name" "$E3: $DEC utf-8-sig" \
  "mig 0003 '\"0002\"'; enc $M/$V/0003_step.py bom; lit 0003"
case_ "d47b:encoding an undecidable cookie (BOM plus latin-1) is refused" "$E3: $UND" \
  "mig 0003 '\"0002\"'; enc $M/$V/0003_step.py line1 '# coding: latin-1'; enc $M/$V/0003_step.py bom; lit 0003"
case_ "d47b:encoding a utf-8 cookie spelled UTF8 is refused (the interpreter reports UTF8, not utf-8)" "$E3: $DEC UTF8" \
  "mig 0003 '\"0002\"'; enc $M/$V/0003_step.py line1 '# coding: UTF8'; lit 0003"
case_ "d47b:encoding an honest utf-8 cookie on line 1 is refused: no cookie at all (plain UTF-8 is the default)" "$E3: $COOK" \
  "mig 0003 '\"0002\"'; enc $M/$V/0003_step.py line1 '# -*- coding: utf-8 -*-'; lit 0003"
case_ "d47b:encoding a utf-8 cookie on line 2 (vim fileencoding=) is refused" "$E3: $COOK" \
  "mig 0003 '\"0002\"'; enc $M/$V/0003_step.py line2 '# vim: set fileencoding=utf-8 :'; lit 0003"
case_ "d47b:encoding positive control: non-ASCII UTF-8 text (as main's files carry) passes" PASS:0003 \
  "mig 0003 '\"0002\"'; sed -i '1s/Synthetic revision/Synthetic — naïve revision/' $M/$V/0003_step.py; lit 0003"
case_ "d47b:encoding positive control: a coding comment past line 2 is not a cookie to the interpreter and passes" PASS:0003 \
  "mig 0003 '\"0002\"'; printf '# coding: latin-1\n' >> $M/$V/0003_step.py; lit 0003"
case_ "d47b:encoding the candidate's release script under a utf-7 cookie hiding a second _MIGRATION_HEAD is refused ..." \
  "\\[encoding\\] scripts/verify_release_artifact.py: $DEC utf-7" \
  "mig 0003 '\"0002\"'; lit 0003; enc $M/$S utf7 '_MIGRATION_HEAD = \"0002\"'"
check "d47b:encoding ...positive control: the interpreter's parse of that script sees TWO _MIGRATION_HEAD stores" "[ \"\$(seen $M/$S _MIGRATION_HEAD)\" = 2 ]"
case_ "d47b:encoding the candidate's release script with a utf-8 cookie is refused" "\\[encoding\\] scripts/verify_release_artifact.py: $COOK" \
  "mig 0003 '\"0002\"'; lit 0003; enc $M/$S line1 '# coding: utf-8'"
cp "$T/overlay.orig" "$T/overlay.main"; enc "$T/overlay.orig" utf7 '_MIGRATION_HEAD: Final[str] = "0001"'
fresh; mig 0003 '"0002"'; lit 0003; commit cand; run_chain
check "d47b:encoding a trusted overlay under a utf-7 cookie FAILs as a trusted-base defect" \
  "refused '\\[encoding\\] scripts/verify_release_artifact.py: $DEC utf-7; only plain UTF-8 is read \\(trusted base: a trusted-base defect; fix main first\\)'"
cp "$T/overlay.main" "$T/overlay.orig"

# --- I8: the candidate's own literal agrees with its chain (what CI checks) --------------------------
case_ "d47b:I8 appending 0003 but leaving the candidate's literal at 0002 is refused" "\\[I8\\] the candidate.s _MIGRATION_HEAD is 0002, its chain.s head is 0003" \
  'mig 0003 "\"0002\""'
case_ "d47b:I8 a literal set to 0099 with no new migration is refused" "\\[I8\\] the candidate.s _MIGRATION_HEAD is 0099, its chain.s head is 0002" \
  'lit 0099'
case_ "d47b:I8 two _MIGRATION_HEAD assignments in the candidate's script are refused" '\[I8\] scripts/verify_release_artifact.py has 2 `_MIGRATION_HEAD` assignment\(s\)' \
  "mig 0003 '\"0002\"'; lit 0003; printf '_MIGRATION_HEAD = \"0002\"\n' >> $M/$S"
case_ "d47b:I8 a non-canonical literal line (_MIGRATION_HEAD = \"0003\") is refused" '\[I8\] scripts/verify_release_artifact.py has 1 `_MIGRATION_HEAD` assignment\(s\)' \
  "mig 0003 '\"0002\"'; lit 0003; sed -i 's/^_MIGRATION_HEAD: Final\\[str\\] = /_MIGRATION_HEAD = /' $M/$S"
case_ "d47b:I8 a candidate without scripts/verify_release_artifact.py is refused" "\\[I8\\] cannot read the candidate.s scripts/verify_release_artifact.py" \
  'mig 0003 "\"0002\""; git -C $M rm -q $S'

# --- I7: main must agree with itself (a trusted-base defect is never a pass) -------------------------
fresh; lit 0001; commit bad-main; BADLIT=$(git -C "$M" rev-parse HEAD); git -C "$M" show "$BADLIT:$S" > "$T/overlay.orig"
fresh; mig 0003 '"0002"'; lit 0003; commit cand; run_chain "$BADLIT"
check "d47b:I7 a trusted base whose literal (0001) is not its chain's head (0002) FAILs as a trusted-base defect" "refused '\\[I7\\] the trusted base.s _MIGRATION_HEAD is 0001, its chain.s head is 0002 \\(a trusted-base defect; fix main first\\)'"
fresh; mig 0003 '"0002"'; mig 0004 '"0002"'; lit 0004; commit bad-chain; BADCHAIN=$(git -C "$M" rev-parse HEAD); git -C "$M" show "$BADCHAIN:$S" > "$T/overlay.orig"
fresh; mig 0003 '"0002"'; lit 0003; commit cand; run_chain "$BADCHAIN"
check "d47b:I7 a trusted base with a branched chain FAILs as a trusted-base defect" "refused '\\[I4\\] revision 0002 has 2 children .* \\(trusted base: a trusted-base defect; fix main first\\)'"
printf '_MIGRATION_HEAD: Final[str] = "0002"\n' >> "$T/overlay.orig"; fresh; commit same; run_chain
check "d47b:I7 a trusted overlay with two literal lines FAILs as a trusted-base defect" "refused '\\[I7\\] scripts/verify_release_artifact.py has 2 .* \\(trusted base: a trusted-base defect; fix main first\\)'"
git -C "$M" show "$MAIN:$S" > "$T/overlay.orig"

# --- isolation: the trusted script itself never imports from the candidate tree or PYTHONPATH -------
MK=$T/markers; mkdir "$MK"
for m in ast re subprocess warnings collections; do
  printf 'open("%s/%s", "w").write("ran")\n' "$MK" "$m" > "$T/shadow-$m.py"
done
fresh; mig 0003 '"0002"'; lit 0003; mkdir -p "$M/src"
for m in ast re subprocess warnings collections; do cp "$T/shadow-$m.py" "$M/$m.py"; cp "$T/shadow-$m.py" "$M/src/$m.py"; done; commit shadows
(cd "$M" && PYTHONPATH="$M/src:$M" python3 -c 'import ast' 2>/dev/null)
check "d47b:iso positive control: the planted ast.py runs under a plain python3 with PYTHONPATH naming the candidate's src/" "[ -e $MK/ast ]"
rm -f "$MK"/*; PYTHONPATH="$M/src:$M" run_chain
check "d47b:iso with ast/re/subprocess/warnings/collections shadows tracked at the top level and in src/, and PYTHONPATH naming them, the verdict is unchanged ..." "passes 0003"
check "d47b:iso ...and none of them ran (python3 -I -B: no cwd, no script dir, no PYTHON* env)" "[ -z \"\$(ls -A $MK)\" ]"

check "d47b:static gate 40 calls the trusted script isolated, from the trusted gate set (\$(dirname \"\$0\")/lib), never the candidate's copy" \
  "grep -qE '^mhead=\"\\\$\\(python3 -I -B \"\\\$\\(dirname \"\\\$0\"\\)/lib/migration_chain.py\" ' $G/40-evidence-at-head.sh"
echo "test_gate40_migration_chain: $pass ok, $fail failed"
[ "$fail" -eq 0 ]
