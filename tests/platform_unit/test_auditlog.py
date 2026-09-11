"""Hash-chained audit log: tamper evidence and chain continuity."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import textwrap
import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from chronos.auditlog.log import (
    AuditLog,
    AuditLogCorruptionError,
    ChainState,
    verify_chain,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]


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


def _sequences(path: Path) -> list[int]:
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [int(json.loads(line)["sequence"]) for line in lines]


def _lock_path(path: Path) -> Path:
    # The sibling the writer serializes on, named like the registry's (`<ledger>.lock`).
    return path.with_name(path.name + ".lock")


def _wait_for(predicate: Callable[[], bool], timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


class TestSerializedAppend:
    """Appends are serialized against a FRESH head, not a cached one (D1 Gap B, candidate C/D).

    Before this, each ``AuditLog`` cached sequence and head at construction and advanced
    only its private copy, so two instances on one path — an accidental double start —
    both minted the same successor, and nothing re-read the tail before writing. Recovery
    also read only the last row, so a writer would extend a chain whose middle was already
    broken. These tests pin the transaction: per-path thread lock plus ``flock`` on an
    owner-only sibling lock file, taken BEFORE recovery; a full fresh verification of the
    chain under the lock; then write, flush, fsync, release.
    """

    def test_two_instances_serialize_against_a_fresh_head(self, tmp_path: Path) -> None:
        # Daybreak D1 §3 Gap B, verbatim: today both return sequence 0.
        path = tmp_path / "audit.jsonl"
        first = AuditLog(path)
        second = AuditLog(path)  # same cached genesis/head
        assert first.append("first", {}).sequence == 0
        assert second.append("second", {}).sequence == 1
        assert verify_chain(path).state is ChainState.VALID

    def test_append_refuses_to_extend_past_middle_corruption(self, tmp_path: Path) -> None:
        # D1 §6 bullet 2: recovery read only the last row, so a writer appended past a
        # broken middle until a separate verifier ran. Fresh recovery must walk the chain.
        path = tmp_path / "audit.jsonl"
        write_records(path, 3)
        lines = path.read_text(encoding="utf-8").splitlines()
        assert "payload-1" in lines[1]
        lines[1] = lines[1].replace("payload-1", "payload-X")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        assert verify_chain(path).state is ChainState.BROKEN  # the corruption is observable

        with pytest.raises(AuditLogCorruptionError, match="line 2"):
            AuditLog(path).append("after_corruption", {"n": 4})
        # Nothing was appended past the break.
        assert len(_sequences(path)) == 3

    def test_two_threads_appending_through_separate_instances_keep_one_chain(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "audit.jsonl"
        per_thread = 25
        barrier = threading.Barrier(2)
        failures: list[BaseException] = []

        def worker(tag: str) -> None:
            try:
                log = AuditLog(path)
                barrier.wait(timeout=10)  # both instances exist; now they overlap
                for i in range(per_thread):
                    log.append("race", {"tag": tag, "i": i})
            except BaseException as error:  # surfaced by the assertion below
                failures.append(error)

        threads = [threading.Thread(target=worker, args=(tag,)) for tag in ("a", "b")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        assert not failures, failures
        assert not any(thread.is_alive() for thread in threads)

        sequences = _sequences(path)
        assert len(sequences) == 2 * per_thread
        assert sequences == list(range(2 * per_thread))
        result = verify_chain(path)
        assert result.state is ChainState.VALID, result.detail

    def test_two_processes_appending_through_separate_instances_keep_one_chain(
        self, tmp_path: Path
    ) -> None:
        """The cross-process half. A thread lock cannot serialize two processes; only the
        ``flock`` can, so this is the test that fails if the OS lock is dropped."""

        path = tmp_path / "audit.jsonl"
        go = tmp_path / "go"
        per_process = 40
        child = textwrap.dedent(
            """
            import sys, time
            from pathlib import Path
            from chronos.auditlog.log import AuditLog

            path, go = Path(sys.argv[1]), Path(sys.argv[2])
            count, tag = int(sys.argv[3]), sys.argv[4]
            log = AuditLog(path)
            go.with_name(f"ready-{tag}").write_text("", encoding="utf-8")  # explicit ack
            deadline = time.monotonic() + 20
            while not go.exists():  # released only once BOTH children have acknowledged
                if time.monotonic() > deadline:
                    raise SystemExit("barrier timeout")
                time.sleep(0.001)
            for i in range(count):
                log.append("race", {"tag": tag, "i": i})
            """
        )
        children = [
            subprocess.Popen(
                [sys.executable, "-c", child, str(path), str(go), str(per_process), tag],
                cwd=_REPO_ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for tag in ("a", "b")
        ]
        assert _wait_for(lambda: all((tmp_path / f"ready-{tag}").exists() for tag in ("a", "b")))
        go.write_text("go", encoding="utf-8")  # both instances exist; release them together
        outcomes = [proc.communicate(timeout=120) for proc in children]
        for proc, (_out, err) in zip(children, outcomes, strict=True):
            assert proc.returncode == 0, err

        sequences = _sequences(path)
        assert len(sequences) == 2 * per_process
        assert sequences == list(range(2 * per_process))
        result = verify_chain(path)
        assert result.state is ChainState.VALID, result.detail

    def test_lock_file_is_an_owner_only_regular_file(self, tmp_path: Path) -> None:
        # Mirrors the registry's rule for its lock (src/chronos/registry/ledger.py:1-12).
        path = tmp_path / "audit.jsonl"
        AuditLog(path).append("startup", {"n": 1})
        lock = _lock_path(path)
        metadata = lock.lstat()
        assert stat.S_ISREG(metadata.st_mode), "lock must be a regular file"
        assert stat.S_IMODE(metadata.st_mode) == 0o600
        assert stat.S_IMODE(path.stat().st_mode) == 0o600  # the log itself stays private

    def test_symlinked_lock_path_is_refused_without_touching_the_victim(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "audit.jsonl"
        victim = tmp_path / "victim"
        victim.write_text("do not touch", encoding="utf-8")
        victim.chmod(0o644)
        _lock_path(path).symlink_to(victim)

        with pytest.raises(AuditLogCorruptionError, match="symlink"):
            AuditLog(path)
        assert victim.read_text(encoding="utf-8") == "do not touch"
        assert stat.S_IMODE(victim.stat().st_mode) == 0o644
        assert not path.exists()

    def test_a_lock_file_replaced_during_acquisition_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``flock`` binds an inode, not a name. If the lock file is unlinked and recreated
        between our open and our lock, two writers can each hold "the" lock on different
        inodes; the writer must notice the name no longer points at the inode it holds."""

        import fcntl

        from chronos.auditlog import log as log_module

        path = tmp_path / "audit.jsonl"
        lock = _lock_path(path)
        real_flock = fcntl.flock

        def swap_then_lock(descriptor: int, operation: int) -> None:
            if operation == fcntl.LOCK_EX:
                lock.unlink()
                lock.write_text("", encoding="utf-8")  # a different inode under the same name
            real_flock(descriptor, operation)

        monkeypatch.setattr(log_module.fcntl, "flock", swap_then_lock)
        with pytest.raises(AuditLogCorruptionError, match="replaced"):
            AuditLog(path)
        assert not path.exists()


