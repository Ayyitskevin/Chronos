"""Read-only campaign status contract tests."""

from __future__ import annotations

import argparse
import json
import socket
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from chronos.cli.campaign_status import cmd_campaign_status
from chronos.persistence import hash_chain


def _arguments(root: Path, *, now: datetime) -> argparse.Namespace:
    return argparse.Namespace(
        mandate=root / "mandate.json",
        registry=root / "registry.json",
        database=root / "chronos.db",
        state_dir=root,
        kill_switch=root / "live_kill_switch.json",
        audit_file=root / "platform_audit.jsonl",
        health_snapshot=root / "health.json",
        now=now,
    )


def _snapshot(root: Path) -> tuple[tuple[str, int, int], ...]:
    return tuple(
        sorted((path.name, path.stat().st_size, path.stat().st_mtime_ns) for path in root.iterdir())
    )


def _health(now: datetime) -> dict[str, object]:
    blocked = {"state": "BLOCKED", "reasons": []}
    return {
        "schema_version": 2,
        "status": "ok",
        "status_scope": "compatibility_only",
        "broker_mode": "demo",
        "environment": "paper",
        "read_only": False,
        "writer_lease_held": True,
        "reconciliation_status": "RECONCILED",
        "reconciliation_generation": 1,
        "assessed_at": now.isoformat(),
        "liveness": {"state": "LIVE", "reasons": []},
        "service_readiness": {"state": "READY", "reasons": []},
        "trading_capability": {
            "paper_new_exposure": blocked,
            "live_new_exposure": blocked,
            "autonomous_new_exposure": blocked,
        },
        "observations": {
            "writer_role": "WRITER",
            "store_readable": True,
            "startup_faults": [],
            "tasks": [
                {
                    "name": "autonomy",
                    "state": "RUNNING",
                    "observation_state": "CURRENT",
                    "age_seconds": 1.0,
                    "required_for_writer": True,
                    "failure_code": None,
                }
            ],
            "broker_loop_running": True,
            "broker": {
                "observation_state": "CURRENT",
                "connected": False,
                "connection_state": "demo",
                "observed_environment": "paper",
                "age_seconds": 1.0,
                "generation": 1,
            },
            "reconciliation": {
                "status": "RECONCILED",
                "observation_state": "CURRENT",
                "age_seconds": 1.0,
                "generation": 1,
            },
            "clock": "UNKNOWN",
            "clock_evidence": {
                "provider": "disabled",
                "observation_state": "UNKNOWN",
                "age_seconds": None,
                "maximum_error_seconds": None,
                "maximum_allowed_error_seconds": None,
                "failure_code": "disabled",
                "generation": 0,
            },
        },
    }


def _artifacts(root: Path, *, now: datetime) -> None:
    (root / "mandate.json").write_text('{"private":"ARTIFACT_SENTINEL"}', encoding="utf-8")
    (root / "registry.json").write_text('{"credential":"REGISTRY_SENTINEL"}', encoding="utf-8")
    (root / "state_generation.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "installation_id": "install-a",
                "created_at": (now - timedelta(days=10)).isoformat(),
                "materialized": ["kill_switch"],
            }
        ),
        encoding="utf-8",
    )
    (root / "live_kill_switch.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "engaged": False,
                "reason": "",
                "initiated_by": "owner",
                "engaged_at": None,
                "note": "campaign clear",
            }
        ),
        encoding="utf-8",
    )
    (root / "platform_audit.jsonl").write_text("", encoding="utf-8")
    (root / "health.json").write_text(json.dumps(_health(now)), encoding="utf-8")
    with sqlite3.connect(root / "chronos.db") as database:
        database.executescript(
            """
            CREATE TABLE installation_identity (id INTEGER PRIMARY KEY, installation_id TEXT);
            INSERT INTO installation_identity VALUES (1, 'install-a');
            CREATE TABLE autonomy_decision_attempts (
                id INTEGER PRIMARY KEY,
                account_fingerprint TEXT,
                decision_id TEXT,
                admitted INTEGER,
                refusals INTEGER,
                first_seen_at TEXT,
                last_seen_at TEXT
            );
            INSERT INTO autonomy_decision_attempts VALUES
                (1, 'account', 'd1', 0, 2, 'x', 'x'),
                (2, 'account', 'd2', 1, 1, 'x', 'x');
            CREATE TABLE autonomy_proposer_revocations (
                id INTEGER PRIMARY KEY,
                proposer_id TEXT,
                secret_sha256 TEXT,
                reason TEXT,
                revoked_at TEXT
            );
            CREATE TABLE hash_chain_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                stream TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                kind TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                previous_hash TEXT NOT NULL,
                record_hash TEXT NOT NULL,
                UNIQUE(stream, sequence)
            );
            """
        )
        for sequence in range(1, 3):
            _insert_chain_row(
                database,
                stream="autonomy.cycles:account",
                sequence=sequence,
                kind="cycle",
                payload={"cycle": sequence},
                recorded_at=now - timedelta(minutes=sequence),
            )
        for sequence in range(1, 4):
            _insert_chain_row(
                database,
                stream="autonomy.decisions:account",
                sequence=sequence,
                kind="refused",
                payload={"decision_id": f"d{sequence}", "refusal": "MODE_CANNOT_SUBMIT"},
                recorded_at=now - timedelta(seconds=sequence),
            )


