"""gates/lib/migration_chain.py — TRUSTED (gate 40, D-47(b)): the expected migration head, as DATA.

Run by gate 40 from the trusted gate set, never from the candidate, and always isolated:

    python3 -I -B <trusted gates>/lib/migration_chain.py <trusted repo> <trusted commit> \
        <lane repo> <PR_HEAD_SHA> <trusted overlay of scripts/verify_release_artifact.py>

Both migration chains are read from git OBJECTS (main's at the trusted commit, the candidate's
at PR_HEAD_SHA in the lane's object store): modes and bytes come from the trees, never from a work
tree, and no migration is imported, compiled to code or executed. Each revision file's header is
read with ``ast.parse`` on its text (size-capped; any parser failure is a named FAIL). That text is
the text the interpreter runs: a revision file or release script with a BOM, a coding cookie, or
any encoding the interpreter would not read as plain UTF-8 is a named FAIL ``[encoding]`` (under
``# coding: utf-7`` a "comment" is code to the interpreter). Invariants, each a named FAIL
``[I1]``..``[I8]``:

  I1 versions/ holds only regular ``NNNN_lowercase.py`` blobs (no link, gitlink, subtree, other)
  I2 every ``revision`` is 4 digits, equals its file name's prefix, and is unique
  I3 append-only: every main revision file is in the candidate, same name, same mode, same bytes
  I4 one root, every ``down_revision`` exists, no cycle, no revision with two children, one head
  I5 main's head is an ancestor of the candidate's head
  I6 numbering continues by one: ``int(revision) == int(down_revision) + 1``
  I7 main's own chain passes I1-I6 and its head equals main's ``_MIGRATION_HEAD`` literal (else a
     trusted-base defect, never a pass)
  I8 the candidate's ``scripts/verify_release_artifact.py`` declares the same head (what CI checks)

Every revision file's MODULE LEVEL must also be data and imports only (named FAIL ``[module]``),
so a rebinding WRITTEN IN THE FILE (``globals()[...]``, ``exec``, a star import) is refused: an
optional docstring, then ``from __future__ import annotations``
(annotations never run), then only imports without ``*``, one-name assignments of literal values
(constants, tuples/lists of them, and ``<sqlalchemy alias>.<CamelCase>(...)`` constructors on
literals, as main's column tuples use), and undecorated defs without type parameters whose
defaults are constants. Anything else is refused, benign or not (a negative number, an attribute
alias such as ``schema.TABLE``, a lowercase ``sa.text(...)`` call, a class, a decorator): a
legitimate need is a gates/ change first. Def bodies are NOT inspected (they run only in the
sandbox), and an IMPORTED module is candidate code that runs at import: it can still rebind the
importing file's header, which this step cannot see (the README's trust model says so), so the
body that runs at import is NOT proven unable to rebind a header.

On success the trusted overlay's one ``_MIGRATION_HEAD`` line is set to the derived head (no write
when it is already main's) and the head is printed. Every other byte of the overlay stays main's.
"""

from __future__ import annotations

import ast
import io
import os
import re
import subprocess
import sys
import tokenize
import warnings
from collections import Counter

VERSIONS = "src/chronos/persistence/migrations/versions"
SCRIPT = "scripts/verify_release_artifact.py"
NAME = re.compile(r"[0-9]{4}_[a-z0-9_]+\.py")
REVISION = re.compile(r"[0-9]{4}")
LITERAL = re.compile(r'_MIGRATION_HEAD: Final\[str\] = "([0-9]{4})"')
ASSIGNS_LITERAL = re.compile(r"\s*_MIGRATION_HEAD\b\s*(?::[^=]*)?=(?!=)")
HEADER = ("revision", "down_revision", "branch_labels", "depends_on")
MAX_FILES, MAX_BYTES, MAX_SCRIPT_BYTES = 256, 64 * 1024, 1024 * 1024
REGULAR = ("100644", "100755")
TRUSTED_DEFECT = "(trusted base: a trusted-base defect; fix main first)"
CONSTRUCTOR = re.compile(r"[A-Z][A-Za-z0-9]*")
COOKIE = re.compile(rb"#[^\n]*?coding[:=]")