class TestTransactionShape:
    """The transaction is observable, not inferred from a happy-path race (precheck gaps 1-3, 5).

    ``AuditLog._after_recovery`` is a test seam: a callable invoked inside the transaction
    after fresh recovery and before the write, while both locks are held. With it a test can
    park writer A inside the critical section and PROVE writer B cannot cross, instead of
    hoping a barrier produced overlap. It does nothing when unset.
    """

    def test_a_parked_writer_holds_the_thread_lock_and_a_second_thread_cannot_cross(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "audit.jsonl"
        first = AuditLog(path)
        second = AuditLog(path)
        inside, release = threading.Event(), threading.Event()

        def park() -> None:
            inside.set()
            assert release.wait(timeout=10), "the test never released the parked writer"

        first._after_recovery = park
        results: dict[str, int] = {}
        second_done = threading.Event()

        def run_first() -> None:
            results["a"] = first.append("a", {}).sequence

        def run_second() -> None:
            results["b"] = second.append("b", {}).sequence
            second_done.set()

        thread_a = threading.Thread(target=run_first)
        thread_a.start()
        assert inside.wait(timeout=10), "writer A never reached the seam"
        # Mechanism: the per-path thread lock is one object for both instances and is held now.
        assert second._thread_lock is first._thread_lock
        assert first._thread_lock.locked(), "the thread lock is not held inside the transaction"

        thread_b = threading.Thread(target=run_second)
        thread_b.start()
        assert not second_done.wait(timeout=0.5), "writer B crossed the lock while A was inside"
        assert not path.exists(), "something was written while A was parked before its write"

        release.set()
        thread_a.join(timeout=10)
        thread_b.join(timeout=10)
        assert not thread_a.is_alive() and not thread_b.is_alive()
        assert (results["a"], results["b"]) == (0, 1)
        assert _sequences(path) == [0, 1]
        assert verify_chain(path).state is ChainState.VALID

    def test_a_second_process_cannot_enter_while_the_first_is_inside(self, tmp_path: Path) -> None:
        """The cross-process half of gap 1, with explicit acknowledgements and a parked writer.

        Only the OS lock can hold a second PROCESS out; a thread lock says nothing about it.
        Without ``flock`` child B finishes while A is parked and the assertion below fails
        deterministically — no scheduling luck involved.
        """

        path = tmp_path / "audit.jsonl"
        signals = tmp_path / "signals"
        signals.mkdir()
        child = textwrap.dedent(
            """
            import sys, time
            from pathlib import Path
            from chronos.auditlog.log import AuditLog

            path, signals, tag = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]

            def wait_for(name):
                deadline = time.monotonic() + 20
                while not (signals / name).exists():
                    if time.monotonic() > deadline:
                        raise SystemExit(f"timeout waiting for {name}")
                    time.sleep(0.001)

            log = AuditLog(path)
            (signals / f"ready-{tag}").write_text("", encoding="utf-8")
            wait_for(f"go-{tag}")
            if tag == "a":
                def park():
                    (signals / "inside-a").write_text("", encoding="utf-8")
                    wait_for("release-a")
                log._after_recovery = park
            log.append("race", {"tag": tag})
            (signals / f"done-{tag}").write_text("", encoding="utf-8")
            """
        )
        children = [
            subprocess.Popen(
                [sys.executable, "-c", child, str(path), str(signals), tag],
                cwd=_REPO_ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for tag in ("a", "b")
        ]
        try:
            both_ready = lambda: all((signals / f"ready-{tag}").exists() for tag in ("a", "b"))  # noqa: E731
            assert _wait_for(both_ready), "children never acknowledged"
            (signals / "go-a").write_text("", encoding="utf-8")
            assert _wait_for(lambda: (signals / "inside-a").exists()), "A never parked inside"
            (signals / "go-b").write_text("", encoding="utf-8")
            assert not _wait_for(lambda: (signals / "done-b").exists(), timeout=0.5), (
                "process B appended while process A held the lock"
            )
            (signals / "release-a").write_text("", encoding="utf-8")
            outcomes = [proc.communicate(timeout=60) for proc in children]
        finally:
            for proc in children:
                if proc.poll() is None:
                    proc.kill()
        for proc, (_out, err) in zip(children, outcomes, strict=True):
            assert proc.returncode == 0, err
        assert (signals / "done-a").exists() and (signals / "done-b").exists()
        assert _sequences(path) == [0, 1]
        assert verify_chain(path).state is ChainState.VALID

    def test_construction_recovers_under_the_lock_not_outside_it(self, tmp_path: Path) -> None:
        """D1 §4 row C: lock acquisition must precede recovery — at construction too.

        While writer A is parked inside its transaction, a fresh instance's construction must
        BLOCK on the lock. Once A releases, that construction must see the corruption planted
        meanwhile and refuse. A construction that verified outside the lock would return (or
        raise) immediately instead of blocking.
        """

        path = tmp_path / "audit.jsonl"
        write_records(path, 3)
        first = AuditLog(path)
        inside, release = threading.Event(), threading.Event()

        def park() -> None:
            inside.set()
            assert release.wait(timeout=10)

        first._after_recovery = park
        thread_a = threading.Thread(target=lambda: first.append("a", {}))
        thread_a.start()
        assert inside.wait(timeout=10)

        # Plant middle corruption while A is inside (flock is advisory; a plain write lands).
        lines = path.read_text(encoding="utf-8").splitlines()
        lines[1] = lines[1].replace("payload-1", "payload-X")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        constructed = threading.Event()
        outcome: dict[str, BaseException | None] = {}

        def construct() -> None:
            try:
                AuditLog(path)
                outcome["error"] = None
            except AuditLogCorruptionError as error:
                outcome["error"] = error
            finally:
                constructed.set()

        thread_b = threading.Thread(target=construct)
        thread_b.start()
        assert not constructed.wait(timeout=0.5), "construction verified outside the append lock"

        release.set()
        thread_a.join(timeout=10)
        thread_b.join(timeout=10)
        assert not thread_a.is_alive() and not thread_b.is_alive()
        error = outcome["error"]
        assert isinstance(error, AuditLogCorruptionError), error
        assert "line 2" in str(error)

    def test_the_lock_spans_recovery_write_flush_fsync_and_unlock_is_last(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Observe the order rather than trust it: lock -> recover -> (seam) -> fsync -> unlock,
        for construction and for append. A fortunate schedule cannot pass an early unlock here."""

        import fcntl

        from chronos.auditlog import log as log_module

        events: list[str] = []
        real_flock, real_fsync = fcntl.flock, os.fsync
        real_recover = log_module.AuditLog._recover

        def flock(descriptor: int, operation: int) -> None:
            events.append({fcntl.LOCK_EX: "lock", fcntl.LOCK_UN: "unlock"}.get(operation, "flock?"))
            real_flock(descriptor, operation)

        def fsync(descriptor: int) -> None:
            events.append("fsync")
            real_fsync(descriptor)

        def recover(self: log_module.AuditLog) -> tuple[int, str]:
            events.append("recover")
            return real_recover(self)

        monkeypatch.setattr(log_module.fcntl, "flock", flock)
        monkeypatch.setattr(log_module.os, "fsync", fsync)
        monkeypatch.setattr(log_module.AuditLog, "_recover", recover)

        path = tmp_path / "audit.jsonl"
        log = AuditLog(path)
        assert events[0] == "lock" and events[-1] == "unlock", events
        assert "recover" in events and events.count("unlock") == 1, events

        events.clear()
        log._after_recovery = lambda: events.append("seam")
        log.append("k", {"n": 1})
        assert events[0] == "lock", events
        assert events[-1] == "unlock" and events.count("unlock") == 1, events
        assert "seam" in events, "the seam did not fire inside the transaction"
        order = [events.index(name) for name in ("lock", "recover", "seam", "fsync")]
        assert order == sorted(order), events
        assert events.index("fsync") < len(events) - 1, events  # fsync strictly before the unlock

    def test_a_nested_append_from_inside_the_transaction_raises_instead_of_deadlocking(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "audit.jsonl"
        log = AuditLog(path)
        nested: list[BaseException] = []

        def try_nested_append() -> None:
            try:
                log.append("nested", {})
            except AuditLogCorruptionError as error:
                nested.append(error)

        log._after_recovery = try_nested_append
        outer: list[int] = []
        thread = threading.Thread(target=lambda: outer.append(log.append("outer", {}).sequence))
        thread.start()
        thread.join(timeout=10)
        assert not thread.is_alive(), "a nested append deadlocked on the non-reentrant lock"
        assert len(nested) == 1 and "re-entrant" in str(nested[0]), nested
        assert outer == [0]
        assert _sequences(path) == [0]
        assert verify_chain(path).state is ChainState.VALID

    def test_an_exception_inside_the_transaction_releases_both_locks(self, tmp_path: Path) -> None:
        path = tmp_path / "audit.jsonl"
        log = AuditLog(path)
        log.append("first", {})

        def boom() -> None:
            raise RuntimeError("boom")

        log._after_recovery = boom
        with pytest.raises(RuntimeError, match="boom"):
            log.append("second", {})
        log._after_recovery = None
        assert not log._thread_lock.locked(), "thread lock leaked past the exception"
        # A fresh instance needs the OS lock too; it must not hang, and the failed append
        # must have written nothing.
        assert AuditLog(path).append("third", {}).sequence == 1
        assert _sequences(path) == [0, 1]
        assert verify_chain(path).state is ChainState.VALID

    def test_a_failed_fsync_releases_the_lock_and_the_chain_stays_consistent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from chronos.auditlog import log as log_module

        path = tmp_path / "audit.jsonl"
        log = AuditLog(path)
        log.append("first", {})

        def failing_fsync(descriptor: int) -> None:
            raise OSError("disk went away")

        monkeypatch.setattr(log_module.os, "fsync", failing_fsync)
        with pytest.raises(OSError, match="disk went away"):
            log.append("second", {})
        monkeypatch.undo()
        assert not log._thread_lock.locked()
        # The next append either continues (the line was fully written before fsync failed)
        # or fails closed on verification — never hangs, never forks the chain.
        record = AuditLog(path).append("third", {})
        sequences = _sequences(path)
        assert sequences == list(range(len(sequences)))
        assert record.sequence == sequences[-1]
        assert verify_chain(path).state is ChainState.VALID
