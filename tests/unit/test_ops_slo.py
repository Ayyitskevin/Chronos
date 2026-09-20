"""M3 ops plane, SLO-1: the typed SLO document, the offline evaluator over the watchdog's
evidence files, its CLI, and the one ``/health`` observation the cache feeds.

Numbered to the packet contract. 1x pins ``SloDocument`` and its consistency validators
(contract 1), 2x the evaluator's readings and refusals (contract 2), 3x the CLI exit codes and
default-off (contract 3), 4x the ``/health`` observation that changes no verdict (contract 4),
5x the runbook (contract 5). Every clock is a fixed instant; every file lives under
``tmp_path``; no HTTP leaves the process (the only HTTP client here is FastAPI's TestClient
against an app with no lifespan, no backend and no broker).
"""

from __future__ import annotations

import ast
import json
import os
import stat
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from chronos.api import operational_health as collector_module
from chronos.api.routes.health import router as health_router
from chronos.config.settings import Settings
from chronos.operations import health as health_module
from chronos.operations import slo as slo_module
from chronos.operations.external_probe import ExternalProbeState
from chronos.operations.health import (
    OperationalFacts,
    OperationalObservations,
    SloFact,
    WriterRole,
    evaluate_operational_health,
)
from chronos.operations.slo import (
    EVALUATION_CACHE,
    EVIDENCE_LOG,
    EXIT_BAD_DOCUMENT,
    EXIT_CODES,
    HEARTBEAT,
    PROBE_STATES,
    WATCHDOG_VERDICTS,
    SloCacheError,
    SloDocument,
    SloDocumentError,
    SloEvaluation,
    SloState,
    evaluate,
    load_document,
    parse_document,
    read_evaluation_cache,
    write_evaluation_cache,
)
from chronos.operations.slo import main as slo_main
from chronos.operations.watchdog import WatchdogState

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "chronos"
NOW = datetime(2026, 9, 19, 14, 0, tzinfo=UTC)
SAFE_ENV = {
    **os.environ,
    "BROKER_MODE": "demo",
    "ALLOW_ORDER_TRANSMIT": "false",
    "ALLOW_LIVE_TRADING": "false",
    "PYTHONDONTWRITEBYTECODE": "1",
}
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


def _line(
    at: datetime,
    monotonic: float,
    *,
    state: str = "HEALTHY",
    elapsed_ms: float = 10.0,
    verdict: str = "HEALTHY",
) -> dict[str, object]:
    return {
        "assessed_at": at.isoformat(),
        "state": state,
        "failure_code": None if state == "HEALTHY" else "readiness_not_ready",
        "elapsed_ms": elapsed_ms,
        "monotonic": monotonic,
        "liveness_status": 200,
        "readiness_status": 200 if state == "HEALTHY" else 503,
        "target_origin": "http://backend.test",
        "verdict": verdict,
    }


def _synthetic_log(
    count: int = 100,
    *,
    interval_s: float = 10.0,
    end: datetime = NOW,
    elapsed: list[float] | None = None,
    states: dict[int, str] | None = None,
    verdicts: dict[int, str] | None = None,
    wall_steps: dict[int, float] | None = None,
) -> list[dict[str, object]]:
    """``count`` ticks ending at ``end``; the i-th line (0-based) is ``elapsed[i]`` ms."""

    lines: list[dict[str, object]] = []
    start_wall = end - timedelta(seconds=interval_s * (count - 1))
    start_mono = 5_000.0
    step_offset = 0.0
    for index in range(count):
        step_offset += (wall_steps or {}).get(index, 0.0)
        lines.append(
            _line(
                start_wall + timedelta(seconds=interval_s * index + step_offset),
                start_mono + interval_s * index,
                state=(states or {}).get(index, "HEALTHY"),
                elapsed_ms=(elapsed or [10.0] * count)[index],
                verdict=(verdicts or {}).get(index, "HEALTHY"),
            )
        )
    return lines


def _evidence(
    tmp_path: Path,
    lines: list[dict[str, object]] | None,
    *,
    heartbeat_age_s: float | None = 5.0,
    now: datetime = NOW,
) -> Path:
    ops = tmp_path / "ops"
    ops.mkdir(parents=True, exist_ok=True)
    if lines is not None:
        (ops / EVIDENCE_LOG).write_text(
            "".join(json.dumps(line, sort_keys=True) + "\n" for line in lines), encoding="utf-8"
        )
    if heartbeat_age_s is not None:
        observed = now - timedelta(seconds=heartbeat_age_s)
        (ops / HEARTBEAT).write_text(
            json.dumps(
                {
                    "boot_id": "boot-A",
                    "last_healthy_at": observed.isoformat(),
                    "last_healthy_monotonic": 5_990.0,
                    "last_observed_at": observed.isoformat(),
                    "monotonic": 5_990.0,
                    "pid": 4242,
                    "version": "0.1.0",
                    "verdict": {
                        "state": "HEALTHY",
                        "reason": "test",
                        "since": None,
                        "evidence_path": "x",
                    },
                }
            ),
            encoding="utf-8",
        )
    return ops


def _document(**objectives: float) -> SloDocument:
    return parse_document(objectives)


def _report(evaluation: SloEvaluation, objective: str) -> slo_module.ObjectiveReport:
    found = [report for report in evaluation.objectives if report.objective == objective]
    assert len(found) == 1, f"{objective} reported {len(found)} times"
    return found[0]


# ------------------------------------------------------------------ 1. the document


