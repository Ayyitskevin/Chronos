"""Read-only query surface over the compiled SKB (AI Quant plan B2).

A pure, side-effect-free filter/aggregation layer over an :class:`SKBStore` —
it answers the structured questions the corpus can honestly support (by
disposition, reason, family, direction, classification, executability, port
status, selection candidacy). It imports no runtime/order module and is imported
by none; the CLI (`chronos skb ...`) is its only caller.

Honesty note: the corpus tags timeframe and data dependencies only in prose, so
questions like "blocked on intraday data" are NOT answerable as a structured
query — the disposition/reason vocabulary is the structured surface, and prose
feasibility text is carried per script for a human to read, never inferred.

Issue #181 makes two of those questions structured for the five scripts whose
Pine was read: ``max_concurrent_positions`` and ``timeframe_binding``. The other
37 stay unmeasured and answer no filter on a measured value, which keeps "we read
this and it holds one position" distinct from "we never looked".
"""

from __future__ import annotations

from chronos.skb.schema import (
    Classification,
    DerivedStrategy,
    Direction,
    Disposition,
    DispositionReason,
    IntegrityStatus,
    PineScriptEntry,
    SKBStore,
    StrategyFamily,
    TimeframeBinding,
)


def query_scripts(
    store: SKBStore,
    *,
    disposition: Disposition | None = None,
    reason: DispositionReason | None = None,
    family: StrategyFamily | None = None,
    direction: Direction | None = None,
    classification: Classification | None = None,
    integrity: IntegrityStatus | None = None,
    executable: bool | None = None,
    ported: bool | None = None,
    tradable_direction: Direction | None = None,
    max_concurrent_positions: int | None = None,
    timeframe_binding: TimeframeBinding | None = None,
) -> tuple[PineScriptEntry, ...]:
    """Filter the Pine scripts by any combination of structured fields.

    ``executable`` selects integrity PASS_WITH_CONSTRAINTS. ``tradable_direction``
    matches a side inclusively — LONG also matches BIDIRECTIONAL, SHORT also
    matches BIDIRECTIONAL — for "long strategies" style questions.

    ``max_concurrent_positions`` and ``timeframe_binding`` filter the
    source-measured fields (issue #181). Both match exactly, so an unmeasured
    script is never returned by a filter on a measured value — asking for
    ``max_concurrent_positions=1`` yields the scripts read and found to hold one
    position, not the scripts assumed to. ``timeframe_binding=UNKNOWN`` is the
    query for "not yet measured".
    """

    def matches(entry: PineScriptEntry) -> bool:
        if disposition is not None and entry.disposition is not disposition:
            return False
        if reason is not None and entry.disposition_reason is not reason:
            return False
        if family is not None and entry.strategy_family is not family:
            return False
        if direction is not None and entry.direction is not direction:
            return False
        if classification is not None and entry.classification is not classification:
            return False
        if integrity is not None and entry.integrity_status is not integrity:
            return False
        if executable is not None:
            is_exec = entry.integrity_status is IntegrityStatus.PASS_WITH_CONSTRAINTS
            if is_exec is not executable:
                return False
        if ported is not None and (entry.disposition is Disposition.PORTED) is not ported:
            return False
        if (
            max_concurrent_positions is not None
            and entry.max_concurrent_positions != max_concurrent_positions
        ):
            return False
        if timeframe_binding is not None and entry.timeframe_binding is not timeframe_binding:
            return False
        return tradable_direction is None or _tradable(entry.direction, tradable_direction)

    return tuple(entry for entry in store.pine_scripts if matches(entry))


def _tradable(actual: Direction, wanted: Direction) -> bool:
    if actual is Direction.BIDIRECTIONAL:
        return wanted in (Direction.LONG, Direction.SHORT, Direction.BIDIRECTIONAL)
    return actual is wanted


def deferred_strategies(store: SKBStore) -> tuple[PineScriptEntry, ...]:
    """Executable standalone strategies that are portable but not yet ported."""

    return query_scripts(store, disposition=Disposition.DEFERRED)


def derived_by_status(store: SKBStore, status: str) -> tuple[DerivedStrategy, ...]:
    return tuple(d for d in store.derived_strategies if d.status == status)


def disposition_counts(store: SKBStore) -> dict[str, int]:
    counts: dict[str, int] = {d.value: 0 for d in Disposition}
    for entry in store.pine_scripts:
        counts[entry.disposition.value] += 1
    return {k: v for k, v in counts.items() if v}


def timeframe_binding_counts(store: SKBStore) -> dict[str, int]:
    """How many scripts carry each timeframe binding, ``unknown`` included.

    ``unknown`` is reported rather than filtered out: the count of unmeasured
    scripts is the honest headline of this field, not noise to hide.
    """

    counts: dict[str, int] = {b.value: 0 for b in TimeframeBinding}
    for entry in store.pine_scripts:
        counts[entry.timeframe_binding.value] += 1
    return counts


def measured_scripts(store: SKBStore) -> tuple[PineScriptEntry, ...]:
    """The scripts whose Pine source was read for the #181 properties."""

    return tuple(e for e in store.pine_scripts if e.source_property_citation)


def family_counts(store: SKBStore) -> dict[str, int]:
    counts: dict[str, int] = {}
    for entry in store.pine_scripts:
        counts[entry.strategy_family.value] = counts.get(entry.strategy_family.value, 0) + 1
    return dict(sorted(counts.items()))
