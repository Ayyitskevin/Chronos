"""The frozen C1-C6 predicate, executable — and unable to over-claim (Daybreak finding 6).

`research/selection_manifest.json` froze six criteria and nothing has ever evaluated them.
This suite pins the module that does: that it answers every criterion with the frozen text
quoted, that today every answer is UNVERIFIED with the missing input named, that a single
cell can never be promoted to `BACKTEST_VALIDATED`, and that a criteria document which is
not the expected one is refused wholesale rather than judged.

The positive control from #183 is reused as this verdict's control, in both directions:
the campaign run that *selects* is the only input in the repository on which C4's round-trip
count is a real measurement, and the verdict must report that number and **still** refuse to
call the criterion satisfied.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from chronos.backtest.engine import BacktestConfig
from chronos.registry import RegistryLedger
from chronos.research.campaign import run_campaign
from chronos.research.selection_verdict import (
    CRITERION_IDS,
    SELECTION_MANIFEST_PATH,
    CriterionOutcome,
    CriterionState,
    SelectionManifestUnusable,
    SelectionState,
    _aggregate,
    evaluate_selection,
    load_selection_manifest,
)
from chronos.research.walkforward import WalkForwardReport, WalkForwardVerdict
from chronos.risk.policy import load_risk_policy
from tests.support.synthetic_corpus import write_manifest, write_synthetic_corpus

REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST = REPO_ROOT / SELECTION_MANIFEST_PATH
CONTROL_POLICY = REPO_ROOT / "config" / "risk.synthetic-control.yaml"
SYMBOL = "SYNTHA"
CANDIDATE = "mean_reversion_v1"
TWIN = "baseline_random_entries"


@pytest.fixture(scope="module")
def manifest_bytes() -> bytes:
    return load_selection_manifest(MANIFEST)


@pytest.fixture(scope="module")
def control_reports(tmp_path_factory: pytest.TempPathFactory) -> dict[str, WalkForwardReport]:
    """#183's corpus, through the real campaign — the one input where C4 has a number."""

    root = tmp_path_factory.mktemp("selection-verdict")
    corpus = root / "corpus"
    path = write_synthetic_corpus(corpus, SYMBOL, 2600)
    write_manifest(corpus, {SYMBOL: path})
    report = run_campaign(
        strategies=[CANDIDATE, TWIN],
        symbols=[SYMBOL],
        data_dir=corpus,
        policy=load_risk_policy(CONTROL_POLICY),
        ledger=RegistryLedger(root / "ledger.jsonl"),
        halt_dir=root / "halt",
        config=BacktestConfig(initial_cash_usd=100_000.0, slippage_bps_per_side=1.0),
        seed=7,
    )
    return {cell.strategy_id: cell.report for cell in report.cells if cell.report is not None}


def _outcome(verdict: object, criterion_id: str) -> CriterionOutcome:
    outcomes = verdict.outcomes  # type: ignore[attr-defined]
    return next(o for o in outcomes if o.criterion_id == criterion_id)


def test_every_criterion_is_answered_with_its_frozen_text_quoted(
    manifest_bytes: bytes, control_reports: dict[str, WalkForwardReport]
) -> None:
    """Six answers, and each `frozen_text` is a substring of the manifest's own bytes."""

    verdict = evaluate_selection(manifest_bytes=manifest_bytes, report=control_reports[CANDIDATE])
    assert tuple(o.criterion_id for o in verdict.outcomes) == CRITERION_IDS
    raw = manifest_bytes.decode("utf-8")
    for outcome in verdict.outcomes:
        assert outcome.frozen_text, outcome.criterion_id
        # Quoted, not synthesised: the exact bytes digested must contain it.
        assert outcome.frozen_text[:60] in raw, outcome.criterion_id


def test_today_every_criterion_is_unverified_and_names_what_is_missing(
    manifest_bytes: bytes, control_reports: dict[str, WalkForwardReport]
) -> None:
    """The artifact this module exists to produce.

    Not "the candidate fails" — "here is what would have to exist for the question to have
    an answer", per criterion, in a form the next lane can read rather than re-derive.
    """

    verdict = evaluate_selection(manifest_bytes=manifest_bytes, report=control_reports[CANDIDATE])
    for outcome in verdict.outcomes:
        assert outcome.state is CriterionState.UNVERIFIED, (outcome.criterion_id, outcome.detail)
        assert outcome.missing_inputs, outcome.criterion_id
        assert outcome.detail, outcome.criterion_id
        for missing in outcome.missing_inputs:
            assert missing.name and missing.computed_by


