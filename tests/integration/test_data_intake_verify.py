"""Read-only owner-delivery intake verification contract tests."""

from __future__ import annotations

import hashlib
import json
import socket
from datetime import date, timedelta
from pathlib import Path

import pytest

from chronos.cli.main import main
from chronos.research.data_intake import CAMPAIGN_SYMBOLS
from chronos.research.session_calendar import SessionCalendar

#: The campaign universe under test IS the production constant, not a copy of it.
_SYMBOLS = CAMPAIGN_SYMBOLS
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
    assert output.startswith(f"CERTIFIED {delivery / 'INTAKE.json'}: certification_report_sha256=")
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


def test_data_verify_refuses_an_incomplete_holdout_map_exactly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    delivery = _write_synthetic_delivery(tmp_path)
    manifest = _manifest(delivery)
    holdout_map = manifest["holdout_map"]
    assert isinstance(holdout_map, list)
    manifest["holdout_map"] = [span for span in holdout_map if span["symbol"] == "QQQ"]
    _rewrite_manifest(delivery, manifest)

    code = main(["data", "verify", "--delivery", str(delivery)])

    output = capsys.readouterr().out
    assert code == 2
    assert output == (
        f"UNVERIFIED {delivery / 'INTAKE.json'}: certification request is invalid "
        "(holdout map symbols differ from the certification request: map-only=[], "
        "certification-only=['DIA', 'GLD', 'IWM', 'SPY', 'TLT'])\n"
    )


@pytest.mark.parametrize(
    ("topology", "reason"),
    [
        ("late", "leaving session 2024-01-02 undeclared"),
        ("early", "leaving session 2024-03-28 undeclared"),
        ("gap", "leaves session 2024-02-02 undeclared"),
        ("overlap", "overlap"),
    ],
)
def test_data_verify_refuses_invalid_holdout_topology(
    topology: str,
    reason: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    delivery = _write_synthetic_delivery(tmp_path)
    manifest = _manifest(delivery)
    spans = manifest["holdout_map"]
    assert isinstance(spans, list)
    qqq = next(span for span in spans if span["symbol"] == "QQQ")
    if topology == "late":
        qqq["start"] = "2024-01-03"
    elif topology == "early":
        qqq["end"] = "2024-03-27"
    elif topology == "gap":
        qqq["end"] = "2024-02-01"
        spans.append({**qqq, "name": "qqq-second", "start": "2024-02-03", "end": "2024-03-28"})
    else:
        qqq["end"] = "2024-02-15"
        spans.append({**qqq, "name": "qqq-second", "start": "2024-02-01", "end": "2024-03-28"})
    _rewrite_manifest(delivery, manifest)

    code = main(["data", "verify", "--delivery", str(delivery)])

    output = capsys.readouterr().out
    assert code == 2
    assert output.startswith(
        f"UNVERIFIED {delivery / 'INTAKE.json'}: certification request is invalid "
    )
    assert reason in output


def test_data_verify_holdout_tiling_covers_pre_window_warmup_rows(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    delivery = _write_synthetic_delivery(tmp_path)
    bar_path = delivery / "bars/QQQ.csv"
    rows = bar_path.read_text(encoding="utf-8").splitlines()
    warmup = rows[1].replace(_START.isoformat(), (_START - timedelta(days=4)).isoformat())
    bar_path.write_text("\n".join([rows[0], warmup, *rows[1:]]) + "\n", encoding="utf-8")
    manifest = _manifest(delivery)
    qqq = manifest["symbols"]["QQQ"]
    raw = bar_path.read_bytes()
    qqq["bars_sha256"] = hashlib.sha256(raw).hexdigest()
    qqq["bar_count"] = len(rows)
    _rewrite_manifest(delivery, manifest)

    code = main(["data", "verify", "--delivery", str(delivery)])

    output = capsys.readouterr().out
    assert code == 2
    assert "leaving session 2023-12-29 undeclared" in output


def test_data_verify_names_the_report_digest_without_claiming_content_identity(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    delivery = _write_synthetic_delivery(tmp_path)
    first_code = main(["data", "verify", "--delivery", str(delivery)])
    first = capsys.readouterr().out

    bar_path = delivery / "bars/QQQ.csv"
    rows = bar_path.read_text(encoding="utf-8").splitlines()
    shifted = [rows[0]]
    for row in rows[1:]:
        day, open_, high, low, close, volume = row.split(",")
        shifted.append(
            ",".join(
                [day, *(str(float(value) + 10.0) for value in (open_, high, low, close)), volume]
            )
        )
    bar_path.write_text("\n".join(shifted) + "\n", encoding="utf-8")
    manifest = _manifest(delivery)
    manifest["symbols"]["QQQ"]["bars_sha256"] = hashlib.sha256(bar_path.read_bytes()).hexdigest()
    _rewrite_manifest(delivery, manifest)

    second_code = main(["data", "verify", "--delivery", str(delivery)])
    second = capsys.readouterr().out

    prefix = f"CERTIFIED {delivery / 'INTAKE.json'}: certification_report_sha256="
    assert first_code == second_code == 0
    assert first.startswith(prefix)
    assert second.startswith(prefix)
    assert first.removeprefix(prefix) == second.removeprefix(prefix)