def test_1a_the_document_is_typed_closed_and_refuses_an_empty_or_malformed_objective() -> None:
    document = _document(probe_latency_p95_ms=250.0, window_s=600.0)
    assert document.probe_latency_p95_ms == 250.0 and document.watchdog_deadline_s is None
    with pytest.raises(SloDocumentError, match="extra"):
        parse_document({"probe_latency_p95_ms": 250.0, "window_s": 600.0, "x": 1})
    with pytest.raises(SloDocumentError, match="no objective"):
        parse_document({})
    with pytest.raises(SloDocumentError, match="not an object"):
        parse_document([1, 2])
    with pytest.raises(SloDocumentError, match="window_s"):
        parse_document({"readiness_availability_pct": 99.0})
    for bad in (0, -1.0, float("inf"), float("nan"), True, "90"):
        with pytest.raises(SloDocumentError):
            parse_document({"watchdog_deadline_s": bad})
    with pytest.raises(SloDocumentError, match="less than or equal to 100"):
        parse_document({"readiness_availability_pct": 100.5, "window_s": 60.0})
    assert parse_document({"watchdog_deadline_s": 90}).watchdog_deadline_s == 90.0


def test_1b_the_deadman_max_age_must_be_at_least_twice_the_watchdog_deadline() -> None:
    with pytest.raises(
        SloDocumentError, match=r"deadman_max_age_s=100\.0.*2 x watchdog_deadline_s"
    ):
        _document(watchdog_deadline_s=90.0, deadman_max_age_s=100.0)
    assert _document(watchdog_deadline_s=90.0, deadman_max_age_s=180.0).deadman_max_age_s == 180.0
    # each alone is fine: the rule binds the pair, it does not demand the pair
    assert _document(deadman_max_age_s=1.0).watchdog_deadline_s is None


def test_1c_the_deadline_must_be_three_times_the_cadence_the_evidence_records(
    tmp_path: Path,
) -> None:
    ops = _evidence(tmp_path, _synthetic_log(100, interval_s=10.0))
    with pytest.raises(SloDocumentError, match=r"watchdog_deadline_s=20\.0.*3 x .*10\.0"):
        evaluate(ops, _document(watchdog_deadline_s=20.0), NOW)
    accepted = evaluate(ops, _document(watchdog_deadline_s=30.0), NOW)
    assert _report(accepted, "watchdog_deadline_s").state is SloState.MET
    # one line: the cadence is unmeasurable, so the objective is UNKNOWN — not a refusal
    single = _evidence(tmp_path / "single", _synthetic_log(1))
    report = _report(
        evaluate(single, _document(watchdog_deadline_s=20.0), NOW), "watchdog_deadline_s"
    )
    assert report.state is SloState.UNKNOWN and "cadence" in report.reason


def test_1d_a_document_violating_either_rule_exits_64_typed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    real = datetime.now(tz=UTC)  # the CLI reads the real clock: date the evidence to it
    ops = _evidence(tmp_path, _synthetic_log(100, interval_s=10.0, end=real), now=real)
    document = tmp_path / "slo.json"
    document.write_text(json.dumps({"watchdog_deadline_s": 90, "deadman_max_age_s": 100}))
    assert slo_main(["--evidence-dir", str(ops), "--slo", str(document)]) == EXIT_BAD_DOCUMENT
    assert "2 x watchdog_deadline_s" in capsys.readouterr().err
    document.write_text(json.dumps({"watchdog_deadline_s": 20}))
    assert slo_main(["--evidence-dir", str(ops), "--slo", str(document)]) == EXIT_BAD_DOCUMENT
    assert "3 x" in capsys.readouterr().err
    assert not (ops / EVALUATION_CACHE).exists(), "a refused document publishes nothing"


# ------------------------------------------------------------------ 2. the evaluator


def test_2a_p95_latency_over_a_synthetic_100_line_log(tmp_path: Path) -> None:
    lines = _synthetic_log(100, elapsed=[float(i) for i in range(1, 101)])
    ops = _evidence(tmp_path, lines)
    met = _report(
        evaluate(ops, _document(probe_latency_p95_ms=95.0, window_s=990.0), NOW),
        "probe_latency_p95_ms",
    )
    assert met.state is SloState.MET and met.measured == 95.0 and met.window_s == 990.0
    assert met.samples == 100
    breached = _report(
        evaluate(ops, _document(probe_latency_p95_ms=94.9, window_s=990.0), NOW),
        "probe_latency_p95_ms",
    )
    assert breached.state is SloState.BREACHED and breached.measured == 95.0
    # a narrower window keeps only the newest lines: the last 20 ticks are 81..100 ms
    narrow = _report(
        evaluate(ops, _document(probe_latency_p95_ms=99.0, window_s=190.0), NOW),
        "probe_latency_p95_ms",
    )
    assert narrow.samples == 20 and narrow.measured == 99.0 and narrow.state is SloState.MET


