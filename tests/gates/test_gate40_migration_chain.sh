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
case_ "d47b:data a revision whose module body runs code (writes a marker, os.system) still passes on its literal header ..." PASS:0003 \
  "mig 0003 '\"0002\"'; printf 'open(\"%s\", \"w\").write(\"ran\")\nimport os\nos.system(\"touch %s.sys\")\nraise SystemExit(3)\n' $mark $mark >> $M/$V/0003_step.py; lit 0003"
check "d47b:data ...and the trusted step never imported or executed it (no marker from open() or os.system)" "[ ! -e $mark ] && [ ! -e $mark.sys ]"

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
hdr "branch_labels as a tuple is refused" '\[header\] versions/0003_step.py: `branch_labels` is not a literal' \
  "sed -i 's/^branch_labels = .*/branch_labels = (\"x\",)/' $M/$V/0003_step.py"
hdr "branch_labels as a string is refused" '\[header\] versions/0003_step.py: `branch_labels` must be None' \
  "sed -i 's/^branch_labels = .*/branch_labels = \"x\"/' $M/$V/0003_step.py"
hdr "depends_on naming a revision is refused" '\[header\] versions/0003_step.py: `depends_on` must be None' \
  "sed -i 's/^depends_on = .*/depends_on = \"0001\"/' $M/$V/0003_step.py"
check "d47b:header a function-local variable named revision is NOT a module-level binding (no false refusal)" \
  "fresh; mig 0003 '\"0002\"'; printf 'def downgrade() -> None:\n    revision = 1\n    del revision\n' >> $M/$V/0003_step.py; lit 0003; commit cand; run_chain; passes 0003"

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
