"""M3 ops plane, W-1: the evidence-only watchdog and the dead-man check over its heartbeat.

Numbered to the packet contract. 1x pins ``chronos.operations.watchdog`` (contract 1), 2x pins
``chronos.operations.deadman`` (contract 2), 3x the two CLIs and default-off (contract 3), 4x the
runbook (contract 4). Every HTTP call goes through ``httpx.MockTransport``; every clock is a fake;
every file lives under ``tmp_path``. Nothing here — and nothing in the modules — halts, kills or
restarts anything: 1f pins the import graph in both directions.
"""

from __future__ import annotations

import ast
import json
import os
import stat
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from chronos.operations import deadman as deadman_module
from chronos.operations import watchdog as watchdog_module
from chronos.operations.deadman import (
    DeadmanConfigurationError,
    DeadmanState,
    check_deadman,
)
from chronos.operations.deadman import main as deadman_main
from chronos.operations.external_probe import probe_external_health
from chronos.operations.watchdog import (
    EVIDENCE_LOG,
    HEARTBEAT,
    Watchdog,
    WatchdogConfigurationError,
    WatchdogEvidenceError,
    WatchdogState,
)
from chronos.operations.watchdog import main as watchdog_main

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "chronos"
NOW = datetime(2026, 9, 14, 22, 0, tzinfo=UTC)
AUTHORITY_PACKAGES = (
    "chronos.control",
    "chronos.orders",
    "chronos.execution",
    "chronos.autonomy",
    "chronos.supervisor",
    "chronos.portfolio",
    "chronos.risk",
    "chronos.broker",
    "chronos.services",
    "chronos.runtime",
)
SAFE_ENV = {
    **os.environ,
    "BROKER_MODE": "demo",
    "ALLOW_ORDER_TRANSMIT": "false",
    "ALLOW_LIVE_TRADING": "false",
    "PYTHONDONTWRITEBYTECODE": "1",
}


class _Clock:
    """A wall clock and a monotonic timer that move together unless a test moves one alone."""

    def __init__(self, wall: datetime = NOW, monotonic: float = 5_000.0) -> None:
        self.wall = wall
        self.mono = monotonic

    def now(self) -> datetime:
        return self.wall

    def monotonic(self) -> float:
        return self.mono

    def advance(self, seconds: float) -> None:
        self.wall += timedelta(seconds=seconds)
        self.mono += seconds


def _probe(clock: _Clock, backend: dict[str, bool]) -> Callable[[], object]:
    """A probe bound to a MockTransport whose answers follow the mutable ``backend`` flags."""

    def handler(request: httpx.Request) -> httpx.Response:
        key = "live" if request.url.path.endswith("/live") else "ready"
        return httpx.Response(200 if backend[key] else 503)

    transport = httpx.MockTransport(handler)
    return lambda: probe_external_health(
        "http://backend.test", transport=transport, clock=clock.now, timer=clock.monotonic
    )


def _watchdog(
    tmp_path: Path,
    clock: _Clock,
    backend: dict[str, bool],
    *,
    interval: float = 10.0,
    deadline: float = 90.0,
) -> Watchdog:
    return Watchdog(
        probe=_probe(clock, backend),  # type: ignore[arg-type]
        interval_s=interval,
        deadline_s=deadline,
        evidence_dir=tmp_path / "ops",
        clock=clock.now,
        timer=clock.monotonic,
        sleep=clock.advance,
        pid=4242,
    )


