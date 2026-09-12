"""``verify_chain_text``: the chain verdict over bytes a caller already holds.

Added for the monitoring snapshot (F4 HOLD): a consumer that verifies a path and then
re-reads the path to derive from it has two reads, and a replacement between them lets a
VALID verdict authorise BROKEN rows. Verifying the captured text closes that; this file
pins that the text verdict is the path verdict over the same bytes, detail strings
included, and that the helper is part of the package's public surface.

Kept separate from ``test_auditlog.py`` on purpose: that file is heavily rewritten on the
F2 branch (``claude/auditlog-serialized-append``) and this one must merge cleanly either way.
"""

from __future__ import annotations

from pathlib import Path

import chronos.auditlog as auditlog_pkg
from chronos.auditlog import AuditLog, ChainState, verify_chain, verify_chain_text


def _chain(path: Path, count: int) -> None:
    log = AuditLog(path)
    for index in range(count):
        log.append("test_event", {"index": index, "value": f"payload-{index}"})


def test_it_is_exported_from_the_package() -> None:
    assert "verify_chain_text" in auditlog_pkg.__all__
    assert auditlog_pkg.verify_chain_text is verify_chain_text


def test_a_valid_chain_reads_the_same_by_text_and_by_path(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    _chain(path, 3)
    by_path = verify_chain(path)
    by_text = verify_chain_text(path.read_text(encoding="utf-8"))
    assert by_text.state is ChainState.VALID
    assert (by_text.state, by_text.detail) == (by_path.state, by_path.detail)
    assert "3 records" in by_text.detail


def test_a_broken_chain_reads_the_same_by_text_and_by_path(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    _chain(path, 3)
    lines = path.read_text(encoding="utf-8").splitlines()
    lines[1] = lines[1].replace("payload-1", "payload-X")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    by_path = verify_chain(path)
    by_text = verify_chain_text(path.read_text(encoding="utf-8"))
    assert by_text.state is ChainState.BROKEN
    assert by_text.detail == by_path.detail == "line 2: hash mismatch"


def test_an_unreadable_row_and_a_gap_keep_their_line_numbers(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    _chain(path, 3)
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    del lines[1]
    assert verify_chain_text("\n".join(lines) + "\n").detail == "line 2: sequence gap"
    assert verify_chain_text(text + '{"broken').detail.startswith("line 4: unreadable record")


def test_empty_text_is_a_valid_empty_chain_like_an_empty_file(tmp_path: Path) -> None:
    # An existing empty file is VALID with zero records (D1 §6 bullet 4); text mirrors it.
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    assert verify_chain_text("").state is ChainState.VALID
    assert verify_chain_text("").detail == verify_chain(empty).detail
    # ABSENT is a statement about a PATH, not about text: only verify_chain can say it.
    assert verify_chain(tmp_path / "missing.jsonl").state is ChainState.ABSENT
