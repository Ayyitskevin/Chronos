"""The frozen C1-C6 selection predicate, as one executable verdict.

`research/selection_manifest.json` froze six criteria before validation results were
computed. Nothing has ever evaluated them. `scripts/run_research.py` produces base,
cost-stress and sensitivity rows; the SKB compiler loads the criteria as provenance; the
Five-Tool campaign binds their digest into an identity without reading them. So "zero
strategies selected" has been a claim assembled by scripts and held in a reader's memory,
and the question it rests on — *on what evidence?* — has had no data structure.

This module is that structure. It answers each criterion with PASS, FAIL or **UNVERIFIED**,
quotes the frozen text it judged against, and — this is the part that makes an UNVERIFIED
useful rather than merely honest — names the exact input it lacked and the function that
already exists to compute it.

## What it will say today, and why that is the point

Every criterion comes back UNVERIFIED. Two independent reasons, neither of which a cleverer
evaluator could talk its way past:

- **The criteria are prose.** There is no machine-readable window, threshold or comparator
  anywhere in the manifest. In particular there is no `validation_window` field: the string
  "validation window 2018-2021" appears only inside a prose note. Five of six criteria say
  "on the validation window", and the manifest does not say in data what that window is.
- **A campaign report carries none of the figures.** `WalkForwardReport` holds Sharpe-family
  statistics and a trade count. Total return, profit factor and max drawdown — required by
  C1, C2, C3 and C4 — live in :class:`chronos.backtest.metrics.PerformanceMetrics`, which
  ``run_campaign`` never computes.

A six-row table of UNVERIFIED with each gap named is not a failure to evaluate. It is the
first artifact in this repository that states *which* evidence is missing for *which* frozen
criterion, in a form the next lane can consume rather than re-derive.

## What it will never do

Promote. `multiple_testing_guard` in the manifest says a lone single-symbol C1 pass among
five symbols is weaker evidence and "the interpretation MUST apply this discount". A function
cannot apply a judgement, so a single-cell verdict never returns ``BACKTEST_VALIDATED``: when
every criterion it could evaluate passes, it returns ``UNVERIFIED`` with that guard quoted, and
the promotion stays where the manifest puts it.

Nor does it change anything. No threshold moves, no criterion text is edited, and the
manifest is read-only to this module. C4's numbers live here as constants because they must
to be executable — and :data:`_TEXT_ANCHORS` is what stops them drifting from the text they
came from: if the manifest is ever re-worded, the criterion returns UNVERIFIED naming the
anchor that stopped matching, rather than evaluating a constant the text no longer supports.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from chronos.research.walkforward import WalkForwardReport

#: The manifest that froze the criteria, relative to the repository root.
SELECTION_MANIFEST_PATH = "research/selection_manifest.json"

CRITERION_IDS = ("C1", "C2", "C3", "C4", "C5", "C6")

#: C4's numbers, bound here because a criterion that lives only in prose is not executable.
#: Guarded by :data:`_TEXT_ANCHORS`; see the module docstring.
_C4_MIN_CLOSED_TRADES = 20
_C4_MIN_PROFIT_FACTOR = 1.1

#: Substrings each frozen criterion must still contain for this module's reading of it to
#: hold. A re-wording is not assumed to be harmless: it produces UNVERIFIED naming the
#: anchor, which is the only honest answer when the text and the code have diverged.
_TEXT_ANCHORS: dict[str, tuple[str, ...]] = {
    "C1": ("net-positive after base costs", "on at least one symbol"),
    "C2": ("random-entry twin", "BOTH total return and Sharpe"),
    "C3": ("SMA-trend baseline", "max drawdown"),
    "C4": ("profit factor >= 1.1", ">= 20 closed trades"),
    "C5": ("parameter sensitivity", "SENSITIVITY"),
    "C6": ("final test window", "run once"),
}

#: The baselines the criteria name, mapped to the strategy ids that implement them.
_RANDOM_ENTRY_TWIN = "baseline_random_entries"
_SMA_TREND_BASELINE = "baseline_sma_trend"

_METRICS = "chronos.backtest.metrics.compute_metrics"


class CriterionState(StrEnum):
    """One criterion's answer. UNVERIFIED is not a soft FAIL; it is *no answer*."""

    PASS = "PASS"
    FAIL = "FAIL"
    UNVERIFIED = "UNVERIFIED"


