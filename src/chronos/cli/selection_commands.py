"""The frozen C1-C6 selection verdict, at the operator's hand.

`chronos.research.selection_verdict` answers the six frozen criteria for one
(strategy, symbol) cell. This is the command that puts that answer in front of an
owner without a Python session.

## What it prints, and why the exit code is 1

Today every criterion returns UNVERIFIED, because the criteria are prose with no
machine-readable window and a campaign report carries none of the figures four of them
need. **That table is the artifact**, not a consolation: it is a per-criterion statement
of what is missing and which existing function would compute it. Exit 0 is reserved for
``BACKTEST_VALIDATED``, which a single-cell verdict cannot reach by ruling — the
manifest's own multiple-testing guard reserves promotion for a reader. So this command
exits 1 today, with the table, and that is the intended owner-visible outcome rather
than a failure to run.

Exit 2 is separate and means the inputs could not be judged at all: an unreadable
manifest, a report file that is not a report, a digest that is not the expected one.
That distinction mirrors ``data verify``'s: a refusal to judge is not a judgement.

## Read-only

It opens files, parses JSON, and prints. No network, no ``Settings``, no writes — the
same boundary the intake commands hold, bound by the same style of tripwire test.
"""

from __future__ import annotations

import argparse
import json
import textwrap
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # imported for annotations only; see the note below
    from chronos.research.selection_verdict import SelectionVerdict
    from chronos.research.walkforward import WalkForwardReport, WindowResult

# The research imports are LOCAL to the functions that need them, and that is
# load-bearing rather than stylistic. `chronos.research.walkforward` reaches the backtest
# engine and therefore `chronos.execution.brokers`; importing it at module scope puts a
# broker module into every import graph that touches `chronos.cli.main`, and
# tests/platform_unit/test_monitoring.py::test_monitoring_pulls_no_broker_module_transitively
# fails — which it did, on the first version of this file. Registration must stay cheap:
# `add_selection_command` is what `main.py` imports, and it needs nothing but argparse.
# Do not hoist these to the top.

_WIDTH = 92


class SelectionInputUnusable(RuntimeError):
    """A report file could not be read as a walk-forward or campaign report."""

    def __init__(self, path: Path, reason: str) -> None:
        super().__init__(reason)
        self.path = path
        self.reason = reason


