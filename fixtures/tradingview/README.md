# TradingView reference fixtures — owner export required

There is no genuine TradingView fixture in this directory.  Catalog `00`,
**Five-Tool Confluence AIO v3.6**, remains TradingView parity `UNVERIFIED`.
The deterministic fixture under `tests/fixtures/tradingview_synthetic/` has
`provenance: internal_spec`; it tests the importer and comparator only and is
not evidence about TradingView behavior.

The strict loader lives in `chronos.research.tradingview`.  It is pinned to:

- catalog number: `00`
- Pine SHA-256:
  `e51d5a40d2e933bf86847c7432364ba8934fd2de653d6aec3d7205639248e45f`
- Pine input count: `219`
- trace schema: `chronos.five_tool_tradingview_trace.v1`

An export from any other script revision fails closed.

## Owner export procedure

1. In TradingView, load the pinned catalog `00` script on the intended symbol,
   timeframe, chart timezone, and session.  Record the data source/subscription
   and every one of the 219 input values.
2. Export chart data containing all catalog `00` Data Window `*_EXPORT` plots and
   preserve the Strategy Tester trade list as a separate artifact. The current Pine
   telemetry does not directly export `regime_flip`, `entry_decision`,
   `exit_decision`, or `position_side`; Strategy Tester fills also occur on a
   different clock from signal-bar decisions. Do not infer or shift these fields.
   A reviewed, content-addressed normalizer or additional pinned Pine telemetry is
   required before the v1 schema can be treated as genuine parity evidence.
3. Normalize the export to the exact ordered `CSV_COLUMNS` tuple in
   `src/chronos/research/tradingview.py`.  Pine's numeric regime values map to
   `bear`, `neutral`, and `bull`; event fields use the documented enum strings;
   booleans are exactly `true` or `false`.  Optional numeric warm-up values may
   be empty, `na`, `NaN`, or `null`.  Infinite values are invalid.
4. Save the normalized trace as
   `fixtures/tradingview/00_<symbol>_<timeframe>_trace.csv` and its metadata as
   `fixtures/tradingview/00_<symbol>_<timeframe>_trace.meta.json`.
5. Populate every metadata key shown below. Preserve `input_config` in the exact
   Pine source order and native runtime types: in particular, `input.time` values are
   signed UNIX-millisecond integers, not formatted timestamp strings.
   `input_config_sha256` is SHA-256
   over compact canonical JSON of the full `input_config` object (UTF-8,
   recursively sorted keys, separators `,` and `:`, no NaN).  `trace_sha256` is
   SHA-256 over the exact CSV bytes.  Set `provenance` to `genuine` only for an
   owner-exported TradingView artifact. Create a detached owner-attestation file
   that binds the export, normalizer, and preserved trade-list identities. Obtain
   its trusted SHA-256 through an independent owner-reviewed channel; do not copy
   the trusted value from fixture metadata. The loader verifies bytes and digests,
   but does not authenticate a signer or interpret the attestation contents.

```json
{
  "schema_version": "chronos.five_tool_tradingview_trace.v1",
  "provenance": "genuine",
  "catalog_number": "00",
  "pine_sha256": "e51d5a40d2e933bf86847c7432364ba8934fd2de653d6aec3d7205639248e45f",
  "symbol": "SPY",
  "timeframe": "1D",
  "chart_timezone": "America/New_York",
  "session": "0930-1600:23456",
  "timestamp_semantics": "bar_close",
  "data_source": "TradingView subscription/adjustment description",
  "exported_at_utc": "2026-08-08T12:00:00Z",
  "input_count": 219,
  "input_config": {
    "<every Pine input name>": "<exact typed value>"
  },
  "input_config_sha256": "<64 lowercase hex characters>",
  "trace_sha256": "<64 lowercase hex characters>",
  "row_count": 1234,
  "owner_attestation_sha256": "<SHA-256 of detached attestation bytes>"
}
```

Load a genuine fixture only by supplying both `owner_attestation_path` and the
independently obtained `trusted_owner_attestation_sha256` to
`load_trace_fixture`. Missing or mismatched values fail closed.

The loader preserves the chart timezone and session as metadata but normalizes
every aware row timestamp to UTC.  Rows must already be strictly increasing.
It never sorts, forward-fills, nearest-neighbor joins, or applies an off-by-one
shift, so comparison is causal and exact by bar identity.

## Comparison contract

Timestamps, enums, booleans, counters, and entry/exit decisions compare
exactly.  Optional numeric nulls match only nulls.  Present floats use the
named absolute and relative tolerances in `FLOAT_TOLERANCES` (`indicator`,
`price`, or `account_value`); the comparator rejects missing or extra tolerance
definitions.

A failure reports the first divergent timestamp and field, expected and actual
values, both state digests, both active-gate sets, and the numeric tolerance if
one applied. A matching internal-spec fixture returns `UNVERIFIED`. A trusted
genuine reference can currently produce a scoped mismatch (`FAILED`), but an
exact match remains `UNVERIFIED`: v1 has no independently attestable Python
candidate identity or reviewed normalizer for the missing decision fields. The
trace schema also contains no fill time, price, quantity, commission, or order
lifecycle, so execution parity is always reported separately as `UNVERIFIED`.