class SelectionState(StrEnum):
    """The aggregate. ``BACKTEST_VALIDATED`` is unreachable from a single cell by design."""

    BACKTEST_VALIDATED = "BACKTEST_VALIDATED"
    NOT_ELIGIBLE = "NOT_ELIGIBLE"
    UNVERIFIED = "UNVERIFIED"


@dataclass(frozen=True, slots=True)
class MissingInput:
    """A named gap, and the function that already exists to fill it.

    The second field is what makes an UNVERIFIED table actionable rather than merely
    truthful: the follow-on lane reads its own specification off this output instead of
    re-deriving which figure was missing and where it comes from.
    """

    name: str
    computed_by: str


@dataclass(frozen=True, slots=True)
class CriterionOutcome:
    criterion_id: str
    frozen_text: str
    state: CriterionState
    detail: str
    inputs_used: tuple[str, ...] = ()
    missing_inputs: tuple[MissingInput, ...] = ()


@dataclass(frozen=True, slots=True)
class SelectionVerdict:
    manifest_sha256: str
    manifest_frozen_at_utc: str
    strategy_id: str
    symbol: str
    outcomes: tuple[CriterionOutcome, ...]
    state: SelectionState
    detail: str


class SelectionManifestUnusable(RuntimeError):
    """The manifest could not be read as the frozen criteria document."""


def load_selection_manifest(path: Any) -> bytes:
    """One read. The caller passes these bytes to :func:`evaluate_selection`.

    Bytes rather than a path, so the digest and the parse describe the same read — digesting
    one read and parsing another is how a verdict comes to describe a file that no longer
    exists (the descriptor-bound discipline of ADR-0053, applied to a document).
    """

    return bytes(path.read_bytes())


