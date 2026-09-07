"""Rehearse the synthetic owner-intake chain without touching repository state."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from chronos.cli.main import main
from chronos.research.data_certification import HISTORY_ROOT
from chronos.research.data_intake import certify_loaded_intake, load_intake


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


def _attestation(path: Path) -> Path:
    value = {
        "kind": "sampled_actions",
        "source_id": "synthetic-independent-fixture",
        "sampled_action_count": 1,
        "symbols": ["QQQ", "SPY", "IWM", "DIA", "GLD", "TLT"],
        "note": "synthetic fixture only; not market data",
    }
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def _assemble(store: Path, delivery: Path, attestation: Path, basis: str) -> int:
    return main(
        [
            "data",
            "assemble",
            "--store",
            str(store),
            "--out",
            str(delivery),
            "--delivery-id",
            "synthetic-rehearsal",
            "--source-id",
            "synthetic-test-generator-not-a-vendor",
            "--source-receipt-sha256",
            "0" * 64,
            "--retrieved-at",
            "2026-09-05T00:00:00Z",
            "--retrieval-method",
            "generated inside pytest",
            "--license-note",
            "synthetic fixture; not licensed market data",
            "--provider-price-basis",
            basis,
            "--attestation",
            str(attestation),
        ]
    )


def test_synthetic_capture_assemble_verify_certify_freeze_is_temp_only(
    tmp_path: Path, capsys
) -> None:
    store = tmp_path / "store"
    delivery = tmp_path / "delivery"
    release = tmp_path / "release"
    history = tmp_path / "history"
    attestation = _attestation(tmp_path / "attestation.json")
    before = _snapshot(HISTORY_ROOT)

    assert main(["data", "synth-store", "--out", str(store), "--seed", "7"]) == 0
    assert _assemble(store, delivery, attestation, "unadjusted_as_traded") == 0
    assert main(["data", "verify", "--delivery", str(delivery)]) == 0
    assert (
        main(
            [
                "data",
                "certify",
                "--delivery",
                str(delivery),
                "--output",
                str(release),
                "--history-root",
                str(history),
            ]
        )
        == 0
    )
    after = _snapshot(HISTORY_ROOT)
    assert before == after

    release_doc = json.loads((release / "release.json").read_text(encoding="utf-8"))
    certification = certify_loaded_intake(load_intake(delivery), delivery=delivery)
    assert str(certification.provider_price_basis) == "unadjusted_as_traded"
    assert release_doc["provider_price_basis"] == "unadjusted_as_traded"
    assert set(json.loads((history / "MANIFEST.json").read_text())["symbols"]) == {
        "QQQ",
        "SPY",
        "IWM",
        "DIA",
        "GLD",
        "TLT",
    }

    refused_delivery = tmp_path / "refused-delivery"
    assert _assemble(store, refused_delivery, attestation, "ibkr_trades_split_adjusted") == 0
    assert main(["data", "verify", "--delivery", str(refused_delivery)]) == 2
    assert "ibkr_trades_split_adjusted cannot satisfy adjustment_policy" in capsys.readouterr().out
    assert (
        main(
            [
                "data",
                "certify",
                "--delivery",
                str(delivery),
                "--output",
                str(HISTORY_ROOT / "rehearsal-release"),
                "--history-root",
                str(tmp_path / "guarded-history"),
            ]
        )
        == 2
    )
    assert "inside the repository store" in capsys.readouterr().out
    certify_code = main(
        [
            "data",
            "certify",
            "--delivery",
            str(refused_delivery),
            "--output",
            str(tmp_path / "refused-release"),
            "--history-root",
            str(tmp_path / "refused-history"),
        ]
    )
    assert certify_code == 2
    assert not (tmp_path / "refused-release").exists()
    assert not (tmp_path / "refused-history").exists()
