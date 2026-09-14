"""Run the per-symbol delivery gates over a capture store holding ANY subset of symbols.

`data verify` requires a delivery of exactly the six campaign symbols
(`data_intake.py:430`), which is right for a verdict and useless for the first capture:
an owner who pulls DIA on Monday cannot ask anything of those bytes until the other five
exist. This command closes that gap without touching the identity — it judges bars, not a
delivery, and it never returns a verdict.

## What it reports, and where the codes come from

The gates are `certification.gate_symbol_bars` — the same function `certify_export` calls
for every window it judges, extracted rather than reimplemented so the two cannot diverge.
Findings therefore carry the verifier's own `FindingKind` values: `MISSING_SESSION`,
`UNEXPECTED_BAR`, `COVERAGE_BELOW_FLOOR`, `BLOCKING_QUALITY_ISSUE`,
`UNCLASSIFIED_MATERIAL_MOVE`, `UNRECONCILED_SPLIT`, `CALENDAR_NOT_COVERED`.

## What it will not do

**No verdict, in any shape.** No `Verdict`, no certification digest, no report file, no
release, and no write of any kind — this module opens files and returns objects. A
partial capture cannot satisfy the frozen criteria, and a command that answered anyway
would be a second, weaker acceptance surface next to the real one. The delivery-level
gates are absent for the same reason rather than by oversight: the attestation, the
provider price basis, the holdout map and the six-symbol identity are not answerable from
one symbol's files, so this says nothing about them.

**It does not soften anything.** Every gate it runs is the gate `data verify` runs, at the
same thresholds. A store that passes here still has to become a delivery and be certified;
what it buys is finding out on Monday rather than after the sixth capture.

## Two kinds of outcome

A **finding** is what the gates say about bars that could be read. A **refusal** is the
store not being a readable subject at all — a missing bars file, a bars filename that is
not the upper-case symbol stem the layout requires, an unparseable CSV, an adjusted-close
column (§2's delivery is unadjusted as traded), or a `MANIFEST.json` whose own witnesses
disagree with the bytes. Refusals name the file, exactly as `data assemble`
does, and reuse its checks (`data_assemble.cross_check_manifest`, `parse_actions`).

An absent action file is neither: it is reported as absent, and split reconciliation then
has nothing to check for that symbol. An absent stream and a reviewed-empty one are
different claims, and this command will not blur them any more than assemble will.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from chronos.marketdata.csv_provider import load_daily_csv_bytes
from chronos.research.certification import Finding, SymbolWindow, gate_symbol_bars
from chronos.research.data_assemble import (
    AssembleRefusal,
    cross_check_manifest,
    parse_actions,
)
from chronos.research.session_calendar import SessionCalendar


class CheckRefusal(RuntimeError):
    """The store is not a readable subject, and the reason names the file."""

    def __init__(self, path: Path, reason: str) -> None:
        super().__init__(reason)
        self.path = path
        self.reason = reason


@dataclass(frozen=True, slots=True)
class SymbolCheck:
    """What the gates said about one symbol's bars. Evidence, not a verdict."""

    symbol: str
    bar_count: int
    start: date
    end: date
    coverage: float
    findings: tuple[Finding, ...]
    #: None means no action file exists for this symbol — which is not the same claim as
    #: an empty one, and is why this is not simply 0.
    action_count: int | None
    manifest_checked: bool


@dataclass(frozen=True, slots=True)
class CheckResult:
    store: Path
    symbols: tuple[SymbolCheck, ...]

    @property
    def finding_count(self) -> int:
        return sum(len(item.findings) for item in self.symbols)


def _refuse(path: Path, reason: str) -> CheckRefusal:
    return CheckRefusal(path, reason)