def _lines(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _heartbeat(path: Path, clock: _Clock, *, age_s: float = 0.0) -> None:
    """Write a heartbeat as the watchdog would have ``age_s`` seconds ago on ``clock``."""

    observed = clock.wall - timedelta(seconds=age_s)
    document = {
        "last_healthy_at": observed.isoformat(),
        "last_observed_at": observed.isoformat(),
        "monotonic": clock.mono - age_s,
        "pid": 4242,
        "version": "0.1.0",
        "verdict": {"state": "HEALTHY", "reason": "test", "since": None, "evidence_path": "x"},
    }
    path.write_text(json.dumps(document), encoding="utf-8")


# ------------------------------------------------------------------ 1. the watchdog


def test_1a_no_trip_before_the_deadline_and_a_trip_exactly_at_it(tmp_path: Path) -> None:
    clock = _Clock()
    backend = {"live": True, "ready": True}
    dog = _watchdog(tmp_path, clock, backend)
    assert dog.tick().state is WatchdogState.HEALTHY
    backend["ready"] = False  # the backend stops being ready; the deadline clock starts here
    clock.advance(80.0)
    before = dog.tick()
    assert before.state is WatchdogState.HEALTHY, before.reason
    clock.advance(9.999)
    assert dog.tick().state is WatchdogState.HEALTHY
    clock.advance(0.001)  # exactly deadline_s since the last HEALTHY observation
    tripped = dog.tick()
    assert tripped.state is WatchdogState.TRIPPED
    assert "deadline 90.000 s" in tripped.reason
    assert tripped.since == NOW  # the last HEALTHY observation
    assert tripped.evidence_path == str(tmp_path / "ops" / EVIDENCE_LOG)


def test_1a2_never_healthy_counts_from_the_first_tick(tmp_path: Path) -> None:
    clock = _Clock()
    dog = _watchdog(tmp_path, clock, {"live": False, "ready": False}, deadline=30.0)
    assert dog.tick().state is WatchdogState.HEALTHY  # silent for 0 s so far
    clock.advance(30.0)
    verdict = dog.tick()
    assert verdict.state is WatchdogState.TRIPPED
    assert verdict.since is None
    assert "never" in verdict.reason


def test_1b_a_healthy_observation_resets_the_deadline(tmp_path: Path) -> None:
    clock = _Clock()
    backend = {"live": True, "ready": False}
    dog = _watchdog(tmp_path, clock, backend)
    assert dog.tick().state is WatchdogState.HEALTHY  # never HEALTHY yet: counted from this tick
    clock.advance(90.0)
    assert dog.tick().state is WatchdogState.TRIPPED
    backend["ready"] = True
    clock.advance(10.0)
    assert dog.tick().state is WatchdogState.HEALTHY
    backend["ready"] = False
    clock.advance(89.0)
    assert dog.tick().state is WatchdogState.HEALTHY
    clock.advance(1.0)
    assert dog.tick().state is WatchdogState.TRIPPED


def test_1c_a_backwards_wall_clock_jump_never_untrips(tmp_path: Path) -> None:
    clock = _Clock()
    backend = {"live": True, "ready": True}
    dog = _watchdog(tmp_path, clock, backend)
    dog.tick()
    backend["live"] = False
    clock.advance(90.0)
    assert dog.tick().state is WatchdogState.TRIPPED
    clock.wall -= timedelta(hours=1)  # NTP step backwards; the monotonic timer keeps going
    clock.mono += 10.0
    verdict = dog.tick()
    assert verdict.state is WatchdogState.TRIPPED
    assert verdict.since == NOW
    # and the evidence records the wall clock it saw, so the jump is visible after the fact
    last = _lines(tmp_path / "ops" / EVIDENCE_LOG)[-1]
    assert last["assessed_at"] == (NOW + timedelta(seconds=90) - timedelta(hours=1)).isoformat()


def test_1d_the_jsonl_is_append_only_and_every_line_parses(tmp_path: Path) -> None:
    clock = _Clock()
    backend = {"live": True, "ready": True}
    dog = _watchdog(tmp_path, clock, backend)
    log = tmp_path / "ops" / EVIDENCE_LOG
    for _ in range(3):
        dog.tick()
        clock.advance(10.0)
    before = log.read_bytes()
    backend["ready"] = False
    dog.tick()
    after = log.read_bytes()
    assert after.startswith(before) and after != before, "a tick must only append"
    lines = _lines(log)
    assert len(lines) == 4
    for line in lines:
        assert set(line) >= {"assessed_at", "state", "failure_code", "elapsed_ms", "monotonic"}
        assert line["state"] in {"HEALTHY", "UNHEALTHY", "UNKNOWN"}
    assert lines[-1]["state"] == "UNHEALTHY"
    assert lines[-1]["failure_code"] == "readiness_not_ready"
    assert lines[0]["failure_code"] is None
    assert all(stat.S_ISREG(log.lstat().st_mode) for _ in range(1))


def test_1e_heartbeat_is_replaced_atomically_and_a_planted_symlink_is_refused(
    tmp_path: Path,
) -> None:
    clock = _Clock()
    backend = {"live": True, "ready": True}
    dog = _watchdog(tmp_path, clock, backend)
    ops = tmp_path / "ops"
    heartbeat = ops / HEARTBEAT
    dog.tick()
    first = json.loads(heartbeat.read_text(encoding="utf-8"))
    assert set(first) == {
        "last_healthy_at",
        "last_observed_at",
        "monotonic",
        "pid",
        "version",
        "verdict",
    }
    assert first["pid"] == 4242 and first["monotonic"] == 5_000.0
    assert first["last_healthy_at"] == NOW.isoformat() == first["last_observed_at"]
    assert first["verdict"]["state"] == "HEALTHY"
    assert [p.name for p in ops.iterdir() if p.name not in {HEARTBEAT, EVIDENCE_LOG}] == [], (
        "no temp file may survive a tick"
    )
    clock.advance(10.0)
    backend["ready"] = False
    dog.tick()
    second = json.loads(heartbeat.read_text(encoding="utf-8"))
    assert second["last_healthy_at"] == NOW.isoformat()
    assert second["last_observed_at"] == (NOW + timedelta(seconds=10)).isoformat()
    assert stat.S_ISREG(heartbeat.lstat().st_mode)
    # a planted symlink at the heartbeat path: refused, typed, and the target never written
    sentinel = tmp_path / "sentinel.json"
    sentinel.write_text("SENTINEL", encoding="utf-8")
    heartbeat.unlink()
    heartbeat.symlink_to(sentinel)
    clock.advance(10.0)
    with pytest.raises(WatchdogEvidenceError, match="is not a regular file"):
        dog.tick()
    assert heartbeat.is_symlink()
    assert sentinel.read_text(encoding="utf-8") == "SENTINEL"
    assert [p.name for p in ops.iterdir() if p.name not in {HEARTBEAT, EVIDENCE_LOG}] == []


def test_1e2_a_planted_symlink_at_the_evidence_log_is_refused_not_followed(tmp_path: Path) -> None:
    clock = _Clock()
    dog = _watchdog(tmp_path, clock, {"live": True, "ready": True})
    ops = tmp_path / "ops"
    sentinel = tmp_path / "sentinel.jsonl"
    sentinel.write_text("SENTINEL\n", encoding="utf-8")
    (ops / EVIDENCE_LOG).symlink_to(sentinel)
    with pytest.raises(WatchdogEvidenceError, match="symlink"):
        dog.tick()
    assert sentinel.read_text(encoding="utf-8") == "SENTINEL\n"


def test_1e3_a_fifo_at_the_evidence_log_is_refused_within_a_second(tmp_path: Path) -> None:
    clock = _Clock()
    dog = _watchdog(tmp_path, clock, {"live": True, "ready": True})
    os.mkfifo(tmp_path / "ops" / EVIDENCE_LOG)
    started = time.monotonic()
    with pytest.raises(WatchdogEvidenceError, match=r"fifo|not a regular file"):
        dog.tick()
    assert time.monotonic() - started < 1.0


def test_1f_import_graph_the_watchdog_and_deadman_touch_no_authority_module() -> None:
    for module in (watchdog_module, deadman_module):
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))  # type: ignore[arg-type]
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module, *(f"{node.module}.{a.name}" for a in node.names)]
            for name in names:
                assert not any(
                    name == pkg or name.startswith(f"{pkg}.") for pkg in AUTHORITY_PACKAGES
                ), f"{module.__name__} imports {name}"
    # and the reverse: no authority module may reach INTO the watch layers (the operations
    # boundary of tests/safety/test_operational_health_boundary.py, extended to the two modules)
    forbidden_dirs = [
        SRC / p.split(".", 1)[1] for p in AUTHORITY_PACKAGES if p != "chronos.runtime"
    ]
    candidates = [SRC / "runtime.py", *(f for d in forbidden_dirs for f in d.rglob("*.py"))]
    for path in candidates:
        text = path.read_text(encoding="utf-8")
        assert "operations.watchdog" not in text and "operations.deadman" not in text, path


