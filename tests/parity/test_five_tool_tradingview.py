"""Strict Five-Tool TradingView fixture and first-divergence contracts."""

from __future__ import annotations

import csv
import hashlib
import io
import json
from dataclasses import replace
from datetime import UTC, timedelta
from pathlib import Path
from typing import Any

import pytest

from chronos.research.tradingview import (
    PINNED_PINE_INPUT_COUNT,
    EntryDecision,
    FixtureProvenance,
    FixtureSchemaError,
    ParityStatus,
    compare_trace_fixtures,
    genuine_reference_present,
    load_trace_fixture,
)

FIXTURE_DIR = Path(__file__).parents[1] / "fixtures" / "tradingview_synthetic"
FIXTURE_CSV = FIXTURE_DIR / "five_tool_trace.csv"
FIXTURE_META = FIXTURE_DIR / "five_tool_trace.meta.json"


def _load_synthetic() -> Any:
    return load_trace_fixture(FIXTURE_CSV, FIXTURE_META)


def _copy_fixture(tmp_path: Path) -> tuple[Path, Path]:
    csv_path = tmp_path / "trace.csv"
    meta_path = tmp_path / "trace.meta.json"
    csv_path.write_bytes(FIXTURE_CSV.read_bytes())
    meta_path.write_bytes(FIXTURE_META.read_bytes())
    return csv_path, meta_path


def _read_metadata(path: Path) -> dict[str, Any]:
    loaded: Any = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _write_metadata(path: Path, document: dict[str, Any]) -> None:
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")


def _read_csv(path: Path) -> list[list[str]]:
    return list(csv.reader(io.StringIO(path.read_text(encoding="utf-8"))))


def _write_csv_and_repin(csv_path: Path, meta_path: Path, rows: list[list[str]]) -> None:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerows(rows)
    encoded = buffer.getvalue().encode("utf-8")
    csv_path.write_bytes(encoded)
    metadata = _read_metadata(meta_path)
    metadata["trace_sha256"] = hashlib.sha256(encoded).hexdigest()
    metadata["row_count"] = len(rows) - 1
    _write_metadata(meta_path, metadata)


def test_loads_typed_synthetic_fixture_and_normalizes_timestamp_to_utc() -> None:
    fixture = _load_synthetic()

    assert fixture.metadata.provenance is FixtureProvenance.INTERNAL_SPEC
    assert fixture.metadata.chart_timezone == "America/New_York"
    assert fixture.metadata.session == "0930-1600:23456"
    assert fixture.rows[0].timestamp_utc.tzinfo is UTC
    assert fixture.rows[0].timestamp_utc.isoformat() == "2024-01-02T21:00:00+00:00"
    assert fixture.rows[0].source_timestamp == "2024-01-02T16:00:00-05:00"
    assert fixture.rows[0].regime_z is None
    assert len(fixture.rows[2].state_digest) == 64


def test_synthetic_exact_match_never_claims_tradingview_parity() -> None:
    fixture = _load_synthetic()

    report = compare_trace_fixtures(fixture, fixture)

    assert report.matched is True
    assert report.compared_rows == 3
    assert report.first_divergence is None
    assert report.parity_status is ParityStatus.UNVERIFIED
    assert genuine_reference_present([fixture]) is False


def test_rejects_wrong_pinned_source_hash(tmp_path: Path) -> None:
    csv_path, meta_path = _copy_fixture(tmp_path)
    metadata = _read_metadata(meta_path)
    metadata["pine_sha256"] = "0" * 64
    _write_metadata(meta_path, metadata)

    with pytest.raises(FixtureSchemaError, match="pinned catalog 00 source"):
        load_trace_fixture(csv_path, meta_path)


def test_rejects_input_config_digest_mismatch(tmp_path: Path) -> None:
    csv_path, meta_path = _copy_fixture(tmp_path)
    metadata = _read_metadata(meta_path)
    metadata["input_config"]["strict_markov"] = False
    _write_metadata(meta_path, metadata)

    with pytest.raises(FixtureSchemaError, match="does not match canonical input_config"):
        load_trace_fixture(csv_path, meta_path)


def test_genuine_fixture_requires_complete_219_input_config(tmp_path: Path) -> None:
    csv_path, meta_path = _copy_fixture(tmp_path)
    metadata = _read_metadata(meta_path)
    metadata["provenance"] = "genuine"
    _write_metadata(meta_path, metadata)

    with pytest.raises(
        FixtureSchemaError,
        match=rf"require all {PINNED_PINE_INPUT_COUNT} inputs",
    ):
        load_trace_fixture(csv_path, meta_path)


def test_compare_rejects_config_identity_mismatch_before_rows() -> None:
    fixture = _load_synthetic()
    other_metadata = replace(fixture.metadata, input_config_sha256="f" * 64)
    candidate = replace(fixture, metadata=other_metadata)

    with pytest.raises(FixtureSchemaError, match="metadata mismatch for input_config_sha256"):
        compare_trace_fixtures(fixture, candidate)


