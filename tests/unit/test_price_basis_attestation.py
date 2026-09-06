"""The declared provider price basis is checked, and a split-adjusted feed is refused.

ADR-0059, slice 1. Two properties, and the second is the one that is easy to get wrong:

**The basis is declared and closed.** An unknown value, a non-string, or the dividend-adjusted
member refuses; only ``unadjusted_as_traded`` proceeds.

**A split-free window is NOT evidence of raw levels.** A split *after* the delivered window
rescales the whole series if the provider restates history, and nothing in the pipeline sees
it — certification reconciles split-implied returns only inside the window, ``adjust`` skips
future-dated actions, and its dividend factor divides by the *delivered* close. So
``ibkr_trades_split_adjusted`` refuses even with an empty in-window split set, and the
per-symbol ``no_split_in_window`` declaration is additional evidence, never permission.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from chronos.research.certification import (
    CERTIFICATION_SCHEMA_VERSION,
    ProviderPriceBasis,
    SymbolWindow,
    certify_export,
)
from chronos.research.data_intake import CAMPAIGN_SYMBOLS, IntakeUnverified, load_intake
from chronos.research.session_calendar import SessionCalendar

_START = date(2024, 1, 2)
_END = date(2024, 3, 28)
_SESSIONS = list(SessionCalendar().sessions(_START, _END))
_SPLIT_SYMBOL = "QQQ"

# Distinct from None, which is itself one of the non-boolean values under test.
_DERIVE_FROM_FIXTURE = object()


def _bars_csv(sessions: list[date]) -> bytes:
    rows = ["date,open,high,low,close,volume"]
    rows += [f"{d.isoformat()},100.0,101.0,99.0,100.0,1000000.0" for d in sessions]
    return ("\n".join(rows) + "\n").encode()


def _split_action(ex_date: date) -> dict[str, Any]:
    return {
        "kind": "split",
        "ex_date": ex_date.isoformat(),
        "value": 2.0,
        "source": "synthetic-test-only",
        "note": "synthetic fixture",
    }


def build_delivery(
    root: Path,
    *,
    basis: object = "unadjusted_as_traded",
    split_on: date | None = None,
    split_symbol: str = _SPLIT_SYMBOL,
    no_split_in_window: object = _DERIVE_FROM_FIXTURE,
    schema_version: int = 2,
) -> Path:
    """A six-symbol delivery. Synthetic bytes only — never market data.

    ``no_split_in_window`` defaults to the truth for the fixture, so a test that wants a false
    declaration must say so and cannot get one by accident.
    """

    delivery = root / "delivery"
    (delivery / "bars").mkdir(parents=True, exist_ok=True)
    (delivery / "corporate_actions").mkdir(parents=True, exist_ok=True)
    bar_bytes = _bars_csv(_SESSIONS)
    entries: dict[str, Any] = {}
    windows = []
    for symbol in CAMPAIGN_SYMBOLS:
        (delivery / "bars" / f"{symbol}.csv").write_bytes(bar_bytes)
        actions: list[dict[str, Any]] = []
        if split_on is not None and symbol == split_symbol:
            actions.append(_split_action(split_on))
        action_bytes = (json.dumps(actions, indent=2, sort_keys=True) + "\n").encode()
        (delivery / "corporate_actions" / f"{symbol}.json").write_bytes(action_bytes)
        in_window = split_on is not None and symbol == split_symbol and _START <= split_on <= _END
        declared: object = (
            (not in_window) if no_split_in_window is _DERIVE_FROM_FIXTURE else no_split_in_window
        )
        entries[symbol] = {
            "window": {"start": _START.isoformat(), "end": _END.isoformat()},
            "bars_sha256": hashlib.sha256(bar_bytes).hexdigest(),
            "bar_count": len(_SESSIONS),
            "corporate_actions_sha256": hashlib.sha256(action_bytes).hexdigest(),
            "corporate_action_count": len(actions),
            "no_split_in_window": declared,
        }
        windows.append({"symbol": symbol, "start": _START.isoformat(), "end": _END.isoformat()})
    manifest = {
        "schema_version": schema_version,
        "delivery_id": "synthetic-test-only-do-not-use",
        "supersedes": None,
        "interval": "1d",
        "adjustment_policy": "unadjusted_as_traded",
        "provider_price_basis": basis,
        "provenance": {
            "source_id": "synthetic-test-generator-not-a-vendor",
            "source_receipt_sha256": "0" * 64,
            "retrieved_at": "2026-09-06T00:00:00Z",
            "retrieval_method": "generated inside pytest tmp_path",
            "license_note": "synthetic fixture; not licensed market data",
        },
        "symbols": entries,
        "corporate_action_attestation": {
            "kind": "reviewed_no_actions",
            "source_id": "synthetic-independent-review-fixture",
            "windows": windows,
            "note": "synthetic fixture only; no market-data claim",
        },
        "classified_moves": [],
        "holdout_map": [
            {
                "symbol": symbol,
                "name": f"{symbol.lower()}-synthetic-seen",
                "start": _START.isoformat(),
                "end": _END.isoformat(),
                "status": "seen",
            }
            for symbol in CAMPAIGN_SYMBOLS
        ],
    }
    (delivery / "INTAKE.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return delivery


# ------------------------------------------------------------------ the refusal table


@pytest.mark.parametrize("split_on", [None, date(2024, 2, 15)], ids=["no-split", "in-window"])
def test_a_raw_basis_is_accepted_either_way(tmp_path: Path, split_on: date | None) -> None:
    """The raw declaration is the only accepted one, and an in-window split does not block it.

    A declared split under a raw basis is the existing reconciliation's business
    (UNRECONCILED_SPLIT at certification time), not the basis check's.
    """

    intake = load_intake(build_delivery(tmp_path, split_on=split_on))
    assert intake.provider_price_basis is ProviderPriceBasis.UNADJUSTED_AS_TRADED


@pytest.mark.parametrize("split_on", [None, date(2024, 2, 15)], ids=["no-split", "in-window"])
def test_a_split_adjusted_basis_refuses_even_with_no_in_window_split(
    tmp_path: Path, split_on: date | None
) -> None:
    """Mutation row 1. The no-split case is the whole point: it must NOT be an escape hatch.

    Asserting the exact reason, not merely that it refused: removing the basis rule leaves the
    pre-existing exit-1 UNRECONCILED_SPLIT for the in-window case, so a generic "it refused"
    assertion would survive the mutation this test exists to kill.
    """

    delivery = build_delivery(tmp_path, basis="ibkr_trades_split_adjusted", split_on=split_on)
    with pytest.raises(IntakeUnverified) as caught:
        load_intake(delivery)
    assert "provider_price_basis ibkr_trades_split_adjusted cannot satisfy" in caught.value.reason
    assert "a split after the delivered window rescales the whole series" in caught.value.reason


def test_a_dividend_adjusted_basis_is_refused_outright(tmp_path: Path) -> None:
    delivery = build_delivery(tmp_path, basis="ibkr_adjusted_last_total_return")
    with pytest.raises(IntakeUnverified) as caught:
        load_intake(delivery)
    assert "split- AND dividend-adjusted" in caught.value.reason
    assert "no declaration rescues it" in caught.value.reason


@pytest.mark.parametrize(
    "basis", ["", "raw", "unadjusted", "IBKR_TRADES_SPLIT_ADJUSTED", "total_return"]
)
def test_an_unknown_basis_refuses_without_coercion(tmp_path: Path, basis: str) -> None:
    with pytest.raises(IntakeUnverified) as caught:
        load_intake(build_delivery(tmp_path, basis=basis))
    assert "is not one of" in caught.value.reason


@pytest.mark.parametrize("basis", [1, True, None, ["unadjusted_as_traded"]])
def test_a_non_string_basis_refuses(tmp_path: Path, basis: object) -> None:
    with pytest.raises(IntakeUnverified) as caught:
        load_intake(build_delivery(tmp_path, basis=basis))
    assert "must be a string" in caught.value.reason


def test_a_missing_basis_refuses(tmp_path: Path) -> None:
    delivery = build_delivery(tmp_path)
    manifest = json.loads((delivery / "INTAKE.json").read_text())
    del manifest["provider_price_basis"]
    (delivery / "INTAKE.json").write_text(json.dumps(manifest, indent=2))
    with pytest.raises(IntakeUnverified) as caught:
        load_intake(delivery)
    assert "provider_price_basis" in caught.value.reason


# ------------------------------------------- the per-symbol declaration, both directions


@pytest.mark.parametrize(
    "split_on", [_START, date(2024, 2, 15), _END], ids=["first", "mid", "last"]
)
def test_a_true_declaration_over_an_in_window_split_refuses_and_names_the_symbol(
    tmp_path: Path, split_on: date
) -> None:
    """Mutation row 2, isolated from the basis check by declaring the RAW basis.

    Window edges are included: an inclusive bound that silently became exclusive would let a
    split on the first or last session through.
    """

    delivery = build_delivery(tmp_path, split_on=split_on, no_split_in_window=True)
    with pytest.raises(IntakeUnverified) as caught:
        load_intake(delivery)
    assert f"{_SPLIT_SYMBOL}: no_split_in_window is true but" in caught.value.reason
    assert split_on.isoformat() in caught.value.reason


@pytest.mark.parametrize("symbol", CAMPAIGN_SYMBOLS)
def test_the_declaration_is_checked_for_every_symbol(tmp_path: Path, symbol: str) -> None:
    delivery = build_delivery(
        tmp_path, split_on=date(2024, 2, 15), split_symbol=symbol, no_split_in_window=True
    )
    with pytest.raises(IntakeUnverified) as caught:
        load_intake(delivery)
    assert f"{symbol}: no_split_in_window is true but" in caught.value.reason


def test_a_false_declaration_over_a_split_free_window_refuses(tmp_path: Path) -> None:
    """Mutation row 3: the direction a template-copied value gets wrong."""

    delivery = build_delivery(tmp_path, split_on=None, no_split_in_window=False)
    with pytest.raises(IntakeUnverified) as caught:
        load_intake(delivery)
    assert (
        "no_split_in_window is false but the action file declares no split" in caught.value.reason
    )


@pytest.mark.parametrize("declared", [1, 0, "true", "false", None], ids=repr)
def test_the_declaration_must_be_a_json_boolean(tmp_path: Path, declared: object) -> None:
    """isinstance(True, int) is True, so an unguarded check accepts 1, 0 and strings."""

    with pytest.raises(IntakeUnverified) as caught:
        load_intake(build_delivery(tmp_path, no_split_in_window=declared))
    assert "must be a JSON boolean" in caught.value.reason


def test_a_split_after_the_window_is_accepted_and_proves_nothing(tmp_path: Path) -> None:
    """The residual, pinned as behaviour: this is exactly the case the basis rule exists for.

    A split dated after the window is not an in-window split, so the declaration is true and
    the raw delivery loads. Under a split-adjusted feed that same series could have been
    rescaled by it — which is why no split-adjusted delivery is accepted at all.
    """

    intake = load_intake(build_delivery(tmp_path, split_on=date(2024, 6, 3)))
    assert intake.provider_price_basis is ProviderPriceBasis.UNADJUSTED_AS_TRADED


def test_the_schema_version_moved_to_two(tmp_path: Path) -> None:
    with pytest.raises(IntakeUnverified) as caught:
        load_intake(build_delivery(tmp_path, schema_version=1))
    assert "schema_version must be 2" in caught.value.reason


# ------------------------------------------------------- the report and release records


def _report(basis: ProviderPriceBasis):
    """A minimal certification, built directly.

    Only one basis is ACCEPTED by the intake parser, so two production parses cannot produce
    two bases to compare. Calling certify_export directly is what lets the digest difference
    be measured at all (Astra's precision note on mutation row 4).
    """

    return certify_export(
        dataset_id="synthetic-basis-fixture",
        windows=[SymbolWindow("SPY", _START, _END)],
        series_by_symbol={},
        actions_by_symbol={},
        attestation=None,
        provider_price_basis=basis,
        calendar=SessionCalendar(),
    )


def test_the_report_records_the_basis(tmp_path: Path) -> None:
    """Mutation row 4a: presence in the mapping. This alone catches a dropped field."""

    mapping = json.loads(_report(ProviderPriceBasis.UNADJUSTED_AS_TRADED).canonical_json())
    assert mapping["provider_price_basis"] == "unadjusted_as_traded"
    assert mapping["schema_version"] == CERTIFICATION_SCHEMA_VERSION


def test_the_basis_is_bound_into_the_certification_digest() -> None:
    """Mutation row 4b: presence is not binding. Two reports differing ONLY in basis must
    produce different digests, or the field is decoration the digest does not cover."""

    raw = _report(ProviderPriceBasis.UNADJUSTED_AS_TRADED)
    adjusted = _report(ProviderPriceBasis.IBKR_TRADES_SPLIT_ADJUSTED)
    assert raw.certification_digest != adjusted.certification_digest
