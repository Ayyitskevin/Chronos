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
    EXIT_CODES,
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
    boot_id: str = "boot-A",
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
        boot_id=boot_id,
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


def test_1a2_never_healthy_is_tripped_from_the_first_tick(tmp_path: Path) -> None:
    """r1 P1 (fail closed): a watchdog that has never seen HEALTHY has nothing to certify —
    it publishes TRIPPED on a non-HEALTHY tick, not a HEALTHY-until-the-deadline grace."""

    clock = _Clock()
    backend = {"live": False, "ready": False}
    dog = _watchdog(tmp_path, clock, backend, deadline=30.0)
    verdict = dog.tick()
    assert verdict.state is WatchdogState.TRIPPED
    assert verdict.since is None
    assert "ever" in verdict.reason
    heartbeat = json.loads((tmp_path / "ops" / HEARTBEAT).read_text(encoding="utf-8"))
    assert heartbeat["verdict"]["state"] == "TRIPPED" and heartbeat["last_healthy_at"] is None
    backend["live"] = backend["ready"] = True
    clock.advance(10.0)
    assert dog.tick().state is WatchdogState.HEALTHY


def test_1b_a_healthy_observation_resets_the_deadline(tmp_path: Path) -> None:
    clock = _Clock()
    backend = {"live": True, "ready": True}
    dog = _watchdog(tmp_path, clock, backend)
    assert dog.tick().state is WatchdogState.HEALTHY
    backend["ready"] = False
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
        "boot_id",
        "last_healthy_at",
        "last_healthy_monotonic",
        "last_observed_at",
        "monotonic",
        "pid",
        "version",
        "verdict",
    }
    assert first["boot_id"] == "boot-A" and first["last_healthy_monotonic"] == 5_000.0
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


# ------------------------------------------------------------------ r1 (Daybreak HOLD at 50ad133)


def _tripped_dir(tmp_path: Path, clock: _Clock, backend: dict[str, bool]) -> Watchdog:
    """A watchdog run to TRIPPED on tmp_path/ops (one HEALTHY tick, then 90 s of silence)."""

    backend["live"] = backend["ready"] = True
    dog = _watchdog(tmp_path, clock, backend)
    assert dog.tick().state is WatchdogState.HEALTHY
    backend["ready"] = False
    clock.advance(90.0)
    assert dog.tick().state is WatchdogState.TRIPPED
    dog.close()
    return dog


def test_r1_1a_a_restart_over_a_tripped_evidence_dir_stays_tripped_until_healthy(
    tmp_path: Path,
) -> None:
    """P1 restart: the reviewer's probe — at 50ad133 the second instance published HEALTHY."""

    clock = _Clock()
    backend: dict[str, bool] = {}
    _tripped_dir(tmp_path, clock, backend)
    clock.advance(10.0)
    restarted = _watchdog(tmp_path, clock, backend)  # same dir, peer still UNHEALTHY
    verdict = restarted.tick()
    assert verdict.state is WatchdogState.TRIPPED, verdict.reason
    assert "before this process started" in verdict.reason
    assert verdict.since == NOW  # the prior HEALTHY, carried over from the heartbeat
    heartbeat = json.loads((tmp_path / "ops" / HEARTBEAT).read_text(encoding="utf-8"))
    assert heartbeat["verdict"]["state"] == "TRIPPED"
    assert heartbeat["last_healthy_at"] == NOW.isoformat()
    backend["ready"] = True
    clock.advance(10.0)
    assert restarted.tick().state is WatchdogState.HEALTHY


def test_r1_1b_a_restart_with_a_recent_prior_healthy_restores_the_interval(tmp_path: Path) -> None:
    clock = _Clock()
    backend = {"live": True, "ready": True}
    first = _watchdog(tmp_path, clock, backend)
    first.tick()
    first.close()
    backend["ready"] = False
    clock.advance(60.0)  # the old process died; 60 s of the 90 s deadline already elapsed
    restarted = _watchdog(tmp_path, clock, backend)
    assert restarted.tick().state is WatchdogState.HEALTHY  # 60 s since HEALTHY, under 90
    clock.advance(29.0)
    assert restarted.tick().state is WatchdogState.HEALTHY
    clock.advance(1.0)  # 90 s since the prior HEALTHY, counted across the restart
    assert restarted.tick().state is WatchdogState.TRIPPED