def test_1g_configuration_is_validated_typed() -> None:
    clock = _Clock()
    for bad in (0.0, -1.0, float("nan"), float("inf"), True):
        with pytest.raises(WatchdogConfigurationError, match="positive finite"):
            Watchdog(
                probe=lambda: None,  # type: ignore[arg-type,return-value]
                interval_s=bad,  # type: ignore[arg-type]
                deadline_s=90.0,
                evidence_dir=Path("/nonexistent/never-created"),
                clock=clock.now,
                timer=clock.monotonic,
            )


# ------------------------------------------------------------------ 2. the dead-man check


def test_2a_a_fresh_heartbeat_is_alive(tmp_path: Path) -> None:
    clock = _Clock()
    path = tmp_path / HEARTBEAT
    _heartbeat(path, clock, age_s=30.0)
    verdict = check_deadman(path, 180.0, clock=clock.now, timer=clock.monotonic)
    assert verdict.state is DeadmanState.ALIVE
    assert verdict.heartbeat_age_s == 30.0


def test_2b_a_heartbeat_stale_by_both_clocks_is_dead(tmp_path: Path) -> None:
    clock = _Clock()
    path = tmp_path / HEARTBEAT
    _heartbeat(path, clock, age_s=181.0)
    verdict = check_deadman(path, 180.0, clock=clock.now, timer=clock.monotonic)
    assert verdict.state is DeadmanState.DEAD
    assert verdict.heartbeat_age_s == 181.0
    assert "181.0 s wall" in verdict.reason and "181.0 s monotonic" in verdict.reason