def _window(value: Any, *, path: Path) -> WindowResult:
    from chronos.research.walkforward import WindowResult

    try:
        return WindowResult(
            start=date.fromisoformat(str(value["start"])),
            end=date.fromisoformat(str(value["end"])),
            bars=int(value["bars"]),
            trades=int(value["trades"]),
            oos_return=float(value["oos_return"]),
            sharpe=None if value["sharpe"] is None else float(value["sharpe"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise SelectionInputUnusable(path, f"a window is not a WindowResult ({error})") from error


def _report(value: Any, *, path: Path) -> WalkForwardReport:
    """Rebuild one report, refusing anything that is not exactly one.

    Strict on purpose: a report reconstructed from a partial mapping would let the
    verdict judge a cell nobody produced, and its `detail` lines would name numbers that
    came from a default rather than from a run.
    """

    from chronos.research.walkforward import WalkForwardReport, WalkForwardVerdict

    if not isinstance(value, dict):
        raise SelectionInputUnusable(path, "a report entry is not a JSON object")
    try:
        sharpe_ci = value["sharpe_ci"]
        return WalkForwardReport(
            strategy_id=str(value["strategy_id"]),
            symbol=str(value["symbol"]),
            seed=int(value["seed"]),
            windows=tuple(_window(window, path=path) for window in value["windows"]),
            pooled_bars=int(value["pooled_bars"]),
            pooled_trades=int(value["pooled_trades"]),
            pooled_sharpe=None if value["pooled_sharpe"] is None else float(value["pooled_sharpe"]),
            sharpe_ci=None if sharpe_ci is None else (float(sharpe_ci[0]), float(sharpe_ci[1])),
            psr=None if value["psr"] is None else float(value["psr"]),
            deflated_sharpe=(
                None if value["deflated_sharpe"] is None else float(value["deflated_sharpe"])
            ),
            trial_count=int(value["trial_count"]),
            purged_cv=str(value["purged_cv"]),
            verdict=WalkForwardVerdict(str(value["verdict"])),
            reason=str(value["reason"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise SelectionInputUnusable(
            path, f"not a walk-forward report ({error.__class__.__name__}: {error})"
        ) from error


def load_reports(path: Path) -> tuple[WalkForwardReport, ...]:
    """One walk-forward report, or every evaluated cell of a campaign report.

    A campaign report yields one verdict per cell rather than one for the file: the
    criteria are per symbol and are never pooled across them.
    """

    try:
        payload = json.loads(path.read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as error:
        raise SelectionInputUnusable(path, f"unreadable JSON ({error})") from error
    if not isinstance(payload, dict):
        raise SelectionInputUnusable(path, "report file is not a JSON object")
    if "cells" in payload:
        cells = payload["cells"]
        if not isinstance(cells, list):
            raise SelectionInputUnusable(path, "campaign report 'cells' is not a list")
        reports = tuple(
            _report(cell.get("report"), path=path)
            for cell in cells
            if isinstance(cell, dict) and cell.get("report") is not None
        )
        if not reports:
            raise SelectionInputUnusable(path, "campaign report has no evaluated cell")
        return reports
    return (_report(payload, path=path),)


def _wrapped(text: str, indent: str) -> list[str]:
    return textwrap.wrap(text, width=_WIDTH - len(indent)) or [""]


def _print_verdict(verdict: SelectionVerdict) -> None:
    print(f"\n{verdict.strategy_id} / {verdict.symbol}")
    print(f"  criteria: {verdict.manifest_sha256} (frozen {verdict.manifest_frozen_at_utc})")
    for outcome in verdict.outcomes:
        print(f"\n  {outcome.criterion_id}  {outcome.state.value}")
        quoted = _wrapped(outcome.frozen_text, "      ")
        for index, line in enumerate(quoted):
            opening = '      "' if index == 0 else "       "
            closing = '"' if index == len(quoted) - 1 else ""
            print(f"{opening}{line}{closing}")
        # The marker goes on the first line only: repeating it at every wrap point breaks
        # the sentence for anything reading this output, and reads as a list to a person.
        for index, line in enumerate(_wrapped(outcome.detail, "      -> ")):
            print(f"      -> {line}" if index == 0 else f"         {line}")
        for used in outcome.inputs_used:
            print(f"      used: {used}")
        for missing in outcome.missing_inputs:
            print(f"      MISSING: {missing.name}")
            for line in _wrapped(missing.computed_by, "               "):
                print(f"               {line}")
    print(f"\n  VERDICT: {verdict.state.value}")
    for line in _wrapped(verdict.detail, "    "):
        print(f"    {line}")


def cmd_research_selection(args: argparse.Namespace) -> int:
    """Evaluate the frozen criteria for every cell in a report. Read-only."""

    from chronos.research.selection_verdict import (
        CriterionState,
        SelectionManifestUnusable,
        SelectionState,
        evaluate_selection,
        load_selection_manifest,
    )

    try:
        manifest_bytes = load_selection_manifest(args.manifest)
        reports = load_reports(args.report)
        baselines: dict[str, Any] = {}
        for baseline_path in args.baseline_report or ():
            for baseline in load_reports(baseline_path):
                baselines[baseline.strategy_id] = baseline
    except OSError as error:
        print(f"UNUSABLE {args.manifest}: {error}")
        return 2
    except SelectionInputUnusable as error:
        print(f"UNUSABLE {error.path}: {error.reason}")
        return 2

    verdicts: list[Any] = []
    for report in reports:
        try:
            verdicts.append(
                evaluate_selection(
                    manifest_bytes=manifest_bytes,
                    report=report,
                    baseline_reports=baselines,
                    expected_sha256=args.expected_sha256,
                )
            )
        except SelectionManifestUnusable as error:
            print(f"UNUSABLE {args.manifest}: {error}")
            return 2

    for verdict in verdicts:
        _print_verdict(verdict)

    unverified = sum(
        1
        for verdict in verdicts
        for outcome in verdict.outcomes
        if outcome.state is CriterionState.UNVERIFIED
    )
    print(
        f"\n{len(verdicts)} cell(s); {unverified} criterion answer(s) UNVERIFIED. "
        "Exit 0 is reserved for BACKTEST_VALIDATED, which a single-cell verdict does not "
        "reach: the manifest's multiple-testing guard reserves promotion for a reader."
    )
    if args.expected_sha256 is not None and verdicts[0].manifest_sha256 != args.expected_sha256:
        # Judged nothing, and says so with an exit code that is not a verdict: these are
        # not the criteria the caller pinned, so the table above is a refusal, not a result.
        print(
            f"UNUSABLE {args.manifest}: digest {verdicts[0].manifest_sha256} is not the "
            f"expected {args.expected_sha256}"
        )
        return 2
    if all(verdict.state is SelectionState.BACKTEST_VALIDATED for verdict in verdicts):
        return 0
    return 1


def add_selection_command(sub: Any) -> None:
    """Register ``research selection`` on the operator CLI."""

    selection = sub.add_parser(
        "selection",
        help="answer the frozen C1-C6 criteria for a campaign report (read-only)",
    )
    selection.add_argument("--manifest", type=Path, required=True)
    selection.add_argument("--report", type=Path, required=True)
    selection.add_argument("--baseline-report", type=Path, action="append", default=[])
    selection.add_argument("--expected-sha256", default=None)
    selection.set_defaults(func=cmd_research_selection)


__all__ = ["add_selection_command", "cmd_research_selection", "load_reports"]