def available_symbols(store: Path) -> tuple[str, ...]:
    """Every symbol the store has bars for, whatever subset that is.

    The store's bars filenames are upper-case symbol stems: ``bars/<SYMBOL>.csv`` is the
    layout ``histdata.store.bars_path`` writes and the verifier reads (``CAMPAIGN_SYMBOLS``
    are upper-case), and ``check_store`` reopens each file by exactly that name. A stem that
    is not its own upper-case is therefore refused HERE, before any gate runs, naming the
    file and the name it would have to carry — the previous version folded ``dia`` to
    ``DIA``, reopened ``DIA.csv``, and handed the operator a FileNotFoundError traceback.
    Two files that fold to one symbol are refused as ambiguous rather than resolved: which
    of them is canonical is not a choice this command makes.

    The extension follows the same rule. ``bars/DIA.CSV`` used to fall outside a ``*.csv``
    glob and simply vanish — the store reported five symbols and no findings, exit 0, with
    DIA silently absent from the check. Every file whose name is ``.csv`` case-insensitively
    is therefore scanned and grouped by its upper-cased stem, and a member that is not
    exactly ``<STEM>.csv`` is the near miss: refused naming the canonical file, or, with the
    canonical file also present, refused as ambiguous. Files that are not ``.csv`` under any
    casing (an operator's notes) are still ignored — the refusal is not widened to them.
    """

    bars = store / "bars"
    if not bars.is_dir():
        raise _refuse(bars, "the store has no bars/ directory")
    by_symbol: dict[str, list[Path]] = {}
    for path in sorted(bars.iterdir()):
        if not path.is_file() or not path.name.lower().endswith(".csv"):
            continue
        by_symbol.setdefault(path.name[: -len(".csv")].upper(), []).append(path)
    for symbol, paths in sorted(by_symbol.items()):
        canonical = bars / f"{symbol}.csv"
        if len(paths) > 1:
            names = ", ".join(path.name for path in paths)
            raise _refuse(
                bars,
                f"{symbol}: ambiguous bars files ({names}); the store's bars filenames are "
                f"upper-case symbol stems (bars/{symbol}.csv, the histdata.store layout) and "
                "two files cannot both be it — remove one rather than have the gates guess",
            )
        (path,) = paths
        if path != canonical:
            raise _refuse(
                path,
                f"bars filename {path.name!r} is not the upper-case symbol stem plus "
                f"lower-case .csv the store layout requires (bars/{symbol}.csv, "
                "histdata.store.bars_path); rename it rather than have the gates guess "
                "which symbol it is",
            )
    return tuple(sorted(by_symbol))


def _is_symbol_stem(symbol: object) -> bool:
    """True when ``symbol`` is the one thing a manifest key may be: an upper-case filename stem.

    The same rule ``available_symbols`` applies to the bars filenames, read from the other
    side: ``f"{symbol}.csv"`` must be a single POSIX path component (no ``/``, no NUL) whose
    stem is its own upper-case. Anything else cannot name ``bars/<SYMBOL>.csv`` and must
    not be joined onto the store path at all.
    """

    if not isinstance(symbol, str) or not symbol or symbol != symbol.upper():
        return False
    return "/" not in symbol and "\x00" not in symbol