def test_2b_availability_over_a_window_with_one_unhealthy_tick(tmp_path: Path) -> None:
    ops = _evidence(tmp_path, _synthetic_log(100, states={42: "UNHEALTHY"}))
    met = _report(
        evaluate(ops, _document(readiness_availability_pct=99.0, window_s=990.0), NOW),
        "readiness_availability_pct",
    )
    assert met.state is SloState.MET and met.measured == 99.0 and met.samples == 100
    breached = _report(
        evaluate(ops, _document(readiness_availability_pct=99.5, window_s=990.0), NOW),
        "readiness_availability_pct",
    )
    assert breached.state is SloState.BREACHED and breached.measured == 99.0
    # an UNKNOWN tick (timeout, redirect) is not availability either
    unknown_tick = _evidence(tmp_path / "u", _synthetic_log(100, states={7: "UNKNOWN"}))
    report = _report(
        evaluate(unknown_tick, _document(readiness_availability_pct=99.5, window_s=990.0), NOW),
        "readiness_availability_pct",
    )
    assert report.state is SloState.BREACHED and report.measured == 99.0


def test_2c_unknown_when_the_log_is_shorter_than_the_window_or_absent(tmp_path: Path) -> None:
    document = _document(
        probe_latency_p95_ms=50.0,
        readiness_availability_pct=99.0,
        window_s=600.0,
        watchdog_deadline_s=90.0,
        deadman_max_age_s=180.0,
        clock_max_error_s=5.0,
    )
    short = _evidence(tmp_path / "short", _synthetic_log(10))
    evaluation = evaluate(short, document, NOW)
    for objective in ("probe_latency_p95_ms", "readiness_availability_pct"):
        report = _report(evaluation, objective)
        assert report.state is SloState.UNKNOWN, objective
        assert "90.0 s" in report.reason and "600.0 s" in report.reason
    assert evaluation.state is SloState.UNKNOWN
    absent = evaluate(tmp_path / "nowhere", document, NOW)
    assert absent.state is SloState.UNKNOWN
    assert all(report.state is SloState.UNKNOWN for report in absent.objectives)
    assert {report.objective for report in absent.objectives} == {
        "probe_latency_p95_ms",
        "readiness_availability_pct",
        "watchdog_deadline_s",
        "deadman_max_age_s",
        "clock_max_error_s",
    }
    assert all(report.measured is None for report in absent.objectives)
    no_heartbeat = _evidence(tmp_path / "nohb", _synthetic_log(100), heartbeat_age_s=None)
    report = _report(evaluate(no_heartbeat, document, NOW), "deadman_max_age_s")
    assert report.state is SloState.UNKNOWN and "absent" in report.reason


def test_2d_the_watchdog_deadline_objective_reads_the_longest_silence_and_any_tripped_verdict(
    tmp_path: Path,
) -> None:
    silent = {index: "UNHEALTHY" for index in range(50, 55)}  # 5 ticks: a 60 s stretch
    ops = _evidence(tmp_path, _synthetic_log(100, states=silent))
    met = _report(evaluate(ops, _document(watchdog_deadline_s=90.0), NOW), "watchdog_deadline_s")
    assert met.state is SloState.MET and met.measured == 60.0
    breached = _report(
        evaluate(ops, _document(watchdog_deadline_s=50.0), NOW), "watchdog_deadline_s"
    )
    assert breached.state is SloState.BREACHED and breached.measured == 60.0
    tripped = _evidence(tmp_path / "t", _synthetic_log(100, verdicts={99: "TRIPPED"}))
    report = _report(
        evaluate(tripped, _document(watchdog_deadline_s=90.0), NOW), "watchdog_deadline_s"
    )
    assert report.state is SloState.BREACHED and "TRIPPED" in report.reason


def test_2e_the_deadman_max_age_objective_reads_the_heartbeat_age(tmp_path: Path) -> None:
    document = _document(deadman_max_age_s=180.0)
    fresh = _evidence(tmp_path / "fresh", _synthetic_log(3), heartbeat_age_s=30.0)
    report = _report(evaluate(fresh, document, NOW), "deadman_max_age_s")
    assert report.state is SloState.MET and report.measured == 30.0
    stale = _evidence(tmp_path / "stale", _synthetic_log(3), heartbeat_age_s=1_000.0)
    report = _report(evaluate(stale, document, NOW), "deadman_max_age_s")
    assert report.state is SloState.BREACHED and report.measured == 1_000.0
    malformed = _evidence(tmp_path / "bad", _synthetic_log(3), heartbeat_age_s=None)
    (malformed / HEARTBEAT).write_text('{"last_observed_at": 12}', encoding="utf-8")
    report = _report(evaluate(malformed, document, NOW), "deadman_max_age_s")
    assert report.state is SloState.UNKNOWN and "malformed" in report.reason
    future = _evidence(tmp_path / "future", _synthetic_log(3), heartbeat_age_s=-60.0)
    report = _report(evaluate(future, document, NOW), "deadman_max_age_s")
    assert report.state is SloState.UNKNOWN and "future" in report.reason


def test_2f_the_clock_max_error_objective_reads_wall_versus_monotonic_disagreement(
    tmp_path: Path,
) -> None:
    ops = _evidence(tmp_path, _synthetic_log(100, wall_steps={60: 3.0}))
    met = _report(evaluate(ops, _document(clock_max_error_s=5.0), NOW), "clock_max_error_s")
    assert met.state is SloState.MET and met.measured == 3.0
    breached = _report(evaluate(ops, _document(clock_max_error_s=2.0), NOW), "clock_max_error_s")
    assert breached.state is SloState.BREACHED and breached.measured == 3.0
    single = _evidence(tmp_path / "single", _synthetic_log(1))
    report = _report(evaluate(single, _document(clock_max_error_s=5.0), NOW), "clock_max_error_s")
    assert report.state is SloState.UNKNOWN and "two" in report.reason


