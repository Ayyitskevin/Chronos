"""``verify_chain_text`` / ``verify_pair_text`` / ``read_audit_pair``: content-level verdicts.

Added for the monitoring snapshot (F4 HOLD): a consumer that verifies a path and then
re-reads the path to derive from it has two reads, and a replacement between them lets a
VALID verdict authorise BROKEN rows. Verifying captured content closes that.

After the head anchor (P1), the verdict a path deserves is the PAIR's (D2 §2): the log
beside its anchor. ``verify_pair_text`` is that matrix over content a caller already holds
and is the one place it lives — ``verify_chain`` delegates to it after its own capability
reads, so the two cannot disagree. ``verify_chain_text`` remains the chain-only verdict for
callers that hold nothing but log text; this file pins that it does NOT see the anchor.
``read_audit_pair`` is the capability read both ``verify_chain`` and the monitoring snapshot
use: one descriptor-relative, no-follow, exact-0600 read of each file, creating nothing.

Kept separate from ``test_auditlog.py`` on purpose: that file is heavily rewritten on the
F2/P1 branches and this one must merge cleanly either way.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import chronos.auditlog as auditlog_pkg
from chronos.auditlog import (
    AuditLog,
    AuditLogCorruptionError,
    ChainState,
    read_audit_pair,
    verify_chain,
    verify_chain_text,
    verify_pair_text,
)

_GENESIS = "0" * 64


def _chain(path: Path, count: int) -> None:
    log = AuditLog(path)
    for index in range(count):
        log.append("test_event", {"index": index, "value": f"payload-{index}"})


def _anchor_of(path: Path) -> Path:
    return path.with_name(path.stem + ".head.json")


def _anchor_bytes(count: int, last_hash: str) -> bytes:
    return (json.dumps({"count": count, "last_hash": last_hash}, sort_keys=True) + "\n").encode(
        "utf-8"
    )


def test_they_are_exported_from_the_package() -> None:
    for name, function in (
        ("verify_chain_text", verify_chain_text),
        ("verify_pair_text", verify_pair_text),
        ("read_audit_pair", read_audit_pair),
    ):
        assert name in auditlog_pkg.__all__, name
        assert getattr(auditlog_pkg, name) is function


def test_a_valid_pair_reads_the_same_by_content_and_by_path(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    _chain(path, 3)
    text = path.read_text(encoding="utf-8")
    anchor = _anchor_of(path).read_bytes()
    by_path = verify_chain(path)
    by_pair = verify_pair_text(text, anchor)
    assert by_pair.state is ChainState.VALID
    assert (by_pair.state, by_pair.detail) == (by_path.state, by_path.detail)
    assert by_pair.detail == "chain + anchor intact (3 records)"
    # The chain-only verdict is VALID too, and says so in its own words: it judged no anchor.
    by_text = verify_chain_text(text)
    assert by_text.state is ChainState.VALID
    assert by_text.detail == "chain intact (3 records)"


def test_a_broken_chain_reads_the_same_by_content_and_by_path(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    _chain(path, 3)
    lines = path.read_text(encoding="utf-8").splitlines()
    lines[1] = lines[1].replace("payload-1", "payload-X")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    text = path.read_text(encoding="utf-8")
    anchor = _anchor_of(path).read_bytes()
    # The chain is judged before the anchor, so its precise first-line failure wins everywhere.
    by_path = verify_chain(path)
    by_pair = verify_pair_text(text, anchor)
    by_text = verify_chain_text(text)
    assert by_pair.state is by_text.state is by_path.state is ChainState.BROKEN
    assert by_pair.detail == by_text.detail == by_path.detail == "line 2: hash mismatch"


def test_an_unreadable_row_and_a_gap_keep_their_line_numbers(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    _chain(path, 3)
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    del lines[1]
    assert verify_chain_text("\n".join(lines) + "\n").detail == "line 2: sequence gap"
    assert verify_chain_text(text + '{"broken').detail.startswith("line 4: unreadable record")


def test_verify_chain_text_does_not_see_the_anchor(tmp_path: Path) -> None:
    """The documented limitation, pinned: a tail-truncated log with its stale anchor is VALID
    to the chain-only verdict and BROKEN to the pair. A consumer that must see truncation
    (monitoring, the CLI, recovery) judges the pair; ``verify_chain_text`` is for callers
    that hold nothing but text and say so."""

    path = tmp_path / "audit.jsonl"
    _chain(path, 2)
    anchor = _anchor_of(path).read_bytes()
    truncated = path.read_text(encoding="utf-8").splitlines()[0] + "\n"
    assert verify_chain_text(truncated).state is ChainState.VALID
    assert verify_chain_text(truncated).detail == "chain intact (1 records)"
    by_pair = verify_pair_text(truncated, anchor)
    assert by_pair.state is ChainState.BROKEN
    assert "truncation/rollback" in by_pair.detail, by_pair.detail
    path.write_text(truncated, encoding="utf-8")
    assert verify_chain(path).detail == by_pair.detail


def test_the_pair_matrix_edges_match_verify_chain(tmp_path: Path) -> None:
    # Both absent is the only ABSENT, and only a path-level reader can say it about a path.
    missing = tmp_path / "missing.jsonl"
    absent = verify_pair_text(None, None)
    assert absent.state is ChainState.ABSENT
    by_path = verify_chain(missing)
    assert (absent.state, absent.detail) == (by_path.state, by_path.detail)
    # An empty log is a VALID empty chain only beside an anchor that says so.
    assert verify_chain_text("").state is ChainState.VALID
    assert verify_chain_text("").detail == "chain intact (0 records)"
    empty_pair = verify_pair_text("", _anchor_bytes(0, _GENESIS))
    assert empty_pair.state is ChainState.VALID
    assert empty_pair.detail == "chain + anchor intact (0 records)"
    # An existing log with no anchor is BROKEN until the owner bootstraps it (D2 §2).
    legacy = verify_pair_text("", None)
    assert legacy.state is ChainState.BROKEN
    assert legacy.detail == "head anchor missing for existing audit log; owner bootstrap required"
    # An anchor with no log is deletion, never a fresh start.
    deleted = verify_pair_text(None, _anchor_bytes(0, _GENESIS))
    assert deleted.state is ChainState.BROKEN
    assert "truncation/deletion" in deleted.detail, deleted.detail


class TestReadAuditPair:
    """One capability read of each file; unsafe entries refuse; nothing is created."""

    def test_it_returns_the_log_text_and_the_anchor_bytes(self, tmp_path: Path) -> None:
        path = tmp_path / "audit.jsonl"
        _chain(path, 2)
        text, anchor = read_audit_pair(path)
        assert text == path.read_text(encoding="utf-8")
        assert anchor == _anchor_of(path).read_bytes()

    def test_an_absent_pair_is_none_none_and_creates_nothing(self, tmp_path: Path) -> None:
        before = sorted(os.listdir(tmp_path))
        assert read_audit_pair(tmp_path / "missing.jsonl") == (None, None)
        assert read_audit_pair(tmp_path / "no-such-dir" / "audit.jsonl") == (None, None)
        assert sorted(os.listdir(tmp_path)) == before
        assert not (tmp_path / "no-such-dir").exists()

    def test_an_anchor_with_no_log_is_read_as_such(self, tmp_path: Path) -> None:
        path = tmp_path / "audit.jsonl"
        _chain(path, 1)
        anchor = _anchor_of(path).read_bytes()
        path.unlink()
        assert read_audit_pair(path) == (None, anchor)

    @pytest.mark.parametrize("which", ["log", "anchor"])
    def test_a_loose_mode_entry_refuses_and_is_not_tightened(
        self, tmp_path: Path, which: str
    ) -> None:
        path = tmp_path / "audit.jsonl"
        _chain(path, 1)
        target = path if which == "log" else _anchor_of(path)
        os.chmod(target, 0o644)
        with pytest.raises(AuditLogCorruptionError, match="mode 0o644, not 0o600"):
            read_audit_pair(path)
        assert (target.stat().st_mode & 0o777) == 0o644  # reported, never repaired

    def test_a_symlinked_anchor_refuses_without_reading_through(self, tmp_path: Path) -> None:
        path = tmp_path / "audit.jsonl"
        _chain(path, 1)
        genuine = _anchor_of(path)
        elsewhere = tmp_path / "elsewhere.json"
        elsewhere.write_bytes(genuine.read_bytes())
        os.chmod(elsewhere, 0o600)
        genuine.unlink()
        genuine.symlink_to(elsewhere)
        with pytest.raises(AuditLogCorruptionError, match="is a symlink"):
            read_audit_pair(path)