def test_2b2_exactly_max_age_is_still_alive(tmp_path: Path) -> None:
    clock = _Clock()
    path = tmp_path / HEARTBEAT
    _heartbeat(path, clock, age_s=180.0)
    assert check_deadman(path, 180.0, clock=clock.now, timer=clock.monotonic).state is (
        DeadmanState.ALIVE
    )


def test_2c_absent_unreadable_and_malformed_heartbeats_are_dead_typed(tmp_path: Path) -> None:
    clock = _Clock()
    path = tmp_path / HEARTBEAT
    absent = check_deadman(path, 180.0, clock=clock.now, timer=clock.monotonic)
    assert absent.state is DeadmanState.DEAD and "absent" in absent.reason
    assert absent.heartbeat_age_s is None
    for payload, needle in (
        ("not json", "not JSON"),
        ("[1, 2]", "not an object"),
        ('{"last_observed_at": "2026-09-14T22:00:00+00:00"}', "monotonic"),
        ('{"monotonic": 1.0}', "last_observed_at"),
        ('{"last_observed_at": "yesterday", "monotonic": 1.0}', "last_observed_at"),
        ('{"last_observed_at": "2026-09-14T22:00:00", "monotonic": 1.0}', "timezone"),
        ('{"last_observed_at": "2026-09-14T22:00:00+00:00", "monotonic": "1"}', "monotonic"),
        ('{"last_observed_at": "2026-09-14T22:00:00+00:00", "monotonic": NaN}', "monotonic"),
    ):
        path.write_text(payload, encoding="utf-8")
        verdict = check_deadman(path, 180.0, clock=clock.now, timer=clock.monotonic)
        assert verdict.state is DeadmanState.DEAD, payload
        assert "malformed" in verdict.reason and needle in verdict.reason, verdict.reason
    _heartbeat(path, clock)
    path.chmod(0o000)
    try:
        unreadable = check_deadman(path, 180.0, clock=clock.now, timer=clock.monotonic)
    finally:
        path.chmod(0o644)
    assert unreadable.state is DeadmanState.DEAD and "unreadable" in unreadable.reason


