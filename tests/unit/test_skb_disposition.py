"""SKB disposition classifier tests (AI Quant plan B2).

The disposition backfill is a pure function of three clean categoricals
(``is_ported`` provenance, ``integrity_status``, ``classification`` + trade
``direction``) — never prose. These tests pin every priority rule in isolation
on synthetic entries, then assert the deterministic split over the real 42-script
corpus so a corpus/rule drift is caught. Every reason maps to exactly one
disposition, and no script is left UNCLASSIFIED after the backfill.
"""

from __future__ import annotations

from chronos.skb.compiler import compile_skb
from chronos.skb.disposition import _DISPOSITION_OF, _REASON_DETAIL, classify_disposition
from chronos.skb.schema import (
    Classification,
    Direction,
    Disposition,
    DispositionReason,
    IntegrityStatus,
    PineForensicFlags,
    PineScriptEntry,
    StrategyFamily,
)

_NO_FLAGS = PineForensicFlags(
    uses_request_security=False,
    uses_lookahead_on=False,
    uses_barstate_isconfirmed=False,
    uses_calc_on_every_tick_true=False,
    uses_process_orders_on_close_true=False,
    uses_strategy_orders=False,
    pyramiding_gt_0=False,
)


def _entry(
    *,
    classification: Classification = Classification.STRATEGY,
    direction: Direction = Direction.LONG,
    integrity: IntegrityStatus = IntegrityStatus.PASS_WITH_CONSTRAINTS,
) -> PineScriptEntry:
    return PineScriptEntry(
        catalog_number="99",
        filename="x.pine",
        title="x",
        sha256="0" * 64,
        bytes=1,
        lines=1,
        pine_version="6",
        declaration="strategy",
        cycle="",
        classification=classification,
        strategy_family=StrategyFamily.TREND_FOLLOWING,
        direction=direction,
        integrity_status=integrity,
        confidence="high",
        deterministic_translation_feasibility="x",
        forensic_flags=_NO_FLAGS,
    )


# --- priority rules, one per branch -----------------------------------------


def test_ported_wins_over_everything() -> None:
    # is_ported takes priority even when the Pine is a non-executable indicator
    # (mirrors catalog 11: a Study the mean_reversion spec derives from).
    disp, reason, detail = classify_disposition(
        _entry(
            classification=Classification.INDICATOR,
            direction=Direction.NONE,
            integrity=IntegrityStatus.NON_EXECUTABLE_INDICATOR,
        ),
        is_ported=True,
    )
    assert disp is Disposition.PORTED
    assert reason is DispositionReason.PORTED_TO_SPEC
    assert detail


def test_requires_rewrite_is_blocked_on() -> None:
    disp, reason, _ = classify_disposition(
        _entry(integrity=IntegrityStatus.REQUIRES_REWRITE), is_ported=False
    )
    assert disp is Disposition.BLOCKED_ON
    assert reason is DispositionReason.REQUIRES_REWRITE


def test_non_executable_indicator_is_rejected() -> None:
    disp, reason, _ = classify_disposition(
        _entry(
            classification=Classification.INDICATOR,
            direction=Direction.NONE,
            integrity=IntegrityStatus.NON_EXECUTABLE_INDICATOR,
        ),
        is_ported=False,
    )
    assert disp is Disposition.REJECTED
    assert reason is DispositionReason.NON_EXECUTABLE_INDICATOR


def test_strategy_addon_is_rejected_not_standalone() -> None:
    disp, reason, _ = classify_disposition(
        _entry(classification=Classification.STRATEGY_ADDON, direction=Direction.LONG),
        is_ported=False,
    )
    assert disp is Disposition.REJECTED
    assert reason is DispositionReason.STRATEGY_ADDON_NOT_STANDALONE


def test_executable_standalone_strategy_is_deferred() -> None:
    disp, reason, _ = classify_disposition(
        _entry(classification=Classification.STRATEGY, direction=Direction.BIDIRECTIONAL),
        is_ported=False,
    )
    assert disp is Disposition.DEFERRED
    assert reason is DispositionReason.EXECUTABLE_STRATEGY_NOT_YET_PORTED


def test_executable_strategy_without_direction_is_not_standalone() -> None:
    # A "strategy" declaration with no trade direction is a readout, not tradable.
    disp, reason, _ = classify_disposition(
        _entry(classification=Classification.STRATEGY, direction=Direction.NONE),
        is_ported=False,
    )
    assert disp is Disposition.REJECTED
    assert reason is DispositionReason.NOT_A_STANDALONE_STRATEGY


def test_executable_indicator_with_direction_is_not_standalone() -> None:
    # PASS_WITH_CONSTRAINTS but declared an indicator (e.g. catalog 05/06/38).
    disp, reason, _ = classify_disposition(
        _entry(classification=Classification.INDICATOR, direction=Direction.BIDIRECTIONAL),
        is_ported=False,
    )
    assert disp is Disposition.REJECTED
    assert reason is DispositionReason.NOT_A_STANDALONE_STRATEGY


# --- internal-table integrity -----------------------------------------------


def test_every_produced_reason_maps_to_one_disposition() -> None:
    # The reason->disposition table is total over the reasons the classifier can
    # emit, and each produced reason carries a human detail.
    producible = set(_DISPOSITION_OF)
    assert producible == set(_REASON_DETAIL)
    for reason in producible:
        assert _DISPOSITION_OF[reason] in set(Disposition)
        assert _REASON_DETAIL[reason]


# --- deterministic split over the real corpus -------------------------------


def test_corpus_split_is_exact() -> None:
    store = compile_skb()
    counts: dict[Disposition, int] = {d: 0 for d in Disposition}
    for entry in store.pine_scripts:
        counts[entry.disposition] += 1
    assert counts[Disposition.PORTED] == 2
    assert counts[Disposition.DEFERRED] == 4
    assert counts[Disposition.BLOCKED_ON] == 1
    assert counts[Disposition.REJECTED] == 35
    assert counts[Disposition.UNCLASSIFIED] == 0
    assert sum(counts.values()) == 42


def test_deferred_set_is_the_four_unported_executable_strategies() -> None:
    store = compile_skb()
    deferred = {
        p.catalog_number for p in store.pine_scripts if p.disposition is Disposition.DEFERRED
    }
    assert deferred == {"00", "02", "0A", "16"}


def test_blocked_on_is_the_single_requires_rewrite_script() -> None:
    store = compile_skb()
    blocked = [p for p in store.pine_scripts if p.disposition is Disposition.BLOCKED_ON]
    assert [p.catalog_number for p in blocked] == ["08"]
    assert blocked[0].integrity_status is IntegrityStatus.REQUIRES_REWRITE


def test_every_script_has_a_reason_consistent_with_its_disposition() -> None:
    store = compile_skb()
    for entry in store.pine_scripts:
        assert entry.disposition is not Disposition.UNCLASSIFIED
        assert entry.disposition_reason is not DispositionReason.UNCLASSIFIED
        assert _DISPOSITION_OF[entry.disposition_reason] is entry.disposition
        assert entry.disposition_detail  # a human line is always attached
