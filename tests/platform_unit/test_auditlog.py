"""Hash-chained audit log: tamper evidence and chain continuity."""

from __future__ import annotations

import errno
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

        with pytest.raises(AuditLogCorruptionError, match=r"is a symlink"):
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
        # The transaction may already have created the (empty) log: O_CREAT happens when the
        # descriptor is opened, before recovery. No RECORD may exist while A is parked.
        assert not path.exists() or path.read_text(encoding="utf-8") == "", (
            "something was written while A was parked before its write"
        )

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

        def recover(self: log_module.AuditLog, *args: object, **kwargs: object) -> tuple[int, str]:
            events.append("recover")
            return real_recover(self, *args, **kwargs)

        real_fdopen = os.fdopen

        class _Observed:
            """Wraps the transaction's text handle so write and flush are events too."""

            def __init__(self, inner: object) -> None:
                self._inner = inner

            def __enter__(self) -> _Observed:
                self._inner.__enter__()  # type: ignore[attr-defined]
                return self

            def __exit__(self, *exc: object) -> None:
                self._inner.__exit__(*exc)  # type: ignore[attr-defined]

            def write(self, data: str) -> int:
                events.append("write")
                return self._inner.write(data)  # type: ignore[attr-defined, no-any-return]

            def flush(self) -> None:
                events.append("flush")
                self._inner.flush()  # type: ignore[attr-defined]

            def __getattr__(self, name: str) -> object:
                return getattr(self._inner, name)

        def fdopen(*args: object, **kwargs: object) -> _Observed:
            return _Observed(real_fdopen(*args, **kwargs))  # type: ignore[arg-type]

        monkeypatch.setattr(log_module.fcntl, "flock", flock)
        monkeypatch.setattr(log_module.os, "fsync", fsync)
        monkeypatch.setattr(log_module.AuditLog, "_recover", recover)
        monkeypatch.setattr(log_module.os, "fdopen", fdopen)

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
        # write -> flush -> fsync -> unlock: a buffered write that is fsynced before it is
        # flushed is not durable, and closing the handle after fsync would flush it too late.
        assert "write" in events and "flush" in events, events
        order = [
            events.index(name) for name in ("lock", "recover", "seam", "write", "flush", "fsync")
        ]
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
        """A failed log fsync leaves the record written but never anchored: the anchor still
        names the previous head. This pin used to allow either "continue" or "fail closed";
        the head anchor decides it (D2 §1) — the next writer refuses the pair as a crash
        window, the chain is neither extended nor forked, and the locks are released."""

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
        sequences = _sequences(path)
        assert sequences == list(range(len(sequences)))  # written in order, never forked
        result = verify_chain(path)
        assert result.state is ChainState.BROKEN
        assert "crash window" in result.detail, result.detail
        # A fresh instance needs the OS lock too; it must not hang, and it must not extend
        # or repair the pair: reviewed recovery, not silent repair.
        with pytest.raises(AuditLogCorruptionError, match="crash window"):
            AuditLog(path)
        assert _sequences(path) == sequences


class TestLogPathCapability:
    """The LOG path is handled descriptor-relative and no-follow, like the lock (D1 §6 bullet 1).

    Before this the writer followed the log target for read and append and only then let
    ``secure_owner_only`` notice a symlink — so a planted link redirected the write before
    anything refused. Conductor's ruling on the F2 addendum: fold the log-path hardening in
    with the lock's, mirroring the registry ledger's rule (``registry/ledger.py:1-12``).
    """

    def test_a_symlinked_log_path_is_refused_before_any_read_or_write_of_the_target(
        self, tmp_path: Path
    ) -> None:
        victim = tmp_path / "victim.jsonl"
        write_records(victim, 2)  # a VALID chain, so a follower would happily extend it
        before = victim.read_bytes()
        victim_mode = stat.S_IMODE(victim.stat().st_mode)

        path = tmp_path / "audit.jsonl"
        path.symlink_to(victim)
        with pytest.raises(AuditLogCorruptionError, match=r"is a symlink"):
            AuditLog(path).append("through_the_link", {"n": 3})

        assert victim.read_bytes() == before, "the write went through the symlink"
        assert stat.S_IMODE(victim.stat().st_mode) == victim_mode
        assert path.is_symlink()  # the link itself was not replaced or removed

    def test_a_log_replaced_under_a_running_writer_is_refused(self, tmp_path: Path) -> None:
        """Path swap after construction: same bytes, different inode. The RUNNING writer
        pinned the inode it recovered from and must refuse; a fresh instance (a restart)
        re-pins and continues — detecting a rollback across restarts is the anchor lane's job."""

        path = tmp_path / "audit.jsonl"
        log = AuditLog(path)
        log.append("first", {"n": 1})
        swap = tmp_path / "swap.jsonl"
        swap.write_bytes(path.read_bytes())
        os.replace(swap, path)  # atomic rename: new inode, identical content

        with pytest.raises(AuditLogCorruptionError, match="replaced"):
            log.append("after_swap", {"n": 2})
        assert _sequences(path) == [0]
        assert AuditLog(path).append("after_restart", {"n": 2}).sequence == 1
        assert verify_chain(path).state is ChainState.VALID

    def test_a_log_swapped_for_a_symlink_is_refused_without_touching_the_victim(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "audit.jsonl"
        log = AuditLog(path)
        log.append("first", {"n": 1})
        victim = tmp_path / "victim.jsonl"
        write_records(victim, 2)
        before = victim.read_bytes()

        path.unlink()
        path.symlink_to(victim)
        with pytest.raises(AuditLogCorruptionError, match=r"is a symlink"):
            log.append("through_the_link", {"n": 2})
        assert victim.read_bytes() == before
        assert stat.S_IMODE(victim.stat().st_mode) == 0o600

    def test_a_hard_linked_log_is_refused(self, tmp_path: Path) -> None:
        # A second name for the same inode is a handle an actor keeps after the writer
        # believes the file is private; the registry refuses it and so does this.
        path = tmp_path / "audit.jsonl"
        log = AuditLog(path)
        log.append("first", {"n": 1})
        os.link(path, tmp_path / "alias.jsonl")
        with pytest.raises(AuditLogCorruptionError, match="links"):
            log.append("second", {"n": 2})
        assert _sequences(path) == [0]

    def test_a_symlinked_parent_directory_is_refused_without_creating_anything(
        self, tmp_path: Path
    ) -> None:
        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real, target_is_directory=True)
        with pytest.raises(AuditLogCorruptionError, match=r"is a symlink or not a real directory"):
            AuditLog(link / "audit.jsonl")
        assert list(real.iterdir()) == [], "something was created through the parent symlink"

    def test_a_missing_parent_is_still_created_and_the_log_stays_private(
        self, tmp_path: Path
    ) -> None:
        # The behaviour consumers rely on: a fresh data directory appears on first use.
        path = tmp_path / "nested" / "deeper" / "audit.jsonl"
        AuditLog(path).append("first", {"n": 1})
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert path.parent.is_dir() and not path.parent.is_symlink()
        assert verify_chain(path).state is ChainState.VALID