def _manifest_entries(store: Path) -> dict[str, Any] | None:
    """The manifest's per-symbol witnesses, or None when the store has no manifest.

    Absence is reported rather than treated as agreement: a capture writes a manifest, so a
    store without one is unusual enough that the operator should be told the witness
    cross-check did not run, instead of reading its silence as a pass.
    """

    path = store / "MANIFEST.json"
    if not path.exists():
        return None
    try:
        document = json.loads(path.read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as error:
        raise _refuse(path, f"unreadable JSON ({error})") from error
    if not isinstance(document, dict):
        raise _refuse(path, "MANIFEST.json is not a JSON object")
    entries = document.get("symbols")
    if not isinstance(entries, dict):
        raise _refuse(path, "MANIFEST.json has no 'symbols' object")
    return entries


def check_store(store: Path, symbols: tuple[str, ...] | None = None) -> CheckResult:
    """Judge each requested symbol's bars against the per-symbol gates. Read-only.

    ``symbols`` defaults to every symbol the store holds bars for, so a one-symbol capture
    needs no flag beyond ``--store``.
    """

    requested = tuple(symbol.upper() for symbol in symbols) if symbols else None
    present = available_symbols(store)
    chosen = requested if requested is not None else present
    if not chosen:
        raise _refuse(store / "bars", "the store holds no bars to check")
    missing = [symbol for symbol in chosen if symbol not in present]
    if missing:
        raise _refuse(
            store / "bars",
            f"the store has no bars for {', '.join(sorted(missing))}; it holds "
            f"{', '.join(present) or 'nothing'}",
        )

    entries = _manifest_entries(store)
    if entries is not None:
        # A manifest key is untrusted text. Only an upper-case filename stem can name
        # bars/<SYMBOL>.csv; an absolute or traversal-shaped key would make the join below
        # resolve OUTSIDE the store (pathlib drops the prefix), so no path is built from a
        # key until every key has passed the same rule the bars filenames must.
        malformed = sorted(repr(symbol) for symbol in entries if not _is_symbol_stem(symbol))
        if malformed:
            raise _refuse(
                store / "MANIFEST.json",
                f"MANIFEST 'symbols' key(s) {', '.join(malformed)} are not an upper-case "
                "symbol stem (bars/<SYMBOL>.csv, the histdata.store layout); a key that "
                "cannot name a bars file is refused rather than resolved as a path",
            )
        # The mirror of the witness cross-check below: a symbol the manifest records whose
        # bars file is ABSENT was silently skipped (five symbols checked, exit 0, DIA gone).
        # It is the store's own record disagreeing with its bytes, so it is refused at the
        # store level, before any gate, whichever subset was requested.
        # "Backed" means a regular file the store validated (`available_symbols`: regular files
        # with the canonical name) — not any entry at the name: a directory or a FIFO at
        # bars/DIA.csv satisfied Path.exists() and let a SPY-only check run (C-2X review).
        unbacked = sorted(str(symbol) for symbol in entries if symbol not in present)
        if unbacked:
            named = ", ".join(f"{symbol} (bars/{symbol}.csv)" for symbol in unbacked)
            raise _refuse(
                store / "MANIFEST.json",
                f"MANIFEST records bars for {named} but the store has no such file; "
                "the store's own record disagrees with its bytes",
            )
    calendar = SessionCalendar()
    checked: list[SymbolCheck] = []
    for symbol in chosen:
        bars_path = store / "bars" / f"{symbol}.csv"
        raw = bars_path.read_bytes()
        try:
            loaded = load_daily_csv_bytes(raw, path=bars_path, symbol=symbol, source="history")
        except Exception as error:
            raise _refuse(bars_path, f"the verifier would refuse these bars ({error})") from error
        series = loaded.series
        if not series.bars:
            raise _refuse(bars_path, f"{symbol}: no bars in the file")
        if loaded.has_adjusted_close:
            raise _refuse(
                bars_path,
                f"{symbol}: carries an adjusted-close column; a delivery must be "
                "unadjusted as traded",
            )
        first, last = series.bars[0].session_date, series.bars[-1].session_date

        actions_path = store / "corporate_actions" / f"{symbol}.json"
        actions = None
        if actions_path.exists():
            try:
                actions = parse_actions(actions_path.read_bytes(), actions_path)
            except AssembleRefusal as error:
                # The same read `data assemble` performs; only the exception's name differs,
                # and the message already names the file and the reason.
                raise _refuse(error.path, error.reason) from error

        if entries is not None:
            try:
                cross_check_manifest(
                    store / "MANIFEST.json",
                    symbol,
                    entries.get(symbol) or {},
                    bar_count=len(series.bars),
                    bars_sha256=loaded.sha256,
                    first=first,
                    last=last,
                    action_count=len(actions) if actions is not None else 0,
                    actions_sha256=_actions_digest(actions_path),
                )
            except AssembleRefusal as error:
                raise _refuse(error.path, error.reason) from error

        coverage, findings = gate_symbol_bars(
            SymbolWindow(symbol=symbol, start=first, end=last),
            series,
            actions or (),
            calendar=calendar,
        )
        checked.append(
            SymbolCheck(
                symbol=symbol,
                bar_count=len(series.bars),
                start=first,
                end=last,
                coverage=coverage.coverage,
                findings=findings,
                action_count=len(actions) if actions is not None else None,
                manifest_checked=entries is not None and symbol in entries,
            )
        )
    return CheckResult(store=store, symbols=tuple(checked))


def _actions_digest(path: Path) -> str:
    """The digest of an action file, or the digest of nothing when there is no file.

    An absent file has no bytes to hash, and the manifest cannot hold a witness for bytes
    that do not exist — so the empty string skips that witness rather than inventing one.
    """

    if not path.exists():
        return ""
    return hashlib.sha256(path.read_bytes()).hexdigest()


__all__ = [
    "CheckRefusal",
    "CheckResult",
    "SymbolCheck",
    "available_symbols",
    "check_store",
]
