"""SKB query-surface tests (AI Quant plan B2).

The query layer is a pure, read-only filter/aggregation over an ``SKBStore``.
These tests pin the filter semantics (each field, combinations, the inclusive
``tradable_direction`` matching that folds BIDIRECTIONAL into both sides) and the
aggregations (disposition/family counts, deferred/derived helpers) against the
real compiled corpus. The module imports no runtime/order code.
"""

from __future__ import annotations

import ast
from pathlib import Path

from chronos.skb import query
from chronos.skb.compiler import compile_skb
from chronos.skb.schema import (
    Direction,
    Disposition,
    DispositionReason,
    IntegrityStatus,
    StrategyFamily,
)


def test_query_by_disposition_matches_counts() -> None:
    store = compile_skb()
    assert len(query.query_scripts(store, disposition=Disposition.PORTED)) == 2
    assert len(query.query_scripts(store, disposition=Disposition.DEFERRED)) == 4
    assert len(query.query_scripts(store, disposition=Disposition.BLOCKED_ON)) == 1
    assert len(query.query_scripts(store, disposition=Disposition.REJECTED)) == 35


def test_no_filter_returns_the_whole_corpus() -> None:
    store = compile_skb()
    assert len(query.query_scripts(store)) == store.pine_script_count == 42


def test_query_by_reason() -> None:
    store = compile_skb()
    deferred = query.query_scripts(
        store, reason=DispositionReason.EXECUTABLE_STRATEGY_NOT_YET_PORTED
    )
    assert {e.catalog_number for e in deferred} == {"00", "02", "0A", "16"}


def test_query_by_family() -> None:
    store = compile_skb()
    mr = query.query_scripts(store, family=StrategyFamily.MEAN_REVERSION)
    assert mr  # corpus has mean-reversion scripts
    assert all(e.strategy_family is StrategyFamily.MEAN_REVERSION for e in mr)


def test_query_by_executable_selects_pass_with_constraints() -> None:
    store = compile_skb()
    execs = query.query_scripts(store, executable=True)
    assert execs
    assert all(e.integrity_status is IntegrityStatus.PASS_WITH_CONSTRAINTS for e in execs)
    # And the complement is exactly the non-PASS scripts.
    non_execs = query.query_scripts(store, executable=False)
    assert all(e.integrity_status is not IntegrityStatus.PASS_WITH_CONSTRAINTS for e in non_execs)
    assert len(execs) + len(non_execs) == 42


def test_query_by_ported_flag() -> None:
    store = compile_skb()
    ported = query.query_scripts(store, ported=True)
    assert {e.catalog_number for e in ported} == {"01", "11"}
    not_ported = query.query_scripts(store, ported=False)
    assert len(not_ported) == 40
    assert all(e.disposition is not Disposition.PORTED for e in not_ported)


def test_combined_filters_are_conjunctive() -> None:
    store = compile_skb()
    # Deferred AND bidirectional narrows the four deferred to the two swing ones.
    result = query.query_scripts(
        store, disposition=Disposition.DEFERRED, direction=Direction.BIDIRECTIONAL
    )
    assert {e.catalog_number for e in result} == {"00", "0A", "16"}


def test_tradable_direction_folds_bidirectional_into_both_sides() -> None:
    store = compile_skb()
    longs = query.query_scripts(store, tradable_direction=Direction.LONG)
    shorts = query.query_scripts(store, tradable_direction=Direction.SHORT)
    # Every bidirectional script appears in BOTH the long and short views.
    bidir = {
        e.catalog_number for e in query.query_scripts(store, direction=Direction.BIDIRECTIONAL)
    }
    long_ids = {e.catalog_number for e in longs}
    short_ids = {e.catalog_number for e in shorts}
    assert bidir  # corpus has bidirectional scripts
    assert bidir <= long_ids
    assert bidir <= short_ids
    # A pure LONG script is in the long view but never in the short view.
    pure_long = {e.catalog_number for e in query.query_scripts(store, direction=Direction.LONG)}
    assert pure_long  # corpus has long-only scripts
    assert pure_long <= long_ids
    assert pure_long.isdisjoint(short_ids)
    # A NONE-direction script is tradable in no side.
    none_ids = {e.catalog_number for e in query.query_scripts(store, direction=Direction.NONE)}
    assert none_ids.isdisjoint(long_ids)
    assert none_ids.isdisjoint(short_ids)


def test_deferred_strategies_helper_matches_query() -> None:
    store = compile_skb()
    assert query.deferred_strategies(store) == query.query_scripts(
        store, disposition=Disposition.DEFERRED
    )


def test_disposition_counts_omit_empty_buckets_and_sum_to_total() -> None:
    store = compile_skb()
    counts = query.disposition_counts(store)
    assert counts == {"ported": 2, "deferred": 4, "blocked_on": 1, "rejected": 35}
    assert "unclassified" not in counts  # empty bucket dropped
    assert sum(counts.values()) == store.pine_script_count


def test_family_counts_are_sorted_and_total_the_corpus() -> None:
    store = compile_skb()
    counts = query.family_counts(store)
    assert list(counts) == sorted(counts)  # deterministic, sorted keys
    assert sum(counts.values()) == 42
    assert set(counts) <= {f.value for f in StrategyFamily}


def test_derived_by_status() -> None:
    store = compile_skb()
    prototypes = query.derived_by_status(store, "research_prototype")
    assert {d.strategy_id for d in prototypes} == {"regime_trend_v1", "mean_reversion_v1"}
    assert query.derived_by_status(store, "no_such_status") == ()


def test_query_module_imports_no_runtime_code() -> None:
    # The read-only query surface must never pull in an order/broker/runtime
    # module — it is a knowledge layer, not an execution path.
    source = Path(query.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
    forbidden = ("orders", "broker", "execution", "runtime", "live", "control")
    offending = [m for m in imported if any(part in m.split(".") for part in forbidden)]
    assert offending == []
    # Only schema is imported from chronos.
    chronos_imports = {m for m in imported if m.startswith("chronos")}
    assert chronos_imports == {"chronos.skb.schema"}
