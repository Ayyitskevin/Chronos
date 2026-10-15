"""N3-3: the archive / pre-cutover backup pruner (Kevin's ruling 1, 2026-10-15: 90 days,
then delete).

Envelopes here are synthetic stand-ins written into tmp_path: a directory
``chronos-<stamp>/`` holding a random-bytes ``chronos.db.age`` and a ``manifest.json`` with the
fields the real writer records (the classifier reads only the manifest, never the ciphertext,
so no real key or backup is needed). ONE test (the last) builds a REAL envelope with the
repository's own ``backup()`` and a throwaway age key generated at run time, to prove the
classifier accepts what the real writer produces. The clock is pinned: ``main(argv, clock=…)``
is the only way to set "now" — there is no ``--now`` flag.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from chronos.operations import backup_pruner as pruner

NOW = datetime(2026, 12, 1, 12, 0, tzinfo=UTC)
PRE = "schema_version:17"
POST = "schema_version:18"
LEAK_PROBE = "sha-that-must-never-be-logged"


def _stamp(moment: datetime) -> str:
    return moment.strftime("%Y%m%dT%H%M%SZ")


def make_envelope(
    root: Path,
    age_days: float,
    *,
    head: str = PRE,
    extra: dict[str, bytes] | None = None,
    manifest: object | None = None,
) -> Path:
    taken = NOW - timedelta(days=age_days)
    envelope = root / f"chronos-{_stamp(taken)}"
    envelope.mkdir(parents=True)
    (envelope / "chronos.db.age").write_bytes(os.urandom(64))
    body = manifest
    if body is None:
        body = {
            "taken_at": taken.isoformat(),
            "snapshot_completed_at": taken.isoformat(),
            "source_path": "data/chronos.db",
            "backup_path": str(envelope / "chronos.db.age"),
            "sha256": LEAK_PROBE,
            "schema_head": head,
            "row_counts": {"application_events": 3},
            "audit_head": None,
            "audit_log_path": None,
            "encryption": {"scheme": "age-x25519-v1"},
            "ciphertext_sha256": "0" * 64,
        }
    text = body if isinstance(body, str) else json.dumps(body)
    (envelope / "manifest.json").write_text(text, encoding="utf-8")
    for name, payload in (extra or {}).items():
        (envelope / name).write_bytes(payload)
    return envelope


def run_cli(root: Path, *flags: str) -> tuple[int, dict[str, object], str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = pruner.main(["--root", str(root), *flags], clock=lambda: NOW)
    text = out.getvalue()
    return code, json.loads(text) if text.strip() else {}, text + err.getvalue()


@pytest.fixture
def root(tmp_path: Path) -> Path:
    directory = tmp_path / "backups"
    directory.mkdir()
    return directory


def names(directory: Path) -> set[str]:
    return {entry.name for entry in directory.iterdir()}


# ------------------------------------------------------------------ the contract


def test_a_dry_run_is_the_default_and_deletes_nothing(root: Path) -> None:
    old = make_envelope(root, 400)
    code, report, _ = run_cli(root)
    assert code == pruner.EXIT_WOULD_PRUNE
    assert old.is_dir()
    assert report["applied"] is False
    assert [c["name"] for c in report["candidates"]] == [old.name]  # type: ignore[index, union-attr]


def test_apply_deletes_only_old_pre_cutover_envelopes(root: Path) -> None:
    old = make_envelope(root, 400)
    older = make_envelope(root, 800)
    young = make_envelope(root, 30)
    post = make_envelope(root, 401, head=POST)
    code, report, _ = run_cli(root, "--apply")
    assert code == pruner.EXIT_PRUNED
    assert names(root) == {young.name, post.name}
    assert sorted(report["deleted"]) == sorted([old.name, older.name])  # type: ignore[arg-type]
    assert not old.exists() and not older.exists()


def test_89_days_is_kept_90_is_kept_91_is_pruned_at_the_pinned_now(root: Path) -> None:
    d89 = make_envelope(root, 89)
    d90 = make_envelope(root, 90)
    d91 = make_envelope(root, 91)
    code, _report, _ = run_cli(root, "--apply")
    assert code == pruner.EXIT_PRUNED
    assert names(root) == {d89.name, d90.name}
    assert not d91.exists()


def test_a_post_cutover_backup_is_never_pruned_however_old(root: Path) -> None:
    post = make_envelope(root, 3000, head=POST)
    code, report, _ = run_cli(root, "--apply")
    assert code == pruner.EXIT_NOTHING_TO_DO
    assert post.is_dir()
    assert report["deleted"] == []


def test_what_cannot_be_classified_is_kept_and_reported(root: Path) -> None:
    kept = {
        make_envelope(root, 500, head="alembic:0016").name: "alembic form",
        make_envelope(root, 501, head="unversioned").name: "unversioned",
        make_envelope(root, 502, manifest="{not json").name: "bad json",
        make_envelope(root, 503, extra={"cleartext.db": b"x"}).name: "stray file",
        make_envelope(root, 504, manifest={"schema_head": PRE}).name: "partial manifest",
    }
    loose = root / "chronos-restore-copy"
    loose.mkdir()
    (loose / "chronos.db").write_bytes(b"cleartext")
    (root / ".chronos-20260101T000000Z.0123456789abcdef.tmp").mkdir()
    (root / "notes.txt").write_text("hi", encoding="utf-8")
    before = names(root)
    code, report, _ = run_cli(root, "--apply")
    assert code == pruner.EXIT_NOTHING_TO_DO
    assert names(root) == before
    reported = {item["name"] for item in report["unclassified"]}  # type: ignore[union-attr, index]
    assert set(kept) <= reported
    assert {loose.name, "notes.txt"} <= reported


def test_a_symlink_in_the_root_refuses_the_whole_run_and_deletes_nothing(
    root: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    victim = make_envelope(outside, 900)
    old = make_envelope(root, 400)
    (root / "chronos-20200101T000000Z").symlink_to(victim, target_is_directory=True)
    code, report, _ = run_cli(root, "--apply")
    assert code == pruner.EXIT_REFUSED
    assert victim.is_dir() and old.is_dir()
    assert any("symlink" in str(r) for r in report["refused"])  # type: ignore[union-attr]


def test_a_symlinked_root_is_refused(root: Path, tmp_path: Path) -> None:
    make_envelope(root, 400)
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    code, _report, _ = run_cli(alias, "--apply")
    assert code == pruner.EXIT_REFUSED
    assert len(names(root)) == 1


def test_nothing_to_do_exits_zero_and_a_missing_root_is_refused(root: Path, tmp_path: Path) -> None:
    make_envelope(root, 5)
    assert run_cli(root, "--apply")[0] == pruner.EXIT_NOTHING_TO_DO
    assert run_cli(tmp_path / "absent", "--apply")[0] == pruner.EXIT_REFUSED


def test_the_retention_is_a_named_90_with_no_flag_to_change_it_or_the_clock() -> None:
    assert pruner.RETENTION_DAYS == 90
    flags = {
        option for action in pruner.build_parser()._actions for option in action.option_strings
    }
    assert flags == {"-h", "--help", "--root", "--apply", "--pretty"}


def test_output_names_paths_and_counts_never_manifest_contents(root: Path) -> None:
    make_envelope(root, 400)
    make_envelope(root, 3, head=POST)
    _code, report, text = run_cli(root, "--apply", "--pretty")
    assert LEAK_PROBE not in text
    assert report["counts"] == {"deleted": 1, "kept": 1, "unclassified": 0, "refused": 0}


def test_a_real_envelope_from_the_repositorys_writer_is_classified_and_pruned(
    root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    age, keygen = shutil.which("age"), shutil.which("age-keygen")
    if age is None or keygen is None:
        pytest.skip("age is not installed")
    from chronos.operations import restore_drill as drill
    from chronos.operations.restore_drill import Clock, backup
    from chronos.persistence import database as database_module
    from chronos.persistence.database import Database

    monkeypatch.setattr(database_module, "_REPAIR_WINDOW_OPEN", True)
    keys = tmp_path / "keys"
    keys.mkdir()
    identity = keys / "backup-host.age"
    subprocess.run([keygen, "-o", str(identity)], check=True, capture_output=True)
    identity.chmod(0o600)
    host_public = (
        subprocess.run([keygen, "-y", str(identity)], check=True, capture_output=True)
        .stdout.decode()
        .strip()
    )
    recipients = keys / "backup-recipients.txt"
    recipients.write_text(f"{host_public}\n{drill.KEVIN_RECOVERY_RECIPIENT}\n", encoding="utf-8")
    monkeypatch.setenv(drill.IDENTITY_ENV, str(identity))
    monkeypatch.setenv(drill.RECIPIENTS_ENV, str(recipients))

    data = tmp_path / "data"
    data.mkdir()
    db = data / "chronos.db"
    database = Database(f"sqlite:///{db}")
    database.initialize()
    database.dispose()
    taken = NOW - timedelta(days=120)
    ticks = iter([1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
    backup(db, root, clock=Clock(wall=lambda: taken, monotonic=lambda: next(ticks)))
    code, report, _ = run_cli(root, "--apply")
    assert code == pruner.EXIT_PRUNED
    assert report["counts"]["deleted"] == 1  # type: ignore[index]
    assert names(root) == set()


# ------------------------------------------------------- r1: Daybreak's HOLD (7420fa8)
#
# Each test below failed at 7420fa8 (commit 37e962b recorded that as strict xfails; the fix
# commit removes the markers). The swap tests wrap ``os`` calls to move the classified directory
# aside and put a lookalike at its name at a chosen instant, deterministically.

SENTINEL = "SENTINEL-manifest-value-7f3a91"


def _replace_with_lookalike(envelope: Path, held: Path, *, files: bool) -> Path:
    """Move the classified envelope aside and put a different directory at its name."""
    envelope.rename(held)
    envelope.mkdir()
    if files:
        (envelope / "chronos.db.age").write_bytes(b"replacement ciphertext")
        (envelope / "manifest.json").write_text("{}", encoding="utf-8")
    return envelope


def test_a_directory_swapped_in_before_the_delete_opens_it_is_not_deleted(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = make_envelope(root, 400)
    held = root / "held-original"
    real_open = os.open
    seen = {"ran": False}

    def swapping_open(path, flags, mode=0o777, *, dir_fd=None):  # type: ignore[no-untyped-def]
        # fire on the open the DELETE issues (not the scan's), whatever order the module opens in
        in_delete = sys._getframe(1).f_code.co_name == "_delete_envelope"
        if path == old.name and in_delete and not seen["ran"]:
            _replace_with_lookalike(old, held, files=True)
            seen["ran"] = True
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", swapping_open)
    code, report, _ = run_cli(root, "--apply")
    monkeypatch.undo()
    assert seen["ran"], "the swap hook never fired: the test lost its positive control"
    assert (old / "chronos.db.age").exists(), "the substituted directory's files were deleted"
    assert (old / "chronos.db.age").read_bytes() == b"replacement ciphertext"
    assert (old / "manifest.json").exists()
    assert report["deleted"] == []
    assert code == pruner.EXIT_REFUSED
    assert (held / "manifest.json").exists()  # the classified inode is untouched


def test_a_directory_swapped_in_before_the_final_rmdir_is_not_removed(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = make_envelope(root, 400)
    held = root / "held-original"
    real_rmdir = os.rmdir
    seen = {"ran": False}

    def swapping_rmdir(path, *, dir_fd=None):  # type: ignore[no-untyped-def]
        if not seen["ran"]:
            seen["ran"] = True
            if old.exists():
                old.rename(held)  # the emptied, classified directory moves aside ...
            old.mkdir()  # ... and an empty lookalike takes its name
        return real_rmdir(path, dir_fd=dir_fd)

    monkeypatch.setattr(os, "rmdir", swapping_rmdir)
    run_cli(root, "--apply")
    monkeypatch.undo()
    assert seen["ran"], "the swap hook never fired: the test lost its positive control"
    assert old.is_dir(), "the substitute directory was removed in place of the classified one"


def test_a_scheme_that_is_not_exactly_the_restore_drills_is_never_pruned(root: Path) -> None:
    from chronos.operations.restore_drill import AGE_SCHEME

    typo = make_envelope(root, 91)
    manifest = json.loads((typo / "manifest.json").read_text(encoding="utf-8"))
    manifest["encryption"] = {"scheme": "age-typo-not-an-envelope"}
    (typo / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    good = make_envelope(root, 92)
    assert (
        json.loads((good / "manifest.json").read_text(encoding="utf-8"))["encryption"]["scheme"]
        == AGE_SCHEME
    )  # the fixture's good scheme IS the canonical one
    code, report, _ = run_cli(root, "--apply")
    assert code == pruner.EXIT_PRUNED
    assert typo.is_dir() and not good.exists()
    assert [item["name"] for item in report["unclassified"]] == [typo.name]  # type: ignore[index, union-attr]


def test_no_manifest_or_directory_value_reaches_any_output_field(root: Path) -> None:
    bad_head = make_envelope(root, 400, head=SENTINEL)
    bad_scheme = make_envelope(root, 401)
    manifest = json.loads((bad_scheme / "manifest.json").read_text(encoding="utf-8"))
    manifest["encryption"] = {"scheme": SENTINEL}
    (bad_scheme / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    bad_time = make_envelope(root, 402)
    manifest = json.loads((bad_time / "manifest.json").read_text(encoding="utf-8"))
    manifest["snapshot_completed_at"] = SENTINEL
    (bad_time / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    stray = make_envelope(root, 403, extra={f"{SENTINEL}.db": b"x"})
    make_envelope(root, 404)  # a real candidate, so --apply runs the delete path too
    for flags in ((), ("--apply",), ("--apply", "--pretty")):
        _code, report, text = run_cli(root, *flags)
        assert SENTINEL not in text, flags
        assert SENTINEL not in json.dumps(report), flags
    assert {bad_head, bad_scheme, bad_time, stray} <= {p for p in root.iterdir()}


def test_a_symlink_nested_in_an_envelope_refuses_the_whole_run(root: Path, tmp_path: Path) -> None:
    peer = make_envelope(root, 400)
    linked = make_envelope(root, 401)
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text((linked / "manifest.json").read_text(encoding="utf-8"), encoding="utf-8")
    (linked / "manifest.json").unlink()
    (linked / "manifest.json").symlink_to(elsewhere)
    code, report, _ = run_cli(root, "--apply")
    assert code == pruner.EXIT_REFUSED
    assert peer.is_dir() and linked.is_dir() and elsewhere.exists()
    assert report["deleted"] == []
    assert any("symlink" in str(r) for r in report["refused"])  # type: ignore[union-attr]


def test_a_symlink_deep_inside_any_directory_refuses_the_whole_run(
    root: Path, tmp_path: Path
) -> None:
    peer = make_envelope(root, 400)
    copy = root / "restored-copy" / "nested" / "deeper"
    copy.mkdir(parents=True)
    (copy / "link").symlink_to(tmp_path)
    code, report, _ = run_cli(root, "--apply")
    assert code == pruner.EXIT_REFUSED
    assert peer.is_dir()
    assert report["deleted"] == []


def test_a_directory_swapped_in_between_the_check_and_the_quarantine_is_put_back(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """New with the quarantine design: the swap lands between the descriptor's identity check
    and the rename. The rename then moves the SUBSTITUTE; the identity check on the quarantined
    entry catches it, the substitute goes back to its name untouched, the run refuses."""
    old = make_envelope(root, 400)
    held = root / "held-original"
    real_rename = os.rename
    seen = {"ran": False}

    def swapping_rename(src, dst, *, src_dir_fd=None, dst_dir_fd=None):  # type: ignore[no-untyped-def]
        if src == old.name and not seen["ran"]:
            _replace_with_lookalike(old, held, files=True)
            seen["ran"] = True
        return real_rename(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

    monkeypatch.setattr(os, "rename", swapping_rename)
    code, report, _ = run_cli(root, "--apply")
    monkeypatch.undo()
    assert seen["ran"], "the swap hook never fired: the test lost its positive control"
    assert code == pruner.EXIT_REFUSED
    assert report["deleted"] == []
    assert (old / "chronos.db.age").read_bytes() == b"replacement ciphertext"  # back, untouched
    assert (old / "manifest.json").exists()
    assert (held / "manifest.json").exists() and (held / "chronos.db.age").exists()
    assert not [n for n in names(root) if n.startswith(pruner._QUARANTINE_PREFIX)]


def test_a_successful_delete_leaves_no_quarantine_behind(root: Path) -> None:
    make_envelope(root, 400)
    make_envelope(root, 5)
    code, _report, _ = run_cli(root, "--apply")
    assert code == pruner.EXIT_PRUNED
    assert not [n for n in names(root) if n.startswith(pruner._QUARANTINE_PREFIX)]


def test_a_tree_too_deep_to_check_for_symlinks_refuses_the_run(root: Path) -> None:
    peer = make_envelope(root, 400)
    deep = root / "restored-copy"
    for _ in range(pruner._MAX_WALK_DEPTH + 3):
        deep = deep / "d"
    deep.mkdir(parents=True)
    code, report, _ = run_cli(root, "--apply")
    assert code == pruner.EXIT_REFUSED
    assert peer.is_dir()
    assert report["deleted"] == []