def test_2g_a_fifo_symlink_or_directory_at_either_file_is_unknown_typed_within_a_second(
    tmp_path: Path,
) -> None:
    document = _document(probe_latency_p95_ms=50.0, window_s=60.0, deadman_max_age_s=180.0)
    ops = _evidence(tmp_path, None, heartbeat_age_s=None)
    os.mkfifo(ops / EVIDENCE_LOG)
    # the process-level pin FIRST (W-1 3c's shape): the CLI through the real interpreter under
    # an outer timeout, so a pathname open that BLOCKS on the fifo fails here in 5 s and never
    # hangs this process — an in-process call on the fifo must come after it, never before
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "chronos.operations.slo",
            "--evidence-dir",
            str(ops),
            "--slo",
            str(_write_document(tmp_path, document)),
        ],
        capture_output=True,
        text=True,
        timeout=5,
        env=SAFE_ENV,
        check=False,
    )
    assert completed.returncode == 3, completed.stderr
    assert json.loads(completed.stdout)["state"] == "UNKNOWN"
    started = time.monotonic()
    evaluation = evaluate(ops, document, NOW)
    assert time.monotonic() - started < 1.0
    report = _report(evaluation, "probe_latency_p95_ms")
    assert report.state is SloState.UNKNOWN
    assert "fifo" in report.reason or "not a regular file" in report.reason
    fresh = tmp_path / "fresh-heartbeat.json"
    fresh.write_text(json.dumps({"last_observed_at": NOW.isoformat()}), encoding="utf-8")
    (ops / HEARTBEAT).symlink_to(fresh)
    report = _report(evaluate(ops, document, NOW), "deadman_max_age_s")
    assert report.state is SloState.UNKNOWN and "symlink" in report.reason
    linked_parent = tmp_path / "linked"
    linked_parent.symlink_to(tmp_path / "ops")
    report = _report(evaluate(linked_parent, document, NOW), "deadman_max_age_s")
    assert report.state is SloState.UNKNOWN and "symlink" in report.reason
    directory = _evidence(tmp_path / "d", None, heartbeat_age_s=None)
    (directory / EVIDENCE_LOG).mkdir()
    report = _report(evaluate(directory, document, NOW), "probe_latency_p95_ms")
    assert report.state is SloState.UNKNOWN and "directory" in report.reason


def test_2h_a_torn_last_line_is_where_the_writer_died_and_a_malformed_body_is_unknown(
    tmp_path: Path,
) -> None:
    ops = _evidence(tmp_path, _synthetic_log(100))
    with (ops / EVIDENCE_LOG).open("a", encoding="utf-8") as handle:
        handle.write('{"assessed_at": "2026-09-19T14:00:10+00:00", "sta')
    report = _report(
        evaluate(ops, _document(readiness_availability_pct=99.0, window_s=990.0), NOW),
        "readiness_availability_pct",
    )
    assert report.state is SloState.MET and report.samples == 100
    (ops / EVIDENCE_LOG).write_text("not json\n" * 3, encoding="utf-8")
    report = _report(
        evaluate(ops, _document(readiness_availability_pct=99.0, window_s=990.0), NOW),
        "readiness_availability_pct",
    )
    assert report.state is SloState.UNKNOWN and "malformed" in report.reason


# ------------------------------------------- r1: malformed evidence is UNKNOWN, never MET


def _probe_record(
    seconds_ago: int, monotonic: float, state: str, verdict: str
) -> dict[str, object]:
    """Daybreak's probe record (logs/daybreak-probe-slo1.py), verbatim in shape."""

    return {
        "assessed_at": (NOW - timedelta(seconds=seconds_ago)).isoformat(),
        "monotonic": monotonic,
        "state": state,
        "elapsed_ms": 1.0,
        "verdict": verdict,
    }


def _deadline_report(tmp_path: Path, rows: list[dict[str, object]]) -> slo_module.ObjectiveReport:
    ops = _evidence(tmp_path, rows, heartbeat_age_s=None)
    return _report(evaluate(ops, _document(watchdog_deadline_s=30.0), NOW), "watchdog_deadline_s")


def test_r1_1a_the_vocabularies_are_the_probe_and_watchdog_enums_verbatim() -> None:
    """Mirrored, not imported: W-1's default-off scan (test_ops_watchdog 3d) forbids naming
    ``operations.watchdog`` anywhere else in src, and 3c above forbids the import; this pin is
    what keeps the mirror honest."""

    assert {state.value for state in ExternalProbeState} == PROBE_STATES
    assert {state.value for state in WatchdogState} == WATCHDOG_VERDICTS
    assert {"HEALTHY", "UNHEALTHY", "UNKNOWN"} == PROBE_STATES
    assert {"HEALTHY", "TRIPPED"} == WATCHDOG_VERDICTS