def test_timestamp_mismatch_is_not_shifted_or_nearest_neighbor_aligned() -> None:
    fixture = _load_synthetic()
    changed = replace(
        fixture.rows[1],
        timestamp_utc=fixture.rows[1].timestamp_utc + timedelta(minutes=1),
        source_timestamp="2024-01-03T16:01:00-05:00",
    )
    candidate = replace(fixture, rows=(fixture.rows[0], changed, fixture.rows[2]))

    report = compare_trace_fixtures(fixture, candidate)

    assert report.matched is False
    assert report.first_divergence is not None
    assert report.first_divergence.field == "timestamp_utc"
    assert report.first_divergence.timestamp_utc == fixture.rows[1].timestamp_utc
    assert report.compared_rows == 1


def test_named_float_tolerance_accepts_rounding_and_reports_larger_drift() -> None:
    fixture = _load_synthetic()
    within = replace(fixture.rows[1], strength=48.0 + 5e-9)
    within_fixture = replace(fixture, rows=(fixture.rows[0], within, fixture.rows[2]))
    assert compare_trace_fixtures(fixture, within_fixture).matched is True

    outside = replace(fixture.rows[1], strength=48.001)
    outside_fixture = replace(fixture, rows=(fixture.rows[0], outside, fixture.rows[2]))
    report = compare_trace_fixtures(fixture, outside_fixture)

    assert report.first_divergence is not None
    assert report.first_divergence.field == "strength"
    assert report.first_divergence.tolerance is not None
    assert report.first_divergence.tolerance.name == "indicator"
    assert report.first_divergence.tolerance.abs_tol == 1e-8
    assert report.first_divergence.tolerance.rel_tol == 1e-9


def test_first_divergence_includes_state_digests_and_active_gates() -> None:
    fixture = _load_synthetic()
    changed = replace(fixture.rows[2], entry_decision=EntryDecision.NONE)
    candidate = replace(fixture, rows=(*fixture.rows[:2], changed))

    report = compare_trace_fixtures(fixture, candidate)

    assert report.first_divergence is not None
    divergence = report.first_divergence
    assert divergence.field == "entry_decision"
    assert divergence.timestamp_utc == fixture.rows[2].timestamp_utc
    assert divergence.expected_state_digest == fixture.rows[2].state_digest
    assert divergence.actual_state_digest == changed.state_digest
    assert "regime_flip" in divergence.expected_gates
    assert "entry_long" in divergence.expected_gates
    assert "entry_long" not in divergence.actual_gates


@pytest.mark.parametrize("mode", ["missing", "unknown"])
def test_rejects_missing_or_unknown_csv_columns(tmp_path: Path, mode: str) -> None:
    csv_path, meta_path = _copy_fixture(tmp_path)
    rows = _read_csv(csv_path)
    if mode == "missing":
        index = rows[0].index("strength")
        for row in rows:
            del row[index]
    else:
        for index, row in enumerate(rows):
            row.append("mystery" if index == 0 else "")
    _write_csv_and_repin(csv_path, meta_path, rows)

    with pytest.raises(FixtureSchemaError, match="CSV columns do not match strict ordered schema"):
        load_trace_fixture(csv_path, meta_path)


def test_nan_empty_and_null_tokens_normalize_to_none(tmp_path: Path) -> None:
    csv_path, meta_path = _copy_fixture(tmp_path)
    rows = _read_csv(csv_path)
    header = rows[0]
    rows[1][header.index("regime_z")] = "NaN"
    rows[1][header.index("strength")] = ""
    rows[1][header.index("mansfield")] = "null"
    _write_csv_and_repin(csv_path, meta_path, rows)

    fixture = load_trace_fixture(csv_path, meta_path)

    assert fixture.rows[0].regime_z is None
    assert fixture.rows[0].strength is None
    assert fixture.rows[0].mansfield is None


def test_rejects_nonfinite_numeric_value(tmp_path: Path) -> None:
    csv_path, meta_path = _copy_fixture(tmp_path)
    rows = _read_csv(csv_path)
    rows[2][rows[0].index("strength")] = "Infinity"
    _write_csv_and_repin(csv_path, meta_path, rows)

    with pytest.raises(FixtureSchemaError, match="must be finite"):
        load_trace_fixture(csv_path, meta_path)


def test_rejects_out_of_order_rows_instead_of_sorting_them(tmp_path: Path) -> None:
    csv_path, meta_path = _copy_fixture(tmp_path)
    rows = _read_csv(csv_path)
    rows[1], rows[2] = rows[2], rows[1]
    _write_csv_and_repin(csv_path, meta_path, rows)

    with pytest.raises(FixtureSchemaError, match="strictly increasing"):
        load_trace_fixture(csv_path, meta_path)


def test_rejects_unknown_metadata_key(tmp_path: Path) -> None:
    csv_path, meta_path = _copy_fixture(tmp_path)
    metadata = _read_metadata(meta_path)
    metadata["parity_verified"] = True
    _write_metadata(meta_path, metadata)

    with pytest.raises(FixtureSchemaError, match=r"unknown=\['parity_verified'\]"):
        load_trace_fixture(csv_path, meta_path)
