"""The synthetic six-symbol store is deterministic, store-shaped, and certifiable.

The fixture exists so lane A's assemble tests and the owner's runbook rehearsal have a store
before any capture runs. That only works if it is byte-stable — hence the pinned digest — and
if what it writes is the store's own schema rather than this generator's idea of one.
"""

from __future__ import annotations

import csv
import hashlib
import json
from datetime import date
from itertools import pairwise
from pathlib import Path

import pytest

from chronos.histdata.holdout import load_holdouts
from chronos.histdata.store import read_actions, read_bars
from chronos.marketdata.quality import validate_series
from chronos.research.certification import (
    MATERIAL_RETURN_THRESHOLD,
    SPLIT_RECONCILIATION_TOLERANCE,
    _split_implied_return,
)
from chronos.research.data_intake import CAMPAIGN_SYMBOLS
from chronos.research.session_calendar import SessionCalendar
from chronos.research.synth_store import SYNTHETIC_SOURCE, generate_store

_START = date(2024, 1, 2)
_END = date(2024, 6, 28)
_SEED = 7

#: Digest of the whole store at (seed 7, 2024-01-02 .. 2024-06-28). A fixture whose bytes
#: move between runs cannot pin anything downstream, so this is the property under test —
#: if a change to the generator is deliberate, re-pin it deliberately.
_PINNED_DIGEST = (
    "1a896d69c848bbaba106fbc8ac30a38615123b1962998a8b051cafb17484d044"  # pragma: allowlist secret
)


def _store_digest(root: Path) -> str:
    sha = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_file():
            sha.update(path.relative_to(root).as_posix().encode())
            sha.update(path.read_bytes())
    return sha.hexdigest()