def test_r1_1b_an_unknown_verdict_token_is_malformed_and_unknown_never_met(tmp_path: Path) -> None:
    report = _deadline_report(
        tmp_path,
        [
            _probe_record(20, 0.0, "HEALTHY", "HEALTHY"),
            _probe_record(10, 10.0, "UNHEALTHY", "TRIPPED "),
            _probe_record(0, 20.0, "HEALTHY", "HEALTHY"),
        ],
    )
    assert report.state is SloState.UNKNOWN
    assert "line 2" in report.reason and "verdict" in report.reason and "malformed" in report.reason
    assert report.measured is None and report.samples == 0
    # the whole log is refused: every log-based objective is UNKNOWN, none is computed
    ops = _evidence(
        tmp_path / "all",
        [
            _probe_record(20, 0.0, "HEALTHY", "HEALTHY"),
            _probe_record(10, 10.0, "UNHEALTHY", "TRIPPED "),
            _probe_record(0, 20.0, "HEALTHY", "HEALTHY"),
        ],
    )
    evaluation = evaluate(
        ops,
        _document(
            probe_latency_p95_ms=50.0,
            readiness_availability_pct=1.0,
            window_s=20.0,
            watchdog_deadline_s=30.0,
            clock_max_error_s=5.0,
        ),
        NOW,
    )
    log_based = {
        r.objective: r for r in evaluation.objectives if r.objective != "deadman_max_age_s"
    }
    assert len(log_based) == 4
    assert all(r.state is SloState.UNKNOWN and "line 2" in r.reason for r in log_based.values())


def test_r1_1c_a_lower_cased_state_is_malformed_and_unknown(tmp_path: Path) -> None:
    report = _deadline_report(
        tmp_path,
        [
            _probe_record(20, 0.0, "HEALTHY", "HEALTHY"),
            _probe_record(10, 10.0, "HEALTHY", "HEALTHY"),
            _probe_record(0, 20.0, "healthy", "HEALTHY"),
        ],
    )
    assert report.state is SloState.UNKNOWN
    assert "line 3" in report.reason and "state" in report.reason
    for token in ("", "HEALTHY ", "READY", "Healthy", "UNHEALTHY\n"):
        report = _deadline_report(
            tmp_path / f"t{abs(hash(token))}",
            [
                _probe_record(10, 0.0, "HEALTHY", "HEALTHY"),
                _probe_record(0, 10.0, token, "HEALTHY"),
            ],
        )
        assert report.state is SloState.UNKNOWN and "line 2" in report.reason, token


def test_r1_1d_a_valid_tripped_verdict_still_reads_breached(tmp_path: Path) -> None:
    report = _deadline_report(
        tmp_path,
        [
            _probe_record(20, 0.0, "HEALTHY", "HEALTHY"),
            _probe_record(10, 10.0, "UNHEALTHY", "TRIPPED"),
            _probe_record(0, 20.0, "HEALTHY", "HEALTHY"),
        ],
    )
    assert report.state is SloState.BREACHED and "TRIPPED" in report.reason
    assert report.measured == 20.0 and report.samples == 3


def test_r1_2a_a_decreasing_monotonic_sequence_is_malformed_and_unknown_naming_the_line(
    tmp_path: Path,
) -> None:
    report = _deadline_report(
        tmp_path,
        [
            _probe_record(30, 0.0, "HEALTHY", "HEALTHY"),
            _probe_record(20, 10.0, "HEALTHY", "HEALTHY"),
            _probe_record(10, 5.0, "HEALTHY", "HEALTHY"),
            _probe_record(0, 15.0, "HEALTHY", "HEALTHY"),
        ],
    )
    assert report.state is SloState.UNKNOWN
    assert "line 3" in report.reason and "monotonic" in report.reason
    assert report.measured is None


def test_r1_2b_equal_monotonic_and_out_of_order_assessed_at_are_malformed(tmp_path: Path) -> None:
    equal = _deadline_report(
        tmp_path / "equal",
        [
            _probe_record(20, 0.0, "HEALTHY", "HEALTHY"),
            _probe_record(10, 10.0, "HEALTHY", "HEALTHY"),
            _probe_record(0, 10.0, "HEALTHY", "HEALTHY"),
        ],
    )
    assert (
        equal.state is SloState.UNKNOWN and "line 3" in equal.reason and "monotonic" in equal.reason
    )
    reordered = _deadline_report(
        tmp_path / "wall",
        [
            _probe_record(20, 0.0, "HEALTHY", "HEALTHY"),
            _probe_record(5, 10.0, "HEALTHY", "HEALTHY"),
            _probe_record(10, 20.0, "HEALTHY", "HEALTHY"),  # wall clock went backwards 5 s
        ],
    )
    assert reordered.state is SloState.UNKNOWN
    assert "line 3" in reordered.reason and "assessed_at" in reordered.reason
    same_instant = _deadline_report(
        tmp_path / "same",
        [
            _probe_record(10, 0.0, "HEALTHY", "HEALTHY"),
            _probe_record(10, 10.0, "HEALTHY", "HEALTHY"),
        ],
    )
    assert same_instant.state is SloState.UNKNOWN and "assessed_at" in same_instant.reason


def test_r1_2c_a_clean_log_reads_as_before_and_the_cadence_is_the_median_spacing(
    tmp_path: Path,
) -> None:
    clean = _deadline_report(
        tmp_path,
        [_probe_record(30 - 10 * i, 10.0 * i, "HEALTHY", "HEALTHY") for i in range(4)],
    )
    # a clean 10 s cadence: the longest stretch between HEALTHY observations is one interval
    assert clean.state is SloState.MET and clean.measured == 10.0 and clean.samples == 4
    lines, problem = slo_module._parse_lines(
        "".join(
            json.dumps(_probe_record(30 - 10 * i, [0.0, 10.0, 25.0, 35.0][i], "HEALTHY", "HEALTHY"))
            + "\n"
            for i in range(4)
        ).encode("utf-8")
    )
    assert problem is None and slo_module._cadence_s(lines) == 10.0


# ------------------------------------------------------------------ 3. the CLI, default-off