def _parsed(manifest_bytes: bytes) -> tuple[dict[str, Any], str]:
    digest = hashlib.sha256(manifest_bytes).hexdigest()
    try:
        document = json.loads(manifest_bytes.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise SelectionManifestUnusable(f"manifest is not UTF-8 JSON: {error}") from error
    if not isinstance(document, dict):
        raise SelectionManifestUnusable("manifest is not a JSON object")
    return document, digest


def _criteria_texts(document: Mapping[str, Any]) -> tuple[str, ...]:
    texts = document.get("criteria_for_backtest_validated")
    if not isinstance(texts, list) or len(texts) != len(CRITERION_IDS):
        raise SelectionManifestUnusable(
            "criteria_for_backtest_validated must be a list of "
            f"{len(CRITERION_IDS)} strings, got {type(texts).__name__}"
        )
    if not all(isinstance(text, str) for text in texts):
        raise SelectionManifestUnusable("criteria_for_backtest_validated holds a non-string")
    return tuple(str(text) for text in texts)


def _anchor_failure(criterion_id: str, text: str) -> str | None:
    for anchor in _TEXT_ANCHORS[criterion_id]:
        if anchor not in text:
            return anchor
    return None


def _unverified(
    criterion_id: str,
    text: str,
    detail: str,
    missing: tuple[MissingInput, ...],
    inputs_used: tuple[str, ...] = (),
) -> CriterionOutcome:
    return CriterionOutcome(
        criterion_id=criterion_id,
        frozen_text=text,
        state=CriterionState.UNVERIFIED,
        detail=detail,
        inputs_used=inputs_used,
        missing_inputs=missing,
    )


_WINDOW_GAP = MissingInput(
    name="the validation window's dates",
    computed_by=(
        "no function: the manifest has no validation_window field, and a campaign report's "
        "out-of-sample span is derived from warmup bars, not from a date"
    ),
)


def _c1(text: str, report: WalkForwardReport) -> CriterionOutcome:
    return _unverified(
        "C1",
        text,
        "net return after the named base costs is not on a campaign report, and the window "
        "the criterion names is not defined in the manifest",
        (
            MissingInput(
                "total return after costs",
                f"{_METRICS} -> PerformanceMetrics.total_return_fraction",
            ),
            MissingInput(
                "the cost model the run used (USD 1 min commission + 2 bps/side slippage)",
                "no function: a campaign report does not record its BacktestConfig",
            ),
            _WINDOW_GAP,
        ),
    )


def _c2(
    text: str, report: WalkForwardReport, baselines: Mapping[str, WalkForwardReport]
) -> CriterionOutcome:
    twin = baselines.get(_RANDOM_ENTRY_TWIN)
    inputs_used: tuple[str, ...] = ()
    compared = "the random-entry twin's report was not supplied"
    if twin is not None:
        inputs_used = (
            f"report.pooled_sharpe={report.pooled_sharpe}",
            f"{_RANDOM_ENTRY_TWIN}.pooled_sharpe={twin.pooled_sharpe}",
        )
        compared = f"Sharpe compared: candidate {report.pooled_sharpe} vs twin {twin.pooled_sharpe}"
    return _unverified(
        "C2",
        text,
        f"{compared}; the criterion requires BOTH total return and Sharpe, and total return "
        "is not on a campaign report, so half a conjunction cannot answer it",
        (
            MissingInput(
                "total return, candidate and twin",
                f"{_METRICS} -> PerformanceMetrics.total_return_fraction",
            ),
            _WINDOW_GAP,
        )
        + (
            (
                MissingInput(
                    f"a {_RANDOM_ENTRY_TWIN} report for this symbol",
                    "run_campaign with that strategy",
                ),
            )
            if twin is None
            else ()
        ),
        inputs_used,
    )


def _c3(
    text: str, report: WalkForwardReport, baselines: Mapping[str, WalkForwardReport]
) -> CriterionOutcome:
    baseline = baselines.get(_SMA_TREND_BASELINE)
    inputs_used = (f"{_SMA_TREND_BASELINE} report supplied",) if baseline is not None else ()
    return _unverified(
        "C3",
        text,
        "neither total return nor max drawdown is on a campaign report, for the candidate "
        "or for the SMA-trend baseline",
        (
            MissingInput(
                "total return, candidate and baseline",
                f"{_METRICS} -> PerformanceMetrics.total_return_fraction",
            ),
            MissingInput(
                "max drawdown, candidate and baseline",
                f"{_METRICS} -> PerformanceMetrics.max_drawdown_fraction",
            ),
            _WINDOW_GAP,
        )
        + (
            (
                MissingInput(
                    f"a {_SMA_TREND_BASELINE} report for this symbol",
                    "run_campaign with that strategy",
                ),
            )
            if baseline is None
            else ()
        ),
        inputs_used,
    )


def _c4(text: str, report: WalkForwardReport) -> CriterionOutcome:
    # The count IS measurable, per symbol, as round trips: WalkForwardReport.pooled_trades
    # counts ClosedTrade entries (entry->exit pairs) whose entry falls in the report's
    # out-of-sample span, for this one symbol. "pooled" there means across the walk-forward's
    # windows, never across symbols. It still cannot decide C4, because that span is derived
    # from warmup bars rather than from the window the criterion names.
    return _unverified(
        "C4",
        text,
        f"round trips measured for this symbol: {report.pooled_trades} against the frozen "
        f"floor of {_C4_MIN_CLOSED_TRADES}; but the span measured is the report's "
        "out-of-sample window, not the validation window the criterion names, and profit "
        "factor and the two stress legs are absent",
        (
            MissingInput("profit factor", f"{_METRICS} -> PerformanceMetrics.profit_factor"),
            MissingInput(
                "net-positive under 2x commission stress and >= 10 bps slippage stress",
                "no function: separate runs, which a campaign report does not reference",
            ),
            _WINDOW_GAP,
        ),
        (f"report.pooled_trades={report.pooled_trades}",),
    )


def _c5(text: str, report: WalkForwardReport) -> CriterionOutcome:
    return _unverified(
        "C5",
        text,
        "a campaign report has no notion of parameter variants",
        (
            MissingInput(
                "the SENSITIVITY variants' results",
                "scripts/run_research.py SENSITIVITY rows; not exposed as a library function",
            ),
            _WINDOW_GAP,
        ),
    )


def _c6(text: str, report: WalkForwardReport) -> CriterionOutcome:
    return _unverified(
        "C6",
        text,
        "structurally absent from this input: run_campaign refuses a stage_end reaching "
        "FINAL_START (2022-01-01), so a campaign report can never carry final-window "
        "evidence — this is not a missing measurement but a missing kind of run",
        (
            MissingInput(
                "a final/holdout window run",
                "no function: the C2 holdout guardian mediates that read, and it is a "
                "separate owner-typed event",
            ),
        ),
    )


def _aggregate(outcomes: tuple[CriterionOutcome, ...], *, guard: str) -> tuple[SelectionState, str]:
    """The manifest's own ladder, with UNVERIFIED added as the fail-closed rung.

    FAIL outranks UNVERIFIED deliberately. The alternative lets a candidate that definitely
    fails a criterion be reported as merely unmeasured, which is the more flattering error
    and therefore the more dangerous one.
    """

    by_id = {outcome.criterion_id: outcome for outcome in outcomes}
    blocking = [
        cid for cid in ("C1", "C2", "C3", "C4", "C5") if by_id[cid].state is CriterionState.FAIL
    ]
    if blocking:
        return (
            SelectionState.NOT_ELIGIBLE,
            f"fails {','.join(blocking)} (manifest: fails any of C1-C5)",
        )
    unverified = [cid for cid in CRITERION_IDS if by_id[cid].state is CriterionState.UNVERIFIED]
    if unverified:
        return (
            SelectionState.UNVERIFIED,
            f"{','.join(unverified)} could not be evaluated; a candidate is never promoted "
            "on incomplete evidence",
        )
    if by_id["C6"].state is CriterionState.FAIL:
        return SelectionState.NOT_ELIGIBLE, "passes C1-C5 but fails C6 (manifest)"
    # Every criterion this function could evaluate passed. It still does not promote: the
    # manifest reserves that judgement for a reader who applies the multiple-testing discount.
    return (
        SelectionState.UNVERIFIED,
        "every criterion evaluated here passed, and promotion is not this function's to make: "
        f"{guard}",
    )


def evaluate_selection(
    *,
    manifest_bytes: bytes,
    report: WalkForwardReport,
    baseline_reports: Mapping[str, WalkForwardReport] | None = None,
    expected_sha256: str | None = None,
) -> SelectionVerdict:
    """Answer the six frozen criteria for one (strategy, symbol) cell.

    No threshold, window or comparator is a parameter. A caller who could lower this
    function's floor would have the same defect the walk-forward's configurable floor has:
    a floor a caller can lower is not a floor.
    """

    document, digest = _parsed(manifest_bytes)
    texts = _criteria_texts(document)
    frozen_at = str(document.get("re_frozen_at_utc") or document.get("frozen_at_utc") or "")
    guard = str(document.get("multiple_testing_guard") or "")
    baselines: Mapping[str, WalkForwardReport] = baseline_reports or {}

    if expected_sha256 is not None and expected_sha256 != digest:
        outcomes = tuple(
            _unverified(
                cid,
                text,
                f"manifest digest {digest} does not match the expected {expected_sha256}; "
                "these are not the criteria this verdict was asked to judge against",
                (
                    MissingInput(
                        "the expected criteria document", "no function: supply the pinned bytes"
                    ),
                ),
            )
            for cid, text in zip(CRITERION_IDS, texts, strict=True)
        )
        return SelectionVerdict(
            manifest_sha256=digest,
            manifest_frozen_at_utc=frozen_at,
            strategy_id=report.strategy_id,
            symbol=report.symbol,
            outcomes=outcomes,
            state=SelectionState.UNVERIFIED,
            detail="the criteria document is not the expected one",
        )

    evaluators = {
        "C1": lambda text: _c1(text, report),
        "C2": lambda text: _c2(text, report, baselines),
        "C3": lambda text: _c3(text, report, baselines),
        "C4": lambda text: _c4(text, report),
        "C5": lambda text: _c5(text, report),
        "C6": lambda text: _c6(text, report),
    }
    evaluated: list[CriterionOutcome] = []
    for cid, text in zip(CRITERION_IDS, texts, strict=True):
        anchor = _anchor_failure(cid, text)
        if anchor is not None:
            evaluated.append(
                _unverified(
                    cid,
                    text,
                    f"the frozen text no longer contains {anchor!r}, so this module's reading "
                    "of it may no longer hold; re-review the criterion rather than evaluating "
                    "a constant the text does not support",
                    (
                        MissingInput(
                            "a reviewed reading of the re-worded criterion",
                            "no function: human review",
                        ),
                    ),
                )
            )
            continue
        evaluated.append(evaluators[cid](text))

    frozen = tuple(evaluated)
    state, detail = _aggregate(frozen, guard=guard)
    return SelectionVerdict(
        manifest_sha256=digest,
        manifest_frozen_at_utc=frozen_at,
        strategy_id=report.strategy_id,
        symbol=report.symbol,
        outcomes=frozen,
        state=state,
        detail=detail,
    )


__all__ = [
    "CRITERION_IDS",
    "SELECTION_MANIFEST_PATH",
    "CriterionOutcome",
    "CriterionState",
    "MissingInput",
    "SelectionManifestUnusable",
    "SelectionState",
    "SelectionVerdict",
    "evaluate_selection",
    "load_selection_manifest",
]
