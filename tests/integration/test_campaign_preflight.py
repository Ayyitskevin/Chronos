"""Read-only campaign preflight contract tests."""

from __future__ import annotations

import argparse
import socket
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from chronos.cli.campaign_preflight import cmd_campaign_preflight


def _far_expiry(now: datetime) -> datetime:
    """A far-future expiry that exists for every ``now`` (``now.replace(year=2099)`` raises
    ``ValueError`` on 29 February)."""

    return now + timedelta(days=36500)


def test_the_far_expiry_is_valid_and_later_on_29_february() -> None:
    leap_day = datetime(2028, 2, 29, 12, 0, tzinfo=UTC)  # injected: no clock is moved
    with pytest.raises(ValueError):
        leap_day.replace(year=2099)  # the hazard the helper exists to avoid
    far = _far_expiry(leap_day)
    assert far > leap_day and far.tzinfo is not None and far.year > 2099


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
        expires_at=_far_expiry(now),
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
        "chronos.orders.recovery_hold.resolve_installation",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("seeder")),
    )
    monkeypatch.setattr(
        "chronos.orders.recovery_hold.evaluate_startup_recovery_hold",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("seeder")),
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
    assert "missing state_generation marker and installation_identity row" in output
    assert "backend URL" in output


def test_preflight_flags_0012_adoption_sentinel_for_writer_boot(tmp_path, capsys):
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
                expires_at=_far_expiry(now),
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
    assert code == 1
    assert "0012 adoption sentinel" in output
    assert "boot the backend writer once" in output
    monkeypatch.undo()


def test_preflight_does_not_write_read_only_state_dir(tmp_path, capsys):
    (tmp_path / "worker.service").write_text(
        "UnsetEnvironment=CHRONOS_WORKER_FORWARD\n", encoding="utf-8"
    )
    (tmp_path / "policy.md").write_text("policy", encoding="utf-8")
    before = sorted((p.name, p.stat().st_size, p.stat().st_mtime_ns) for p in tmp_path.iterdir())
    tmp_path.chmod(0o500)
    try:
        code = cmd_campaign_preflight(_args(tmp_path))
        after = sorted((p.name, p.stat().st_size, p.stat().st_mtime_ns) for p in tmp_path.iterdir())
    finally:
        tmp_path.chmod(0o700)
    assert code == 1
    assert before == after


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


def _boot_writer():
    from fastapi.testclient import TestClient

    from chronos.api.main import create_app
    from chronos.config.settings import get_settings

    get_settings.cache_clear()
    return TestClient(create_app())


def _synthetic_backend(monkeypatch, root):
    """The real app over a synthetic store and state directory under ``root``."""

    from chronos.config.settings import get_settings

    for name, value in {
        "BROKER_MODE": "demo",
        "ALLOW_ORDER_TRANSMIT": "false",
        "ALLOW_LIVE_TRADING": "false",
        "DEMO_PROFILE": "empty_account",
        "DATABASE_URL": f"sqlite:///{root / 'chronos.db'}",
        "LOG_FILE": str(root / "chronos.log"),
        "BACKEND_TOKEN_FILE": str(root / "backend_api_token"),
        "LIVE_KILL_SWITCH_FILE": str(root / "live_kill_switch.json"),
        "SESSION_BASELINE_FILE": str(root / "session_drawdown.json"),
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("AUTONOMY_MANDATE_FILE", raising=False)
    get_settings.cache_clear()


def _state_identity_line(root, capsys):
    cmd_campaign_preflight(_args(root, evidence=False))
    lines = [
        line for line in capsys.readouterr().out.splitlines() if "ADR-0054] state identity" in line
    ]
    assert len(lines) == 1, lines
    return lines[0]


def test_preflight_passes_state_identity_after_the_acknowledged_cutover_restart(
    monkeypatch, tmp_path, capsys
):
    """PGAP-I: cutover, held boot, note, restart; the next preflight passes ADR-0054."""

    _synthetic_backend(monkeypatch, tmp_path)
    with _boot_writer():
        pass
    assert _state_identity_line(tmp_path, capsys) == (
        "PASS [ADR-0054] state identity: marker/database pair consistent; no recovery hold"
    )
    (tmp_path / "chronos.db").rename(tmp_path / "chronos.db.archived-20261025T000000Z")
    unverified = (
        "FAIL [SHADOW_CAMPAIGN ADR-0054] state identity: UNVERIFIED; missing "
        "installation_identity row; boot the backend writer once, then re-run preflight"
    )
    with _boot_writer() as client:
        token = (tmp_path / "backend_api_token").read_text(encoding="utf-8").strip()
        assert _state_identity_line(tmp_path, capsys) == unverified
        acknowledged = client.post(
            "/live/recovery/acknowledge",
            json={"note": "synthetic N3 cutover"},
            headers={"X-Chronos-Token": token},
        )
        assert acknowledged.status_code == 200
        # the note takes effect at the next start, so this process changes nothing yet
        assert _state_identity_line(tmp_path, capsys) == unverified
    with _boot_writer() as client:
        assert client.app.state.backend.recovery_hold is None
    assert _state_identity_line(tmp_path, capsys) == (
        "PASS [ADR-0054] state identity: marker/database pair consistent; no recovery hold"
    )


def test_preflight_stays_unverified_after_an_unacknowledged_cutover_restart(
    monkeypatch, tmp_path, capsys
):
    """The regression: without the note, restarting the writer does not clear the gap."""

    _synthetic_backend(monkeypatch, tmp_path)
    with _boot_writer():
        pass
    (tmp_path / "chronos.db").rename(tmp_path / "chronos.db.archived-20261025T000000Z")
    for _ in range(2):
        with _boot_writer() as client:
            hold = client.app.state.backend.recovery_hold
            assert hold is not None and hold.reason.value == "database_replaced"
    assert _state_identity_line(tmp_path, capsys) == (
        "FAIL [SHADOW_CAMPAIGN ADR-0054] state identity: UNVERIFIED; missing "
        "installation_identity row; boot the backend writer once, then re-run preflight"
    )