def test_3a_cli_exit_codes_and_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # the CLI reads the real clock: date the evidence to it, and keep the log longer than the
    # window so the moving boundary never decides how many lines are in it
    real = datetime.now(tz=UTC) - timedelta(seconds=1)
    ops = _evidence(
        tmp_path,
        _synthetic_log(110, states={42: "UNHEALTHY"}, end=real),
        heartbeat_age_s=5.0,
        now=real,
    )
    document = tmp_path / "slo.json"
    document.write_text(
        json.dumps(
            {"readiness_availability_pct": 90.0, "window_s": 990.0, "deadman_max_age_s": 180.0}
        )
    )
    argv = ["--evidence-dir", str(ops), "--slo", str(document)]
    assert slo_main(argv) == EXIT_CODES[SloState.MET] == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["state"] == "MET" and len(printed["objectives"]) == 2
    assert json.loads((ops / EVALUATION_CACHE).read_text(encoding="utf-8")) == printed
    assert slo_main([*argv, "--pretty"]) == 0
    assert capsys.readouterr().out.startswith("{\n  ")
    document.write_text(json.dumps({"readiness_availability_pct": 99.5, "window_s": 990.0}))
    assert slo_main(argv) == EXIT_CODES[SloState.BREACHED] == 2
    assert json.loads(capsys.readouterr().out)["state"] == "BREACHED"
    document.write_text(json.dumps({"readiness_availability_pct": 99.0, "window_s": 5_000.0}))
    assert slo_main(argv) == EXIT_CODES[SloState.UNKNOWN] == 3
    assert json.loads(capsys.readouterr().out)["state"] == "UNKNOWN"
    # BREACHED outranks UNKNOWN: a breached objective next to an unmeasurable one is 2
    (ops / HEARTBEAT).unlink()
    document.write_text(
        json.dumps(
            {"readiness_availability_pct": 99.5, "window_s": 990.0, "deadman_max_age_s": 180.0}
        )
    )
    assert slo_main(argv) == 2
    printed = json.loads(capsys.readouterr().out)
    assert {o["objective"]: o["state"] for o in printed["objectives"]} == {
        "readiness_availability_pct": "BREACHED",
        "deadman_max_age_s": "UNKNOWN",
    }
    document.write_text("{")
    assert slo_main(argv) == EXIT_BAD_DOCUMENT == 64
    assert "not JSON" in capsys.readouterr().err
    document.write_text(json.dumps({"window_s": 10}))
    assert slo_main(argv) == 64
    assert "no objective" in capsys.readouterr().err


def test_3b_nothing_in_the_repository_starts_the_evaluator() -> None:
    """The W-1 default-off scan (test_ops_watchdog 3d), extended to the new module: the only
    modules that may name ``operations.slo`` are the two that READ its cache for the
    ``/health`` observation, and neither of them may reach the evaluator or its entry point."""

    allowed_imports = {
        SRC / "operations" / "health.py": {"SloState"},
        SRC / "api" / "operational_health.py": {"read_evaluation_cache"},
    }
    for path in SRC.rglob("*.py"):
        if path == SRC / "operations" / "slo.py":
            continue
        text = path.read_text(encoding="utf-8")
        if "operations.slo" not in text and "operations import slo" not in text:
            continue
        assert path in allowed_imports, f"{path} names operations.slo"
        imported: set[str] = set()
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.ImportFrom) and node.module == "chronos.operations.slo":
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.Import):
                assert not any("operations.slo" in a.name for a in node.names), path
        assert imported <= allowed_imports[path], f"{path} imports {imported}"
        for forbidden in ("evaluate(", "slo_main", ".main(", "write_evaluation_cache"):
            assert forbidden not in text, f"{path} reaches {forbidden}"
    main_py = (SRC / "api" / "main.py").read_text(encoding="utf-8")
    assert "slo" not in main_py.lower(), "no lifespan task starts the evaluator"
    for unit in (ROOT / "docs" / "ops").glob("*.service"):
        assert "slo" not in unit.read_text(encoding="utf-8").lower(), unit
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "operations.slo" not in pyproject


def test_3c_import_graph_the_evaluator_and_the_authority_packages_never_meet() -> None:
    tree = ast.parse(Path(slo_module.__file__).read_text(encoding="utf-8"))  # type: ignore[arg-type]
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module, *(f"{node.module}.{a.name}" for a in node.names)]
        for name in names:
            assert not any(
                name == pkg or name.startswith(f"{pkg}.") for pkg in AUTHORITY_PACKAGES
            ), f"slo imports {name}"
            assert not name.startswith("chronos.operations.deadman"), "mirror, do not import"
            assert not name.startswith("chronos.operations.watchdog"), "mirror, do not import"
    forbidden_dirs = [
        SRC / p.split(".", 1)[1] for p in AUTHORITY_PACKAGES if p != "chronos.runtime"
    ]
    for path in (SRC / "runtime.py", *(f for d in forbidden_dirs for f in d.rglob("*.py"))):
        assert "operations.slo" not in path.read_text(encoding="utf-8"), path


# ------------------------------------------------------------------ 4. the /health observation