@pytest.fixture(scope="module")
def store(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("synth-store")
    generate_store(out, seed=_SEED, start=_START, end=_END)
    return out


def test_the_store_is_byte_stable_across_runs(tmp_path: Path) -> None:
    """Two runs of one seed must produce identical bytes, manifest included.

    ``captured_at`` is the trap: it lands in MANIFEST.json, and a wall-clock value would make
    every run differ while every other assertion here still passed.
    """

    a, b = tmp_path / "a", tmp_path / "b"
    generate_store(a, seed=_SEED, start=_START, end=_END)
    generate_store(b, seed=_SEED, start=_START, end=_END)
    assert _store_digest(a) == _store_digest(b)


def test_the_pinned_digest_still_describes_the_generator(store: Path) -> None:
    assert _store_digest(store) == _PINNED_DIGEST, (
        "the synthetic store's bytes changed; if that is deliberate, re-pin _PINNED_DIGEST "
        "and tell whoever consumes this fixture"
    )


def test_a_different_seed_produces_a_different_store(tmp_path: Path, store: Path) -> None:
    """The positive control for the digest test: it must be capable of differing."""

    other = tmp_path / "other"
    generate_store(other, seed=_SEED + 1, start=_START, end=_END)
    assert _store_digest(other) != _store_digest(store)


def test_the_layout_is_the_history_store_layout(store: Path) -> None:
    assert (store / "MANIFEST.json").is_file()
    assert (store / "HOLDOUTS.json").is_file()
    for symbol in CAMPAIGN_SYMBOLS:
        assert (store / "bars" / f"{symbol}.csv").is_file()
        assert (store / "corporate_actions" / f"{symbol}.json").is_file()
    assert {p.name for p in store.iterdir()} == {
        "bars",
        "corporate_actions",
        "MANIFEST.json",
        "HOLDOUTS.json",
    }


def test_every_bar_lands_on_a_real_session(store: Path) -> None:
    """Weekdays are not enough: a bar on a market holiday is an UNEXPECTED_BAR finding."""

    expected = set(SessionCalendar().sessions(_START, _END))
    for symbol in CAMPAIGN_SYMBOLS:
        with (store / "bars" / f"{symbol}.csv").open() as handle:
            days = [date.fromisoformat(row["date"]) for row in csv.DictReader(handle)]
        assert days == sorted(days)
        assert len(set(days)) == len(days)
        assert set(days) == expected
        assert not [d for d in days if d.weekday() >= 5]


def test_the_bars_pass_the_quality_gate_the_store_enforces(store: Path) -> None:
    for symbol in CAMPAIGN_SYMBOLS:
        report = validate_series(read_bars(store, symbol, source=SYNTHETIC_SOURCE))
        assert not report.blocking, (symbol, [i.detail for i in report.issues if i.blocking])


def test_the_manifest_is_the_stores_own_schema(store: Path) -> None:
    """Written through store.write_bars/write_actions, so the fields are the store's."""

    manifest = json.loads((store / "MANIFEST.json").read_text())
    assert manifest["schema_version"] == 1
    assert set(manifest["symbols"]) == set(CAMPAIGN_SYMBOLS)
    for symbol in CAMPAIGN_SYMBOLS:
        bars = manifest["symbols"][symbol]["bars"]
        assert set(bars) == {
            "source",
            "exchange",
            "sha256",
            "rows",
            "start",
            "end",
            "adjusted",
            "captured_at",
            "corrections",
        }
        assert bars["adjusted"] is False, "the store's contract is unadjusted as-traded"
        assert bars["source"] == SYNTHETIC_SOURCE, "synthetic prices must say so"
        assert set(manifest["symbols"][symbol]["corporate_actions"]) == {
            "sha256",
            "count",
            "captured_at",
        }


def test_the_declared_split_is_material_and_reconciles(store: Path) -> None:
    """The point of the fixture: give the material-move check something real to resolve.

    A move at or beyond the material threshold that no action explains blocks certification;
    one that matches its declared ratio does not. This asserts both halves for the split, and
    that no OTHER material move is left unexplained.
    """

    splits = [a for a in read_actions(store, "QQQ") if a.kind.value == "split"]
    assert len(splits) == 1
    split = splits[0]

    with (store / "bars" / "QQQ.csv").open() as handle:
        closes = [
            (date.fromisoformat(r["date"]), float(r["close"])) for r in csv.DictReader(handle)
        ]
    by_date = {d: i for i, (d, _) in enumerate(closes)}
    index = by_date[split.ex_date]
    observed = closes[index][1] / closes[index - 1][1] - 1.0

    assert abs(observed) >= MATERIAL_RETURN_THRESHOLD, "the split must be a material move"
    assert abs(observed - _split_implied_return(split.value)) <= SPLIT_RECONCILIATION_TOLERANCE

    unexplained = [
        (day, close / prior - 1.0)
        for (_, prior), (day, close) in pairwise(closes)
        if abs(close / prior - 1.0) >= MATERIAL_RETURN_THRESHOLD and day != split.ex_date
    ]
    assert not unexplained, f"material moves with no declared action: {unexplained}"


def test_dividends_are_declared_in_native_ex_date_basis(store: Path) -> None:
    """Every dividend-paying symbol carries a stream; the non-payer legitimately carries none."""

    for symbol in CAMPAIGN_SYMBOLS:
        actions = read_actions(store, symbol)
        dividends = [a for a in actions if a.kind.value == "cash_dividend"]
        assert all(a.source == SYNTHETIC_SOURCE for a in actions)
        assert all(a.value > 0 for a in dividends)
        assert list(actions) == sorted(actions, key=lambda a: (a.ex_date, a.kind.value, a.value))
        if symbol == "GLD":
            assert not dividends, "GLD pays no distribution; an empty stream is the truth"
        else:
            assert dividends


def test_the_holdout_declaration_is_loadable_and_covers_the_campaign_symbols(store: Path) -> None:
    windows = load_holdouts(store)
    assert len(windows) == 1
    window = windows[0]
    assert set(window.symbols) == set(CAMPAIGN_SYMBOLS)
    assert window.reason
    assert window.start <= window.end <= _END
    assert all(window.applies_to(symbol) for symbol in CAMPAIGN_SYMBOLS)


def test_an_empty_range_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        generate_store(tmp_path / "backwards", seed=_SEED, start=_END, end=_START)
