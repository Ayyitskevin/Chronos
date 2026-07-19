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
) -> tuple[PineScriptEntry, ...]:
    """Filter the Pine scripts by any combination of structured fields.

    ``executable`` selects integrity PASS_WITH_CONSTRAINTS. ``tradable_direction``
    matches a side inclusively — LONG also matches BIDIRECTIONAL, SHORT also
    matches BIDIRECTIONAL — for "long strategies" style questions.
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


def family_counts(store: SKBStore) -> dict[str, int]:
    counts: dict[str, int] = {}
    for entry in store.pine_scripts:
        counts[entry.strategy_family.value] = counts.get(entry.strategy_family.value, 0) + 1
    return dict(sorted(counts.items()))