def test_4a_health_observations_carry_the_last_evaluation_from_the_cache(tmp_path: Path) -> None:
    ops = _evidence(tmp_path, _synthetic_log(100, states={42: "UNHEALTHY"}))
    evaluation = evaluate(ops, _document(readiness_availability_pct=99.5, window_s=990.0), NOW)
    assert evaluation.state is SloState.BREACHED
    write_evaluation_cache(ops, evaluation)
    reading = read_evaluation_cache(ops / EVALUATION_CACHE)
    assert reading.evaluated_at == NOW and reading.state is SloState.BREACHED
    assert reading.problem is None
    # the fact the collector builds from that reading, and the observation the projection shows
    fact = collector_module.slo_fact(ops / EVALUATION_CACHE)
    assert fact == SloFact(evaluated_at=NOW, state=SloState.BREACHED, problem=None)
    health = evaluate_operational_health(
        OperationalFacts(slo=fact), now=NOW + timedelta(seconds=30)
    )
    assert health.observations.slo.state is SloState.BREACHED
    assert health.observations.slo.evaluated_at == NOW
    assert health.observations.slo.age_seconds == 30.0
    # the field exists on the response model and is UNKNOWN with nothing behind it
    assert "slo" in OperationalObservations.model_fields
    app = FastAPI()
    app.include_router(health_router)
    with TestClient(app) as client:
        body = client.get("/health").json()
    assert body["observations"]["slo"] == {
        "evaluated_at": None,
        "state": "UNKNOWN",
        "age_seconds": None,
        "problem": "no evaluation cache is configured",
    }
    # default-off: the setting that names the cache defaults to None
    assert Settings.model_fields["ops_slo_evaluation_file"].default is None
    # a planted entry at the cache path is a typed problem, never followed
    fresh = tmp_path / "fresh-cache.json"
    fresh.write_text(json.dumps(json.loads(evaluation.model_dump_json())), encoding="utf-8")
    link = tmp_path / "linked-cache.json"
    link.symlink_to(fresh)
    linked = collector_module.slo_fact(link)
    assert linked.state is None and linked.evaluated_at is None
    assert linked.problem is not None and "symlink" in linked.problem
    absent = collector_module.slo_fact(tmp_path / "absent.json")
    assert absent.state is None and absent.problem is not None and "absent" in absent.problem
    source = (SRC / "api" / "operational_health.py").read_text(encoding="utf-8")
    assert "settings.ops_slo_evaluation_file" in source and "slo_fact(" in source


def test_4b_a_breached_cache_changes_no_verdict() -> None:
    fact_sets = (
        OperationalFacts(),
        OperationalFacts(
            backend_initialized=True, writer_role=WriterRole.WRITER, store_readable=True
        ),
    )
    for facts in fact_sets:
        baseline = evaluate_operational_health(facts, now=NOW)
        for state in SloState:
            with_slo = evaluate_operational_health(
                facts.model_copy(update={"slo": SloFact(evaluated_at=NOW, state=state)}), now=NOW
            )
            assert with_slo.liveness == baseline.liveness, state
            assert with_slo.service_readiness == baseline.service_readiness, state
            assert with_slo.trading_capability == baseline.trading_capability, state
            assert with_slo.observations.slo.state is state
    ready = evaluate_operational_health(
        fact_sets[1].model_copy(update={"slo": SloFact(evaluated_at=NOW, state=SloState.BREACHED)}),
        now=NOW,
    )
    assert ready.service_readiness.state.value == "READY"


def test_4c_ast_no_verdict_branch_in_the_projection_reads_the_slo_fact() -> None:
    tree = ast.parse(Path(health_module.__file__).read_text(encoding="utf-8"))  # type: ignore[arg-type]
    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "evaluate_operational_health"
    )
    for node in ast.walk(function):
        if isinstance(node, ast.If | ast.IfExp):
            assert "slo" not in ast.dump(node.test).lower(), ast.unparse(node.test)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"add", "update", "discard"}
        ):
            assert "slo" not in ast.dump(node).lower(), ast.unparse(node)
    # the observation is built from the fact and nothing else in the function touches it
    mentions = [
        node
        for node in ast.walk(function)
        if isinstance(node, ast.Attribute)
        and node.attr == "slo"
        and isinstance(node.value, ast.Name)
    ]
    assert mentions, "the projection must read facts.slo for the observation"


def test_4d_the_cache_is_published_atomically_and_a_planted_entry_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ops = _evidence(tmp_path, _synthetic_log(100))
    document = _document(readiness_availability_pct=99.0, window_s=990.0)
    first = evaluate(ops, document, NOW)
    write_evaluation_cache(ops, first)
    second = evaluate(ops, document, NOW + timedelta(seconds=60))
    write_evaluation_cache(ops, second)
    assert sorted(p.name for p in ops.iterdir()) == [HEARTBEAT, EVALUATION_CACHE, EVIDENCE_LOG]
    assert read_evaluation_cache(ops / EVALUATION_CACHE).evaluated_at == NOW + timedelta(seconds=60)
    assert stat.S_IMODE((ops / EVALUATION_CACHE).stat().st_mode) == 0o600
    real = datetime.now(tz=UTC)
    fifo_dir = _evidence(tmp_path / "fifo", _synthetic_log(3, end=real), now=real)
    os.mkfifo(fifo_dir / EVALUATION_CACHE)
    started = time.monotonic()
    with pytest.raises(SloCacheError, match=r"fifo|not a regular file"):
        write_evaluation_cache(fifo_dir, first)
    assert time.monotonic() - started < 1.0
    assert stat.S_ISFIFO((fifo_dir / EVALUATION_CACHE).lstat().st_mode), "left in place"
    link_dir = _evidence(tmp_path / "link", _synthetic_log(3))
    sentinel = tmp_path / "sentinel.json"
    sentinel.write_text("SENTINEL", encoding="utf-8")
    (link_dir / EVALUATION_CACHE).symlink_to(sentinel)
    with pytest.raises(SloCacheError, match="symlink"):
        write_evaluation_cache(link_dir, first)
    assert sentinel.read_text(encoding="utf-8") == "SENTINEL"
    assert sorted(p.name for p in link_dir.iterdir()) == [HEARTBEAT, EVALUATION_CACHE, EVIDENCE_LOG]
    met_document = _write_document(tmp_path, _document(deadman_max_age_s=180.0))
    assert slo_main(["--evidence-dir", str(fifo_dir), "--slo", str(met_document)]) == 3, (
        "an unpublishable observation is UNKNOWN to /health, and the exit says so"
    )
    captured = capsys.readouterr()
    assert json.loads(captured.out)["state"] == "MET", "the evaluation itself was MET"
    assert "slo cache not published" in captured.err and "fifo" in captured.err


