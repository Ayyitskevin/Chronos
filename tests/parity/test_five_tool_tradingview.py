"""Strict Five-Tool TradingView fixture and first-divergence contracts."""

from __future__ import annotations

import csv
import hashlib
import io
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from chronos.marketdata.bars import Bar, BarInterval, BarSeries
from chronos.research.five_tool.alignment import align_five_tool_inputs
from chronos.research.five_tool.engine import evaluate_batch
from chronos.research.five_tool.models import FiveToolBarInput, FiveToolSettings
from chronos.research.tradingview import (
    PINNED_PINE_INPUT_COUNT,
    EntryDecision,
    FixtureProvenance,
    FixtureSchemaError,
    ParityStatus,
    compare_trace_fixtures,
    genuine_reference_present,
    load_trace_fixture,
    trace_row_from_engine,
    trace_rows_from_engine,
)

FIXTURE_DIR = Path(__file__).parents[1] / "fixtures" / "tradingview_synthetic"
FIXTURE_CSV = FIXTURE_DIR / "five_tool_trace.csv"
FIXTURE_META = FIXTURE_DIR / "five_tool_trace.meta.json"


def _genuine_input_config() -> dict[str, bool | int | float | str]:
    return dict(
        FiveToolSettings.defaults(history_start_utc=datetime(2024, 1, 1, tzinfo=UTC)).inputs
    )


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
    assert report.verification_scope == "closed_bar_signal_trace_only"
    assert report.execution_parity_status is ParityStatus.UNVERIFIED
    assert "reference_is_not_a_trusted_owner_export" in report.verification_blockers
    assert (
        "independent_candidate_and_normalizer_attestation_not_implemented"
        in report.verification_blockers
    )
    assert genuine_reference_present([fixture]) is False


def test_copied_trusted_reference_cannot_self_certify() -> None:
    fixture = _load_synthetic()
    forged_metadata = replace(
        fixture.metadata,
        provenance=FixtureProvenance.GENUINE,
        owner_attestation_sha256="a" * 64,
        trusted_owner_export=True,
    )
    reference = replace(fixture, metadata=forged_metadata)
    candidate = replace(fixture, metadata=forged_metadata)

    report = compare_trace_fixtures(reference, candidate)

    assert report.matched is True
    assert report.parity_status is ParityStatus.UNVERIFIED
    assert report.verification_blockers == (
        "independent_candidate_and_normalizer_attestation_not_implemented",
    )


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