def test_r1_1c_a_prior_heartbeat_whose_healthy_is_already_stale_starts_tripped(
    tmp_path: Path,
) -> None:
    clock = _Clock()
    ops = tmp_path / "ops"
    ops.mkdir()
    stale = {
        "last_healthy_at": (NOW - timedelta(seconds=500)).isoformat(),
        "last_observed_at": (NOW - timedelta(seconds=5)).isoformat(),
        "monotonic": 4_995.0,
        "pid": 1,
        "version": "0.1.0",
        "verdict": {"state": "HEALTHY", "reason": "lying", "since": None, "evidence_path": "x"},
    }
    path = ops / HEARTBEAT
    path.write_text(json.dumps(stale), encoding="utf-8")
    path.chmod(0o600)
    dog = _watchdog(tmp_path, clock, {"live": True, "ready": False})
    verdict = dog.tick()
    assert verdict.state is WatchdogState.TRIPPED
    assert verdict.since == NOW - timedelta(seconds=500)


def test_r1_2a_a_hardlinked_evidence_log_is_refused_and_the_other_name_untouched(
    tmp_path: Path,
) -> None:
    """P1 capability: at 50ad133 S_ISREG passed and the tick appended into the sentinel."""

    clock = _Clock()
    ops = tmp_path / "ops"
    ops.mkdir()
    sentinel = tmp_path / "sentinel.jsonl"
    sentinel.write_text("SENTINEL\n", encoding="utf-8")
    sentinel.chmod(0o600)
    os.link(sentinel, ops / EVIDENCE_LOG)
    dog = _watchdog(tmp_path, clock, {"live": True, "ready": True})
    with pytest.raises(WatchdogEvidenceError, match="2 links"):
        dog.tick()
    assert sentinel.read_text(encoding="utf-8") == "SENTINEL\n"
    assert not (ops / HEARTBEAT).exists(), "nothing is published after a refused append"


def test_r1_2b_a_symlinked_ancestor_is_refused_and_nothing_is_created_outside(
    tmp_path: Path,
) -> None:
    clock = _Clock()
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "link").symlink_to(real)
    with pytest.raises(WatchdogEvidenceError, match="symlink"):
        Watchdog(
            probe=_probe(clock, {"live": True, "ready": True}),  # type: ignore[arg-type]
            interval_s=10.0,
            deadline_s=90.0,
            evidence_dir=tmp_path / "link" / "ops",
            clock=clock.now,
            timer=clock.monotonic,
        )
    assert list(real.iterdir()) == [], "the walk stopped at the link; nothing was created"
    assert not (tmp_path / "link" / "ops").exists()


def test_r1_2c_a_name_swap_between_the_check_and_the_write_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _Clock()
    dog = _watchdog(tmp_path, clock, {"live": True, "ready": True})
    ops = tmp_path / "ops"
    dog.tick()  # creates the log and the heartbeat
    decoy = tmp_path / "decoy.jsonl"
    decoy.write_text("DECOY\n", encoding="utf-8")
    decoy.chmod(0o600)
    real_write = os.write

    def swapping_write(fd: int, data: bytes, *args: object) -> int:
        if os.fstat(fd).st_size > 0 and not decoy.exists():  # only the log append, once
            return real_write(fd, data)
        if decoy.exists():
            os.replace(decoy, ops / EVIDENCE_LOG)  # the name now designates another inode
        return real_write(fd, data)

    monkeypatch.setattr(os, "write", swapping_write)
    clock.advance(10.0)
    with pytest.raises(WatchdogEvidenceError, match="replaced"):
        dog.tick()
    monkeypatch.undo()
    assert (ops / EVIDENCE_LOG).read_text(encoding="utf-8") == "DECOY\n", (
        "the decoy was not written"
    )


