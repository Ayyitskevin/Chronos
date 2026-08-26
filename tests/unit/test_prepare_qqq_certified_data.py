from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "scripts" / "prepare_qqq_certified_data.py"
SYMBOLS = ("QQQ", "SPY", "IWM", "DIA", "GLD", "TLT")
END_DATE = "2026-08-21"
IBKR_SOURCE_ALIASES = (
    "IBKR corporate actions",
    "ibkrexport",
    "ibkrdata",
    "IBKRHistorical",
    "ibkr2",
    "I.B.K.R. corporate actions",
    "IB-KR corporate actions",
    "interactive-brokers corporate actions",
    "Inter-active Brokers export",
    "Interactive_Brokers export",
    "InteractiveBrokers LLC",
    "interactivebrokersdata",
    "TWS API corporate actions",
    "T.W.S. export",
    "T-WS API export",
    "Trader Workstation data",
    "TraderWorkstation API",
    "TraderWorkstationHistory",
    "IB Gateway export",
    "IBGateway history",
    "IBGatewayExport",
    "ib_async history",
    "ibasync export",
    "ibAsyncCache",
    "TWSAPIArchive",
    "\uff29\uff22\uff2b\uff32 full-width export",
)
INDEPENDENT_ACTION_SOURCE_IDS = (
    "Invesco QQQ distribution history",
    "State Street SPDR distribution schedule",
    "iShares TLT distribution history",
    "SPDR Gold Shares sponsor archive",
    "official sponsor history for IBEX",
)
INDEPENDENT_ATTESTATION_SOURCE_IDS = (
    "Nasdaq dividend history 2026-08-21",
    "Cboe corporate actions review 2026-08-21",
    "LSEG distribution history 2026-08-21",
    "official exchange bulletin review 2026-08-21",
)
NON_IBKR_LEXICAL_CONTROLS = (
    "net worth statement archive",
    "outwards settlement file",
    "shortwave data mirror",
    "TWSE market data",
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(HELPER), *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def _write_capture(tmp_path: Path) -> tuple[Path, Path, Path]:
    history_root = tmp_path / "history"
    bars_root = history_root / "bars"
    actions_root = tmp_path / "owner_actions"
    bars_root.mkdir(parents=True)
    actions_root.mkdir()
    manifest: dict[str, Any] = {"schema_version": 1, "symbols": {}}
    for symbol in SYMBOLS:
        start = "2000-08-21"
        payload = (
            "date,open,high,low,close,volume\n"
            f"{start},100,101,99,100,1000\n"
            f"{END_DATE},200,201,199,200,2000\n"
        )
        path = bars_root / f"{symbol}.csv"
        path.write_text(payload, encoding="utf-8")
        manifest["symbols"][symbol] = {
            "bars": {
                "source": "ibkr",
                "exchange": "SMART",
                "sha256": _sha256(path),
                "rows": 2,
                "start": start,
                "end": END_DATE,
                "adjusted": False,
                "captured_at": "2026-08-21T22:00:00+00:00",
                "corrections": [],
            }
        }
        action = []
        if symbol != "GLD":
            action = [
                {
                    "kind": "cash_dividend",
                    "ex_date": ex_date,
                    "value": 0.5,
                    "source": f"official sponsor history for {symbol}",
                    "note": "native ex-date amount",
                }
                for ex_date in ("2024-03-21", "2024-06-21", "2024-09-20")
            ]
        (actions_root / f"{symbol}.json").write_text(json.dumps(action) + "\n", encoding="utf-8")
    (history_root / "MANIFEST.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    capture_log = tmp_path / "capture.jsonl"
    capture_log.write_text(
        "".join(
            json.dumps(
                {
                    "symbol": symbol,
                    "rows": 2,
                    "added": 2,
                    "empty_chunks": [],
                    "error": None,
                }
            )
            + "\n"
            for symbol in SYMBOLS
        ),
        encoding="utf-8",
    )
    return history_root, actions_root, capture_log


def _prepare_packet(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    history_root, actions_root, capture_log = _write_capture(tmp_path)
    ingest = _run(
        "ingest-actions",
        "--history-root",
        str(history_root),
        "--input-root",
        str(actions_root),
    )
    assert ingest.returncode == 0, ingest.stderr
    receipt = tmp_path / "source_receipt.json"
    finalized = _run(
        "finalize-receipt",
        "--history-root",
        str(history_root),
        "--capture-log",
        str(capture_log),
        "--output",
        str(receipt),
    )
    assert finalized.returncode == 0, finalized.stderr
    declaration = tmp_path / "certify.json"
    built = _run(
        "build-declaration",
        "--history-root",
        str(history_root),
        "--source-receipt",
        str(receipt),
        "--output",
        str(declaration),
        "--attestation-source-id",
        "nasdaq-independent-sample-2026-08-21",
        "--attestation-count",
        "12",
    )
    assert built.returncode == 0, built.stderr
    return history_root, capture_log, receipt, declaration


def test_packet_binds_bars_actions_receipt_and_conservative_holdout(tmp_path: Path) -> None:
    history_root, _capture_log, receipt, declaration = _prepare_packet(tmp_path)

    manifest = json.loads((history_root / "MANIFEST.json").read_text(encoding="utf-8"))
    for symbol in SYMBOLS:
        actions = manifest["symbols"][symbol]["corporate_actions"]
        assert len(actions["sha256"]) == 64
        assert actions["count"] == (0 if symbol == "GLD" else 3)

    receipt_document = json.loads(receipt.read_text(encoding="utf-8"))
    assert receipt_document["capture"]["symbols"] == list(SYMBOLS)
    assert receipt_document["capture"]["end_date"] == END_DATE
    assert receipt_document["capture"]["bar_interval"] == "1d"
    assert receipt_document["credentials_included"] is False
    assert receipt_document["account_identifiers_included"] is False
    for symbol in SYMBOLS:
        assert len(receipt_document["files"][symbol]["bars_sha256"]) == 64
        assert len(receipt_document["files"][symbol]["actions_sha256"]) == 64

    document = json.loads(declaration.read_text(encoding="utf-8"))
    assert document["dataset_id"] == "chronos-qqq-robustness-daily-v1"
    assert document["catalog_id"] == "chronos-qqq-robustness-daily-v1-release-001"
    assert document["source_receipt_sha256"] == _sha256(receipt)
    assert document["attestation"]["kind"] == "sampled_actions"
    assert document["attestation"]["symbols"] == list(SYMBOLS)
    assert document["attestation"]["sampled_action_count"] == 12
    burned = [span for span in document["holdout_map"] if span["status"] == "burned"]
    assert burned == [
        {
            "symbol": "QQQ",
            "name": "burned-prior-qqq",
            "start": "2022-01-01",
            "end": "2024-01-10",
            "status": "burned",
            "reason": (
                "QQQ 2022-01-01 through 2024-01-10 was consumed by prior Chronos "
                "research and can never be represented as clean"
            ),
        }
    ]
    clean = [span for span in document["holdout_map"] if span["status"] == "clean"]
    assert {span["symbol"] for span in clean} == set(SYMBOLS)
    assert {span["start"] for span in clean} == {"2024-01-11"}
    assert {span["end"] for span in clean} == {END_DATE}


def test_declaration_preserves_only_classified_moves_on_resume(tmp_path: Path) -> None:
    history_root, _capture_log, receipt, declaration = _prepare_packet(tmp_path)
    document = json.loads(declaration.read_text(encoding="utf-8"))
    document["classified_moves"] = [
        {
            "symbol": "QQQ",
            "session_date": "2020-03-16",
            "reason": "documented market-wide circuit-breaker session",
        }
    ]
    declaration.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    resumed = _run(
        "build-declaration",
        "--history-root",
        str(history_root),
        "--source-receipt",
        str(receipt),
        "--output",
        str(declaration),
        "--attestation-source-id",
        "nasdaq-independent-sample-2026-08-21",
        "--attestation-count",
        "12",
    )
    assert resumed.returncode == 0, resumed.stderr

    document["holdout_map"][-1]["start"] = "2024-02-01"
    declaration.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    changed = _run(
        "build-declaration",
        "--history-root",
        str(history_root),
        "--source-receipt",
        str(receipt),
        "--output",
        str(declaration),
        "--attestation-source-id",
        "nasdaq-independent-sample-2026-08-21",
        "--attestation-count",
        "12",
    )
    assert changed.returncode == 1
    assert "only classified_moves may be added" in changed.stderr


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ("missing_symbol", "manifest symbol identity differs"),
        ("wrong_cutoff", "expected '2026-08-21'"),
        ("capture_error", "capture reported an error"),
        ("unexpected_capture_field", "unexpected fields"),
    ],
)
def test_receipt_refuses_campaign_identity_drift(
    tmp_path: Path, mutation: str, expected: str
) -> None:
    history_root, actions_root, capture_log = _write_capture(tmp_path)
    if mutation == "missing_symbol":
        manifest_path = history_root / "MANIFEST.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["symbols"].pop("TLT")
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    elif mutation == "wrong_cutoff":
        manifest_path = history_root / "MANIFEST.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["symbols"]["QQQ"]["bars"]["end"] = "2026-08-20"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    elif mutation == "capture_error":
        rows = [json.loads(line) for line in capture_log.read_text(encoding="utf-8").splitlines()]
        rows[0]["error"] = "gateway refused"
        capture_log.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    else:
        rows = [json.loads(line) for line in capture_log.read_text(encoding="utf-8").splitlines()]
        rows[0]["account_id"] = "must-not-enter-a-sanitized-receipt"
        capture_log.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

    if mutation not in {"capture_error", "unexpected_capture_field"}:
        result = _run(
            "ingest-actions",
            "--history-root",
            str(history_root),
            "--input-root",
            str(actions_root),
        )
    else:
        ingest = _run(
            "ingest-actions",
            "--history-root",
            str(history_root),
            "--input-root",
            str(actions_root),
        )
        assert ingest.returncode == 0, ingest.stderr
        result = _run(
            "finalize-receipt",
            "--history-root",
            str(history_root),
            "--capture-log",
            str(capture_log),
            "--output",
            str(tmp_path / "receipt.json"),
        )
    assert result.returncode == 1
    assert expected in result.stderr


@pytest.mark.parametrize("source", IBKR_SOURCE_ALIASES)
def test_action_ingest_refuses_ibkr_family_as_the_independent_action_source(
    tmp_path: Path, source: str
) -> None:
    history_root, actions_root, _capture_log = _write_capture(tmp_path)
    qqq = json.loads((actions_root / "QQQ.json").read_text(encoding="utf-8"))
    qqq[0]["source"] = source
    (actions_root / "QQQ.json").write_text(json.dumps(qqq), encoding="utf-8")
    result = _run(
        "ingest-actions",
        "--history-root",
        str(history_root),
        "--input-root",
        str(actions_root),
    )
    assert result.returncode == 1
    assert "action stream must be independent" in result.stderr


@pytest.mark.parametrize("source_id", IBKR_SOURCE_ALIASES)
def test_declaration_refuses_ibkr_family_as_the_attestation_source(
    tmp_path: Path, source_id: str
) -> None:
    history_root, _capture_log, receipt, _declaration = _prepare_packet(tmp_path)
    result = _run(
        "build-declaration",
        "--history-root",
        str(history_root),
        "--source-receipt",
        str(receipt),
        "--output",
        str(tmp_path / "blocked-declaration.json"),
        "--attestation-source-id",
        source_id,
        "--attestation-count",
        "12",
    )
    assert result.returncode == 1
    assert "attestation source must be independent" in result.stderr


@pytest.mark.parametrize("source", INDEPENDENT_ACTION_SOURCE_IDS)
def test_action_ingest_accepts_clear_non_ibkr_source_identities(
    tmp_path: Path, source: str
) -> None:
    history_root, actions_root, _capture_log = _write_capture(tmp_path)
    qqq = json.loads((actions_root / "QQQ.json").read_text(encoding="utf-8"))
    qqq[0]["source"] = source
    (actions_root / "QQQ.json").write_text(json.dumps(qqq), encoding="utf-8")
    result = _run(
        "ingest-actions",
        "--history-root",
        str(history_root),
        "--input-root",
        str(actions_root),
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("source_id", INDEPENDENT_ATTESTATION_SOURCE_IDS)
def test_declaration_accepts_clear_non_ibkr_source_identities(
    tmp_path: Path, source_id: str
) -> None:
    history_root, _capture_log, receipt, _declaration = _prepare_packet(tmp_path)
    output = tmp_path / "independent-declaration.json"
    result = _run(
        "build-declaration",
        "--history-root",
        str(history_root),
        "--source-receipt",
        str(receipt),
        "--output",
        str(output),
        "--attestation-source-id",
        source_id,
        "--attestation-count",
        "12",
    )
    assert result.returncode == 0, result.stderr
    declaration = json.loads(output.read_text(encoding="utf-8"))
    assert declaration["attestation"]["source_id"] == source_id


@pytest.mark.parametrize("source", NON_IBKR_LEXICAL_CONTROLS)
def test_action_ingest_does_not_substring_block_unrelated_tws_letters(
    tmp_path: Path, source: str
) -> None:
    history_root, actions_root, _capture_log = _write_capture(tmp_path)
    qqq = json.loads((actions_root / "QQQ.json").read_text(encoding="utf-8"))
    qqq[0]["source"] = source
    (actions_root / "QQQ.json").write_text(json.dumps(qqq), encoding="utf-8")
    result = _run(
        "ingest-actions",
        "--history-root",
        str(history_root),
        "--input-root",
        str(actions_root),
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("source_id", NON_IBKR_LEXICAL_CONTROLS)
def test_declaration_does_not_substring_block_unrelated_tws_letters(
    tmp_path: Path, source_id: str
) -> None:
    history_root, _capture_log, receipt, _declaration = _prepare_packet(tmp_path)
    output = tmp_path / "lexical-control-declaration.json"
    result = _run(
        "build-declaration",
        "--history-root",
        str(history_root),
        "--source-receipt",
        str(receipt),
        "--output",
        str(output),
        "--attestation-source-id",
        source_id,
        "--attestation-count",
        "12",
    )
    assert result.returncode == 0, result.stderr


def test_declaration_refuses_an_all_empty_action_panel(tmp_path: Path) -> None:
    history_root, actions_root, capture_log = _write_capture(tmp_path)
    for symbol in SYMBOLS:
        (actions_root / f"{symbol}.json").write_text("[]\n", encoding="utf-8")
    ingest = _run(
        "ingest-actions",
        "--history-root",
        str(history_root),
        "--input-root",
        str(actions_root),
    )
    assert ingest.returncode == 0, ingest.stderr
    receipt = tmp_path / "source_receipt.json"
    finalized = _run(
        "finalize-receipt",
        "--history-root",
        str(history_root),
        "--capture-log",
        str(capture_log),
        "--output",
        str(receipt),
    )
    assert finalized.returncode == 0, finalized.stderr

    result = _run(
        "build-declaration",
        "--history-root",
        str(history_root),
        "--source-receipt",
        str(receipt),
        "--output",
        str(tmp_path / "certify.json"),
        "--attestation-source-id",
        "nasdaq-independent-sample-2026-08-21",
        "--attestation-count",
        "12",
    )

    assert result.returncode == 1
    assert "all-empty six-symbol corporate-action panel" in result.stderr


def test_declaration_refuses_more_sampled_actions_than_were_ingested(tmp_path: Path) -> None:
    history_root, actions_root, capture_log = _write_capture(tmp_path)
    ingest = _run(
        "ingest-actions",
        "--history-root",
        str(history_root),
        "--input-root",
        str(actions_root),
    )
    assert ingest.returncode == 0, ingest.stderr
    receipt = tmp_path / "source_receipt.json"
    finalized = _run(
        "finalize-receipt",
        "--history-root",
        str(history_root),
        "--capture-log",
        str(capture_log),
        "--output",
        str(receipt),
    )
    assert finalized.returncode == 0, finalized.stderr

    result = _run(
        "build-declaration",
        "--history-root",
        str(history_root),
        "--source-receipt",
        str(receipt),
        "--output",
        str(tmp_path / "certify.json"),
        "--attestation-source-id",
        "nasdaq-independent-sample-2026-08-21",
        "--attestation-count",
        "16",
    )

    assert result.returncode == 1
    assert "cannot attest to 16 sampled actions when only 15 were ingested" in result.stderr


def test_receipt_refuses_a_manifest_count_that_disagrees_with_action_bytes(
    tmp_path: Path,
) -> None:
    history_root, actions_root, capture_log = _write_capture(tmp_path)
    ingest = _run(
        "ingest-actions",
        "--history-root",
        str(history_root),
        "--input-root",
        str(actions_root),
    )
    assert ingest.returncode == 0, ingest.stderr
    manifest_path = history_root / "MANIFEST.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["symbols"]["QQQ"]["corporate_actions"]["count"] = 99
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    result = _run(
        "finalize-receipt",
        "--history-root",
        str(history_root),
        "--capture-log",
        str(capture_log),
        "--output",
        str(tmp_path / "source_receipt.json"),
    )

    assert result.returncode == 1
    assert "manifest count 99 does not match 3 parsed corporate actions" in result.stderr


def test_release_verification_requires_every_symbol_holdout_and_matching_bytes(
    tmp_path: Path,
) -> None:
    _history_root, _capture_log, receipt, declaration = _prepare_packet(tmp_path)
    release_root = tmp_path / "release"
    release_root.mkdir()
    entries = []
    for symbol in SYMBOLS:
        for name, classification in (("seen", "ordinary"), ("final-clean", "holdout")):
            relative = f"{symbol}/{name}.csv"
            target = release_root / relative
            target.parent.mkdir(exist_ok=True)
            target.write_text(f"date,open,high,low,close,volume\n{END_DATE},1,1,1,1,1\n")
            entries.append(
                {
                    "dataset_id": "chronos-qqq-robustness-daily-v1",
                    "partition": f"{symbol}:{name}",
                    "data_version": _sha256(target),
                    "source_id": "ibkr-tws-historical",
                    "source_receipt_sha256": _sha256(receipt),
                    "classification": classification,
                    "path": relative,
                    "sha256": _sha256(target),
                    "byte_count": target.stat().st_size,
                }
            )
    catalog = {
        "schema_version": "chronos-certified-data-catalog-v1",
        "catalog_id": "chronos-qqq-robustness-daily-v1-release-001",
        "entries": entries,
    }
    catalog_path = release_root / "catalog.json"
    catalog_path.write_bytes(
        json.dumps(catalog, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    release = {
        "dataset_id": "chronos-qqq-robustness-daily-v1",
        "catalog_id": "chronos-qqq-robustness-daily-v1-release-001",
        "source_id": "ibkr-tws-historical",
        "source_receipt_sha256": _sha256(receipt),
        "interval": "1d",
        "catalog_manifest_sha256": _sha256(catalog_path),
        "holdout_map": json.loads(declaration.read_text(encoding="utf-8"))["holdout_map"],
    }
    (release_root / "release.json").write_text(json.dumps(release), encoding="utf-8")

    verified = _run(
        "verify-release",
        "--release-root",
        str(release_root),
        "--source-receipt",
        str(receipt),
        "--declaration",
        str(declaration),
    )
    assert verified.returncode == 0, verified.stderr
    assert f"catalog sha256 {_sha256(catalog_path)}" in verified.stdout

    (release_root / "QQQ" / "final-clean.csv").write_text("tampered\n", encoding="utf-8")
    tampered = _run(
        "verify-release",
        "--release-root",
        str(release_root),
        "--source-receipt",
        str(receipt),
        "--declaration",
        str(declaration),
    )
    assert tampered.returncode == 1
    assert "does not match its sha256" in tampered.stderr

    (release_root / "QQQ" / "final-clean.csv").write_text(
        f"date,open,high,low,close,volume\n{END_DATE},1,1,1,1,1\n",
        encoding="utf-8",
    )
    catalog["entries"] = [
        entry
        for entry in entries
        if not (entry["partition"] == "SPY:seen" and entry["classification"] == "ordinary")
    ]
    catalog_path.write_bytes(
        json.dumps(catalog, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    release["catalog_manifest_sha256"] = _sha256(catalog_path)
    (release_root / "release.json").write_text(json.dumps(release), encoding="utf-8")
    incomplete = _run(
        "verify-release",
        "--release-root",
        str(release_root),
        "--source-receipt",
        str(receipt),
        "--declaration",
        str(declaration),
    )
    assert incomplete.returncode == 1
    assert "ordinary and clean identities for all symbols" in incomplete.stderr
