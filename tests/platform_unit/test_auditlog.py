"""Hash-chained audit log: tamper evidence and chain continuity."""

from __future__ import annotations

from pathlib import Path

import pytest

from chronos.auditlog.log import (
    AuditLog,
    AuditLogCorruptionError,
    ChainState,
    verify_chain,
)


def write_records(path: Path, count: int) -> None:
    log = AuditLog(path)
    for i in range(count):
        log.append("test_event", {"index": i, "value": f"payload-{i}"})


class TestVerifyChain:
    def test_appended_chain_verifies(self, tmp_path: Path) -> None:
        path = tmp_path / "audit.jsonl"
        write_records(path, 3)
        result = verify_chain(path)
        assert result.state is ChainState.VALID, result.detail
        assert "3 records" in result.detail

    def test_a_missing_file_is_absent_and_is_not_valid(self, tmp_path: Path) -> None:
        """This test was `test_missing_file_is_vacuously_ok` and asserted `ok` — the name
        and the assertion together are the defect #179 reports. A missing chain has not
        been verified; it has not been examined."""

        result = verify_chain(tmp_path / "never-written.jsonl")
        assert result.state is ChainState.ABSENT
        assert result.state is not ChainState.VALID
        assert "no audit log" in result.detail

    def test_payload_tamper_in_middle_is_detected(self, tmp_path: Path) -> None:
        path = tmp_path / "audit.jsonl"
        write_records(path, 3)
        lines = path.read_text(encoding="utf-8").splitlines()
        # Flip one payload byte in the middle record; its stored record_hash
        # no longer matches the recomputed hash.
        assert "payload-1" in lines[1]
        lines[1] = lines[1].replace("payload-1", "payload-X")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        result = verify_chain(path)
        assert result.state is ChainState.BROKEN
        assert "line 2" in result.detail
        assert "hash mismatch" in result.detail

    def test_deleted_line_is_a_sequence_gap(self, tmp_path: Path) -> None:
        path = tmp_path / "audit.jsonl"
        write_records(path, 3)
        lines = path.read_text(encoding="utf-8").splitlines()
        del lines[1]  # remove the middle record
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        result = verify_chain(path)
        assert result.state is ChainState.BROKEN
        assert "sequence gap" in result.detail

    def test_reordered_records_break_the_chain(self, tmp_path: Path) -> None:
        path = tmp_path / "audit.jsonl"
        write_records(path, 3)
        lines = path.read_text(encoding="utf-8").splitlines()
        lines[1], lines[2] = lines[2], lines[1]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        assert verify_chain(path).state is ChainState.BROKEN


class TestReopen:
    def test_append_after_reopen_continues_chain(self, tmp_path: Path) -> None:
        path = tmp_path / "audit.jsonl"
        first = AuditLog(path)
        first.append("startup", {"n": 1})
        first.append("startup", {"n": 2})

        # A fresh writer (process restart) must continue, not restart, the chain.
        second = AuditLog(path)
        record = second.append("resume", {"n": 3})
        assert record.sequence == 2

        result = verify_chain(path)
        assert result.state is ChainState.VALID, result.detail
        assert "3 records" in result.detail

    def test_corrupt_last_line_fails_closed_on_construction(self, tmp_path: Path) -> None:
        # A process killed mid-append can leave a truncated final line. The
        # next construction must fail closed with a catchable exception, not a
        # raw JSONDecodeError, so a caller can halt cleanly.
        path = tmp_path / "audit.jsonl"
        log = AuditLog(path)
        log.append("startup", {"n": 1})
        with path.open("a", encoding="utf-8") as handle:
            handle.write('{"sequence":1,"kind":"partial","payload":{"x"')
        with pytest.raises(AuditLogCorruptionError):
            AuditLog(path)
        assert verify_chain(path).state is ChainState.BROKEN


class TestTheThreeStatesAreDistinct:
    """absent ≠ valid ≠ broken, asserted together.

    Each state is also pinned by name elsewhere, but a single test comparing all three is
    what fails if any two are ever collapsed back into one another.
    """

    def test_absent_valid_and_broken_are_three_different_states(self, tmp_path: Path) -> None:
        absent = verify_chain(tmp_path / "never-written.jsonl")

        intact = tmp_path / "intact.jsonl"
        write_records(intact, 3)
        valid = verify_chain(intact)

        broken_path = tmp_path / "broken.jsonl"
        write_records(broken_path, 3)
        lines = broken_path.read_text(encoding="utf-8").splitlines()
        lines[1] = lines[1].replace("payload-1", "payload-X")
        broken_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        broken = verify_chain(broken_path)

        # The positive control: the corrupted chain must read BROKEN by name. Without it a
        # verifier that returned ABSENT for everything would satisfy "all three differ".
        assert broken.state is ChainState.BROKEN
        assert valid.state is ChainState.VALID
        assert absent.state is ChainState.ABSENT
        assert len({absent.state, valid.state, broken.state}) == 3

    def test_the_result_cannot_be_unpacked(self, tmp_path: Path) -> None:
        """The migration is by type. An un-updated caller must fail loudly, not silently
        inherit the old True-for-missing answer."""

        result = verify_chain(tmp_path / "never-written.jsonl")
        with pytest.raises(TypeError):
            _ok, _detail = result  # type: ignore[misc]
        assert not hasattr(result, "ok")

    def test_truth_testing_raises_for_every_state(self, tmp_path: Path) -> None:
        """Omitting __bool__ is NOT enough, which is what this pins.

        A dataclass without __bool__ is truthy by default, so `if verify_chain(path):` and
        `assert verify_chain(path)` answered True for a MISSING chain — the original bug,
        reproduced one layer down inside the type built to prevent it. Every state must
        refuse, not just ABSENT: a caller truth-testing the result is wrong regardless of
        which state it happens to hold, and letting VALID answer True would keep the
        pattern alive until the day it meets an absent file.
        """

        absent = verify_chain(tmp_path / "never-written.jsonl")

        intact = tmp_path / "intact.jsonl"
        write_records(intact, 2)
        valid = verify_chain(intact)

        broken_path = tmp_path / "broken.jsonl"
        write_records(broken_path, 3)
        lines = broken_path.read_text(encoding="utf-8").splitlines()
        tampered = lines[1].replace("payload-1", "payload-X")
        assert tampered != lines[1], lines[1]
        lines[1] = tampered
        broken_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        broken = verify_chain(broken_path)

        assert (absent.state, valid.state, broken.state) == (
            ChainState.ABSENT,
            ChainState.VALID,
            ChainState.BROKEN,
        )
        for result in (absent, valid, broken):
            with pytest.raises(TypeError, match="has no truth value"):
                bool(result)
            with pytest.raises(TypeError, match=result.state.value):
                # An implicit truth-test, the shape a caller would actually write.
                if result:
                    pass
