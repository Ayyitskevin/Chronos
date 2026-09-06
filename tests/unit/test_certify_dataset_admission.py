"""The legacy owner CLI admits exactly what the intake CLI admits.

`scripts/certify_dataset.py` is the other entry point that can mint a verdict, and before
ADR-0059's admission rule was shared it PARSED the declared basis, recorded it on the report,
and froze a release for a delivery `chronos data verify` refuses outright — because
`certify_export` records the field and does not judge it. Recording is not admission.

Every case here drives the real `main([...])`, not a forged report or a patched certification,
and asserts three things together: the refusal names its reason, the exit is non-zero, and
**nothing was written**. The last is the one that matters — a refusal that still mints a
catalog is not a refusal.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
from scripts.certify_dataset import main

from chronos.histdata.store import write_actions, write_bars
from chronos.research.certification import ProviderPriceBasis
from chronos.research.session_calendar import SessionCalendar
from chronos.research.synth_store import actions_for, bars_for

# SPY, not QQQ: the synthetic generator puts a 2-for-1 split on QQQ, and a split in the
# window is a separate reconciliation concern that would confound the admission control here.
_SYMBOL = "SPY"
_START = date(2024, 1, 2)
_END = date(2024, 3, 28)
_REFUSED = [
    ProviderPriceBasis.IBKR_TRADES_SPLIT_ADJUSTED,
    ProviderPriceBasis.IBKR_ADJUSTED_LAST_SPLIT_AND_DIVIDEND_ADJUSTED,
]


def _fixture(tmp_path: Path, basis: str) -> tuple[Path, Path, Path]:
    """A minimal certifiable export: synthetic bytes in tmp_path, no market data."""

    sessions = list(SessionCalendar().sessions(_START, _END))
    history = tmp_path / "history"
    write_bars(history, bars_for(_SYMBOL, sessions, seed=11), captured_at=_END.isoformat())
    actions = actions_for(_SYMBOL, sessions)
    write_actions(history, _SYMBOL, actions, captured_at=_END.isoformat())

    declaration = tmp_path / "certify.json"
    declaration.write_text(
        json.dumps(
            {
                "dataset_id": "synthetic-test-only",
                "interval": "1d",
                "catalog_id": "synthetic-test-only-release-001",
                "source_id": "synthetic-test-generator-not-a-vendor",
                "source_receipt_sha256": "0" * 64,
                "provider_price_basis": basis,
                "attestation": {
                    "kind": "sampled_actions",
                    "source_id": "synthetic-independent-review-fixture",
                    "sampled_action_count": len(actions),
                    "symbols": [_SYMBOL],
                    "note": "synthetic fixture only; no market-data claim",
                },
                "windows": [
                    {"symbol": _SYMBOL, "start": _START.isoformat(), "end": _END.isoformat()}
                ],
                "holdout_map": [
                    {
                        "symbol": _SYMBOL,
                        "name": "synthetic-seen",
                        "start": _START.isoformat(),
                        "end": _END.isoformat(),
                        "status": "seen",
                        "reason": "synthetic fixture",
                    }
                ],
                "classified_moves": [],
            }
        ),
        encoding="utf-8",
    )
    return declaration, history, tmp_path / "release"


def _written(output: Path) -> list[Path]:
    return sorted(p for p in output.rglob("*") if p.is_file()) if output.exists() else []


# ------------------------------------------------------------------ the positive control


@pytest.mark.parametrize("command", ["certify", "freeze"])
def test_the_raw_basis_still_certifies_and_freezes(tmp_path: Path, command: str) -> None:
    """Without this, every assertion below could pass because the fixture never certified."""

    declaration, history, output = _fixture(tmp_path, "unadjusted_as_traded")
    argv = [command, "--declaration", str(declaration), "--history-root", str(history)]
    if command == "freeze":
        argv += ["--output", str(output)]

    assert main(argv) == 0
    if command == "freeze":
        assert (output / "catalog.json").is_file()
        assert (output / "release.json").is_file()


# ----------------------------------------------------------------------- the refusals


@pytest.mark.parametrize("basis", _REFUSED, ids=lambda b: b.value)
@pytest.mark.parametrize("command", ["certify", "freeze"])
def test_a_refused_basis_refuses_at_the_legacy_entry_point(
    tmp_path: Path, basis: ProviderPriceBasis, command: str
) -> None:
    declaration, history, output = _fixture(tmp_path, basis.value)
    argv = [command, "--declaration", str(declaration), "--history-root", str(history)]
    if command == "freeze":
        argv += ["--output", str(output)]

    with pytest.raises(SystemExit) as caught:
        main(argv)

    # SystemExit's code is the exit status: a non-zero int, or a message string that argparse
    # and the interpreter both report as exit 1. Either is non-zero; None and 0 are not.
    assert caught.value.code not in (None, 0)
    assert basis.value in str(caught.value)
    assert "unadjusted_as_traded" in str(caught.value)
    assert _written(output) == []


@pytest.mark.parametrize("basis", _REFUSED, ids=lambda b: b.value)
def test_the_refusal_names_why_that_basis_cannot_satisfy_the_contract(
    tmp_path: Path, basis: ProviderPriceBasis
) -> None:
    """The reason is the shared rule's, so the two entry points cannot drift apart."""

    declaration, history, _ = _fixture(tmp_path, basis.value)
    with pytest.raises(SystemExit) as caught:
        main(["certify", "--declaration", str(declaration), "--history-root", str(history)])

    reason = str(caught.value)
    if basis is ProviderPriceBasis.IBKR_TRADES_SPLIT_ADJUSTED:
        assert "a split after the delivered window rescales the whole series" in reason
    else:
        assert "the dividend adjustment is not recoverable from the bars" in reason


@pytest.mark.parametrize("command", ["certify", "freeze"])
def test_a_declaration_without_a_basis_refuses_rather_than_defaulting(
    tmp_path: Path, command: str
) -> None:
    """The older owner packet helper emits no basis. It must fail, not be assumed raw."""

    declaration, history, output = _fixture(tmp_path, "unadjusted_as_traded")
    document = json.loads(declaration.read_text())
    del document["provider_price_basis"]
    declaration.write_text(json.dumps(document), encoding="utf-8")

    argv = [command, "--declaration", str(declaration), "--history-root", str(history)]
    if command == "freeze":
        argv += ["--output", str(output)]

    with pytest.raises(SystemExit) as caught:
        main(argv)
    assert "has no provider_price_basis" in str(caught.value)
    assert _written(output) == []
