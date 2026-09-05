"""Owner-delivery certification and frozen-release write-path contract tests."""

from __future__ import annotations

import hashlib
import json
import socket
from datetime import date
from pathlib import Path

import pytest

from chronos.cli.main import main
from chronos.histdata import store
from chronos.research import data_intake, dataset_release
from chronos.research.session_calendar import SessionCalendar

_SYMBOLS = ("QQQ", "SPY", "IWM", "DIA", "GLD", "TLT")
_START = date(2024, 1, 2)
_END = date(2024, 3, 28)


def _snapshot(root: Path) -> tuple[tuple[str, int, str], ...]:
    return tuple(
        sorted(
            (
                str(path.relative_to(root)),
                path.stat().st_size,
                hashlib.sha256(path.read_bytes()).hexdigest(),
            )
            for path in root.rglob("*")
            if path.is_file()
        )
    )


def _write_synthetic_delivery(root: Path) -> Path:
    """Build ephemeral test bytes that cannot be confused with an owner export."""

    delivery = root / "SYNTHETIC_TEST_ONLY_NOT_OWNER_DATA"
    bars_dir = delivery / "bars"
    actions_dir = delivery / "corporate_actions"
    bars_dir.mkdir(parents=True)
    actions_dir.mkdir()
    sessions = SessionCalendar().sessions(_START, _END)
    symbols: dict[str, object] = {}
    attested_windows: list[dict[str, str]] = []
    holdout_map: list[dict[str, str]] = []
    for index, symbol in enumerate(_SYMBOLS):
        rows = ["date,open,high,low,close,volume"]
        for offset, session in enumerate(sessions):
            close = 100.0 + index + (offset * 0.01)
            rows.append(f"{session.isoformat()},{close},{close},{close},{close},1000000")
        bar_bytes = ("\n".join(rows) + "\n").encode()
        action_bytes = b"[]\n"
        (bars_dir / f"{symbol}.csv").write_bytes(bar_bytes)
        (actions_dir / f"{symbol}.json").write_bytes(action_bytes)
        symbols[symbol] = {
            "window": {"start": _START.isoformat(), "end": _END.isoformat()},
            "bars_sha256": hashlib.sha256(bar_bytes).hexdigest(),
            "bar_count": len(sessions),
            "corporate_actions_sha256": hashlib.sha256(action_bytes).hexdigest(),
            "corporate_action_count": 0,
        }
        attested_windows.append(
            {"symbol": symbol, "start": _START.isoformat(), "end": _END.isoformat()}
        )
        holdout_map.append(
            {
                "symbol": symbol,
                "name": f"{symbol.lower()}-synthetic-seen",
                "start": _START.isoformat(),
                "end": _END.isoformat(),
                "status": "seen",
            }
        )
    manifest = {
        "schema_version": 1,
        "delivery_id": "synthetic-test-only-do-not-use",
        "supersedes": None,
        "interval": "1d",
        "adjustment_policy": "unadjusted_as_traded",
        "provenance": {
            "source_id": "synthetic-test-generator-not-a-vendor",
            "source_receipt_sha256": "0" * 64,
            "retrieved_at": "2026-09-05T00:00:00Z",
            "retrieval_method": "generated inside pytest tmp_path",
            "license_note": "synthetic fixture; not licensed market data",
        },
        "symbols": symbols,
        "corporate_action_attestation": {
            "kind": "reviewed_no_actions",
            "source_id": "synthetic-independent-review-fixture",
            "windows": attested_windows,
            "note": "synthetic fixture only; no market-data claim",
        },
        "classified_moves": [],
        "holdout_map": holdout_map,
    }
    (delivery / "INTAKE.json").write_text(json.dumps(manifest), encoding="utf-8")
    return delivery


def _manifest(delivery: Path) -> dict[str, object]:
    return json.loads((delivery / "INTAKE.json").read_text(encoding="utf-8"))


def _rewrite_manifest(delivery: Path, manifest: dict[str, object]) -> None:
    (delivery / "INTAKE.json").write_text(json.dumps(manifest), encoding="utf-8")