class ChainError(Exception):
    pass


def shown(text: object) -> str:
    return re.sub(r"[^ -~]", "?", str(text))[:80]


def git(repo: str, *args: str) -> bytes:
    env = dict(os.environ, GIT_NO_REPLACE_OBJECTS="1")
    done = subprocess.run(["git", "-C", repo, *args], capture_output=True, env=env)
    if done.returncode != 0:
        raise ChainError(f"git {' '.join(args[:2])} failed in {shown(repo)}")
    return done.stdout


def blob(repo: str, oid: str, cap: int, what: str) -> bytes:
    if int(git(repo, "cat-file", "-s", oid)) > cap:
        raise ChainError(f"[parse] {what} is larger than {cap} bytes")
    return git(repo, "cat-file", "blob", oid)


def check_encoding(data: bytes, where: str) -> None:
    """Refuse ``data`` unless the interpreter decodes it as plain UTF-8 (named FAIL ``[encoding]``).

    CPython decodes a source file by its BOM or PEP 263 coding cookie; this step reads the bytes as
    plain UTF-8, so under ``# coding: utf-7`` it would judge comment text that the interpreter runs
    as code. Refused: an encoding other than ``utf-8`` (as ``tokenize.detect_encoding`` reports it,
    BOM included), an undecidable one, and any coding cookie at all: with no BOM and no cookie in
    the first two lines (a superset of every interpreter's cookie lines), every CPython reads UTF-8.
    """
    try:
        encoding, _ = tokenize.detect_encoding(io.BytesIO(data).readline)
    except SyntaxError:
        raise ChainError(
            f"[encoding] {where}: the interpreter cannot determine its encoding (SyntaxError)"
        ) from None
    if encoding != "utf-8":
        raise ChainError(
            f"[encoding] {where}: the interpreter would decode it as {shown(encoding)}; "
            "only plain UTF-8 is read"
        )
    if COOKIE.search(b"\n".join(data.split(b"\n", 2)[:2])):
        raise ChainError(
            f"[encoding] {where}: a coding cookie is not allowed "
            "(plain UTF-8, the default, is what this step reads)"
        )


def versions(repo: str, commit: str) -> dict[str, tuple[str, bytes]]:
    """The versions/ entries at ``commit`` → {name: (mode, bytes)}; I1 shape checks on the way."""
    try:
        raw = git(repo, "ls-tree", "-z", f"{commit}:{VERSIONS}")
    except ChainError:
        raise ChainError(f"[I1] {VERSIONS} is not a directory at {shown(commit)}") from None
    entries = [rec for rec in raw.split(b"\0") if rec]
    if len(entries) > MAX_FILES:
        raise ChainError(f"[I1] versions/ holds more than {MAX_FILES} entries")
    files: dict[str, tuple[str, bytes]] = {}
    for rec in entries:
        meta, _, name_b = rec.partition(b"\t")
        mode, kind, oid = meta.decode().split()
        name = name_b.decode("utf-8", "replace")
        if mode not in REGULAR or kind != "blob":
            raise ChainError(f"[I1] versions/{shown(name)} is not a regular file (mode {mode})")
        if not NAME.fullmatch(name):
            raise ChainError(
                f"[I1] versions/{shown(name)} is not a revision file (NNNN_lowercase.py)"
            )
        files[name] = (mode, blob(repo, oid, MAX_BYTES, f"versions/{name}"))
    if not files:
        raise ChainError(f"[I1] versions/ holds no revision file at {shown(commit)}")
    return files