def test_r1_2d_loose_or_foreign_evidence_entries_are_refused_typed(tmp_path: Path) -> None:
    clock = _Clock()
    ops = tmp_path / "ops"
    ops.mkdir()
    (ops / EVIDENCE_LOG).write_text("", encoding="utf-8")
    (ops / EVIDENCE_LOG).chmod(0o644)
    dog = _watchdog(tmp_path, clock, {"live": True, "ready": True})
    with pytest.raises(WatchdogEvidenceError, match="mode"):
        dog.tick()
    (ops / EVIDENCE_LOG).chmod(0o600)
    (ops / HEARTBEAT).write_text("{}", encoding="utf-8")
    (ops / HEARTBEAT).chmod(0o600)
    with pytest.raises(WatchdogEvidenceError, match="malformed"):
        _watchdog(tmp_path, clock, {"live": True, "ready": True})
    os.link(ops / HEARTBEAT, tmp_path / "other-name.json")
    with pytest.raises(WatchdogEvidenceError, match="2 links"):
        _watchdog(tmp_path, clock, {"live": True, "ready": True})


def test_r1_2e_the_evidence_files_are_created_private_and_the_dir_fd_is_retained(
    tmp_path: Path,
) -> None:
    clock = _Clock()
    dog = _watchdog(tmp_path, clock, {"live": True, "ready": True})
    dog.tick()
    ops = tmp_path / "ops"
    for name in (EVIDENCE_LOG, HEARTBEAT):
        st = (ops / name).lstat()
        assert stat.S_ISREG(st.st_mode) and stat.S_IMODE(st.st_mode) == 0o600 and st.st_nlink == 1
    assert dog.directory_fd >= 0
    dog.close()
    with pytest.raises(WatchdogEvidenceError, match="closed"):
        dog.tick()