def _insert_chain_row(
    database: sqlite3.Connection,
    *,
    stream: str,
    sequence: int,
    kind: str,
    payload: dict[str, object],
    recorded_at: datetime,
) -> None:
    previous = database.execute(
        "SELECT record_hash FROM hash_chain_records WHERE stream=? ORDER BY sequence DESC",
        (stream,),
    ).fetchone()
    previous_hash = previous[0] if previous is not None else hash_chain.GENESIS_HASH
    payload_json = hash_chain.canonical_payload(payload)
    record_hash = hash_chain.compute_hash(
        stream=stream,
        sequence=sequence,
        recorded_at=recorded_at,
        payload_json=payload_json,
        previous_hash=previous_hash,
    )
    database.execute(
        """
        INSERT INTO hash_chain_records
            (stream, sequence, kind, payload_json, recorded_at, previous_hash, record_hash)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            stream,
            sequence,
            kind,
            payload_json,
            recorded_at.isoformat(),
            previous_hash,
            record_hash,
        ),
    )


def _grant_fixtures(monkeypatch: pytest.MonkeyPatch, *, now: datetime) -> None:
    mandate = SimpleNamespace(effective_from=now - timedelta(days=10))
    registration = SimpleNamespace(
        proposer_id="worker",
        secret_sha256="a" * 64,
        enabled=True,
        expires_at=now + timedelta(days=90),
        is_current=lambda instant: instant < now + timedelta(days=90),
    )
    monkeypatch.setattr(
        "chronos.api.autonomy_wiring.load_persistent_mandate",
        lambda path: SimpleNamespace(mandate=mandate),
    )
    monkeypatch.setattr("chronos.cli.mandate_check.review_mandate", lambda *a, **k: [])
    monkeypatch.setattr(
        "chronos.supervisor.proposers.load_proposer_registry",
        lambda path: SimpleNamespace(registry=SimpleNamespace(proposers=(registration,))),
    )


def test_status_is_read_only_and_reports_observed_counts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    now = datetime(2026, 9, 15, 12, tzinfo=UTC)
    _artifacts(tmp_path, now=now)
    _grant_fixtures(monkeypatch, now=now)
    (tmp_path / ".env").write_text("BROKER_MODE=SECRET_SENTINEL", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        socket,
        "socket",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("network attempted")),
    )
    monkeypatch.setattr(
        "chronos.orders.recovery_hold.resolve_installation",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("seeder called")),
    )
    monkeypatch.setattr(
        "chronos.orders.recovery_hold.evaluate_startup_recovery_hold",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("seeder called")),
    )
    before = _snapshot(tmp_path)

    code = cmd_campaign_status(_arguments(tmp_path, now=now))

    output = capsys.readouterr().out
    assert code == 0
    assert "clock basis: 2026-09-15T12:00:00+00:00" in output
    assert "campaign day-count: 10" in output
    assert "cycles observed: 2" in output
    assert "refusals by reason: MODE_CANNOT_SUBMIT=3" in output
    assert "platform audit chain: CLEAR" in output
    assert "campaign audit chain: CLEAR" in output
    assert "terminal journal recomputation: CLEAR" in output
    assert "CAMPAIGN STATUS: CLEAR" in output
    assert "SECRET_SENTINEL" not in output
    assert "ARTIFACT_SENTINEL" not in output
    assert "REGISTRY_SENTINEL" not in output
    assert _snapshot(tmp_path) == before


def test_status_reports_every_unverified_condition_after_unreadable_artifact(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = cmd_campaign_status(_arguments(tmp_path, now=datetime(2026, 9, 15, 12, tzinfo=UTC)))
    output = capsys.readouterr().out
    assert code == 1
    for condition in (
        "recovery hold",
        "blocking mandate finding",
        "credential expiry/revocation",
        "kill switch provenance",
        "platform audit chain",
        "campaign audit chain",
        "terminal journal recomputation",
        "worker liveness",
    ):
        assert f"{condition}: UNVERIFIED" in output
    assert not (tmp_path / "chronos.db").exists()


def test_status_trips_broken_audit_and_unattributed_engaged_kill_switch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    now = datetime(2026, 9, 15, 12, tzinfo=UTC)
    _artifacts(tmp_path, now=now)
    _grant_fixtures(monkeypatch, now=now)
    (tmp_path / "platform_audit.jsonl").write_text("not-json\n", encoding="utf-8")
    (tmp_path / "live_kill_switch.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "engaged": True,
                "reason": "unreadable state",
                "initiated_by": "system",
                "engaged_at": now.isoformat(),
                "note": "",
            }
        ),
        encoding="utf-8",
    )

    code = cmd_campaign_status(_arguments(tmp_path, now=now))

    output = capsys.readouterr().out
    assert code == 1
    assert "kill switch provenance: TRIPPED" in output
    assert "platform audit chain: TRIPPED" in output


def test_status_trips_broken_campaign_and_terminal_digest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    now = datetime(2026, 9, 15, 12, tzinfo=UTC)
    _artifacts(tmp_path, now=now)
    _grant_fixtures(monkeypatch, now=now)
    with sqlite3.connect(tmp_path / "chronos.db") as database:
        database.execute(
            "UPDATE hash_chain_records SET payload_json='{}' "
            "WHERE stream='autonomy.cycles:account' AND sequence=2"
        )

    code = cmd_campaign_status(_arguments(tmp_path, now=now))

    output = capsys.readouterr().out
    assert code == 1
    assert "campaign audit chain: TRIPPED" in output
    assert "terminal journal recomputation: TRIPPED" in output


def test_status_reports_empty_campaign_and_terminal_chains_as_unverified(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    now = datetime(2026, 9, 15, 12, tzinfo=UTC)
    _artifacts(tmp_path, now=now)
    _grant_fixtures(monkeypatch, now=now)
    with sqlite3.connect(tmp_path / "chronos.db") as database:
        database.execute("DELETE FROM hash_chain_records")

    code = cmd_campaign_status(_arguments(tmp_path, now=now))

    output = capsys.readouterr().out
    assert code == 1
    assert "campaign audit chain: UNVERIFIED" in output
    assert "terminal journal recomputation: UNVERIFIED" in output


def test_status_trips_recovery_witness_mismatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    now = datetime(2026, 9, 15, 12, tzinfo=UTC)
    _artifacts(tmp_path, now=now)
    _grant_fixtures(monkeypatch, now=now)
    with sqlite3.connect(tmp_path / "chronos.db") as database:
        database.execute("UPDATE installation_identity SET installation_id='install-b' WHERE id=1")

    code = cmd_campaign_status(_arguments(tmp_path, now=now))

    assert code == 1
    assert "recovery hold: TRIPPED" in capsys.readouterr().out


def test_status_reports_pending_adoption_witness_as_unverified(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    now = datetime(2026, 9, 15, 12, tzinfo=UTC)
    _artifacts(tmp_path, now=now)
    _grant_fixtures(monkeypatch, now=now)
    with sqlite3.connect(tmp_path / "chronos.db") as database:
        database.execute("UPDATE installation_identity SET installation_id=NULL WHERE id=1")

    code = cmd_campaign_status(_arguments(tmp_path, now=now))

    output = capsys.readouterr().out
    assert code == 1
    assert "recovery hold: UNVERIFIED" in output
    assert "pending 0012 adoption sentinel" in output


def test_status_trips_blocking_mandate_and_expired_credential(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from chronos.cli.mandate_check import Severity

    now = datetime(2026, 9, 15, 12, tzinfo=UTC)
    _artifacts(tmp_path, now=now)
    _grant_fixtures(monkeypatch, now=now)
    monkeypatch.setattr(
        "chronos.cli.mandate_check.review_mandate",
        lambda *a, **k: [SimpleNamespace(code="TEST_BLOCK", severity=Severity.BLOCKING)],
    )
    expired = SimpleNamespace(
        proposer_id="worker",
        secret_sha256="b" * 64,
        enabled=True,
        expires_at=now - timedelta(days=1),
        is_current=lambda instant: False,
    )
    monkeypatch.setattr(
        "chronos.supervisor.proposers.load_proposer_registry",
        lambda path: SimpleNamespace(registry=SimpleNamespace(proposers=(expired,))),
    )

    code = cmd_campaign_status(_arguments(tmp_path, now=now))

    output = capsys.readouterr().out
    assert code == 1
    assert "blocking mandate finding: TRIPPED" in output
    assert "credential expiry/revocation: TRIPPED" in output


def test_status_rejects_non_health_response_shape(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    now = datetime(2026, 9, 15, 12, tzinfo=UTC)
    _artifacts(tmp_path, now=now)
    _grant_fixtures(monkeypatch, now=now)
    (tmp_path / "health.json").write_text('{"status":"ok"}', encoding="utf-8")

    code = cmd_campaign_status(_arguments(tmp_path, now=now))

    assert code == 1
    assert "worker liveness: UNVERIFIED" in capsys.readouterr().out


def test_status_rejects_health_snapshot_with_extra_fields(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    now = datetime(2026, 9, 15, 12, tzinfo=UTC)
    _artifacts(tmp_path, now=now)
    _grant_fixtures(monkeypatch, now=now)
    health = _health(now)
    health["invented_status"] = "ARTIFACT_SENTINEL"
    (tmp_path / "health.json").write_text(json.dumps(health), encoding="utf-8")

    code = cmd_campaign_status(_arguments(tmp_path, now=now))

    output = capsys.readouterr().out
    assert code == 1
    assert "worker liveness: UNVERIFIED" in output
    assert "ARTIFACT_SENTINEL" not in output


def test_status_trips_stale_worker_liveness(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    now = datetime(2026, 9, 15, 12, tzinfo=UTC)
    _artifacts(tmp_path, now=now)
    _grant_fixtures(monkeypatch, now=now)
    stale = now - timedelta(minutes=10)
    (tmp_path / "health.json").write_text(json.dumps(_health(stale)), encoding="utf-8")

    code = cmd_campaign_status(_arguments(tmp_path, now=now))

    assert code == 1
    assert "worker liveness: UNVERIFIED" in capsys.readouterr().out


def test_status_trips_non_running_worker_liveness(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    now = datetime(2026, 9, 15, 12, tzinfo=UTC)
    _artifacts(tmp_path, now=now)
    _grant_fixtures(monkeypatch, now=now)
    health = _health(now)
    observations = health["observations"]
    assert isinstance(observations, dict)
    tasks = observations["tasks"]
    assert isinstance(tasks, list)
    task = tasks[0]
    assert isinstance(task, dict)
    task["state"] = "FAILED"
    (tmp_path / "health.json").write_text(json.dumps(health), encoding="utf-8")

    code = cmd_campaign_status(_arguments(tmp_path, now=now))

    assert code == 1
    assert "worker liveness: TRIPPED" in capsys.readouterr().out


def test_status_cannot_write_chmod_state_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    now = datetime(2026, 9, 15, 12, tzinfo=UTC)
    _artifacts(tmp_path, now=now)
    _grant_fixtures(monkeypatch, now=now)
    before = _snapshot(tmp_path)
    tmp_path.chmod(0o500)
    try:
        code = cmd_campaign_status(_arguments(tmp_path, now=now))
        after = _snapshot(tmp_path)
    finally:
        tmp_path.chmod(0o700)
    assert code == 0
    assert after == before
    assert "CAMPAIGN STATUS: CLEAR" in capsys.readouterr().out
