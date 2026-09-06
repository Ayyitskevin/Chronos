"""Turn the owner's capture store into the delivery directory the verifier accepts.

`docs/OWNER_DATA_EXPORT.md` §6 records the gap this closes: the capture "populates the
store, not a delivery", and assembling §2's layout from its output was a manual step. The
two layouts already mirror each other — `bars/<SYM>.csv`, `corporate_actions/<SYM>.json`,
a manifest, a holdout declaration — so the conversion is a copy plus a derived `INTAKE.json`.

## What this module will not do

**It never invents a field an owner has to assert.** Provenance (who exported the data, the
digest of their receipt, when, how, under what licence), the corporate-action attestation,
and the classified-move list are owner acts; §4 says so of the attestation in as many words —
"code cannot do this half". A missing one is a refusal that names the field, never a default
and never an empty string. A delivery that certifies on a fabricated provenance line is worse
than no delivery, because the certification digest would then attest to nothing.

**It never writes to the store.** The store is the capture's output and the delivery is a
copy; this module opens the store read-only and writes only under `--out` — including
refusing an `--out` that resolves inside the store, which is the one placement that turns
"writes only under --out" into a write to the store.

**One snapshot of bytes answers every question.** Each source file is read once; the parse,
the manifest cross-check, the published bytes and the digest in `INTAKE.json` are all that
same snapshot. Re-opening a file to copy it after validating it leaves a window in which a
concurrent capture can substitute the bytes, which produced a delivery that assembled
"successfully" and then failed `data verify` on a bar count it had itself measured.

**It refuses the bar defects the verifier would refuse, at the point where the cause is
visible.** The bars are parsed with `load_daily_csv_bytes` — the same parser `data verify`
uses — before anything is copied, so a timestamped date cell or a missing column is reported
against the store file that has it rather than surfacing later as an opaque `UNVERIFIED` on a
delivery somebody has already moved.

That guarantee stops at the bars, deliberately. The owner declarations are checked for
PRESENCE here and for SHAPE by the verifier: an attestation missing an inner field assembles
and then fails `data verify` as `UNVERIFIED`, naming the field. Re-implementing that schema
here would be a second definition of it, and the first one to drift.

## What it derives, and from what

| INTAKE.json | derived from |
|---|---|
| `symbols[].bars_sha256`, `corporate_actions_sha256` | the validated byte snapshot, hashed |
| `symbols[].bar_count`, `corporate_action_count` | the parsed files, not the manifest |
| `symbols[].window` | the first and last parsed session date, cross-checked against the manifest |
| `holdout_map` | `HOLDOUTS.json`, with the status and reason rules of §2 |
| `interval`, `adjustment_policy` | fixed declarations, per §2 |

The counts and digests are deliberately re-derived from the bytes rather than copied out of
`MANIFEST.json`: §2 calls them "independent claims the verifier can contradict", and a claim
copied from the same document it is meant to check is not independent.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from chronos.histdata.corporate_actions import CorporateAction
from chronos.marketdata.csv_provider import load_daily_csv_bytes
from chronos.research.data_intake import CAMPAIGN_SYMBOLS, INTAKE_SCHEMA_VERSION

INTERVAL = "1d"
ADJUSTMENT_POLICY = "unadjusted_as_traded"
_HOLDOUT_STATUSES = ("clean", "seen", "burned")
_PROVENANCE_FIELDS = (
    "source_id",
    "source_receipt_sha256",
    "retrieved_at",
    "retrieval_method",
    "license_note",
)


class AssembleRefusal(RuntimeError):
    """The store cannot be assembled into a delivery, and the reason names the cause."""

    def __init__(self, path: Path, reason: str) -> None:
        super().__init__(reason)
        self.path = path
        self.reason = reason


@dataclass(frozen=True, slots=True)
class AssembleResult:
    delivery: Path
    symbols: tuple[str, ...]
    bar_rows: int
    action_count: int
    holdout_spans: int
    #: Symbols whose action file is present and an EMPTY array. That is an owner statement
    #: — "reviewed, no actions in this window" — and it is surfaced rather than passed over,
    #: because it is indistinguishable from an absent stream to anything downstream that
    #: reads through store.read_actions().
    owner_declared_no_actions: tuple[str, ...] = ()


def _refuse(path: Path, reason: str) -> AssembleRefusal:
    return AssembleRefusal(path, reason)


def _read_json(path: Path) -> Any:
    if not path.exists():
        raise _refuse(path, "required store file is absent")
    try:
        return json.loads(path.read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as error:
        raise _refuse(path, f"unreadable JSON ({error})") from error


def _require_symbol_set(manifest_path: Path, manifest: Any) -> dict[str, Any]:
    if not isinstance(manifest, dict):
        raise _refuse(manifest_path, "MANIFEST.json is not a JSON object")
    symbols = manifest.get("symbols")
    if not isinstance(symbols, dict):
        raise _refuse(manifest_path, "MANIFEST.json has no 'symbols' object")
    present = {str(name).upper() for name in symbols}
    required = set(CAMPAIGN_SYMBOLS)
    missing = sorted(required - present)
    extra = sorted(present - required)
    # A partial set is not a partial pass. The verifier refuses it and so does this, one
    # step earlier, where the operator can still fix the capture.
    if missing:
        raise _refuse(
            manifest_path,
            f"store is missing required symbol(s): {', '.join(missing)}; "
            f"the delivery contract is exactly {', '.join(CAMPAIGN_SYMBOLS)}",
        )
    if extra:
        raise _refuse(
            manifest_path,
            f"store carries symbol(s) outside the delivery contract: {', '.join(extra)}",
        )
    return {str(name).upper(): entry for name, entry in symbols.items()}


def _provenance_block(provenance: dict[str, str], out: Path) -> dict[str, str]:
    missing = [field for field in _PROVENANCE_FIELDS if not provenance.get(field)]
    if missing:
        raise _refuse(
            out,
            "provenance is an owner assertion and cannot be derived; missing "
            f"--{'/--'.join(field.replace('_', '-') for field in missing)}",
        )
    digest = provenance["source_receipt_sha256"]
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest.lower()):
        raise _refuse(out, "source_receipt_sha256 must be 64 lowercase hex characters")
    return {field: provenance[field] for field in _PROVENANCE_FIELDS}


def _holdout_map(
    store: Path, windows_by_symbol: dict[str, tuple[date, date]]
) -> list[dict[str, str]]:
    """Derive the delivery's per-symbol tiling from the store's windows schema.

    These are two different shapes and the difference is the whole of this function.
    ``HOLDOUTS.json`` is the **store's** schema — `name`, `start`, `end`, an optional
    `symbols` scope, an optional `reason`, and **no status**, because
    ``histdata.holdout.HoldoutWindow`` has no such field. The delivery's ``holdout_map``
    is a **per-symbol tiling** where every supplied session is claimed exactly once and
    every span carries a status. So the status is derived from what a holdout declaration
    *means*, not read from a key the store cannot write:

    - a span inside a declared holdout window is ``clean`` — that is what declaring a
      holdout says: reserved, not yet looked at;
    - the remainder of each symbol's supplied range is ``seen`` — it has been available to
      research all along, and calling it ``clean`` would claim an untouched reserve that
      does not exist. This is the conservative direction: ``seen`` under-claims, ``clean``
      over-claims, and only one of those is recoverable;
    - ``burned`` is **never derived**. It means a reserve was consumed and §2 requires a
      reason for it; a reason is an owner statement, so a burned span can only arrive as
      an explicit declaration in the store's own `reason` field alongside a window whose
      name says so. If a window carries a reason, its spans keep it.
    """

    path = store / "HOLDOUTS.json"
    document = _read_json(path)
    if not isinstance(document, dict):
        raise _refuse(path, "HOLDOUTS.json is not a JSON object")
    windows = document.get("windows")
    if not isinstance(windows, list):
        raise _refuse(path, "HOLDOUTS.json has no 'windows' array")

    declared: dict[str, list[tuple[date, date, str, str]]] = {s: [] for s in windows_by_symbol}
    for index, raw in enumerate(windows):
        where = f"windows[{index}]"
        if not isinstance(raw, dict):
            raise _refuse(path, f"{where} is not an object")
        for field in ("name", "start", "end"):
            if not raw.get(field):
                raise _refuse(path, f"{where} is missing '{field}'")
        try:
            w_start = date.fromisoformat(str(raw["start"]))
            w_end = date.fromisoformat(str(raw["end"]))
        except ValueError as error:
            raise _refuse(path, f"{where} has an unparseable date ({error})") from error
        if w_end < w_start:
            raise _refuse(path, f"{where} ends before it starts")
        scoped = tuple(str(sym).upper() for sym in raw.get("symbols", ()) or ())
        unknown = sorted(set(scoped) - set(windows_by_symbol))
        if unknown:
            raise _refuse(
                path, f"{where} names symbol(s) outside the delivery: {', '.join(unknown)}"
            )
        for symbol in scoped or tuple(windows_by_symbol):
            declared[symbol].append((w_start, w_end, str(raw["name"]), str(raw.get("reason", ""))))

    spans: list[dict[str, str]] = []
    for symbol in sorted(windows_by_symbol):
        supplied_start, supplied_end = windows_by_symbol[symbol]
        ordered = sorted(declared[symbol])
        cursor = supplied_start
        for w_start, w_end, _name, reason in ordered:
            lo, hi = max(w_start, supplied_start), min(w_end, supplied_end)
            if hi < lo:
                continue  # declared entirely outside what this symbol supplies
            if lo < cursor:
                raise _refuse(
                    path,
                    f"{symbol}: holdout windows overlap at {lo.isoformat()}; a session cannot "
                    "carry two classifications",
                )
            if cursor < lo:
                spans.append(_span(symbol, "seen", cursor, _day_before(lo), ""))
            spans.append(_span(symbol, "clean", lo, hi, reason))
            cursor = _day_after(hi)
        if cursor <= supplied_end:
            spans.append(_span(symbol, "seen", cursor, supplied_end, ""))
    return spans


def _span(symbol: str, status: str, start: date, end: date, reason: str) -> dict[str, str]:
    span = {
        "symbol": symbol,
        "name": f"{symbol.lower()}-{status}-{start.isoformat()}",
        "start": start.isoformat(),
        "end": end.isoformat(),
        "status": status,
    }
    if reason:
        span["reason"] = reason
    return span


def _day_before(day: date) -> date:
    return day - timedelta(days=1)


def _day_after(day: date) -> date:
    return day + timedelta(days=1)


def _owner_json(
    path: Path | None, *, field: str, flag: str, default: Any, required: bool, shape: type
) -> Any:
    """Load an owner-supplied JSON file, refusing anything that is not the declared shape.

    A supplied FILENAME is not yet a supplied assertion (Astra, A1): a file holding `null`
    parsed clean here and then failed `data verify` as "corporate_action_attestation must be
    an object", which reports the owner's omission against the delivery instead of against
    the file that has it. The shape is checked at the input, and the message names the flag
    that supplied it rather than a flag spelled from the field name.
    """

    if path is None:
        if required:
            raise _refuse(
                Path(field),
                f"{field} is an owner assertion (§4: code cannot do this half) and must be "
                f"supplied with {flag} <path>",
            )
        return default
    document = _read_json(path)
    if not isinstance(document, shape):
        kind = "a JSON object" if shape is dict else "a JSON array"
        found = "null" if document is None else type(document).__name__
        raise _refuse(path, f"{field} must be {kind}, got {found} ({flag})")
    return document


def assemble_delivery(
    store: Path,
    out: Path,
    *,
    delivery_id: str,
    provenance: dict[str, str],
    attestation_path: Path | None,
    classified_moves_path: Path | None = None,
    supersedes: str | None = None,
) -> AssembleResult:
    """Write the §2 delivery for ``store`` into ``out``. Read-only on ``store``.

    Two passes on purpose: everything is read and validated before anything is written, so
    a refusal leaves no half-built delivery behind and names the store file that caused it.
    """

    _require_disjoint(store, out)
    manifest_path = store / "MANIFEST.json"
    entries = _require_symbol_set(manifest_path, _read_json(manifest_path))
    provenance_block = _provenance_block(provenance, out)
    attestation = _owner_json(
        attestation_path,
        field="corporate_action_attestation",
        flag="--attestation",
        default=None,
        required=True,
        shape=dict,
    )
    classified_moves = _owner_json(
        classified_moves_path,
        field="classified_moves",
        flag="--classified-moves",
        default=[],
        required=False,
        shape=list,
    )

    # ── pass 1: read and validate. Nothing is written in this block. ──
    read: dict[str, dict[str, Any]] = {}
    empty_action_symbols: list[str] = []
    for symbol in CAMPAIGN_SYMBOLS:
        bars_src = store / "bars" / f"{symbol}.csv"
        actions_src = store / "corporate_actions" / f"{symbol}.json"
        if not bars_src.exists():
            raise _refuse(bars_src, f"{symbol}: bars file is absent from the store")
        if not actions_src.exists():
            # Astra's lane-C trap, made explicit: store.read_actions() returns () for a
            # MISSING file, so an absent action stream is indistinguishable from a
            # reviewed-empty one to anything that goes through it. The capture writes no
            # action files at all, so this is the common case, not an edge case — and this
            # command will not author one. An empty array is an owner statement.
            raise _refuse(
                actions_src,
                f"{symbol}: corporate_actions/{symbol}.json is absent. The capture writes "
                "bars and manifest only; the action stream is a separate owner step. "
                "assemble will not create an empty file to stand in for one — an absent "
                "stream and a reviewed-empty stream are different claims",
            )
        # Read ONCE. Everything below — the parse, the cross-checks, the digest and the
        # bytes that get published — is this snapshot, so a concurrent capture writing the
        # store between the parse and the copy cannot produce a delivery whose manifest
        # describes bytes it does not contain (Astra, H2: a bar removed in that gap made
        # assemble report success and `data verify` refuse a bar_count mismatch).
        raw = bars_src.read_bytes()
        actions_raw = actions_src.read_bytes()
        try:
            loaded = load_daily_csv_bytes(raw, path=bars_src, symbol=symbol, source="history")
        except Exception as error:
            raise _refuse(bars_src, f"the verifier would refuse these bars ({error})") from error
        series = loaded.series
        if not series.bars:
            raise _refuse(bars_src, f"{symbol}: no bars in the file")
        if loaded.has_adjusted_close:
            raise _refuse(
                bars_src,
                f"{symbol}: carries an adjusted-close column; the delivery must be "
                "unadjusted as traded",
            )
        entry = entries.get(symbol) or {}
        bars_meta = entry.get("bars") if isinstance(entry, dict) else None
        if isinstance(bars_meta, dict) and bars_meta.get("adjusted"):
            raise _refuse(manifest_path, f"{symbol}: MANIFEST records adjusted bars")
        first, last = series.bars[0].session_date, series.bars[-1].session_date
        actions = _parse_actions(actions_raw, actions_src)
        if not actions:
            empty_action_symbols.append(symbol)
        _cross_check_manifest(
            manifest_path,
            symbol,
            entry,
            bar_count=len(series.bars),
            bars_sha256=hashlib.sha256(raw).hexdigest(),
            first=first,
            last=last,
            action_count=len(actions),
            actions_sha256=hashlib.sha256(actions_raw).hexdigest(),
        )
        read[symbol] = {
            "bars_bytes": raw,
            "actions_bytes": actions_raw,
            "bar_count": len(series.bars),
            "action_count": len(actions),
            "window": (first, last),
        }

    holdout_map = _holdout_map(store, {s: read[s]["window"] for s in CAMPAIGN_SYMBOLS})

    # ── pass 2: write. Everything above has already agreed. ──
    if out.exists() and any(out.iterdir()):
        raise _refuse(out, "delivery target is not empty; assemble writes a fresh directory")
    (out / "bars").mkdir(parents=True, exist_ok=True)
    (out / "corporate_actions").mkdir(parents=True, exist_ok=True)

    symbols_block: dict[str, Any] = {}
    total_rows = total_actions = 0
    for symbol in CAMPAIGN_SYMBOLS:
        item = read[symbol]
        bars_dst = out / "bars" / f"{symbol}.csv"
        actions_dst = out / "corporate_actions" / f"{symbol}.json"
        bars_bytes: bytes = item["bars_bytes"]
        actions_bytes: bytes = item["actions_bytes"]
        # The validated snapshot is what gets published, and the digest is taken from the
        # same object rather than by re-reading the file just written. Re-reading would bind
        # the manifest to whatever is on disk at that instant, which is exactly the byte set
        # nothing has checked.
        bars_dst.write_bytes(bars_bytes)
        actions_dst.write_bytes(actions_bytes)
        first, last = item["window"]
        symbols_block[symbol] = {
            "window": {"start": first.isoformat(), "end": last.isoformat()},
            "bars_sha256": hashlib.sha256(bars_bytes).hexdigest(),
            "bar_count": item["bar_count"],
            "corporate_actions_sha256": hashlib.sha256(actions_bytes).hexdigest(),
            "corporate_action_count": item["action_count"],
        }
        total_rows += item["bar_count"]
        total_actions += item["action_count"]

    intake = {
        "schema_version": INTAKE_SCHEMA_VERSION,
        "delivery_id": delivery_id,
        "supersedes": supersedes,
        "interval": INTERVAL,
        "adjustment_policy": ADJUSTMENT_POLICY,
        "provenance": provenance_block,
        "symbols": symbols_block,
        "corporate_action_attestation": attestation,
        "classified_moves": classified_moves,
        "holdout_map": holdout_map,
    }
    (out / "INTAKE.json").write_text(
        json.dumps(intake, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return AssembleResult(
        delivery=out,
        symbols=CAMPAIGN_SYMBOLS,
        bar_rows=total_rows,
        action_count=total_actions,
        holdout_spans=len(holdout_map),
        owner_declared_no_actions=tuple(empty_action_symbols),
    )


def _parse_actions(payload: bytes, path: Path) -> tuple[CorporateAction, ...]:
    """Parse exactly as the verifier does, so a refusal names the store file that has it.

    Takes the snapshot rather than the path: the parse and the published bytes must be the
    same bytes (H2), and a second read here would reopen that gap on the action stream.
    """

    try:
        raw = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise _refuse(path, f"unreadable JSON ({error})") from error
    if not isinstance(raw, list):
        raise _refuse(path, "corporate actions file is not a JSON array")
    parsed: list[CorporateAction] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise _refuse(path, f"corporate action at index {index} is not an object")
        try:
            parsed.append(CorporateAction.from_mapping(item))
        except (KeyError, TypeError, ValueError) as error:
            raise _refuse(path, f"the verifier would refuse action {index} ({error})") from error
    return tuple(parsed)


def _cross_check_manifest(
    manifest_path: Path,
    symbol: str,
    entry: Any,
    *,
    bar_count: int,
    bars_sha256: str,
    first: date,
    last: date,
    action_count: int,
    actions_sha256: str,
) -> None:
    """Every witness the store recorded about these bytes must agree with the bytes.

    The delivery's own fields are still derived from the snapshot, never copied from here
    (§2 wants independent claims). This is the separate question Astra asked in H3: the store
    ALSO recorded rows, counts and digests, and the previous version compared only the
    window, so a manifest claiming 999999 rows and a zeroed sha256 over untouched data files
    assembled and certified. A store that disagrees with itself is not a store to build a
    delivery from, and the refusal names the symbol and the field rather than the
    disagreement in aggregate.
    """

    if not isinstance(entry, dict):
        return
    bars_meta = entry.get("bars")
    if isinstance(bars_meta, dict):
        _witness(manifest_path, symbol, "bars.start", bars_meta.get("start"), first.isoformat())
        _witness(manifest_path, symbol, "bars.end", bars_meta.get("end"), last.isoformat())
        _witness(manifest_path, symbol, "bars.rows", bars_meta.get("rows"), bar_count)
        _witness(manifest_path, symbol, "bars.sha256", bars_meta.get("sha256"), bars_sha256)
    actions_meta = entry.get("corporate_actions")
    if isinstance(actions_meta, dict):
        _witness(
            manifest_path,
            symbol,
            "corporate_actions.count",
            actions_meta.get("count"),
            action_count,
        )
        _witness(
            manifest_path,
            symbol,
            "corporate_actions.sha256",
            actions_meta.get("sha256"),
            actions_sha256,
        )


def _witness(manifest_path: Path, symbol: str, field: str, declared: Any, derived: Any) -> None:
    """Compare one manifest witness with the value derived from the bytes.

    A witness the store did not record is not a disagreement — the capture is allowed to omit
    fields, and inventing a refusal for silence would refuse stores that are merely terse.
    """

    if declared is None or declared == "":
        return
    if isinstance(declared, bool):
        pass  # a bool is an int in Python; treat it as the type mismatch it is
    elif isinstance(derived, int) and isinstance(declared, int):
        if declared == derived:
            return
    elif str(declared) == str(derived):
        return
    raise _refuse(
        manifest_path,
        f"{symbol}: MANIFEST records {field} {declared!r} but the store's bytes give "
        f"{derived!r}; the store's own record disagrees with its bytes",
    )


def _require_disjoint(store: Path, out: Path) -> None:
    """The delivery must not land inside the store it was assembled from.

    "Read-only on the store" is a claim about the whole command, not only about the copies:
    with `--out <store>/delivery` the previous version created thirteen files beneath its own
    input and then certified the result (Astra, H1). Paths are resolved first, so a symlink
    into the store is refused on the same footing as a literal nested path.
    """

    store_real = store.resolve()
    out_real = out.resolve()
    if out_real == store_real or out_real.is_relative_to(store_real):
        raise _refuse(
            out,
            f"the delivery target resolves inside the store ({out_real} within {store_real}); "
            "assembling would write into the bytes being assembled. Choose an --out beside "
            "the store, not under it",
        )


__all__ = [
    "ADJUSTMENT_POLICY",
    "INTERVAL",
    "AssembleRefusal",
    "AssembleResult",
    "assemble_delivery",
]