def test_2d_a_fifo_heartbeat_is_dead_typed_without_blocking(tmp_path: Path) -> None:
    clock = _Clock()
    path = tmp_path / HEARTBEAT
    os.mkfifo(path)
    started = time.monotonic()
    verdict = check_deadman(path, 180.0, clock=clock.now, timer=clock.monotonic)
    assert time.monotonic() - started < 1.0
    assert verdict.state is DeadmanState.DEAD
    assert "not a regular file" in verdict.reason
    directory = tmp_path / "dir-heartbeat"
    directory.mkdir()
    verdict = check_deadman(directory, 180.0, clock=clock.now, timer=clock.monotonic)
    assert verdict.state is DeadmanState.DEAD and "not a regular file" in verdict.reason


def test_2e_a_symlink_heartbeat_is_dead_even_when_its_target_is_fresh(tmp_path: Path) -> None:
    clock = _Clock()
    target = tmp_path / "fresh.json"
    _heartbeat(target, clock, age_s=1.0)
    link = tmp_path / HEARTBEAT
    link.symlink_to(target)
    verdict = check_deadman(link, 180.0, clock=clock.now, timer=clock.monotonic)
    assert verdict.state is DeadmanState.DEAD
    assert "symlink" in verdict.reason
    assert verdict.heartbeat_age_s is None


def test_2f_disagreeing_clocks_and_a_monotonic_from_another_boot_are_unknown(
    tmp_path: Path,
) -> None:
    clock = _Clock()
    path = tmp_path / HEARTBEAT
    _heartbeat(path, clock, age_s=30.0)
    clock.wall += timedelta(seconds=400)  # the wall clock stepped forward; the timer did not
    verdict = check_deadman(path, 180.0, clock=clock.now, timer=clock.monotonic)
    assert verdict.state is DeadmanState.UNKNOWN and "disagree" in verdict.reason
    clock = _Clock(monotonic=10.0)  # this process booted after the heartbeat's writer
    _heartbeat(path, clock, age_s=30.0)
    path.write_text(
        path.read_text(encoding="utf-8").replace('"monotonic": -20.0', '"monotonic": 500.0'),
        encoding="utf-8",
    )
    verdict = check_deadman(path, 180.0, clock=clock.now, timer=clock.monotonic)
    assert verdict.state is DeadmanState.UNKNOWN and "another boot" in verdict.reason
    clock = _Clock()
    _heartbeat(path, clock)
    clock.wall -= timedelta(seconds=60)  # a heartbeat from the future beyond tolerance
    verdict = check_deadman(path, 180.0, clock=clock.now, timer=clock.monotonic)
    assert verdict.state is DeadmanState.UNKNOWN and "future" in verdict.reason


def test_2g_configuration_is_validated_and_nothing_is_retained(tmp_path: Path) -> None:
    clock = _Clock()
    path = tmp_path / HEARTBEAT
    _heartbeat(path, clock)
    for bad in (0.0, -5.0, float("nan"), True):
        with pytest.raises(DeadmanConfigurationError, match="positive finite"):
            check_deadman(path, bad, clock=clock.now, timer=clock.monotonic)  # type: ignore[arg-type]
    listing = sorted(p.name for p in tmp_path.iterdir())
    check_deadman(path, 180.0, clock=clock.now, timer=clock.monotonic)
    assert sorted(p.name for p in tmp_path.iterdir()) == listing, "the check writes nothing"


# ------------------------------------------------------------------ 3. the CLIs, default-off