def test_the_metrics_gaps_name_the_function_that_could_close_them(
    manifest_bytes: bytes, control_reports: dict[str, WalkForwardReport]
) -> None:
    """Slice three's specification is read off this output, not re-derived.

    Every figure that `PerformanceMetrics` already computes must be named with the function
    that computes it, so "compute the companion metrics report" is specified by the gap
    itself rather than by someone remembering where total return lives.
    """

    verdict = evaluate_selection(manifest_bytes=manifest_bytes, report=control_reports[CANDIDATE])
    pointers = {
        missing.computed_by for outcome in verdict.outcomes for missing in outcome.missing_inputs
    }
    for field in ("total_return_fraction", "profit_factor", "max_drawdown_fraction"):
        assert any(
            "chronos.backtest.metrics.compute_metrics" in pointer
            and f"PerformanceMetrics.{field}" in pointer
            for pointer in pointers
        ), field


def test_c4_reports_the_measured_round_trips_against_the_frozen_floor(
    manifest_bytes: bytes, control_reports: dict[str, WalkForwardReport]
) -> None:
    """The positive half of the control: the verdict can measure, and says the number.

    On #183's corpus the campaign selects with a real round-trip count. C4's detail must
    carry that number and the frozen 20 — otherwise an all-UNVERIFIED table would be
    indistinguishable from a function that cannot evaluate anything, which is the same trap
    #183 closed one layer down.
    """

    report = control_reports[CANDIDATE]
    assert report.pooled_trades >= 20, "the #183 control corpus should still select"
    outcome = _outcome(evaluate_selection(manifest_bytes=manifest_bytes, report=report), "C4")
    assert str(report.pooled_trades) in outcome.detail
    assert "20" in outcome.detail
    assert outcome.inputs_used == (f"report.pooled_trades={report.pooled_trades}",)
    # The negative half, and the more important one: measured is not satisfied.
    assert outcome.state is CriterionState.UNVERIFIED


def test_supplied_baselines_appear_in_inputs_used(
    manifest_bytes: bytes, control_reports: dict[str, WalkForwardReport]
) -> None:
    """What was actually compared has to be visible, or `inputs_used` is decoration."""

    without = _outcome(
        evaluate_selection(manifest_bytes=manifest_bytes, report=control_reports[CANDIDATE]),
        "C2",
    )
    assert without.inputs_used == ()

    with_twin = _outcome(
        evaluate_selection(
            manifest_bytes=manifest_bytes,
            report=control_reports[CANDIDATE],
            baseline_reports={TWIN: control_reports[TWIN]},
        ),
        "C2",
    )
    assert any("pooled_sharpe" in used for used in with_twin.inputs_used)
    assert any(TWIN in used for used in with_twin.inputs_used)
    assert with_twin.state is CriterionState.UNVERIFIED  # Sharpe alone is half a conjunction


def test_a_single_cell_is_never_promoted(
    manifest_bytes: bytes, control_reports: dict[str, WalkForwardReport]
) -> None:
    """`multiple_testing_guard` reserves promotion for a reader; the aggregate says so."""

    verdict = evaluate_selection(manifest_bytes=manifest_bytes, report=control_reports[CANDIDATE])
    assert verdict.state is SelectionState.UNVERIFIED
    assert verdict.state is not SelectionState.BACKTEST_VALIDATED
    assert verdict.manifest_sha256 == hashlib.sha256(manifest_bytes).hexdigest()
    assert verdict.manifest_frozen_at_utc


def test_one_changed_byte_refuses_every_criterion(
    manifest_bytes: bytes, control_reports: dict[str, WalkForwardReport]
) -> None:
    """The same-bytes rule: these are not the criteria the caller pinned.

    A digest mismatch is not a finding about the candidate. Judging it anyway would produce
    a verdict about a document nobody asked for, which is worse than no verdict.
    """

    expected = hashlib.sha256(manifest_bytes).hexdigest()
    mutated = manifest_bytes.replace(b'"purpose"', b'"purpOse"', 1)
    assert mutated != manifest_bytes

    verdict = evaluate_selection(
        manifest_bytes=mutated,
        report=control_reports[CANDIDATE],
        expected_sha256=expected,
    )
    assert verdict.state is SelectionState.UNVERIFIED
    assert len(verdict.outcomes) == len(CRITERION_IDS)
    assert all(o.state is CriterionState.UNVERIFIED for o in verdict.outcomes)
    assert expected in verdict.outcomes[0].detail
    assert verdict.manifest_sha256 != expected


