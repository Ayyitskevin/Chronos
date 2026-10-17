"""N3-3 r6/r7 (Daybreak): the pruner invocations the runbooks document must reach the
envelopes the runbooks' backup commands publish.

The layout is not hand-built: every ``--out``, ``--restore-into`` and ``--root`` value below is
READ FROM THE RUNBOOK TEXT (``RESTORE-DRILL.md`` and ``N3-CUTOVER.md``), so the commands an
owner copies and this test cannot drift apart. The pruner scans only DIRECT children of its
root (reviewed behaviour, unchanged); a runbook that publishes one directory deeper than it
prunes keeps every envelope forever, which is what 21f021c documented.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from tests.unit.test_ops_backup_pruner import NOW, _stamp, make_envelope, run_cli

from chronos.operations import backup_pruner as pruner

ROOT = Path(__file__).resolve().parents[2]
RESTORE_DRILL = (ROOT / "docs" / "ops" / "RESTORE-DRILL.md").read_text(encoding="utf-8")
CUTOVER = (ROOT / "docs" / "ops" / "N3-CUTOVER.md").read_text(encoding="utf-8")
BACKUP_ROOT = "/var/backups/chronos"
PRUNER_CMD = "python -m chronos.operations.backup_pruner"


def _expand(text: str) -> str:
    """The two ``date`` substitutions the runbooks use, pinned to the test clock."""
    text = text.replace("$(date -u +%Y%m%dT%H%M%SZ)", _stamp(NOW))
    return text.replace("$(date -u +%Y%m%d)", NOW.strftime("%Y%m%d"))


def _flag(document: str, command: str, flag: str) -> str:
    """The value of ``flag`` in the fenced block of ``document`` that runs ``command``."""
    for block in re.findall(r"```bash\n(.*?)```", document, flags=re.S):
        if command in block:
            found = re.search(
                rf"{re.escape(flag)} ((?:[^\s$]|\$\([^)]*\))+)", block.replace("\\\n", " ")
            )
            if found:
                return _expand(found.group(1))
    raise AssertionError(f"no fenced command running {command!r} with {flag}")


def _pruner_section() -> str:
    return RESTORE_DRILL.split("## Pruning old backups", 1)[1]


def _default_root() -> str:
    """The ``--root`` of the one-line default pruner command."""
    found = re.search(rf"{re.escape(PRUNER_CMD)} --root (\S+)", _pruner_section())
    assert found, "the pruner section documents no default --root"
    return found.group(1)


def _legacy_routine() -> str:
    """The fenced shell block that prunes pre-r6 layouts, exactly as the owner would run it."""
    for block in re.findall(r"```bash\n(.*?)```", _pruner_section(), flags=re.S):
        if "for d in" in block:
            return textwrap.dedent(block)
    raise AssertionError("the pruner section documents no per-directory routine")


def _digest(directory: Path) -> list[tuple[str, str]]:
    return sorted(
        (
            str(path.relative_to(directory)),
            hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "dir",
        )
        for path in directory.rglob("*")
    )


@pytest.fixture
def host(tmp_path: Path) -> Path:
    """A stand-in for ``/var/backups/chronos`` (every documented path is mapped onto it)."""
    base = tmp_path / "var" / "backups" / "chronos"
    base.mkdir(parents=True, mode=0o700)
    return base


def _on_host(host: Path, documented: str) -> Path:
    assert documented == BACKUP_ROOT or documented.startswith(BACKUP_ROOT + "/"), documented
    return host / documented[len(BACKUP_ROOT) :].lstrip("/")


SHIM = """\
import sys
from datetime import UTC, datetime
from pathlib import Path
from chronos.operations import backup_pruner
args = sys.argv[1:]
with open(sys.argv[0] + ".log", "a", encoding="utf-8") as log:
    log.write(args[args.index("--root") + 1] + "\\n")
