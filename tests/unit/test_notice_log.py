"""REVIEW-TIDY P3-2: the notice window cannot be changed without the counter seeing it."""

from __future__ import annotations

import copy
import sys
import threading

import pytest

from chronos.broker.callbacks import NoticeLog


def _filled(count: int = 5, *, maxlen: int = 3) -> NoticeLog:
    log = NoticeLog(maxlen=maxlen)
    for index in range(count):
        log.append(f"[{index}] n{index}")
    return log


def test_append_counts_every_notice_even_after_the_window_evicts() -> None:
    log = _filled(5, maxlen=3)
    assert log.appended == 5
    assert list(log) == ["[2] n2", "[3] n3", "[4] n4"]
    assert len(log) == 3


def test_extend_counts_each_notice() -> None:
    log = _filled(1)
    log.extend(["[7] a", "[8] b"])
    assert log.appended == 3
    assert list(log)[-2:] == ["[7] a", "[8] b"]


@pytest.mark.parametrize("name", ["appendleft", "extendleft", "insert", "clear", "pop", "popleft"])
def test_no_method_can_change_the_window_around_the_counter(name: str) -> None:
    assert not hasattr(_filled(), name)


def test_the_window_reads_like_a_sequence_for_existing_readers() -> None:
    log = _filled(3, maxlen=5)
    assert tuple(log) == ("[0] n0", "[1] n1", "[2] n2")
    assert log[-1] == "[2] n2"
    assert list(log[-2:]) == ["[1] n1", "[2] n2"]
    assert any("n1" in note for note in log)
    assert bool(log) and not bool(NoticeLog())


@pytest.mark.parametrize("duplicate", [copy.copy, copy.deepcopy])
def test_a_copy_is_independent_and_keeps_the_count(duplicate: object) -> None:
    log = _filled(5, maxlen=3)
    twin = duplicate(log)  # type: ignore[operator]
    assert isinstance(twin, NoticeLog)
    assert twin.appended == 5 and list(twin) == list(log)
    twin.append("[9] only-in-twin")
    assert (twin.appended, log.appended) == (6, 5)
    assert "[9] only-in-twin" not in list(log)


def test_concurrent_appends_are_all_counted() -> None:
    log = NoticeLog(maxlen=10)
    previous = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)  # force frequent thread switches inside the append

    def work() -> None:
        for _ in range(2000):
            log.append("[1] x")

    try:
        threads = [threading.Thread(target=work) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    finally:
        sys.setswitchinterval(previous)
    assert log.appended == 16000
    assert len(log) == 10