def test_a_reworded_criterion_is_refused_rather_than_reinterpreted(
    manifest_bytes: bytes, control_reports: dict[str, WalkForwardReport]
) -> None:
    """The drift guard: constants may not outlive the text they came from."""

    reworded = manifest_bytes.replace(b"profit factor >= 1.1", b"profit factor >= 1.05", 1)
    assert reworded != manifest_bytes

    outcome = _outcome(
        evaluate_selection(manifest_bytes=reworded, report=control_reports[CANDIDATE]), "C4"
    )
    assert outcome.state is CriterionState.UNVERIFIED
    assert "profit factor >= 1.1" in outcome.detail
    assert "1.05" in outcome.frozen_text


def test_no_pooling_across_symbols(
    manifest_bytes: bytes, control_reports: dict[str, WalkForwardReport]
) -> None:
    """One verdict per (strategy, symbol); counts are never summed across symbols."""

    report = control_reports[CANDIDATE]
    verdict = evaluate_selection(manifest_bytes=manifest_bytes, report=report)
    assert verdict.symbol == report.symbol
    assert f"report.pooled_trades={report.pooled_trades}" in _outcome(verdict, "C4").inputs_used
    assert str(report.pooled_trades * 2) not in _outcome(verdict, "C4").detail


def _outcomes(**states: CriterionState) -> tuple[CriterionOutcome, ...]:
    return tuple(
        CriterionOutcome(
            criterion_id=cid,
            frozen_text=f"{cid} text",
            state=states.get(cid, CriterionState.PASS),
            detail="hand-built for the ladder test",
        )
        for cid in CRITERION_IDS
    )


def test_the_precedence_ladder_is_conservative_in_both_directions() -> None:
    """The aggregate is a pure function over outcomes, so its ladder is testable directly.

    No real input reaches a FAIL today — every criterion is UNVERIFIED for want of evidence
    — so the branch that decides between FAIL and UNVERIFIED would otherwise ship untested.
    An untested branch in the rung that decides eligibility is not one to leave to inference.
    """

    guard = "the manifest's multiple-testing guard"

    # A definite failure is not softened by a sibling's missing evidence.
    state, detail = _aggregate(
        _outcomes(C2=CriterionState.FAIL, C4=CriterionState.UNVERIFIED), guard=guard
    )
    assert state is SelectionState.NOT_ELIGIBLE
    assert "C2" in detail

    # Absent a failure, any gap absorbs: never promoted on incomplete evidence.
    state, _ = _aggregate(_outcomes(C4=CriterionState.UNVERIFIED), guard=guard)
    assert state is SelectionState.UNVERIFIED

    # C6 failing alone is not_eligible, per the manifest's own third rule.
    state, detail = _aggregate(_outcomes(C6=CriterionState.FAIL), guard=guard)
    assert state is SelectionState.NOT_ELIGIBLE
    assert "C6" in detail

    # And everything passing still does not promote — the guard is quoted instead.
    state, detail = _aggregate(_outcomes(), guard=guard)
    assert state is SelectionState.UNVERIFIED
    assert guard in detail


def test_an_unusable_manifest_raises_rather_than_returning_a_verdict() -> None:
    with pytest.raises(SelectionManifestUnusable):
        evaluate_selection(manifest_bytes=b"{not json", report=_stub_report())
    with pytest.raises(SelectionManifestUnusable):
        evaluate_selection(
            manifest_bytes=b'{"criteria_for_backtest_validated": []}', report=_stub_report()
        )


def _stub_report(pooled_trades: int = 0) -> WalkForwardReport:
    return WalkForwardReport(
        strategy_id=CANDIDATE,
        symbol=SYMBOL,
        seed=0,
        windows=(),
        pooled_bars=0,
        pooled_trades=pooled_trades,
        pooled_sharpe=None,
        sharpe_ci=None,
        psr=None,
        deflated_sharpe=None,
        trial_count=1,
        purged_cv="",
        verdict=WalkForwardVerdict.INSUFFICIENT_EVIDENCE,
        reason="stub",
    )