def module_scope_stores(tree: ast.Module) -> list[str]:
    """Every name bound at module scope (function, class and lambda bodies excluded)."""
    found: list[str] = []
    stack: list[ast.AST] = list(tree.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            found.append(node.name)
            stack.extend(node.decorator_list)
            if isinstance(node, ast.ClassDef):
                stack.extend([*node.bases, *node.keywords])
            else:
                stack.extend([*node.args.defaults, *[d for d in node.args.kw_defaults if d]])
            continue
        if isinstance(node, ast.Lambda):
            stack.extend([*node.args.defaults, *[d for d in node.args.kw_defaults if d]])
            continue
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            found.append(node.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            found.extend((a.asname or a.name).split(".")[0] for a in node.names)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            found.extend(node.names)
        elif isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name:
            found.append(node.name)
        elif isinstance(node, ast.MatchMapping) and node.rest:
            found.append(node.rest)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            found.append(node.name)
        stack.extend(ast.iter_child_nodes(node))
    return found


def literal_value(node: ast.expr, sa_names: set[str]) -> bool:
    """A constant, a tuple/list of literal values, or ``<sa>.<CamelCase>(literal args)``."""
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, (ast.Tuple, ast.List)):
        return all(literal_value(elt, sa_names) for elt in node.elts)
    if isinstance(node, ast.Call):
        func = node.func
        return (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id in sa_names
            and CONSTRUCTOR.fullmatch(func.attr) is not None
            and all(literal_value(arg, sa_names) for arg in node.args)
            and all(k.arg is not None and literal_value(k.value, sa_names) for k in node.keywords)
        )
    return False


def module_shape(tree: ast.Module, where: str, stores: Counter[str]) -> None:
    """The ``[module]`` whitelist (see the module docstring); def bodies are not read."""
    sa_names = {
        alias.asname or alias.name
        for stmt in tree.body
        if isinstance(stmt, ast.Import)
        for alias in stmt.names
        if alias.name == "sqlalchemy"
    }
    sa_names = {name for name in sa_names if stores[name] == 1}
    body = tree.body
    has_doc = (
        bool(body)
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    )
    start = 1 if has_doc else 0
    first = body[start] if len(body) > start else None
    if not (
        isinstance(first, ast.ImportFrom)
        and first.module == "__future__"
        and first.level == 0
        and [(alias.name, alias.asname) for alias in first.names] == [("annotations", None)]
    ):
        raise ChainError(
            f"[module] {where}: `from __future__ import annotations` must open the module "
            "(after the docstring)"
        )
    for stmt in body[start + 1 :]:
        at = f"[module] {where} line {stmt.lineno}"
        if isinstance(stmt, (ast.Import, ast.ImportFrom)):
            if isinstance(stmt, ast.ImportFrom) and stmt.module == "__future__":
                raise ChainError(f"{at}: a second `from __future__` import is not allowed")
            if any(alias.name == "*" for alias in stmt.names):
                raise ChainError(f"{at}: a star import is not allowed")
            continue
        if isinstance(stmt, (ast.Assign, ast.AnnAssign)):
            targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
            if (
                len(targets) != 1
                or not isinstance(targets[0], ast.Name)
                or stmt.value is None
                or not literal_value(stmt.value, sa_names)
            ):
                raise ChainError(f"{at}: an assignment binds one plain name to a literal value")
            continue
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if stmt.decorator_list:
                raise ChainError(f"{at}: a decorated def is not allowed")
            if getattr(stmt, "type_params", None):
                raise ChainError(f"{at}: a def with type parameters is not allowed")
            defaults = [*stmt.args.defaults, *(d for d in stmt.args.kw_defaults if d is not None)]
            if not all(isinstance(default, ast.Constant) for default in defaults):
                raise ChainError(f"{at}: a def default must be a constant")
            continue
        raise ChainError(f"{at}: a module-level {type(stmt).__name__} statement is not allowed")


def header(name: str, data: bytes) -> tuple[str, str | None]:
    """(revision, down_revision) from the file's literal module-level header; never executed."""
    where = f"versions/{shown(name)}"
    check_encoding(data, where)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            tree = ast.parse(data.decode("utf-8"), filename=name)
    except Exception as exc:  # SyntaxError, UnicodeDecodeError, ValueError, Recursion/MemoryError
        why = type(exc).__name__
        raise ChainError(f"[parse] {where} cannot be parsed as data ({why})") from None
    direct: dict[str, ast.expr] = {}
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
            target, value = stmt.targets[0], stmt.value
        elif isinstance(stmt, ast.AnnAssign) and stmt.simple and stmt.value is not None:
            target, value = stmt.target, stmt.value
        else:
            continue
        if isinstance(target, ast.Name) and target.id in HEADER:
            direct[target.id] = value
    stores = Counter(module_scope_stores(tree))
    values: dict[str, object] = {}
    for field in HEADER:
        if field not in direct or stores[field] != 1:
            raise ChainError(
                f"[header] {where}: `{field}` is not exactly one literal module-level assignment"
            )
        if not isinstance(direct[field], ast.Constant):
            raise ChainError(f"[header] {where}: `{field}` is not a literal")
        values[field] = direct[field].value
    rev, down = values["revision"], values["down_revision"]
    if type(rev) is not str or not REVISION.fullmatch(rev):
        raise ChainError(f"[I2] {where}: `revision` must be a 4-digit string")
    if down is not None and (type(down) is not str or not REVISION.fullmatch(down)):
        raise ChainError(f"[header] {where}: `down_revision` must be a 4-digit string or None")
    for field in ("branch_labels", "depends_on"):
        if values[field] is not None:
            raise ChainError(f"[header] {where}: `{field}` must be None")
    module_shape(tree, where, stores)
    return rev, down


def chain(files: dict[str, tuple[str, bytes]]) -> tuple[str, str, dict[str, str | None]]:
    """(head, root, {revision: down_revision}) of a linear, numbered chain, else ChainError."""
    revs: dict[str, str | None] = {}
    for name in sorted(files):
        rev, down = header(name, files[name][1])
        if rev != name[:4]:
            raise ChainError(
                f"[I2] versions/{shown(name)} declares revision {rev}, "
                f"its file name says {name[:4]}"
            )
        if rev in revs:
            raise ChainError(f"[I2] revision {rev} is declared twice")
        revs[rev] = down
    for rev, down in sorted(revs.items()):
        if down is not None and down not in revs:
            raise ChainError(f"[I4] revision {rev} revises {down}, which does not exist")
    for rev in sorted(revs):
        cur: str | None = rev
        for _ in range(len(revs) + 1):
            if cur is None:
                break
            cur = revs[cur]
        else:
            raise ChainError(f"[I4] down_revision cycle through {rev}")
    roots = sorted(r for r, d in revs.items() if d is None)
    if len(roots) != 1:
        raise ChainError(f"[I4] {len(roots)} roots ({', '.join(roots)}); the chain has exactly one")
    children = Counter(d for d in revs.values() if d is not None)
    for parent, n in sorted(children.items()):
        if n > 1:
            kids = ", ".join(sorted(r for r, d in revs.items() if d == parent))
            raise ChainError(
                f"[I4] revision {parent} has {n} children ({kids}); the chain is linear"
            )
    # One root, no dangling parent, no cycle and no branch already make the chain a single path,
    # so exactly one head follows; checked anyway so a refactor of the above cannot pass two heads.
    heads = sorted(set(revs) - set(children))
    if len(heads) != 1:
        raise ChainError(f"[I4] {len(heads)} heads ({', '.join(heads)}); the chain has exactly one")
    for rev, down in sorted(revs.items()):
        if down is not None and int(rev) != int(down) + 1:
            raise ChainError(f"[I6] revision {rev} revises {down}; the numbering continues by one")
    return heads[0], roots[0], revs


def literal(text: str, tag: str) -> tuple[int, str]:
    """(line index, head) of the one canonical `_MIGRATION_HEAD` line; any other shape fails."""
    lines = text.split("\n")
    hits = [i for i, line in enumerate(lines) if ASSIGNS_LITERAL.match(line)]
    if len(hits) != 1 or not (m := LITERAL.fullmatch(lines[hits[0]])):
        raise ChainError(
            f"[{tag}] {SCRIPT} has {len(hits)} `_MIGRATION_HEAD` assignment(s); exactly one line "
            '`_MIGRATION_HEAD: Final[str] = "NNNN"` is required'
        )
    return hits[0], m.group(1)


def derive(trusted: str, base: str, lane: str, head_sha: str, overlay: str) -> str:
    try:
        main = versions(trusted, base)
        main_head, main_root, _ = chain(main)
        with open(overlay, "rb") as fh:
            main_raw = fh.read()
        check_encoding(main_raw, SCRIPT)
        main_script = main_raw.decode("utf-8")
        line, main_literal = literal(main_script, "I7")
    except ChainError as exc:
        raise ChainError(f"{exc} {TRUSTED_DEFECT}") from None
    except (OSError, UnicodeDecodeError):
        raise ChainError(f"[I7] cannot read the trusted overlay of {SCRIPT}") from None
    if main_literal != main_head:
        raise ChainError(
            f"[I7] the trusted base's _MIGRATION_HEAD is {main_literal}, its chain's head is "
            f"{main_head} (a trusted-base defect; fix main first)"
        )
    cand = versions(lane, head_sha)
    for name, (mode, data) in sorted(main.items()):
        if name not in cand:
            raise ChainError(f"[I3] main's versions/{name} is missing (append-only)")
        if cand[name] != (mode, data):
            raise ChainError(
                f"[I3] main's versions/{name} was changed "
                "(append-only: main's migrations stay byte-identical)"
            )
    head, root, revs = chain(cand)
    # I3 + I4 already put main's whole chain on the candidate's single path; checked explicitly.
    walk: list[str] = []
    cur: str | None = head
    while cur is not None:
        walk.append(cur)
        cur = revs[cur]
    if main_head not in walk or root != main_root:
        raise ChainError(
            f"[I5] main's head {main_head} is not an ancestor of the candidate head {head}"
        )
    try:
        cand_raw = blob(lane, f"{head_sha}:{SCRIPT}", MAX_SCRIPT_BYTES, SCRIPT)
    except ChainError:
        raise ChainError(f"[I8] cannot read the candidate's {SCRIPT}") from None
    check_encoding(cand_raw, SCRIPT)
    try:
        cand_script = cand_raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ChainError(f"[I8] cannot read the candidate's {SCRIPT}") from None
    _, cand_literal = literal(cand_script, "I8")
    if cand_literal != head:
        raise ChainError(
            f"[I8] the candidate's _MIGRATION_HEAD is {cand_literal}, its chain's head is {head}; "
            "bump the literal with the migration"
        )
    if head != main_literal:
        lines = main_script.split("\n")
        lines[line] = f'_MIGRATION_HEAD: Final[str] = "{head}"'
        want = "\n".join(lines).encode("utf-8")
        fd = os.open(overlay, os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW)
        with os.fdopen(fd, "wb") as fh:
            fh.write(want)
        with open(overlay, "rb") as fh:
            if fh.read() != want:
                raise ChainError("[overlay] the trusted overlay does not hold the derived head")
    return head


def main(argv: list[str]) -> int:
    if len(argv) != 5:
        print(
            "usage: migration_chain.py <trusted repo> <trusted commit> <lane repo> <head sha> "
            "<overlay>",
            file=sys.stderr,
        )
        return 2
    try:
        print(derive(*argv))
    except ChainError as exc:
        print(f"migration chain {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
