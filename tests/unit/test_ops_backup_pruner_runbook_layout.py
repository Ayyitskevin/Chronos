"""N3-3 r6 (Daybreak P2-1): the pruner invocations the runbooks document must reach the
envelopes the runbooks' backup commands publish.

The layout is not hand-built: every ``--out``, ``--restore-into`` and ``--root`` value below is
READ FROM THE RUNBOOK TEXT (``RESTORE-DRILL.md`` and ``N3-CUTOVER.md``), so the commands an
owner copies and this test cannot drift apart. The pruner scans only DIRECT children of its
root (reviewed behaviour, unchanged); a runbook that publishes one directory deeper than it
prunes keeps every envelope forever, which is what 21f021c documented.
"""

from __future__ import annotations

import glob
import hashlib
import os
import re
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


def _pruner_roots() -> list[str]:
    """Every ``--root`` the RESTORE-DRILL pruner section documents (quotes stripped)."""
    section = RESTORE_DRILL.split("## Pruning old backups", 1)[1]
    return [
        value.strip('"') for value in re.findall(rf"{re.escape(PRUNER_CMD)} --root (\S+)", section)
    ]


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


def test_the_documented_pruner_runs_reach_every_documented_backup_out_directory(host: Path) -> None:
    ordinary_out = _flag(RESTORE_DRILL, "operations.restore_drill", "--out")
    cutover_out = _flag(CUTOVER, "operations.cutover_archive", "--out")
    restore_targets = [
        _flag(RESTORE_DRILL, "operations.restore_drill", "--restore-into"),
        _flag(CUTOVER, "operations.cutover_archive", "--restore-into"),
    ]
    out_dirs = {"ordinary": _on_host(host, ordinary_out), "cutover": _on_host(host, cutover_out)}

    old: list[Path] = []
    young: list[Path] = []
    for age_old, age_young, out in (
        (120, 30, out_dirs["ordinary"]),
        (200, 45, out_dirs["cutover"]),
    ):
        old.append(make_envelope(out, age_old))
        young.append(make_envelope(out, age_young))

    # A real host may also hold the older dated layout (the 21f021c runbook's --out).
    legacy = host / NOW.strftime("%Y%m%d")
    legacy_old = make_envelope(legacy, 150)
    legacy_young = make_envelope(legacy, 20)
    if legacy not in out_dirs.values():
        old.append(legacy_old)
        young.append(legacy_young)

    restores = [_on_host(host, target) for target in restore_targets]
    for target in restores:
        target.mkdir(parents=True, mode=0o700, exist_ok=True)
        (target / "chronos.db").write_bytes(b"CLEARTEXT-STORE-" + target.name.encode())
        os.chmod(target / "chronos.db", 0o600)
    restore_before = [_digest(target) for target in restores]

    roots = _pruner_roots()
    assert roots, "the pruner section documents no --root"
    invoked: list[Path] = []
    for documented in roots:
        if any(ch in documented for ch in "$*["):
            # The documented per-directory loop: expand its glob exactly as the shell would.
            glob_text = re.search(
                rf"for \w+ in ({re.escape(BACKUP_ROOT)}/\[0-9\]\*/); do", RESTORE_DRILL
            )
            assert glob_text, "a per-directory --root is documented without its loop"
            pattern = str(_on_host(host, glob_text.group(1).rstrip("/"))) + "/"
            invoked += [Path(p.rstrip("/")) for p in sorted(glob.glob(pattern))]
        else:
            invoked.append(_on_host(host, documented))
    assert invoked, "the documented --root values reach no directory on the test host"

    for directory in invoked:
        code, report, _ = run_cli(directory, "--apply")
        assert code in (pruner.EXIT_PRUNED, 0), (directory, report)

    survivors = {path for path in host.rglob("chronos-*") if path.is_dir()}
    assert survivors == set(young), "an envelope older than 90 days survived, or a young one went"
    assert not any(path.exists() for path in old)
    assert [_digest(target) for target in restores] == restore_before


def test_restore_targets_stay_unclassified_at_the_default_root(host: Path) -> None:
    for documented in (
        _flag(RESTORE_DRILL, "operations.restore_drill", "--restore-into"),
        _flag(CUTOVER, "operations.cutover_archive", "--restore-into"),
    ):
        target = _on_host(host, documented)
        target.mkdir(parents=True, mode=0o700, exist_ok=True)
        (target / "chronos.db").write_bytes(b"x")
    code, report, _ = run_cli(host, "--apply")
    assert code == 0
    reasons = {item["name"]: item["why"] for item in report["unclassified"]}  # type: ignore[union-attr, index]
    assert reasons and set(reasons.values()) == {"not a published envelope name"}