_CHILD_APPEND = textwrap.dedent(
    """
    import sys
    from pathlib import Path
    from chronos.auditlog.log import AuditLog
    print(AuditLog(Path(sys.argv[1])).append("child", {"who": "child"}).sequence)
    """
)


def _child_appends(path: Path) -> int:
    """A second PROCESS appends once at ``path`` and returns the sequence it was given."""

    proc = subprocess.run(
        [sys.executable, "-c", _CHILD_APPEND, str(path)],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return int(proc.stdout.strip())


class TestCapabilityHeldEndToEnd:
    """The names must designate the held descriptors for the WHOLE transaction (F2 review §3).

    Daybreak's probe: unlink and recreate the lock from the seam — after the one post-flock
    check — then let a child append through the replacement lock; both writers returned
    sequence 0 and the chain read BROKEN. Same with the parent renamed away and replaced
    (two VALID one-record chains). The fix re-establishes, at every boundary, that the
    canonical parent path, the lock name and the log name still designate what this writer
    holds: after taking the lock, immediately before the write, and after fsync before
    success is reported. The registry ledger asserts its binding the same way around its
    write and its anchor publication.
    """

    def test_a_lock_replaced_after_the_check_cannot_yield_two_successful_writers(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "audit.jsonl"
        lock = _lock_path(path)
        log = AuditLog(path)
        inside, release = threading.Event(), threading.Event()

        def swap_the_lock_then_park() -> None:
            lock.unlink()
            lock.write_text("", encoding="utf-8")  # a fresh inode under the lock's name
            inside.set()
            assert release.wait(timeout=10)

        log._after_recovery = swap_the_lock_then_park
        outcome: dict[str, object] = {}

        def run_first() -> None:
            try:
                outcome["sequence"] = log.append("first", {}).sequence
            except AuditLogCorruptionError as error:
                outcome["error"] = error

        thread = threading.Thread(target=run_first)
        thread.start()
        assert inside.wait(timeout=10)
        # The child locks the REPLACEMENT inode, so nothing holds it out; it appends.
        assert _child_appends(path) == 0
        release.set()
        thread.join(timeout=10)
        assert not thread.is_alive()

        error = outcome.get("error")
        assert isinstance(error, AuditLogCorruptionError), outcome
        assert "lock" in str(error) and "replaced" in str(error), str(error)
        assert _sequences(path) == [0], "two writers completed against one head"
        assert verify_chain(path).state is ChainState.VALID

    def test_a_displaced_parent_cannot_report_success_outside_the_configured_path(
        self, tmp_path: Path
    ) -> None:
        base = tmp_path / "data"
        base.mkdir()
        path = base / "audit.jsonl"
        displaced = tmp_path / "data-displaced"
        log = AuditLog(path)
        inside, release = threading.Event(), threading.Event()

        def displace_the_parent_then_park() -> None:
            os.rename(base, displaced)  # our descriptors now point into data-displaced/
            base.mkdir()  # a fresh, empty directory at the canonical path
            inside.set()
            assert release.wait(timeout=10)

        log._after_recovery = displace_the_parent_then_park
        outcome: dict[str, object] = {}

        def run_first() -> None:
            try:
                outcome["sequence"] = log.append("first", {}).sequence
            except AuditLogCorruptionError as error:
                outcome["error"] = error

        thread = threading.Thread(target=run_first)
        thread.start()
        assert inside.wait(timeout=10)
        assert _child_appends(path) == 0  # a second writer at the canonical path
        release.set()
        thread.join(timeout=10)
        assert not thread.is_alive()

        error = outcome.get("error")
        assert isinstance(error, AuditLogCorruptionError), outcome
        assert "parent" in str(error) and ("displaced" in str(error) or "replaced" in str(error))
        # Nothing was reported as appended outside the configured path...
        assert (displaced / "audit.jsonl").read_text(encoding="utf-8") == ""
        # ...and the canonical path holds exactly the child's record.
        assert _sequences(path) == [0]
        assert verify_chain(path).state is ChainState.VALID

    def test_a_log_replaced_after_open_is_not_reported_durable(self, tmp_path: Path) -> None:
        path = tmp_path / "audit.jsonl"
        log = AuditLog(path)
        log.append("first", {"n": 1})
        before = path.read_bytes()

        def swap_the_log() -> None:
            swap = tmp_path / "swap.jsonl"
            swap.write_bytes(before)
            os.replace(swap, path)  # the held descriptor now points at an unnamed inode

        log._after_recovery = swap_the_log
        with pytest.raises(AuditLogCorruptionError, match="replaced"):
            log.append("second", {"n": 2})
        assert path.read_bytes() == before, (
            "the canonical log gained a record nobody was told about"
        )

    def test_a_log_replaced_after_fsync_refuses_to_report_success(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The record IS durable — on an inode the name no longer designates. The writer
        must say so and must not return an AuditRecord as if the canonical log had it."""

        from chronos.auditlog import log as log_module

        path = tmp_path / "audit.jsonl"
        log = AuditLog(path)
        log.append("first", {"n": 1})
        before = path.read_bytes()
        real_fsync = os.fsync

        def fsync_then_swap(descriptor: int) -> None:
            real_fsync(descriptor)
            swap = tmp_path / "swap.jsonl"
            swap.write_bytes(before)
            os.replace(swap, path)

        monkeypatch.setattr(log_module.os, "fsync", fsync_then_swap)
        with pytest.raises(AuditLogCorruptionError) as info:
            log.append("second", {"n": 2})
        message = str(info.value)
        assert "replaced" in message and "durable" in message and "NOT reported" in message
        assert _sequences(path) == [0]  # the canonical name shows the pre-append chain

    def test_flock_failure_is_a_catchable_audit_refusal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A filesystem without advisory locking must refuse as an audit failure the
        callers already halt on, not escape as a raw OSError (cli/main.py catches only
        AuditLogCorruptionError)."""

        from chronos.auditlog import log as log_module

        def no_flock(descriptor: int, operation: int) -> None:
            raise OSError(errno.ENOSYS, "Function not implemented")

        monkeypatch.setattr(log_module.fcntl, "flock", no_flock)
        path = tmp_path / "audit.jsonl"
        with pytest.raises(AuditLogCorruptionError, match="flock"):
            AuditLog(path)
        assert not path.exists(), "the log was created before the lock was known to be unusable"


# ---------------------------------------------------------------------- the head anchor

_ZERO_HASH = "0" * 64


def _anchor_path(path: Path) -> Path:
    # The sibling the writer publishes beside the log, named like the registry's
    # (`<stem>.head.json`): `audit.jsonl` owns `audit.head.json`.
    return path.with_name(path.stem + ".head.json")


def _anchor_bytes(count: int, last_hash: str) -> bytes:
    # D2 §1: the one deterministic representation, `json.dumps(sort_keys=True) + "\n"`.
    return (json.dumps({"count": count, "last_hash": last_hash}, sort_keys=True) + "\n").encode(
        "utf-8"
    )


def _record_hashes(path: Path) -> list[str]:
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [str(json.loads(line)["record_hash"]) for line in lines]


def test_verify_detects_complete_tail_truncation(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    write_records(path, 3)
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")
    result = verify_chain(path)
    assert result.state is ChainState.BROKEN
    assert "truncation" in result.detail


def _materialize_pair(tmp_path: Path, log_kind: str, anchor_kind: str) -> Path:
    """Build one D2 §2 matrix cell: a ``log_kind`` log beside an ``anchor_kind`` anchor."""

    path = tmp_path / "audit.jsonl"
    anchor = _anchor_path(path)
    hashes: list[str] = []
    if log_kind == "empty":
        path.write_text("", encoding="utf-8")
    elif log_kind in ("valid", "broken"):
        write_records(path, 3)
        hashes = _record_hashes(path)
        if log_kind == "broken":
            lines = path.read_text(encoding="utf-8").splitlines()
            assert "payload-1" in lines[1]
            lines[1] = lines[1].replace("payload-1", "payload-X")
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    else:
        assert log_kind == "absent"
    count = len(hashes)
    last = hashes[-1] if hashes else _ZERO_HASH
    anchor.unlink(missing_ok=True)
    if anchor_kind == "malformed":
        anchor.write_bytes(b"not an anchor\n")
    elif anchor_kind == "matches":
        # For an absent log "matches" cannot mean anything; any well-formed anchor is the cell.
        anchor.write_bytes(
            _anchor_bytes(count, last) if log_kind != "absent" else _anchor_bytes(1, "a" * 64)
        )
    elif anchor_kind == "behind":
        anchor.write_bytes(_anchor_bytes(count - 1, hashes[-2]))
    elif anchor_kind == "ahead":
        anchor.write_bytes(_anchor_bytes(count + 1, "a" * 64))
    elif anchor_kind == "wrong-hash":
        anchor.write_bytes(_anchor_bytes(count, "a" * 64))
    else:
        assert anchor_kind == "absent"
    return path


_MALFORMED_ANCHORS: dict[str, bytes] = {
    "not-json": b"not an anchor\n",
    "not-an-object": b'["count", 1]\n',
    "boolean-count": b'{"count": true, "last_hash": "' + b"a" * 64 + b'"}\n',
    "negative-count": b'{"count": -1, "last_hash": "' + b"a" * 64 + b'"}\n',
    "float-count": b'{"count": 1.0, "last_hash": "' + b"a" * 64 + b'"}\n',
    "duplicate-keys": b'{"count": 3, "count": 3, "last_hash": "' + b"a" * 64 + b'"}\n',
    "missing-key": b'{"count": 3}\n',
    "unknown-key": b'{"count": 3, "last_hash": "' + b"a" * 64 + b'", "path": "x"}\n',
    "not-utf8": b'\xff\xfe{"count": 3, "last_hash": "' + b"a" * 64 + b'"}\n',
    "trailing-value": (
        b'{"count": 3, "last_hash": "' + b"a" * 64 + b'"}\n'
        b'{"count": 3, "last_hash": "' + b"a" * 64 + b'"}\n'
    ),
    "short-hash": b'{"count": 3, "last_hash": "' + b"a" * 63 + b'"}\n',
    "uppercase-hash": b'{"count": 3, "last_hash": "' + b"A" * 64 + b'"}\n',
    "non-hex-hash": b'{"count": 3, "last_hash": "' + b"g" * 64 + b'"}\n',
    "zero-count-with-a-hash": b'{"count": 0, "last_hash": "' + b"a" * 64 + b'"}\n',
    "hash-not-a-string": b'{"count": 3, "last_hash": 3}\n',
    "empty": b"",
}


class TestVerifyChainPair:
    """``verify_chain`` judges the log AND its sibling head anchor (D2 §2).

    A valid prefix used to be VALID: deleting the tail, or restoring an older copy of the
    file, left nothing to disagree with (D1 Gap A). The anchor carries the expected count
    and head hash; the chain is validated first so its precise first-line failure still
    wins, then the pair. Verification stays read-only and lock-free, and reaches both
    leaves descriptor-relative and no-follow.
    """

    @pytest.mark.parametrize(
        ("log_kind", "anchor_kind", "state", "fragments"),
        [
            ("absent", "absent", ChainState.ABSENT, ("no audit log",)),
            ("absent", "malformed", ChainState.BROKEN, ("head anchor unreadable", "absent")),
            ("absent", "matches", ChainState.BROKEN, ("truncation/deletion",)),
            ("absent", "ahead", ChainState.BROKEN, ("truncation/deletion",)),
            ("empty", "absent", ChainState.BROKEN, ("owner bootstrap required",)),
            ("empty", "malformed", ChainState.BROKEN, ("head anchor unreadable",)),
            ("empty", "matches", ChainState.VALID, ("chain + anchor intact (0 records)",)),
            (
                "empty",
                "ahead",
                ChainState.BROKEN,
                ("truncation/rollback", "0 records but anchor expects 1"),
            ),
            ("valid", "absent", ChainState.BROKEN, ("owner bootstrap required",)),
            ("valid", "malformed", ChainState.BROKEN, ("head anchor unreadable",)),
            ("valid", "matches", ChainState.VALID, ("chain + anchor intact (3 records)",)),
            (
                "valid",
                "behind",
                ChainState.BROKEN,
                ("crash window", "3 records but anchor expects 2"),
            ),
            (
                "valid",
                "ahead",
                ChainState.BROKEN,
                ("truncation/rollback", "3 records but anchor expects 4"),
            ),
            ("valid", "wrong-hash", ChainState.BROKEN, ("head hash mismatch",)),
            ("broken", "absent", ChainState.BROKEN, ("line 2", "hash mismatch")),
            ("broken", "matches", ChainState.BROKEN, ("line 2", "hash mismatch")),
            ("broken", "ahead", ChainState.BROKEN, ("line 2", "hash mismatch")),
        ],
    )
    def test_verify_chain_log_anchor_matrix(
        self,
        tmp_path: Path,
        log_kind: str,
        anchor_kind: str,
        state: ChainState,
        fragments: tuple[str, ...],
    ) -> None:
        path = _materialize_pair(tmp_path, log_kind, anchor_kind)
        result = verify_chain(path)
        assert result.state is state, result.detail
        for fragment in fragments:
            assert fragment in result.detail, result.detail

    @pytest.mark.parametrize("shape", sorted(_MALFORMED_ANCHORS))
    def test_every_malformed_anchor_shape_is_broken_not_ignored(
        self, tmp_path: Path, shape: str
    ) -> None:
        path = tmp_path / "audit.jsonl"
        write_records(path, 3)
        _anchor_path(path).write_bytes(_MALFORMED_ANCHORS[shape])
        result = verify_chain(path)
        assert result.state is ChainState.BROKEN, result.detail
        assert "head anchor unreadable" in result.detail, result.detail

    def test_the_writer_publishes_exact_anchor_bytes_the_verifier_accepts(
        self, tmp_path: Path
    ) -> None:
        # The positive control for the matrix: what the writer publishes is the "matches" cell.
        path = tmp_path / "audit.jsonl"
        write_records(path, 3)
        assert _anchor_path(path).read_bytes() == _anchor_bytes(3, _record_hashes(path)[-1])
        result = verify_chain(path)
        assert result.state is ChainState.VALID, result.detail

    def test_verify_refuses_symlinked_log_and_anchor_without_touching_target(
        self, tmp_path: Path
    ) -> None:
        # (a) The log is a symlink to a VALID paired chain, with a byte-exact anchor planted
        # under the link's own name, so a verifier that followed the link would say VALID.
        victim = tmp_path / "victim.jsonl"
        write_records(victim, 2)
        victim_bytes = victim.read_bytes()
        victim_anchor_bytes = _anchor_path(victim).read_bytes()
        linked = tmp_path / "audit.jsonl"
        linked.symlink_to(victim)
        _anchor_path(linked).write_bytes(victim_anchor_bytes)
        result = verify_chain(linked)
        assert result.state is ChainState.BROKEN, result.detail
        assert "is a symlink" in result.detail, result.detail
        assert linked.is_symlink() and victim.read_bytes() == victim_bytes

        # (b) A real log whose anchor is a symlink to a byte-exact valid anchor elsewhere.
        real = tmp_path / "real.jsonl"
        write_records(real, 2)
        real_anchor = _anchor_path(real)
        real_anchor_bytes = real_anchor.read_bytes()
        decoy = tmp_path / "decoy.head.json"
        decoy.write_bytes(real_anchor_bytes)
        real_anchor.unlink()
        real_anchor.symlink_to(decoy)
        result = verify_chain(real)
        assert result.state is ChainState.BROKEN, result.detail
        assert "is a symlink" in result.detail, result.detail
        assert real_anchor.is_symlink() and decoy.read_bytes() == real_anchor_bytes

        # (c) A hard-linked anchor and (d) a FIFO at the anchor path: neither is a regular,
        # single-link file; the FIFO must be refused without hanging.
        real_anchor.unlink()
        real_anchor.write_bytes(decoy.read_bytes())
        os.link(real_anchor, tmp_path / "alias.head.json")
        result = verify_chain(real)
        assert result.state is ChainState.BROKEN, result.detail
        assert "links" in result.detail, result.detail
        (tmp_path / "alias.head.json").unlink()
        real_anchor.unlink()
        os.mkfifo(real_anchor, mode=0o600)
        result = verify_chain(real)
        assert result.state is ChainState.BROKEN, result.detail
        assert "is not a regular file" in result.detail, result.detail

        # (e) A parent directory reached through a symlink.
        real_dir = tmp_path / "realdir"
        real_dir.mkdir()
        write_records(real_dir / "audit.jsonl", 1)
        link_dir = tmp_path / "linkdir"
        link_dir.symlink_to(real_dir, target_is_directory=True)
        result = verify_chain(link_dir / "audit.jsonl")
        assert result.state is ChainState.BROKEN, result.detail
        assert "is a symlink or not a real directory" in result.detail, result.detail

    def test_verify_creates_nothing_and_takes_no_lock(self, tmp_path: Path) -> None:
        # Read-only by contract: monitoring and campaign status snapshot the directory.
        path = tmp_path / "audit.jsonl"
        write_records(path, 2)
        _lock_path(path).unlink()
        before = sorted(p.name for p in tmp_path.iterdir())
        assert verify_chain(path).state is ChainState.VALID
        assert sorted(p.name for p in tmp_path.iterdir()) == before


class TestHeadAnchor:
    """Every append publishes ``<stem>.head.json`` = exact ``{"count", "last_hash"}`` bytes (D2 §1).

    Published through a unique same-directory temp opened ``O_EXCL`` at 0600, fsynced, renamed
    over the anchor with directory descriptors, then a parent fsync — after the log fsync and
    before the cache update and unlock, with the name-space binding re-established both
    before the anchor is touched and after it is durable. The anchor and its temp are
    capability entries like the log and the lock: a symlink, FIFO, hard link or swap is
    refused without touching its target, and only the unpublished temp is ever removed.
    """

    def test_append_publishes_exact_private_anchor_after_log_fsync_before_unlock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import fcntl

        from chronos.auditlog import log as log_module

        path = tmp_path / "audit.jsonl"
        anchor = _anchor_path(path)
        log = AuditLog(path)
        log.append("first", {"n": 1})  # a prior anchor now exists; the next append is observed

        events: list[str] = []
        replacements: list[tuple[str, str, int | None, int | None]] = []
        real_flock, real_fsync, real_replace = fcntl.flock, os.fsync, os.replace
        real_bind = log_module.AuditLog._assert_transaction_bound

        def flock(descriptor: int, operation: int) -> None:
            if operation == fcntl.LOCK_UN:
                events.append("unlock")
            real_flock(descriptor, operation)

        def fsync(descriptor: int) -> None:
            kind = "dir" if stat.S_ISDIR(os.fstat(descriptor).st_mode) else "file"
            events.append(f"{kind} fsync")
            real_fsync(descriptor)

        def replace(
            source: str,
            destination: str,
            *,
            src_dir_fd: int | None = None,
            dst_dir_fd: int | None = None,
        ) -> None:
            replacements.append((source, destination, src_dir_fd, dst_dir_fd))
            events.append("rename")
            real_replace(source, destination, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

        def bind(self: log_module.AuditLog, *args: object, when: str, **kwargs: object) -> None:
            events.append(f"bind: {when}")
            real_bind(self, *args, when=when, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(log_module.fcntl, "flock", flock)
        monkeypatch.setattr(log_module.os, "fsync", fsync)
        monkeypatch.setattr(log_module.os, "replace", replace)
        monkeypatch.setattr(log_module.AuditLog, "_assert_transaction_bound", bind)

        record = log.append("second", {"n": 2})

        # Exact bytes, private mode, no leftover temp.
        assert anchor.read_bytes() == _anchor_bytes(2, record.record_hash)
        assert stat.S_IMODE(anchor.lstat().st_mode) == 0o600
        assert not list(tmp_path.glob(".*.tmp"))
        # Atomic replace through the held parent descriptor, from a unique temp name.
        assert len(replacements) == 1, replacements
        source, destination, source_dir, destination_dir = replacements[0]
        assert source.startswith(".audit.head.json.") and source.endswith(".tmp"), source
        assert destination == "audit.head.json"
        assert source_dir is not None and source_dir == destination_dir
        # Order: log fsync < pre-anchor binding < temp fsync < rename < dir fsync <
        # post-anchor binding < unlock.
        file_fsyncs = [index for index, event in enumerate(events) if event == "file fsync"]
        assert len(file_fsyncs) == 2, events  # the log, then the anchor temp
        log_fsync, temp_fsync = file_fsyncs
        pre = events.index("bind: before the anchor")
        rename = events.index("rename")
        dir_fsync = events.index("dir fsync")
        post = events.index("bind: after the anchor")
        unlock = events.index("unlock")
        assert log_fsync < pre < temp_fsync < rename < dir_fsync < post < unlock, events
        assert events.count("unlock") == 1 and events.count("dir fsync") == 1, events

    def test_pre_anchor_binding_call_is_required(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A name swap right after the LOG fsync must refuse BEFORE the anchor is touched.

        The post-anchor call would also refuse — but only after publishing an anchor
        beside a log the canonical name no longer designates. So this pins WHERE the
        refusal happens: no rename, the prior anchor byte-identical, no temp left.
        """

        from chronos.auditlog import log as log_module

        path = tmp_path / "audit.jsonl"
        anchor = _anchor_path(path)
        log = AuditLog(path)
        log.append("first", {"n": 1})
        before_log, before_anchor = path.read_bytes(), anchor.read_bytes()
        real_fsync, real_replace = os.fsync, os.replace
        swapped = False
        renames: list[tuple[str, str]] = []

        def fsync_then_swap_the_log(descriptor: int) -> None:
            nonlocal swapped
            real_fsync(descriptor)
            if not swapped and stat.S_ISREG(os.fstat(descriptor).st_mode):
                swapped = True
                swap = tmp_path / "swap.jsonl"
                swap.write_bytes(before_log)
                real_replace(swap, path)  # the held log descriptor now points at an unnamed inode

        def replace(source: str, destination: str, **kwargs: object) -> None:
            renames.append((source, destination))
            real_replace(source, destination, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(log_module.os, "fsync", fsync_then_swap_the_log)
        monkeypatch.setattr(log_module.os, "replace", replace)
        with pytest.raises(AuditLogCorruptionError, match="replaced"):
            log.append("second", {"n": 2})
        assert swapped, "the swap never fired; the probe proved nothing"
        assert renames == [], "the anchor was renamed into place after the swap"
        assert anchor.read_bytes() == before_anchor
        assert not list(tmp_path.glob(".*.tmp"))

    def test_post_anchor_binding_call_is_required(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A swap AFTER the anchor is durable (after the parent fsync) can only be caught by
        the final binding call; without it the writer returns success for a pair the
        canonical names no longer designate."""

        from chronos.auditlog import log as log_module

        path = tmp_path / "audit.jsonl"
        log = AuditLog(path)
        log.append("first", {"n": 1})
        before_log = path.read_bytes()
        real_fsync = os.fsync
        swapped = False

        def fsync_then_swap_after_the_directory(descriptor: int) -> None:
            nonlocal swapped
            real_fsync(descriptor)
            if not swapped and stat.S_ISDIR(os.fstat(descriptor).st_mode):
                swapped = True
                swap = tmp_path / "swap.jsonl"
                swap.write_bytes(before_log)
                os.replace(swap, path)

        monkeypatch.setattr(log_module.os, "fsync", fsync_then_swap_after_the_directory)
        with pytest.raises(AuditLogCorruptionError) as info:
            log.append("second", {"n": 2})
        assert swapped, "the swap never fired; the probe proved nothing"
        message = str(info.value)
        assert "replaced" in message and "durable" in message and "NOT reported" in message
        # What the canonical names now designate: the pre-append log beside the new anchor.
        assert _sequences(path) == [0]
        result = verify_chain(path)
        assert result.state is ChainState.BROKEN
        assert "truncation" in result.detail, result.detail

    @pytest.mark.parametrize(
        "case",
        [
            "anchor-symlink",
            "anchor-hard-link",
            "anchor-fifo",
            "temp-symlink",
            "write",
            "fsync",
            "rename",
        ],
    )
    def test_anchor_publish_refuses_symlink_hardlink_fifo_and_cleans_its_unique_temp(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
    ) -> None:
        from chronos.auditlog import log as log_module

        path = tmp_path / "audit.jsonl"
        anchor = _anchor_path(path)
        log = AuditLog(path)
        log.append("first", {"n": 1})
        before_log, before_anchor = path.read_bytes(), anchor.read_bytes()
        victim = tmp_path / "victim.head.json"
        victim.write_bytes(before_anchor)  # byte-exact: a follower would accept it
        fixed_temp = f".{anchor.name}.{'f' * 32}.tmp"

        if case == "anchor-symlink":
            anchor.unlink()
            anchor.symlink_to(victim)
        elif case == "anchor-hard-link":
            os.link(anchor, tmp_path / "alias.head.json")
        elif case == "anchor-fifo":
            anchor.unlink()
            os.mkfifo(anchor, mode=0o600)
        elif case == "temp-symlink":
            monkeypatch.setattr(
                log_module.uuid, "uuid4", lambda: type("U", (), {"hex": "f" * 32})()
            )
            (tmp_path / fixed_temp).symlink_to(victim)
        elif case == "write":
            monkeypatch.setattr(
                log_module.os, "write", lambda *a, **k: (_ for _ in ()).throw(OSError("no write"))
            )
        elif case == "fsync":
            real_fsync = os.fsync
            file_fsyncs = 0

            def fail_the_temp_fsync(descriptor: int) -> None:
                nonlocal file_fsyncs
                if stat.S_ISREG(os.fstat(descriptor).st_mode):
                    file_fsyncs += 1
                    if file_fsyncs == 2:  # the log's fsync succeeded; the anchor temp's fails
                        raise OSError("temp fsync failed")
                real_fsync(descriptor)

            monkeypatch.setattr(log_module.os, "fsync", fail_the_temp_fsync)
        else:
            assert case == "rename"
            monkeypatch.setattr(
                log_module.os,
                "replace",
                lambda *a, **k: (_ for _ in ()).throw(OSError("no rename")),
            )

        with pytest.raises((AuditLogCorruptionError, OSError)) as info:
            log.append("second", {"n": 2})

        if case == "anchor-symlink":
            assert "is a symlink" in str(info.value)
            assert anchor.is_symlink() and victim.read_bytes() == before_anchor
            assert path.read_bytes() == before_log, "the log was extended beside an unsafe anchor"
        elif case == "anchor-hard-link":
            assert "links" in str(info.value)
            assert anchor.read_bytes() == before_anchor
            assert path.read_bytes() == before_log
        elif case == "anchor-fifo":
            assert "is not a regular file" in str(info.value)
            assert stat.S_ISFIFO(anchor.lstat().st_mode)
            assert path.read_bytes() == before_log
        elif case == "temp-symlink":
            # We did not create that entry, so it is not ours to remove; the victim is untouched.
            assert (tmp_path / fixed_temp).is_symlink() and victim.read_bytes() == before_anchor
            assert anchor.read_bytes() == before_anchor
        else:
            assert anchor.read_bytes() == before_anchor, "a failed publication changed the anchor"
        # In every case: no stray temp of ours, and the prior anchor still names the prior head.
        assert not [
            p for p in tmp_path.iterdir() if p.name.endswith(".tmp") and p.name != fixed_temp
        ]

    def test_crash_after_log_fsync_before_anchor_publish_is_broken(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from chronos.auditlog import log as log_module

        path = tmp_path / "audit.jsonl"
        anchor = _anchor_path(path)
        log = AuditLog(path)
        log.append("first", {"n": 1})
        before_anchor = anchor.read_bytes()

        def crash(*args: object, **kwargs: object) -> None:
            raise OSError("simulated crash before the anchor replace")

        monkeypatch.setattr(log_module.os, "replace", crash)
        with pytest.raises(OSError, match="simulated crash"):
            log.append("second", {"n": 2})
        monkeypatch.undo()

        # The record is durable in the log; the anchor still names the previous head.
        assert _sequences(path) == [0, 1]
        assert anchor.read_bytes() == before_anchor
        assert not list(tmp_path.glob(".*.tmp"))
        result = verify_chain(path)
        assert result.state is ChainState.BROKEN
        assert "crash window" in result.detail, result.detail
        assert "2 records but anchor expects 1" in result.detail, result.detail
        # No writer may extend past it: reviewed recovery, not silent repair.
        with pytest.raises(AuditLogCorruptionError, match="crash window"):
            AuditLog(path)
        assert _sequences(path) == [0, 1]

    def test_existing_bare_log_is_not_auto_anchored(self, tmp_path: Path) -> None:
        """A valid legacy log with no anchor is BROKEN until the owner bootstraps it (D2 §3).

        Creating the anchor automatically would certify whatever the file happens to hold —
        including an already rolled-back copy — so construction and append refuse and
        leave the file byte-identical.
        """

        path = tmp_path / "audit.jsonl"
        write_records(path, 3)
        _anchor_path(path).unlink(missing_ok=True)  # the shape a pre-anchor deployment left behind
        before = path.read_bytes()

        with pytest.raises(AuditLogCorruptionError, match="owner bootstrap required"):
            AuditLog(path)
        assert path.read_bytes() == before
        assert not _anchor_path(path).exists()
        assert not list(tmp_path.glob(".*.tmp"))
        result = verify_chain(path)
        assert result.state is ChainState.BROKEN
        assert "owner bootstrap required" in result.detail, result.detail

    def test_first_append_creates_the_pair_only_when_both_are_absent(self, tmp_path: Path) -> None:
        path = tmp_path / "audit.jsonl"
        anchor = _anchor_path(path)
        # An anchor with no log is deletion/truncation of the log, never a fresh start.
        anchor.write_bytes(_anchor_bytes(1, "a" * 64))
        with pytest.raises(AuditLogCorruptionError, match="truncation/deletion"):
            AuditLog(path)
        assert not path.exists(), "a log was created beside an orphaned anchor"
        anchor.unlink()
        record = AuditLog(path).append("first", {"n": 1})
        assert record.sequence == 0
        assert anchor.read_bytes() == _anchor_bytes(1, record.record_hash)
        assert verify_chain(path).state is ChainState.VALID


class TestBootstrapAnchor:
    """``bootstrap_anchor`` is the one public addition: the owner's explicit, once-only
    publication of a first anchor for a legacy log, under the same lock and binding as an
    append, appending nothing (D2 §3)."""

    def test_bootstrap_is_exported_runs_under_the_lock_and_binds_around_publication(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import fcntl

        from chronos.auditlog import bootstrap_anchor
        from chronos.auditlog import log as log_module

        path = tmp_path / "audit.jsonl"
        write_records(path, 3)
        anchor = _anchor_path(path)
        anchor.unlink()
        before = path.read_bytes()
        hashes = _record_hashes(path)

        events: list[str] = []
        real_flock, real_fsync, real_replace = fcntl.flock, os.fsync, os.replace
        real_bind = log_module.AuditLog._assert_transaction_bound

        def flock(descriptor: int, operation: int) -> None:
            events.append({fcntl.LOCK_EX: "lock", fcntl.LOCK_UN: "unlock"}.get(operation, "flock?"))
            real_flock(descriptor, operation)

        def fsync(descriptor: int) -> None:
            kind = "dir" if stat.S_ISDIR(os.fstat(descriptor).st_mode) else "file"
            events.append(f"{kind} fsync")
            real_fsync(descriptor)

        def replace(source: str, destination: str, **kwargs: object) -> None:
            events.append("rename")
            real_replace(source, destination, **kwargs)  # type: ignore[arg-type]

        def bind(self: log_module.AuditLog, *args: object, when: str, **kwargs: object) -> None:
            events.append(f"bind: {when}")
            real_bind(self, *args, when=when, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(log_module.fcntl, "flock", flock)
        monkeypatch.setattr(log_module.os, "fsync", fsync)
        monkeypatch.setattr(log_module.os, "replace", replace)
        monkeypatch.setattr(log_module.AuditLog, "_assert_transaction_bound", bind)

        published = bootstrap_anchor(path)

        assert published == anchor
        assert anchor.read_bytes() == _anchor_bytes(3, hashes[-1])
        assert stat.S_IMODE(anchor.lstat().st_mode) == 0o600
        assert path.read_bytes() == before, "bootstrap appended a record"
        assert not list(tmp_path.glob(".*.tmp"))
        order = [
            events.index(name)
            for name in (
                "lock",
                "bind: before the anchor",
                "file fsync",
                "rename",
                "dir fsync",
                "bind: after the anchor",
                "unlock",
            )
        ]
        assert order == sorted(order), events
        assert events.count("file fsync") == 1 and events.count("unlock") == 1, events
        assert verify_chain(path).state is ChainState.VALID
        assert AuditLog(path).append("after_bootstrap", {"n": 3}).sequence == 3

    def test_bootstrap_returns_none_only_for_an_absent_log(self, tmp_path: Path) -> None:
        from chronos.auditlog import bootstrap_anchor

        path = tmp_path / "audit.jsonl"
        anchor = _anchor_path(path)
        assert bootstrap_anchor(path) is None
        assert not path.exists() and not anchor.exists()
        # An anchor with no log is a truncation/deletion state, never "absent": refused, and
        # neither entry is created or touched.
        anchor.write_bytes(_anchor_bytes(1, "a" * 64))
        with pytest.raises(AuditLogCorruptionError, match="already exists"):
            bootstrap_anchor(path)
        assert not path.exists()
        assert anchor.read_bytes() == _anchor_bytes(1, "a" * 64)