def _write_document(tmp_path: Path, document: SloDocument) -> Path:
    path = tmp_path / "doc.json"
    path.write_text(document.model_dump_json(exclude_none=True), encoding="utf-8")
    return path


def test_4f_a_health_document_recorded_before_slo_1_still_parses() -> None:
    """The campaign status tool validates recorded health.json bodies with extra forbidden
    (``src/chronos/cli/campaign_status.py``); a required new field would have refused every
    document written before this change. On READ the field defaults to an honest absence."""

    from chronos.api.routes.health import HealthResponse

    app = FastAPI()
    app.include_router(health_router)
    with TestClient(app) as client:
        body = client.get("/health").json()
    assert body["observations"]["slo"]["problem"] == "no evaluation cache is configured"
    del body["observations"]["slo"]
    recorded = HealthResponse.model_validate_json(json.dumps(body), extra="forbid")
    assert recorded.observations.slo.state is SloState.UNKNOWN
    assert recorded.observations.slo.evaluated_at is None
    assert recorded.observations.slo.problem == "absent from this health document"
    with pytest.raises(ValueError, match="extra"):
        HealthResponse.model_validate_json(
            json.dumps({**body, "observations": {**body["observations"], "slo_x": 1}}),
            extra="forbid",
        )


def test_4e_the_document_is_read_through_the_same_bounded_no_follow_reader(tmp_path: Path) -> None:
    target = tmp_path / "real.json"
    target.write_text(json.dumps({"deadman_max_age_s": 180.0}), encoding="utf-8")
    link = tmp_path / "slo.json"
    link.symlink_to(target)
    with pytest.raises(SloDocumentError, match="symlink"):
        load_document(link)
    fifo = tmp_path / "fifo.json"
    os.mkfifo(fifo)
    started = time.monotonic()
    with pytest.raises(SloDocumentError, match=r"fifo|not a regular file"):
        load_document(fifo)
    assert time.monotonic() - started < 1.0
    with pytest.raises(SloDocumentError, match="absent"):
        load_document(tmp_path / "missing.json")
    assert load_document(target).deadman_max_age_s == 180.0


# ------------------------------------------------------------------ 5. the runbook


def test_5a_the_runbook_states_what_an_slo_proves_the_format_and_the_exit_codes() -> None:
    runbook = (ROOT / "docs" / "ops" / "WATCHDOG.md").read_text(encoding="utf-8")
    for needle in (
        "## Objectives: what an SLO proves",
        "records nothing",
        "python -m chronos.operations.slo",
        "slo.json",
        "slo-evaluation.json",
        "probe_latency_p95_ms",
        "readiness_availability_pct",
        "window_s",
        "watchdog_deadline_s",
        "deadman_max_age_s",
        "clock_max_error_s",
        "OWNER-ASKS 9",
        "exit 0 all MET",
        "2 any BREACHED",
        "3 any UNKNOWN",
        "64",
        "ops_slo_evaluation_file",
        "changes no verdict",
        "closed vocabularies",
        "does not increase",
    ):
        assert needle in runbook, needle


def test_f1_1_the_runbook_states_the_unset_default_is_an_unknown_observation() -> None:
    """SLO-1-F1 (kimi2's #248 pre-merge read, P3 1 of 2): the prose matches the probed
    behaviour — with `ops_slo_evaluation_file` unset the projection still writes
    `observations.slo`, in state UNKNOWN with problem "no evaluation cache is configured"
    (probe: run-20260913-pm/logs/SLO-1-F1-probe.out)."""

    runbook = " ".join((ROOT / "docs" / "ops" / "WATCHDOG.md").read_text(encoding="utf-8").split())
    assert (
        "The default is unset: `/health` still carries the observation, in state `UNKNOWN` "
        'with `problem` "no evaluation cache is configured"'
    ) in runbook
    assert "The default is unset: no observation." not in runbook


def test_f1_2_the_runbook_states_a_refused_document_leaves_the_previous_observation() -> None:
    """SLO-1-F1 (kimi2's #248 pre-merge read, P3 2 of 2): a document that newly fails
    validation publishes nothing, so the previous — possibly MET — observation stays in
    `/health` until the next successful evaluation; age_seconds is the only staleness
    signal."""

    runbook = " ".join((ROOT / "docs" / "ops" / "WATCHDOG.md").read_text(encoding="utf-8").split())
    assert (
        "A refused document therefore leaves the previous observation — possibly MET — in "
        "`/health` until the next successful evaluation; `age_seconds` is the only "
        "staleness signal."
    ) in runbook