def _make_not_certified(delivery: Path) -> None:
    bar_path = delivery / "bars/QQQ.csv"
    rows = bar_path.read_text(encoding="utf-8").splitlines()
    bar_path.write_text("\n".join([rows[0], *rows[2:]]) + "\n", encoding="utf-8")
    manifest = _manifest(delivery)
    symbols = manifest["symbols"]
    assert isinstance(symbols, dict)
    qqq = symbols["QQQ"]
    assert isinstance(qqq, dict)
    raw = bar_path.read_bytes()
    qqq["bars_sha256"] = hashlib.sha256(raw).hexdigest()
    qqq["bar_count"] = len(rows) - 2
    _rewrite_manifest(delivery, manifest)


def _tripwire(message: str):
    def fail(*args, **kwargs):
        raise AssertionError(message)

    return fail


def test_data_certify_freezes_release_then_merges_existing_store_without_network_or_dotenv(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    delivery = _write_synthetic_delivery(tmp_path)
    output = tmp_path / "release"
    history = tmp_path / "history"
    (tmp_path / ".env").write_text(
        "CHRONOS_DATA_DELIVERY=DOTENV_SECRET_SENTINEL\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("chronos.research.data_certification.HISTORY_ROOT", history)
    monkeypatch.setattr(socket, "socket", _tripwire("network attempted"))
    monkeypatch.setattr(
        "chronos.config.settings.Settings", _tripwire("Settings constructed")
    )
    before = _snapshot(delivery)
    delivery.chmod(0o500)
    try:
        code = main(
            ["data", "certify", "--delivery", str(delivery), "--output", str(output)]
        )
        after = _snapshot(delivery)
    finally:
        delivery.chmod(0o700)

    stdout = capsys.readouterr().out
    assert code == 0
    assert stdout.count("\n") == 1
    assert stdout.startswith(f"CERTIFIED {delivery / 'INTAKE.json'}: ")
    assert f"RELEASE {output / 'release.json'}: " in stdout
    assert f"STORED {history}: " in stdout
    assert "DOTENV_SECRET_SENTINEL" not in stdout
    assert before == after
    assert (output / "catalog.json").is_file()
    release = json.loads((output / "release.json").read_text(encoding="utf-8"))
    assert release["dataset_id"] == "synthetic-test-only-do-not-use"
    assert release["catalog_id"] == "synthetic-test-only-do-not-use"
    assert len(release["partitions"]) == len(_SYMBOLS)
    history_manifest = json.loads((history / "MANIFEST.json").read_text(encoding="utf-8"))
    assert set(history_manifest["symbols"]) == set(_SYMBOLS)
    assert {
        item["bars"]["captured_at"] for item in history_manifest["symbols"].values()
    } == {"2026-09-05T00:00:00+00:00"}
    assert {
        item["corporate_actions"]["captured_at"]
        for item in history_manifest["symbols"].values()
    } == {"2026-09-05T00:00:00+00:00"}


@pytest.mark.parametrize("failure", ["unverified", "not-certified"])
def test_data_certify_writes_nothing_until_the_existing_gates_certify(
    failure: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    delivery = _write_synthetic_delivery(tmp_path)
    output = tmp_path / "release"
    history = tmp_path / "history"
    if failure == "unverified":
        manifest = _manifest(delivery)
        symbols = manifest["symbols"]
        assert isinstance(symbols, dict)
        qqq = symbols["QQQ"]
        assert isinstance(qqq, dict)
        qqq["bars_sha256"] = "f" * 64
        _rewrite_manifest(delivery, manifest)
    else:
        _make_not_certified(delivery)
    monkeypatch.setattr("chronos.research.data_certification.HISTORY_ROOT", history)
    monkeypatch.setattr(dataset_release, "freeze_release", _tripwire("release write called"))
    monkeypatch.setattr(store, "write_bars", _tripwire("bar writer called"))
    monkeypatch.setattr(store, "write_actions", _tripwire("action writer called"))

    code = main(["data", "certify", "--delivery", str(delivery), "--output", str(output)])

    stdout = capsys.readouterr().out
    assert code == (2 if failure == "unverified" else 1)
    assert stdout.startswith("UNVERIFIED " if failure == "unverified" else "NOT_CERTIFIED ")
    assert stdout.count("\n") == 1
    assert not output.exists()
    assert not history.exists()


def test_data_certify_calls_loaded_certification_before_release_before_store(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    delivery = _write_synthetic_delivery(tmp_path)
    output = tmp_path / "release"
    history = tmp_path / "history"
    events: list[str] = []
    original_certify = data_intake.certify_loaded_intake
    original_freeze = dataset_release.freeze_release
    original_bars = store.write_bars
    original_actions = store.write_actions

    def record_certify(*args, **kwargs):
        events.append("certify")
        return original_certify(*args, **kwargs)

    def record_freeze(*args, **kwargs):
        events.append("freeze_release")
        return original_freeze(*args, **kwargs)

    def record_bars(*args, **kwargs):
        events.append("write_bars")
        return original_bars(*args, **kwargs)

    def record_actions(*args, **kwargs):
        events.append("write_actions")
        return original_actions(*args, **kwargs)

    monkeypatch.setattr("chronos.research.data_certification.HISTORY_ROOT", history)
    monkeypatch.setattr(data_intake, "certify_loaded_intake", record_certify)
    monkeypatch.setattr(dataset_release, "freeze_release", record_freeze)
    monkeypatch.setattr(store, "write_bars", record_bars)
    monkeypatch.setattr(store, "write_actions", record_actions)

    code = main(["data", "certify", "--delivery", str(delivery), "--output", str(output)])

    assert code == 0
    assert events[0:2] == ["certify", "freeze_release"]
    assert events.count("certify") == 1
    assert events.count("freeze_release") == 1
    assert events.count("write_bars") == len(_SYMBOLS)
    assert events.count("write_actions") == len(_SYMBOLS)
    assert all(event in {"write_bars", "write_actions"} for event in events[2:])
    capsys.readouterr()


def test_data_certify_release_refusal_stops_before_store(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    delivery = _write_synthetic_delivery(tmp_path)
    output = tmp_path / "release"
    history = tmp_path / "history"
    monkeypatch.setattr("chronos.research.data_certification.HISTORY_ROOT", history)
    monkeypatch.setattr(
        dataset_release,
        "freeze_release",
        _tripwire("synthetic release refusal after certification"),
    )
    monkeypatch.setattr(store, "write_bars", _tripwire("bar writer called"))
    monkeypatch.setattr(store, "write_actions", _tripwire("action writer called"))

    with pytest.raises(AssertionError, match="synthetic release refusal"):
        main(["data", "certify", "--delivery", str(delivery), "--output", str(output)])

    assert not history.exists()
    capsys.readouterr()


def test_data_certify_keeps_corrections_fail_closed_until_slice_c(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    delivery = _write_synthetic_delivery(tmp_path)
    output = tmp_path / "release"
    history = tmp_path / "history"
    intake = data_intake.load_intake(delivery)
    original = intake.series_by_symbol["QQQ"]
    changed = original.bars[0]
    changed_series = type(original)(
        symbol="QQQ",
        interval=original.interval,
        bars=(
            type(changed)(
                symbol=changed.symbol,
                source=changed.source,
                exchange=changed.exchange,
                interval=changed.interval,
                session_date=changed.session_date,
                timestamp_utc=changed.timestamp_utc,
                open=changed.open + 1,
                high=changed.high + 1,
                low=changed.low + 1,
                close=changed.close + 1,
                volume=changed.volume,
            ),
            *original.bars[1:],
        ),
    )
    store.write_bars(
        history,
        changed_series,
        source=intake.provenance.source_id,
        captured_at="2026-09-04T00:00:00+00:00",
    )
    before = (history / "bars/QQQ.csv").read_bytes()
    monkeypatch.setattr("chronos.research.data_certification.HISTORY_ROOT", history)

    code = main(["data", "certify", "--delivery", str(delivery), "--output", str(output)])

    stdout = capsys.readouterr().out
    assert code == 2
    assert stdout.startswith(f"WRITE_FAILED {history}:")
    assert "stored row" in stdout
    assert (history / "bars/QQQ.csv").read_bytes() == before
    assert output.exists()  # freeze precedes the deliberately fail-closed store merge