sys.exit(backup_pruner.main(args, clock=lambda: datetime({y}, {m}, {d}, 12, tzinfo=UTC)))
"""


def _run_routine(
    host: Path, tmp_path: Path, *, apply: bool = False, flags: str | None = None
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    """Run the documented shell routine under bash, hermetically.

    Only two substitutions: the backup root maps onto the test host, and the pruner module
    invocation maps onto a shim that pins the clock (the module itself is the real one).
    """
    shim = tmp_path / "prune_shim.py"
    shim.write_text(SHIM.format(y=NOW.year, m=NOW.month, d=NOW.day), encoding="utf-8")
    script = (
        _legacy_routine()
        .replace(BACKUP_ROOT, str(host))
        .replace(PRUNER_CMD, f"{sys.executable} {shim}")
    )
    assert str(host) in script and str(shim) in script, (
        "the routine no longer names the backup root / pruner"
    )
    env = {
        "PATH": os.environ["PATH"],
        "PYTHONPATH": str(ROOT / "src"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PRUNE_FLAGS": flags if flags is not None else ("--apply" if apply else ""),
    }
    Path(str(shim) + ".log").unlink(missing_ok=True)
    done = subprocess.run(
        ["bash", "-c", script], env=env, capture_output=True, text=True, timeout=60, check=False
    )
    log = Path(str(shim) + ".log")
    return done, (log.read_text(encoding="utf-8").split() if log.exists() else [])


def _restore_dir(path: Path) -> Path:
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    (path / "chronos.db").write_bytes(b"CLEARTEXT-STORE-" + path.name.encode())
    os.chmod(path / "chronos.db", 0o600)
    return path


def test_the_documented_runs_reach_every_documented_backup_out_directory(
    host: Path, tmp_path: Path
) -> None:
    ordinary = _on_host(host, _flag(RESTORE_DRILL, "operations.restore_drill", "--out"))
    cutover = _on_host(host, _flag(CUTOVER, "operations.cutover_archive", "--out"))
    restores = [
        _restore_dir(
            _on_host(host, _flag(RESTORE_DRILL, "operations.restore_drill", "--restore-into"))
        ),
        _restore_dir(
            _on_host(host, _flag(CUTOVER, "operations.cutover_archive", "--restore-into"))
        ),
    ]
    old: list[Path] = []
    young: list[Path] = []
    for out, age_old, age_young in ((ordinary, 120, 30), (cutover, 200, 45)):
        old.append(make_envelope(out, age_old))
        young.append(make_envelope(out, age_young))

    # Layouts older page versions produced: a dated --out and the earlier cutover /n3.
    for legacy in (host / NOW.strftime("%Y%m%d"), host / "n3"):
        if legacy not in (ordinary, cutover):
            old.append(make_envelope(legacy, 150))
            young.append(make_envelope(legacy, 20))

    # Things the routine must never enter: a digit-prefixed restore copy, an unrelated
    # digit-prefixed directory holding a VALID old envelope, and a symlink root.
    keep_digest = [_digest(_restore_dir(host / (NOW.strftime("%Y%m%d") + "-restore")))]
    unrelated = make_envelope(host / "7-unrelated", 300)
    elsewhere = tmp_path / "elsewhere"
    linked = make_envelope(elsewhere, 300)
    keep_digest += [_digest(r) for r in restores]
    untouched = [unrelated, linked]

    # The default one-line run first: a symlink anywhere under its root would refuse it.
    assert _default_root() == BACKUP_ROOT
    code, report, _ = run_cli(host, "--apply")
    assert code in (pruner.EXIT_PRUNED, 0), report
    (host / "8-link").symlink_to(elsewhere)
    (host / "20261016").symlink_to(elsewhere)  # an exact-date NAME that is a symlink
    done, invoked = _run_routine(host, tmp_path, apply=True)
    assert "Traceback" not in done.stderr, done.stderr

    assert not any(path.exists() for path in old), "an envelope older than 90 days survived"
    assert all(path.exists() for path in young + untouched), "a young or out-of-scope envelope went"
    for link in ("8-link", "20261016"):
        assert str(host / link) not in invoked, f"the routine invoked the pruner on symlink {link}"
    assert str(host / "7-unrelated") not in invoked
    assert all(Path(root).name in {NOW.strftime("%Y%m%d"), "n3"} for root in invoked), invoked
    assert [_digest(host / (NOW.strftime("%Y%m%d") + "-restore"))] + [
        _digest(r) for r in restores
    ] == keep_digest
    assert (host / "8-link").is_symlink() and (host / "20261016").is_symlink()


def test_the_routine_is_dry_run_first_and_a_host_with_no_legacy_directory_is_a_no_op(
    host: Path, tmp_path: Path
) -> None:
    done, invoked = _run_routine(host, tmp_path, apply=False)
    assert "Traceback" not in done.stderr, done.stderr
    assert invoked == [], "the routine invoked the pruner on a literal, unmatched glob"
    legacy = make_envelope(host / NOW.strftime("%Y%m%d"), 400)
    done, invoked = _run_routine(host, tmp_path, apply=False)
    assert invoked == [str(legacy.parent)]
    assert legacy.is_dir(), "the dry run deleted"


def test_restore_targets_stay_unclassified_at_the_default_root(host: Path) -> None:
    for documented in (
        _flag(RESTORE_DRILL, "operations.restore_drill", "--restore-into"),
        _flag(CUTOVER, "operations.cutover_archive", "--restore-into"),
    ):
        _restore_dir(_on_host(host, documented))
    code, report, _ = run_cli(host, "--apply")
    assert code == 0
    reasons = {item["name"]: item["why"] for item in report["unclassified"]}  # type: ignore[union-attr, index]
    assert reasons and set(reasons.values()) == {"not a published envelope name"}


OUTSIDE_MARK = "@OUTSIDE@"


@pytest.mark.parametrize(
    "hostile",
    [
        f"--apply --root {OUTSIDE_MARK}",
        "--apply --apply",
        "-a",
        "--apply;",
        "*",
        "--apply ",
        " --apply",
        "--pretty",
    ],
)
def test_a_hostile_prune_flags_value_is_refused_before_any_pruner_runs(
    host: Path, tmp_path: Path, hostile: str
) -> None:
    inside = make_envelope(host / NOW.strftime("%Y%m%d"), 150)
    outside = make_envelope(tmp_path / "injected-outside", 150)
    before = [_digest(host), _digest(outside.parent)]
    done, invoked = _run_routine(
        host, tmp_path, flags=hostile.replace(OUTSIDE_MARK, str(outside.parent))
    )
    assert done.returncode == 2, (done.returncode, done.stderr)
    assert invoked == [], "the pruner was invoked despite a non-canonical PRUNE_FLAGS"
    assert [_digest(host), _digest(outside.parent)] == before
    assert inside.is_dir() and outside.is_dir()


def test_prune_flags_empty_is_a_dry_run_and_exactly_apply_applies(
    host: Path, tmp_path: Path
) -> None:
    envelope = make_envelope(host / NOW.strftime("%Y%m%d"), 150)
    _, invoked = _run_routine(host, tmp_path, flags="")
    assert invoked == [str(envelope.parent)] and envelope.is_dir()
    _, invoked = _run_routine(host, tmp_path, flags="--apply")
    assert invoked == [str(envelope.parent)] and not envelope.exists()


def test_the_pruner_invocation_takes_its_flags_only_from_the_quoted_validated_array() -> None:
    """A structural pin. With the validation present, ``${PRUNE_FLAGS:-}`` or an unquoted array
    behaves the same on every value the validation lets through, so no behavioural test can tell
    them apart; but either one is one edit from reopening the argv injection, so the line is pinned.
    """
    routine = _legacy_routine()
    invocation = [line for line in routine.splitlines() if PRUNER_CMD in line]
    assert len(invocation) == 1, invocation
    assert invocation[0].rstrip().endswith('--root "$d" --pretty "${prune_flags[@]}"')
    assert "PRUNE_FLAGS" not in invocation[0]
    assert routine.index("prune_flags=") < routine.index("for d in")
