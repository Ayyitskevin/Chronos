"""Read-only campaign preflight contract tests."""

from __future__ import annotations

import argparse
import socket
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from chronos.cli.main import cmd_campaign_preflight


def _args(root: Path, **overrides: object) -> argparse.Namespace:
    values = dict(
        mandate=root / "mandate.json",
        registry=root / "registry.json",
        state_dir=root,
        unit=root / "worker.service",
        policy=root / "policy.md",
        provider="local",
        model="model-tag",
        worker_backend_url="http://127.0.0.1:8765",
        worker_symbols="SPY",
        backend_symbols="SPY",
        backend_host="127.0.0.1",
        backend_port=8765,
        evidence=True,
    )
    values.update(overrides)
    return argparse.Namespace(**values)


def test_preflight_passes_with_explicit_inputs_and_no_dotenv(monkeypatch, tmp_path, capsys):
    (tmp_path / ".env").write_text("CHRONOS_WORKER_MODEL=SECRET_SENTINEL\n", encoding="utf-8")
    (tmp_path / "worker.service").write_text(
        "UnsetEnvironment=CHRONOS_WORKER_FORWARD\n", encoding="utf-8"
    )
    (tmp_path / "policy.md").write_text("policy", encoding="utf-8")
    (tmp_path / "mandate.json").write_text("x", encoding="utf-8")
    (tmp_path / "registry.json").write_text("x", encoding="utf-8")
    (tmp_path / "state_generation.json").write_text(
        '{"schema": 1, "installation_id": "install", '
        '"created_at": "2026-01-01T00:00:00+00:00", "materialized": []}',
        encoding="utf-8",
    )
    connection = sqlite3.connect(tmp_path / "chronos.db")
    connection.execute(
        "CREATE TABLE installation_identity (id INTEGER PRIMARY KEY, installation_id TEXT)"
    )
    connection.execute("INSERT INTO installation_identity VALUES (1, 'install')")
    connection.commit()
    connection.close()
    from chronos.autonomy import AutonomyMode

    now = datetime.now(UTC)
    mandate = SimpleNamespace(
        mode=AutonomyMode.SHADOW,
        effective_from=now,
        expires_at=now.replace(year=2099),
        scope=SimpleNamespace(symbols=("SPY",)),
    )
    monkeypatch.setattr(
        "chronos.api.autonomy_wiring.load_persistent_mandate",
        lambda path: SimpleNamespace(mandate=mandate),
    )
    monkeypatch.setattr(
        "chronos.supervisor.proposers.load_proposer_registry",
        lambda path: SimpleNamespace(
            registry=SimpleNamespace(
                proposers=(SimpleNamespace(enabled=True, is_current=lambda now: True),)
            )
        ),
    )
    monkeypatch.setattr(
        socket.socket, "connect", lambda *args: (_ for _ in ()).throw(AssertionError("network"))
    )
    code = cmd_campaign_preflight(_args(tmp_path))
    output = capsys.readouterr().out
    assert code == 0
    assert "SECRET_SENTINEL" not in output
    assert not (tmp_path / "state_generation.json.tmp").exists()


def test_preflight_rejects_forwarding_and_binds_read_only_guarantees(monkeypatch, tmp_path, capsys):
    (tmp_path / "worker.service").write_text(
        "Environment=CHRONOS_WORKER_FORWARD=true\n", encoding="utf-8"
    )
    monkeypatch.setattr(
        "chronos.orders.recovery_hold.evaluate_recovery_hold", lambda **kwargs: None
    )
    monkeypatch.setattr(
        "chronos.orders.state_generation.resolve_installation",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("seeder")),
        raising=False,
    )
    monkeypatch.setattr(
        "chronos.orders.recovery_hold.evaluate_startup_recovery_hold",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("seeder")),
        raising=False,
    )
    code = cmd_campaign_preflight(_args(tmp_path, evidence=False, worker_symbols="GLD"))
    output = capsys.readouterr().out
    assert code == 1
    assert "[SHADOW_CAMPAIGN §1/§2] evidence binding" in output
    assert "[SHADOW_CAMPAIGN §2/§3] forwarding posture" in output
    assert "worker configuration" in output or "symbols" in output
    assert "resolve_installation" not in output
    assert "evaluate_startup_recovery_hold" not in output


def test_preflight_unverified_empty_state_and_backend_failure_without_mandate(tmp_path, capsys):
    (tmp_path / "policy.md").write_text("policy", encoding="utf-8")
    (tmp_path / "worker.service").write_text(
        "UnsetEnvironment=CHRONOS_WORKER_FORWARD\n", encoding="utf-8"
    )
    code = cmd_campaign_preflight(
        _args(tmp_path, mandate=tmp_path / "missing", worker_backend_url="http://127.0.0.1:8000")
    )
    output = capsys.readouterr().out
    assert code == 1
    assert "UNVERIFIED" in output
    assert "backend URL" in output


def test_preflight_accepts_0012_adoption_sentinel(tmp_path, capsys):
    (tmp_path / "policy.md").write_text("policy", encoding="utf-8")
    (tmp_path / "worker.service").write_text(
        "UnsetEnvironment=CHRONOS_WORKER_FORWARD\n", encoding="utf-8"
    )
    from chronos.autonomy import AutonomyMode

    now = datetime.now(UTC)
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        "chronos.api.autonomy_wiring.load_persistent_mandate",
        lambda path: SimpleNamespace(
            mandate=SimpleNamespace(
                mode=AutonomyMode.SHADOW,
                effective_from=now,
                expires_at=now.replace(year=2099),
                scope=SimpleNamespace(symbols=("SPY",)),
            )
        ),
    )
    monkeypatch.setattr(
        "chronos.supervisor.proposers.load_proposer_registry",
        lambda path: SimpleNamespace(
            registry=SimpleNamespace(
                proposers=(SimpleNamespace(enabled=True, is_current=lambda now: True),)
            )
        ),
    )
    (tmp_path / "state_generation.json").write_text(
        '{"schema":1,"installation_id":"install","created_at":"2026-01-01T00:00:00+00:00","materialized":[]}',
        encoding="utf-8",
    )
    connection = sqlite3.connect(tmp_path / "chronos.db")
    connection.execute(
        "CREATE TABLE installation_identity (id INTEGER PRIMARY KEY, installation_id TEXT)"
    )
    connection.execute("INSERT INTO installation_identity VALUES (1, NULL)")
    connection.commit()
    connection.close()
    code = cmd_campaign_preflight(_args(tmp_path))
    output = capsys.readouterr().out
    assert code == 0
    assert "0012 adoption sentinel" in output
    monkeypatch.undo()


def test_preflight_preserves_landed_unsafe_artifact_state(monkeypatch, tmp_path, capsys):
    (tmp_path / "worker.service").write_text(
        "UnsetEnvironment=CHRONOS_WORKER_FORWARD\n", encoding="utf-8"
    )
    from chronos.api.autonomy_wiring import UnsafeMandateFile

    def unsafe(_path):
        raise UnsafeMandateFile("unsafe grant")

    monkeypatch.setattr("chronos.api.autonomy_wiring.load_persistent_mandate", unsafe)
    code = cmd_campaign_preflight(_args(tmp_path))
    output = capsys.readouterr().out
    assert code == 1
    assert "mandate: UNSAFE" in output