def _starved_write(monkeypatch: pytest.MonkeyPatch, *, skip_calls: int = 0) -> None:
    """os.write that persists HALF of one call, then accepts nothing (a disk that stops)."""

    real_write = os.write
    state = {"calls": 0, "starved": False}

    def starving(fd: int, data: bytes, *args: object) -> int:
        state["calls"] += 1
        if state["calls"] <= skip_calls:
            return real_write(fd, data)
        if state["starved"]:
            return 0
        state["starved"] = True
        half = max(1, len(data) // 2)
        return real_write(fd, bytes(data[:half]))

    monkeypatch.setattr(os, "write", starving)


def test_r1_3a_a_short_log_write_raises_typed_and_publishes_no_heartbeat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P2: at 50ad133 the return value of os.write was ignored and a half-written heartbeat
    was published as success."""

    clock = _Clock()
    dog = _watchdog(tmp_path, clock, {"live": True, "ready": True})
    dog.tick()
    ops = tmp_path / "ops"
    before = (ops / HEARTBEAT).read_bytes()
    _starved_write(monkeypatch)  # the first os.write of the tick is the log append
    clock.advance(10.0)
    with pytest.raises(WatchdogEvidenceError, match="short write"):
        dog.tick()
    monkeypatch.undo()
    assert (ops / HEARTBEAT).read_bytes() == before, "the previous heartbeat stays"
    assert [p.name for p in ops.iterdir()] == sorted([EVIDENCE_LOG, HEARTBEAT]) or set(
        p.name for p in ops.iterdir()
    ) == {EVIDENCE_LOG, HEARTBEAT}


def test_r1_3b_a_short_heartbeat_write_keeps_the_previous_heartbeat_byte_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _Clock()
    dog = _watchdog(tmp_path, clock, {"live": True, "ready": True})
    dog.tick()
    ops = tmp_path / "ops"
    before = (ops / HEARTBEAT).read_bytes()
    lines_before = len(_lines(ops / EVIDENCE_LOG))
    _starved_write(monkeypatch, skip_calls=1)  # the log append succeeds; the heartbeat temp starves
    clock.advance(10.0)
    with pytest.raises(WatchdogEvidenceError, match="short write"):
        dog.tick()
    monkeypatch.undo()
    assert (ops / HEARTBEAT).read_bytes() == before
    assert json.loads(before)["verdict"]["state"] == "HEALTHY"
    assert len(_lines(ops / EVIDENCE_LOG)) == lines_before + 1, "the observation was recorded"
    assert {p.name for p in ops.iterdir()} == {EVIDENCE_LOG, HEARTBEAT}, "no temp survives"


def test_r1_4_the_runbook_third_layer_is_host_push_receive_only_and_never_pulls() -> None:
    runbook = (ROOT / "docs" / "ops" / "WATCHDOG.md").read_text(encoding="utf-8")
    lowered = runbook.lower()
    for tool in ("scp", "rsync", "ssh "):
        assert tool not in lowered, f"the runbook must not instruct a {tool.strip()} pull"
    assert "never pulls" in runbook
    assert "receive-only" in lowered
    assert "no chronos-host credential" in lowered or "holds no credential" in lowered
    assert "DESIGN-alert-sidecar.md" in runbook


def test_r1_2f_deadman_walks_to_the_heartbeat_without_following_a_symlinked_ancestor(
    tmp_path: Path,
) -> None:
    clock = _Clock()
    real = tmp_path / "real"
    real.mkdir()
    _heartbeat(real / HEARTBEAT, clock, age_s=1.0)
    (tmp_path / "link").symlink_to(real)
    verdict = check_deadman(
        tmp_path / "link" / HEARTBEAT, 180.0, clock=clock.now, timer=clock.monotonic
    )
    assert verdict.state is DeadmanState.DEAD
    assert "symlink" in verdict.reason


# ---------------------------------------------- r2 (Daybreak HOLD-DELTA at 2f81447)


def test_r2_1a_same_boot_restart_keeps_the_monotonic_deadline_when_the_wall_is_held(
    tmp_path: Path,
) -> None:
    """P1 continuity, Daybreak's exact sequence (logs/daybreak-probe-W-1-r1.py): HEALTHY at
    monotonic 1000 → +80 s monotonic with the wall held → restart into an UNHEALTHY peer →
    +10 s → TRIPPED at 90 s. At 2f81447 the restart said "10.000 s without a HEALTHY"."""

    clock = _Clock(monotonic=1000.0)
    backend = {"live": True, "ready": True}
    first = _watchdog(tmp_path, clock, backend)
    assert first.tick().state is WatchdogState.HEALTHY
    first.close()
    backend["ready"] = False
    clock.mono += 80.0  # same boot; the wall clock stepped back to the recorded value
    restarted = _watchdog(tmp_path, clock, backend)
    clock.mono += 10.0
    verdict = restarted.tick()
    assert verdict.state is WatchdogState.TRIPPED, verdict.reason
    assert "90.000 s" in verdict.reason
    assert verdict.since == NOW


def test_r2_1b_a_different_boot_starts_tripped_until_a_healthy_observation(
    tmp_path: Path,
) -> None:
    clock = _Clock(monotonic=1000.0)
    backend = {"live": True, "ready": True}
    first = _watchdog(tmp_path, clock, backend, boot_id="boot-A")
    first.tick()
    first.close()
    backend["ready"] = False
    clock = _Clock(wall=NOW + timedelta(seconds=5), monotonic=20.0)  # rebooted: a young timer
    restarted = _watchdog(tmp_path, clock, backend, boot_id="boot-B")
    verdict = restarted.tick()
    assert verdict.state is WatchdogState.TRIPPED
    assert "boot" in verdict.reason and "unproven" in verdict.reason
    heartbeat = json.loads((tmp_path / "ops" / HEARTBEAT).read_text(encoding="utf-8"))
    assert heartbeat["verdict"]["state"] == "TRIPPED" and heartbeat["boot_id"] == "boot-B"
    backend["ready"] = True
    clock.advance(10.0)
    assert restarted.tick().state is WatchdogState.HEALTHY


def test_r2_1c_a_prior_monotonic_ahead_of_this_timer_is_not_comparable_and_starts_tripped(
    tmp_path: Path,
) -> None:
    clock = _Clock(monotonic=1000.0)
    backend = {"live": True, "ready": True}
    first = _watchdog(tmp_path, clock, backend)
    first.tick()
    first.close()
    backend["ready"] = False
    clock.mono = 500.0  # the same boot id claimed, but the timer went backwards: unproven
    restarted = _watchdog(tmp_path, clock, backend)
    verdict = restarted.tick()
    assert verdict.state is WatchdogState.TRIPPED and "unproven" in verdict.reason


def test_r2_1d_the_deadline_is_never_shortened_by_either_clock(tmp_path: Path) -> None:
    """The restored interval is max(monotonic, wall, the prior file's own silence)."""

    clock = _Clock(monotonic=1000.0)
    backend = {"live": True, "ready": True}
    first = _watchdog(tmp_path, clock, backend)
    first.tick()
    first.close()
    backend["ready"] = False
    clock.mono += 20.0
    clock.wall += timedelta(seconds=85)  # the wall says 85 s, the timer says 20 s
    restarted = _watchdog(tmp_path, clock, backend)
    clock.advance(5.0)
    verdict = restarted.tick()  # wall-derived 90 s wins: neither clock may shorten the outage
    assert verdict.state is WatchdogState.TRIPPED, verdict.reason


def test_r2_1e_an_unreadable_boot_identity_is_a_typed_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(watchdog_module, "_BOOT_ID_PATH", tmp_path / "no-such-boot_id")
    clock = _Clock()
    with pytest.raises(WatchdogEvidenceError, match="boot identity"):
        Watchdog(
            probe=_probe(clock, {"live": True, "ready": True}),  # type: ignore[arg-type]
            interval_s=10.0,
            deadline_s=90.0,
            evidence_dir=tmp_path / "ops",
            clock=clock.now,
            timer=clock.monotonic,
        )
    assert not (tmp_path / "ops" / HEARTBEAT).exists()


def _plant_before_exchange(
    monkeypatch: pytest.MonkeyPatch, ops: Path, plant: Callable[[], None]
) -> dict[str, int]:
    """Daybreak's swap_before_replace hook, on the exchange: plant an entry at heartbeat.json
    immediately before the real atomic call runs, once."""

    real = watchdog_module._renameat2
    calls = {"n": 0}

    def hooked(src_dir_fd: int, src: str, dst_dir_fd: int, dst: str, flags: int) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            plant()
        real(src_dir_fd, src, dst_dir_fd, dst, flags)

    monkeypatch.setattr(watchdog_module, "_renameat2", hooked)
    return calls


def test_r2_2a_a_symlink_planted_in_the_check_replace_window_survives_and_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P1 envelope: at 2f81447 the planted link was silently displaced (result=accepted)."""

    clock = _Clock()
    dog = _watchdog(tmp_path, clock, {"live": True, "ready": True})
    dog.tick()
    ops = tmp_path / "ops"
    previous = (ops / HEARTBEAT).read_bytes()
    sentinel = tmp_path / "sentinel.json"
    sentinel.write_text("SENTINEL", encoding="utf-8")

    def plant() -> None:
        (ops / HEARTBEAT).unlink()
        (ops / HEARTBEAT).symlink_to(sentinel)

    _plant_before_exchange(monkeypatch, ops, plant)
    clock.advance(10.0)
    with pytest.raises(WatchdogEvidenceError, match="replaced during publication"):
        dog.tick()
    monkeypatch.undo()
    assert (ops / HEARTBEAT).is_symlink(), "the planted entry survives at the name"
    assert sentinel.read_text(encoding="utf-8") == "SENTINEL"
    assert {p.name for p in ops.iterdir()} == {EVIDENCE_LOG, HEARTBEAT}, "no temp, nothing new"
    assert previous  # the previous heartbeat is gone from the name (the planter removed it);
    # what a reader now sees at heartbeat.json is the planted link — the operator's evidence


def test_r2_2b_a_hardlink_to_a_foreign_file_planted_in_the_window_survives_and_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _Clock()
    dog = _watchdog(tmp_path, clock, {"live": True, "ready": True})
    dog.tick()
    ops = tmp_path / "ops"
    foreign = tmp_path / "foreign.json"
    foreign.write_text("FOREIGN", encoding="utf-8")
    foreign.chmod(0o600)

    def plant() -> None:
        (ops / HEARTBEAT).unlink()
        os.link(foreign, ops / HEARTBEAT)

    _plant_before_exchange(monkeypatch, ops, plant)
    clock.advance(10.0)
    with pytest.raises(WatchdogEvidenceError, match="replaced during publication"):
        dog.tick()
    monkeypatch.undo()
    assert (ops / HEARTBEAT).read_text(encoding="utf-8") == "FOREIGN"
    assert foreign.lstat().st_nlink == 2, "the planted hardlink was neither unlinked nor rewritten"
    assert {p.name for p in ops.iterdir()} == {EVIDENCE_LOG, HEARTBEAT}


def test_r2_2c_an_entry_that_appears_at_an_absent_name_is_refused_not_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _Clock()
    ops = tmp_path / "ops"
    dog = _watchdog(tmp_path, clock, {"live": True, "ready": True})  # no heartbeat yet
    sentinel = tmp_path / "sentinel.json"
    sentinel.write_text("SENTINEL", encoding="utf-8")
    _plant_before_exchange(monkeypatch, ops, lambda: (ops / HEARTBEAT).symlink_to(sentinel))
    with pytest.raises(WatchdogEvidenceError, match="appeared during publication"):
        dog.tick()
    monkeypatch.undo()
    assert (ops / HEARTBEAT).is_symlink()
    assert {p.name for p in ops.iterdir()} == {EVIDENCE_LOG, HEARTBEAT}


def test_r2_2d_the_normal_publication_still_replaces_atomically_and_leaves_no_temp(
    tmp_path: Path,
) -> None:
    clock = _Clock()
    dog = _watchdog(tmp_path, clock, {"live": True, "ready": True})
    ops = tmp_path / "ops"
    for _ in range(3):
        dog.tick()
        clock.advance(10.0)
        assert {p.name for p in ops.iterdir()} == {EVIDENCE_LOG, HEARTBEAT}
        st = (ops / HEARTBEAT).lstat()
        assert stat.S_ISREG(st.st_mode) and st.st_nlink == 1 and stat.S_IMODE(st.st_mode) == 0o600


def test_r2_3_the_runbook_states_the_boot_rule_and_the_refused_entry() -> None:
    import re

    runbook = (ROOT / "docs" / "ops" / "WATCHDOG.md").read_text(encoding="utf-8")
    collapsed = re.sub(r"\s+", " ", runbook)
    assert "starts TRIPPED" in collapsed
    assert "same boot" in collapsed and "keeps the original deadline" in collapsed
    assert "left in place" in collapsed


# ---------------------------------------------- r3 (Daybreak HOLD-DELTA at 84ea338)


def _after_exchange(
    monkeypatch: pytest.MonkeyPatch, act: Callable[[int, str, int, str], None]
) -> None:
    """Daybreak's r2 hook: run ``act`` immediately after the real RENAME_EXCHANGE returns, once."""

    real = watchdog_module._renameat2
    fired = {"done": False}

    def hooked(src_dir_fd: int, src: str, dst_dir_fd: int, dst: str, flags: int) -> None:
        real(src_dir_fd, src, dst_dir_fd, dst, flags)
        if flags == watchdog_module._RENAME_EXCHANGE and not fired["done"]:
            fired["done"] = True
            act(src_dir_fd, src, dst_dir_fd, dst)

    monkeypatch.setattr(watchdog_module, "_renameat2", hooked)


def test_r3_1a_a_displaced_entry_that_vanishes_after_the_exchange_withdraws_our_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P1 (Daybreak's exact scenario): at 84ea338 the tick raised but the fresh record stayed
    at heartbeat.json and check_deadman said ALIVE. Now the record is withdrawn: absent → DEAD."""

    clock = _Clock()
    dog = _watchdog(tmp_path, clock, {"live": True, "ready": True})
    dog.tick()
    ops = tmp_path / "ops"

    def vanish(src_dir_fd: int, src: str, dst_dir_fd: int, dst: str) -> None:
        os.unlink(src, dir_fd=src_dir_fd)  # the displaced old heartbeat vanishes at the temp name

    _after_exchange(monkeypatch, vanish)
    clock.advance(10.0)
    with pytest.raises(WatchdogEvidenceError, match="vanished mid-publication"):
        dog.tick()
    monkeypatch.undo()
    assert not (ops / HEARTBEAT).exists(), "the fresh record must not remain readable"
    assert {p.name for p in ops.iterdir()} == {EVIDENCE_LOG}, "no temp survives"
    verdict = check_deadman(ops / HEARTBEAT, 180.0, clock=clock.now, timer=clock.monotonic)
    assert verdict.state is DeadmanState.DEAD and "absent" in verdict.reason
    assert EXIT_CODES[verdict.state] == 2


def test_r3_1b_a_foreign_entry_at_the_name_after_the_vanish_is_left_in_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The withdrawal is identity-bound: only the inode this process wrote is ever removed."""

    clock = _Clock()
    dog = _watchdog(tmp_path, clock, {"live": True, "ready": True})
    dog.tick()
    ops = tmp_path / "ops"
    sentinel = tmp_path / "sentinel.json"
    sentinel.write_text("SENTINEL", encoding="utf-8")

    def vanish_and_plant(src_dir_fd: int, src: str, dst_dir_fd: int, dst: str) -> None:
        os.unlink(src, dir_fd=src_dir_fd)  # the displaced entry vanishes ...
        os.unlink(dst, dir_fd=dst_dir_fd)  # ... and a planter swaps our record for a symlink
        os.symlink(str(sentinel), dst, dir_fd=dst_dir_fd)

    _after_exchange(monkeypatch, vanish_and_plant)
    clock.advance(10.0)
    with pytest.raises(WatchdogEvidenceError, match="left in place"):
        dog.tick()
    monkeypatch.undo()
    assert (ops / HEARTBEAT).is_symlink(), "the foreign entry survives at the name"
    assert sentinel.read_text(encoding="utf-8") == "SENTINEL"
    assert {p.name for p in ops.iterdir()} == {EVIDENCE_LOG, HEARTBEAT}
    verdict = check_deadman(ops / HEARTBEAT, 180.0, clock=clock.now, timer=clock.monotonic)
    assert verdict.state is DeadmanState.DEAD and "symlink" in verdict.reason


def test_r3_1c_the_normal_and_foreign_branches_are_unchanged_by_the_settlement(
    tmp_path: Path,
) -> None:
    clock = _Clock()
    dog = _watchdog(tmp_path, clock, {"live": True, "ready": True})
    ops = tmp_path / "ops"
    for _ in range(3):
        dog.tick()
        clock.advance(10.0)
    heartbeat = json.loads((ops / HEARTBEAT).read_text(encoding="utf-8"))
    assert heartbeat["verdict"]["state"] == "HEALTHY"
    assert {p.name for p in ops.iterdir()} == {EVIDENCE_LOG, HEARTBEAT}
    verdict = check_deadman(ops / HEARTBEAT, 180.0, clock=clock.now, timer=clock.monotonic)
    assert verdict.state is DeadmanState.ALIVE


def test_r3_3_the_runbook_names_the_settled_outcomes_and_the_crash_boundary() -> None:
    import re

    runbook = (ROOT / "docs" / "ops" / "WATCHDOG.md").read_text(encoding="utf-8")
    collapsed = re.sub(r"\s+", " ", runbook)
    assert "indistinguishable" in collapsed
    assert "outage-detection bound" in collapsed
    assert "withdrawn" in collapsed


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
    # the prior heartbeat (seconds old, HEALTHY) is restored: not HEALTHY now, under the deadline
    assert watchdog_main(argv, transport=httpx.MockTransport(handler)) == 1
    lines = _lines(ops / EVIDENCE_LOG)
    assert len(lines) == 2 and lines[-1]["state"] == "UNHEALTHY"
    fresh = [*argv[:-2], str(tmp_path / "fresh-ops"), "--once"]
    assert watchdog_main(fresh, transport=httpx.MockTransport(handler)) == 2, (
        "no prior HEALTHY: TRIPPED"
    )


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