def test_3a_watchdog_once_records_one_observation_and_exits_by_what_it_saw(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    backend = {"live": True, "ready": True}

    def handler(request: httpx.Request) -> httpx.Response:
        key = "live" if request.url.path.endswith("/live") else "ready"
        return httpx.Response(200 if backend[key] else 503)

    ops = tmp_path / "ops"
    argv = [
        "--base-url",
        "http://backend.test",
        "--interval",
        "10",
        "--deadline",
        "90",
        "--evidence-dir",
        str(ops),
        "--once",
    ]
    assert watchdog_main(argv, transport=httpx.MockTransport(handler)) == 0
    printed = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert printed["state"] == "HEALTHY"
    assert len(_lines(ops / EVIDENCE_LOG)) == 1
    assert json.loads((ops / HEARTBEAT).read_text(encoding="utf-8"))["pid"] == os.getpid()
    backend["ready"] = False
    assert watchdog_main(argv, transport=httpx.MockTransport(handler)) == 1
    lines = _lines(ops / EVIDENCE_LOG)
    assert len(lines) == 2 and lines[-1]["state"] == "UNHEALTHY"


def test_3b_watchdog_cli_refuses_bad_configuration_typed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request may leave with a refused configuration")

    transport = httpx.MockTransport(handler)
    base = ["--base-url", "http://backend.test", "--evidence-dir", str(tmp_path / "ops"), "--once"]
    assert watchdog_main([*base, "--interval", "0"], transport=transport) == 2
    assert "interval_s" in capsys.readouterr().err
    assert watchdog_main([*base, "--deadline", "nan"], transport=transport) == 2
    assert "deadline_s" in capsys.readouterr().err
    bad_url = [
        "--base-url",
        "ftp://backend.test",
        "--evidence-dir",
        str(tmp_path / "ops"),
        "--once",
    ]
    assert watchdog_main(bad_url, transport=transport) == 2
    assert "configuration" in capsys.readouterr().err
    assert not (tmp_path / "ops" / EVIDENCE_LOG).exists()


def test_3c_deadman_cli_exit_codes_and_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    clock = _Clock(wall=datetime.now(tz=UTC), monotonic=time.monotonic())
    path = tmp_path / HEARTBEAT
    _heartbeat(path, clock, age_s=1.0)
    assert deadman_main(["--heartbeat", str(path), "--max-age", "180"]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "ALIVE"
    _heartbeat(path, clock, age_s=1_000.0)
    assert deadman_main(["--heartbeat", str(path), "--max-age", "180"]) == 2
    assert json.loads(capsys.readouterr().out)["state"] == "DEAD"
    assert deadman_main(["--heartbeat", str(path), "--max-age", "0"]) == 64
    assert "positive finite" in capsys.readouterr().err
    # the process-level pin: a FIFO through the real interpreter, under an outer timeout
    fifo = tmp_path / "fifo-heartbeat.json"
    os.mkfifo(fifo)
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "chronos.operations.deadman",
            "--heartbeat",
            str(fifo),
            "--max-age",
            "180",
        ],
        capture_output=True,
        text=True,
        timeout=5,
        env=SAFE_ENV,
        check=False,
    )
    assert completed.returncode == 2, completed.stderr
    assert json.loads(completed.stdout)["state"] == "DEAD"


def test_3d_nothing_in_the_repository_starts_either_layer() -> None:
    owners = {SRC / "operations" / "watchdog.py", SRC / "operations" / "deadman.py"}
    for path in SRC.rglob("*.py"):
        if path in owners:
            continue
        text = path.read_text(encoding="utf-8")
        assert "operations.watchdog" not in text and "operations.deadman" not in text, path
    for unit in (ROOT / "docs" / "ops").glob("*.service"):
        assert "watchdog" not in unit.read_text(encoding="utf-8").lower(), unit


# ------------------------------------------------------------------ 4. the runbook


def test_4a_the_runbook_names_the_files_the_limit_and_the_pointer() -> None:
    runbook = (ROOT / "docs" / "ops" / "WATCHDOG.md").read_text(encoding="utf-8")
    for needle in (
        "watchdog.jsonl",
        "heartbeat.json",
        "INCIDENT_RESPONSE.md",
        "dies with the host",
        "do not act",
        "TRIPPED",
        "DEAD",
        "UNKNOWN",
    ):
        assert needle in runbook, needle
    readme = (ROOT / "docs" / "ops" / "README.md").read_text(encoding="utf-8")
    assert "`WATCHDOG.md`" in readme
