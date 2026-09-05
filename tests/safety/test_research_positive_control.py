"""The research selection path can select — a positive control (Kevin's corpus lane).

`docs` and several summaries record that the C4 campaign selects **zero** strategies, and
attribute it to the corpus being too small. That attribution has never been testable. No
test in this repository has seen the pipeline emit a PASS, so "the data is too thin" and
"the selection path cannot select" have produced identical evidence, and only one of them is
comfortable to believe.

This file makes them different. It runs the **real** frozen pipeline —
`chronos.research.campaign.run_campaign` at its own shipped defaults, over
`chronos.research.walkforward.walk_forward` and its `_verdict` — on a generated corpus
(`tests.support.synthetic_corpus`) built so that one shipped family both trades and wins.
It must PASS. Two differently-shaped inputs must be REFUSED, on the two different code paths
that can refuse, so a control that passes for the wrong reason is caught.

## What this does NOT claim

- Nothing about any real instrument. The corpus is generated; the symbols are not tickers.
- Nothing about the holdout guardian. `run_campaign` does not call it: the campaign enforces
  the 2022 wall itself by refusing a `stage_end` that reaches it and slicing the series
  (`campaign.py:FINAL_START`). `histdata/holdout.py` governs a different path, and asserting
  it here would be a claim wider than the check.
- Nothing about whether any *real* strategy is any good. A positive control proves the
  instrument can register a positive; it says nothing about the patient.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from chronos.backtest.engine import BacktestConfig
from chronos.config.settings import Settings
from chronos.registry import RegistryLedger, trial_count
from chronos.research import walkforward
from chronos.research.campaign import CampaignReport, run_campaign
from chronos.research.walkforward import WalkForwardVerdict
from chronos.risk.policy import load_risk_policy
from tests.support.synthetic_corpus import (
    SYNTHETIC_SYMBOLS,
    corpus_digest,
    write_manifest,
    write_synthetic_corpus,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
RESEARCH_POLICY = REPO_ROOT / "config" / "risk.research.yaml"
CONTROL_POLICY = REPO_ROOT / "config" / "risk.synthetic-control.yaml"

SYMBOL = "SYNTHA"
BARS = 2600
SELECTING_FAMILY = "mean_reversion_v1"

#: The corpus this control was built against. Asserted so that a change to the generator
#: fails as a *data* change, loudly, instead of silently moving the verdict underneath the
#: assertions below.
CORPUS_SHA256 = "e26901855721e755f42bfe828b4430e56c58d83caabe804efee3dfe046358b5a"


@pytest.fixture(scope="module")
def corpus(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("synthetic-corpus")
    path = write_synthetic_corpus(root, SYMBOL, BARS)
    write_manifest(root, {SYMBOL: path})
    return root


def _campaign(
    data_dir: Path, tmp_path: Path, *, strategies: tuple[str, ...] = (SELECTING_FAMILY,)
) -> CampaignReport:
    """The real entry point at its own shipped defaults — warmup, test window, trade floor."""

    return run_campaign(
        strategies=list(strategies),
        symbols=[SYMBOL],
        data_dir=data_dir,
        policy=load_risk_policy(CONTROL_POLICY),
        ledger=RegistryLedger(tmp_path / "ledger.jsonl"),
        halt_dir=tmp_path / "halt",
        config=BacktestConfig(initial_cash_usd=100_000.0, slippage_bps_per_side=1.0),
        seed=7,
    )


def test_the_corpus_is_deterministic(corpus: Path) -> None:
    """A generator change must fail here, not silently move every verdict below."""

    assert corpus_digest(corpus / f"{SYMBOL}.csv") == CORPUS_SHA256


def test_the_selection_path_selects(corpus: Path, tmp_path: Path) -> None:
    """The positive control: the real pipeline reaches PASS on a corpus built for it.

    This is the assertion the whole file exists for. If it fails, "zero strategies
    selected" is no longer attributable to the corpus until it passes again.
    """

    report = _campaign(corpus, tmp_path)
    assert report.excluded == (), report.excluded
    assert report.errored == (), report.errored
    (row,) = report.verdict_table

    assert row.verdict == WalkForwardVerdict.PASS.value, (
        f"the selection path did not select: verdict={row.verdict} reason={row.reason!r} "
        f"trades={row.pooled_trades} ci={row.sharpe_ci} dsr={row.deflated_sharpe}"
    )
    # The reason string is part of the proof, not decoration: a wording change on what the
    # pipeline claims it proved should be noticed, not absorbed.
    assert row.reason == "Sharpe CI > 0 and deflated Sharpe >= threshold"
    assert row.pooled_trades >= report.min_trades
    assert row.sharpe_ci is not None and row.sharpe_ci[0] > 0.0
    assert row.deflated_sharpe is not None
    assert row.deflated_sharpe >= walkforward._DSR_PASS_THRESHOLD
    # Exactly one VALIDATION trial per cell: the multiple-testing N stays honest.
    assert trial_count(RegistryLedger(tmp_path / "ledger.jsonl"), strategy_id=SELECTING_FAMILY) == 1


def test_the_control_ran_at_the_settings_trade_floor(corpus: Path, tmp_path: Path) -> None:
    """A `--min-trades 1` selection must never be able to pass as this control.

    The floor is the one criterion a caller can lower per run (`cli/main.py` passes
    `--min-trades` straight through), so the control records what it ran with and ties it to
    the settings default. Read from the field rather than repeating `20` here: a second copy
    of the number would agree with itself while both drifted from the setting.
    """

    report = _campaign(corpus, tmp_path)
    assert report.min_trades == Settings.model_fields["walkforward_min_trades"].default


def test_a_family_that_does_not_trade_is_refused_by_the_floor_alone(
    corpus: Path, tmp_path: Path
) -> None:
    """The floor refusing on trade count alone, with the statistics in excellent shape.

    Buy-and-hold on this corpus produces a strictly positive Sharpe CI and a deflated Sharpe
    at the ceiling — and is refused anyway, because it enters no position inside the
    out-of-sample window and `pooled_trades` counts OOS *entries*. That is the C4 floor
    doing its job where every other gate would have waved the cell through.
    """

    report = _campaign(corpus, tmp_path, strategies=("baseline_buy_hold",))
    (row,) = report.verdict_table

    assert row.verdict == WalkForwardVerdict.INSUFFICIENT_EVIDENCE.value
    assert row.reason == f"only {row.pooled_trades} OOS trades (< {report.min_trades} floor)"
    assert row.pooled_trades < report.min_trades
    # The point of the row: the refusal is the floor's, not the statistics'.
    assert row.sharpe_ci is not None and row.sharpe_ci[0] > 0.0
    assert row.deflated_sharpe is not None
    assert row.deflated_sharpe >= walkforward._DSR_PASS_THRESHOLD


def test_a_truncated_corpus_is_excluded_before_any_statistic(tmp_path: Path) -> None:
    """The other refusal, on the other code path: too short to run at all.

    `run_campaign` excludes a symbol whose sliced series is shorter than
    `warmup + 2 * test_window` and records the reason, rather than skipping it silently. No
    verdict row is produced, so this cannot be confused with a statistical refusal.
    """

    root = tmp_path / "short"
    write_synthetic_corpus(root, SYMBOL, 100)
    report = _campaign(root, tmp_path)

    assert report.verdict_table == ()
    assert [symbol for symbol, _ in report.excluded] == [SYMBOL]
    ((_, reason),) = report.excluded
    assert "short" in reason.lower() or "bars" in reason.lower(), reason


def test_the_synthetic_universe_never_enters_the_research_profile() -> None:
    """The names are confined to the control's own policy, and the control proves it."""

    research = yaml.safe_load(RESEARCH_POLICY.read_text(encoding="utf-8"))
    control = yaml.safe_load(CONTROL_POLICY.read_text(encoding="utf-8"))

    assert set(control["allowed_symbols"]) == set(SYNTHETIC_SYMBOLS)
    for symbol in SYNTHETIC_SYMBOLS:
        assert symbol not in research["allowed_symbols"], (
            f"{symbol} reached config/risk.research.yaml — a generated instrument is now in "
            "the profile that governs real research runs"
        )


def test_the_control_policy_mirrors_the_research_profile(tmp_path: Path) -> None:
    """Everything except the universe and the version must still match.

    A copied config drifts the moment the original is edited. Loading both through the real
    schema and diffing the fields makes that drift a test failure rather than a discovery.
    """

    research = load_risk_policy(RESEARCH_POLICY)
    control = load_risk_policy(CONTROL_POLICY)
    differing = {
        name
        for name in type(research).model_fields
        if getattr(research, name) != getattr(control, name)
    }
    assert differing == {"policy_version", "allowed_symbols"}, differing


def test_the_corpus_is_marked_synthetic_where_a_human_will_look(corpus: Path) -> None:
    """`run_campaign` reads no manifest, so this flag is asserted here or it is nothing."""

    manifest = json.loads((corpus / "MANIFEST.json").read_text(encoding="utf-8"))
    assert manifest["synthetic"] is True
    assert "never a source of evidence" in manifest["not_certified_data"]
    assert manifest["generator"] == "tests.support.synthetic_corpus:write_synthetic_corpus"
    assert manifest["files"][SYMBOL]["sha256"] == CORPUS_SHA256
