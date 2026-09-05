"""Read-only owner-delivery intake verification contract tests."""

from __future__ import annotations

import hashlib
import json
import socket
from datetime import date
from pathlib import Path

import pytest

from chronos.cli.main import main
from chronos.research.session_calendar import SessionCalendar

_SYMBOLS = ("QQQ", "SPY", "IWM", "DIA", "GLD", "TLT")
_START = date(2024, 1, 2)
_END = date(2024, 3, 28)


def _snapshot(root: Path) -> tuple[tuple[str, int, int], ...]:
    return tuple(
        sorted(
            (
                str(path.relative_to(root)),
                path.stat().st_size,
                path.stat().st_mtime_ns,
            )
            for path in root.rglob("*")
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
    symbol_entries: dict[str, object] = {}
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
        symbol_entries[symbol] = {
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
        "symbols": symbol_entries,
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


def test_data_verify_certifies_synthetic_delivery_without_network_dotenv_or_writes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    delivery = _write_synthetic_delivery(tmp_path)
    (tmp_path / ".env").write_text(
        "CHRONOS_DATA_DELIVERY=DOTENV_SECRET_SENTINEL\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        socket,
        "socket",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("network attempted")),
    )
    monkeypatch.setattr(
        "chronos.histdata.store.write_bars",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("bar writer called")),
    )
    monkeypatch.setattr(
        "chronos.histdata.store.write_actions",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("action writer called")),
    )
    monkeypatch.setattr(
        "chronos.research.dataset_release.freeze_release",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("release writer called")),
    )
    monkeypatch.setattr(
        "chronos.config.settings.Settings",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("Settings constructed")),
    )
    before = _snapshot(delivery)
    delivery.chmod(0o500)
    try:
        code = main(["data", "verify", "--delivery", str(delivery)])
        after = _snapshot(delivery)
    finally:
        delivery.chmod(0o700)

    output = capsys.readouterr().out
    assert code == 0
    assert output.count("\n") == 1
    assert output.startswith(f"CERTIFIED {delivery / 'INTAKE.json'}: ")
    assert "DOTENV_SECRET_SENTINEL" not in output
    assert before == after


def test_data_verify_missing_intake_is_unverified(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    delivery = tmp_path / "missing"
    delivery.mkdir()

    code = main(["data", "verify", "--delivery", str(delivery)])

    output = capsys.readouterr().out
    assert code == 2
    assert output == f"UNVERIFIED {delivery / 'INTAKE.json'}: file is missing\n"
    assert "PASS" not in output


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        (lambda manifest: "{", "invalid JSON"),
        (lambda manifest: {**manifest, "schema_version": 2}, "schema_version must be 1"),
    ],
)
def test_data_verify_unparseable_or_wrong_schema_is_unverified(
    mutation,
    reason: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    delivery = _write_synthetic_delivery(tmp_path)
    manifest_path = delivery / "INTAKE.json"
    changed = mutation(_manifest(delivery))
    if isinstance(changed, str):
        manifest_path.write_text(changed, encoding="utf-8")
    else:
        _rewrite_manifest(delivery, changed)

    code = main(["data", "verify", "--delivery", str(delivery)])

    output = capsys.readouterr().out
    assert code == 2
    assert output.startswith(f"UNVERIFIED {manifest_path}: {reason}")
    assert output.count("\n") == 1
    assert "PASS" not in output


@pytest.mark.parametrize(
    "relative_path", [Path("bars/DIA.csv"), Path("corporate_actions/DIA.json")]
)
def test_data_verify_requires_the_dia_files_declared_by_the_six_symbol_identity(
    relative_path: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    delivery = _write_synthetic_delivery(tmp_path)
    missing = delivery / relative_path
    missing.unlink()

    code = main(["data", "verify", "--delivery", str(delivery)])

    output = capsys.readouterr().out
    assert code == 2
    assert output == f"UNVERIFIED {missing}: file is missing\n"


def test_data_verify_requires_dia_in_the_manifest_identity(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    delivery = _write_synthetic_delivery(tmp_path)
    manifest = _manifest(delivery)
    symbols = manifest["symbols"]
    assert isinstance(symbols, dict)
    del symbols["DIA"]
    _rewrite_manifest(delivery, manifest)

    code = main(["data", "verify", "--delivery", str(delivery)])

    output = capsys.readouterr().out
    assert code == 2
    assert output.startswith(f"UNVERIFIED {delivery / 'INTAKE.json'}: symbols must be exactly ")
    assert "DIA" in output


@pytest.mark.parametrize(
    "field",
    ["bars_sha256", "bar_count", "corporate_actions_sha256", "corporate_action_count"],
)
def test_data_verify_refuses_manifest_identity_mismatch(
    field: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    delivery = _write_synthetic_delivery(tmp_path)
    manifest = _manifest(delivery)
    symbols = manifest["symbols"]
    assert isinstance(symbols, dict)
    qqq = symbols["QQQ"]
    assert isinstance(qqq, dict)
    qqq[field] = "f" * 64 if field.endswith("sha256") else 1
    _rewrite_manifest(delivery, manifest)

    code = main(["data", "verify", "--delivery", str(delivery)])

    output = capsys.readouterr().out
    assert code == 2
    assert output.startswith("UNVERIFIED ")
    assert field in output
    assert output.count("\n") == 1


def test_data_verify_outside_calendar_is_unverified(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    delivery = _write_synthetic_delivery(tmp_path)
    manifest = _manifest(delivery)
    symbols = manifest["symbols"]
    assert isinstance(symbols, dict)
    dia = symbols["DIA"]
    assert isinstance(dia, dict)
    window = dia["window"]
    assert isinstance(window, dict)
    window["end"] = "2027-01-04"
    _rewrite_manifest(delivery, manifest)

    code = main(["data", "verify", "--delivery", str(delivery)])

    output = capsys.readouterr().out
    assert code == 2
    assert output.startswith(f"UNVERIFIED {delivery / 'INTAKE.json'}: DIA window ")
    assert "outside the pinned calendar range" in output


def test_data_verify_returns_not_certified_only_after_the_gates_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    delivery = _write_synthetic_delivery(tmp_path)
    bar_path = delivery / "bars" / "QQQ.csv"
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

    code = main(["data", "verify", "--delivery", str(delivery)])

    output = capsys.readouterr().out
    assert code == 1
    assert output.startswith(f"NOT_CERTIFIED {delivery / 'INTAKE.json'}: ")
    assert "MISSING_SESSION" in output
    assert output.count("\n") == 1