def test_arbitrary_219_keys_cannot_forge_genuine_provenance(tmp_path: Path) -> None:
    csv_path, meta_path = _copy_fixture(tmp_path)
    metadata = _read_metadata(meta_path)
    config = {f"not_a_pine_input_{index}": index for index in range(219)}
    metadata["provenance"] = "genuine"
    metadata["input_count"] = len(config)
    metadata["input_config"] = config
    metadata["input_config_sha256"] = hashlib.sha256(
        json.dumps(config, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    metadata["owner_attestation_sha256"] = "a" * 64
    _write_metadata(meta_path, metadata)
    with pytest.raises(FixtureSchemaError, match="exact source-ordered Pine inputs"):
        load_trace_fixture(csv_path, meta_path)


def test_genuine_metadata_requires_detached_out_of_band_attestation(tmp_path: Path) -> None:
    csv_path, meta_path = _copy_fixture(tmp_path)
    metadata = _read_metadata(meta_path)
    config = _genuine_input_config()
    metadata["provenance"] = "genuine"
    metadata["input_count"] = len(config)
    metadata["input_config"] = config
    metadata["input_config_sha256"] = hashlib.sha256(
        json.dumps(config, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    metadata["owner_attestation_sha256"] = "a" * 64
    _write_metadata(meta_path, metadata)
    with pytest.raises(FixtureSchemaError, match="out-of-band owner attestation"):
        load_trace_fixture(csv_path, meta_path)


def test_trusted_genuine_reference_loads_but_match_remains_unverified(
    tmp_path: Path,
) -> None:
    csv_path, meta_path = _copy_fixture(tmp_path)
    attestation_path = tmp_path / "owner-attestation.json"
    attestation_path.write_bytes(b'{"reviewed_export":"fixture-only-test"}\n')
    attestation_digest = hashlib.sha256(attestation_path.read_bytes()).hexdigest()
    metadata = _read_metadata(meta_path)
    config = _genuine_input_config()
    metadata.update(
        {
            "provenance": "genuine",
            "input_count": len(config),
            "input_config": config,
            "input_config_sha256": hashlib.sha256(
                json.dumps(config, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
            "owner_attestation_sha256": attestation_digest,
        }
    )
    _write_metadata(meta_path, metadata)

    reference = load_trace_fixture(
        csv_path,
        meta_path,
        owner_attestation_path=attestation_path,
        trusted_owner_attestation_sha256=attestation_digest,
    )
    candidate = replace(
        reference,
        metadata=replace(
            reference.metadata,
            provenance=FixtureProvenance.INTERNAL_SPEC,
            owner_attestation_sha256=None,
            trusted_owner_export=False,
        ),
    )
    report = compare_trace_fixtures(reference, candidate)

    assert reference.metadata.trusted_owner_export is True
    assert genuine_reference_present([reference]) is True
    assert report.matched is True
    assert report.parity_status is ParityStatus.UNVERIFIED
    assert report.verification_blockers == (
        "independent_candidate_and_normalizer_attestation_not_implemented",
    )


def test_nonfinite_tolerance_is_rejected() -> None:
    from chronos.research.tradingview import FloatTolerance

    with pytest.raises(ValueError, match="non-negative"):
        FloatTolerance("forged", float("inf"), 0.0)


def test_compare_rejects_config_identity_mismatch_before_rows() -> None:
    fixture = _load_synthetic()
    other_metadata = replace(fixture.metadata, input_config_sha256="f" * 64)
    candidate = replace(fixture, metadata=other_metadata)

    with pytest.raises(FixtureSchemaError, match="metadata mismatch for input_config_sha256"):
        compare_trace_fixtures(fixture, candidate)


def test_compare_rejects_data_source_identity_mismatch_before_rows() -> None:
    fixture = _load_synthetic()
    candidate = replace(
        fixture,
        metadata=replace(fixture.metadata, data_source="different-feed-and-adjustment"),
    )

    with pytest.raises(FixtureSchemaError, match="metadata mismatch for data_source"):
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


def test_real_engine_trace_projects_into_comparison_rows_deterministically() -> None:
    start = datetime(2024, 1, 2, 21, tzinfo=UTC)

    def series(symbol: str, scale: float) -> BarSeries:
        bars = tuple(
            Bar(
                symbol=symbol,
                source="internal_spec",
                exchange="AMEX" if symbol == "SPY" else "NYSE",
                interval=BarInterval.DAY_1,
                session_date=(start + timedelta(days=index)).date(),
                timestamp_utc=start + timedelta(days=index),
                open=(100.0 + index * 0.3) * scale,
                high=(101.0 + index * 0.3) * scale,
                low=(99.0 + index * 0.3) * scale,
                close=(100.5 + index * 0.3 + (0.1 if index % 2 else -0.1)) * scale,
                volume=1_000_000.0,
            )
            for index in range(35)
        )
        return BarSeries(symbol=symbol, interval=BarInterval.DAY_1, bars=bars)

    settings = FiveToolSettings.defaults(history_start_utc=start)
    inputs = align_five_tool_inputs(settings, series("AAA", 1.0), series("SPY", 4.0))
    rows = trace_rows_from_engine(evaluate_batch(settings, inputs), inputs)
    combined = hashlib.sha256("".join(row.state_digest for row in rows).encode()).hexdigest()
    assert len(rows) == 35
    assert rows[0].timestamp_utc == start
    assert combined == "136097ff46f57dee883a4bd39c81a66d398a0aa7f676ee712c755848261630ab"


def test_engine_adapter_projects_raw_v2_gates_and_stale_only_avwap_reset() -> None:
    start = datetime(2024, 1, 2, 21, tzinfo=UTC)
    primary = Bar(
        symbol="AAA",
        source="internal_spec",
        exchange="NYSE",
        interval=BarInterval.DAY_1,
        session_date=start.date(),
        timestamp_utc=start,
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.5,
        volume=1_000_000.0,
    )
    item = FiveToolBarInput(primary=primary, benchmark=None)
    trace = evaluate_batch(FiveToolSettings.defaults(history_start_utc=start), (item,))[0]
    features = dict(trace.features)
    features.update(
        {
            "extension": True,
            "extension_active": False,
            "avwap_reset": True,
            "avwap_stale_reset": False,
        }
    )
    gates = dict(trace.gates)
    gates.update(
        {
            "short_review": False,
            "short_review_v2": True,
            "long_review": False,
            "long_review_v2": True,
        }
    )
    projected = trace_row_from_engine(
        replace(trace, features=tuple(features.items()), gates=tuple(gates.items())),
        item,
    )

    assert projected.extension is True
    assert projected.short_review_ok is True
    assert projected.long_review_ok is True
    assert projected.avwap_force_reset is False
