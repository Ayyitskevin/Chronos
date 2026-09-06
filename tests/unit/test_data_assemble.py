"""`data assemble` — the store becomes a delivery, or it refuses and says why (lane A).

The acceptance question is not "does it write files" but **"does `data verify` report the
assembled directory exactly as it reports a hand-built one"**. That is the first test, and it
compares the certification digest rather than the verdict word, because two deliveries can
both be CERTIFIED while attesting to different bytes.

The rest of the file is the refusal surface. Every field this command will not invent —
provenance, the attestation, a symbol's corporate-action stream — has a test that it
refuses and names the field, because a default in any of them is how a certification digest
comes to attest to something nobody asserted.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import date, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any

import pytest

from chronos.cli.main import main
from chronos.research import data_assemble
from chronos.research.certification import ProviderPriceBasis
from chronos.research.data_assemble import AssembleRefusal, assemble_delivery
from chronos.research.data_intake import CAMPAIGN_SYMBOLS, verify_intake
from chronos.research.session_calendar import SessionCalendar
from chronos.research.synth_store import generate_store

# A short window: the generator is deterministic at any span, and a three-month
# store keeps this suite fast while still carrying a split, dividends and a
# declared holdout window.
START = date(2024, 1, 2)
END = date(2024, 3, 28)

PROVENANCE = {
    "source_id": "synthetic-fixture-not-owner-data",
    "source_receipt_sha256": "a" * 64,
    "retrieved_at": "2026-09-06T00:00:00Z",
    "retrieval_method": "synthetic generator (no network)",
    "license_note": "generated in-repo for tests; not redistributable market data",
}


def write_store(root: Path, *, seed: int = 7, start: date = START, end: date = END) -> Path:
    """Lane B1's generator, used rather than imitated.

    A second store-shaped builder here would be a second definition of the store's format,
    and the first thing to rot when the store changes. `generate_store` writes through
    `histdata.store`, so these tests exercise the same bytes Kevin's rehearsal will.
    """

    generate_store(root, seed=seed, start=start, end=end)
    return root


def set_actions(store: Path, symbol: str, payload: str) -> Path:
    """Rewrite a symbol's action stream AND the manifest witnesses that describe it.

    A store whose manifest still records the old digest is a store that disagrees with
    itself, which assemble now refuses (H3) — so a fixture that edits one without the other
    is testing the refusal, not the case it meant to test.
    """

    path = store / "corporate_actions" / f"{symbol}.json"
    path.write_text(payload, encoding="utf-8")
    manifest = json.loads((store / "MANIFEST.json").read_text())
    entry = manifest["symbols"][symbol]["corporate_actions"]
    entry["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    entry["count"] = len(json.loads(payload))
    (store / "MANIFEST.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return path


def _sessions() -> list[date]:
    return list(SessionCalendar().sessions(START, END))


def _sampled_attestation(tmp_path: Path) -> Path:
    path = tmp_path / "sampled-attestation.json"
    path.write_text(
        json.dumps(
            {
                "kind": "sampled_actions",
                "source_id": "independent-synthetic-reviewer",
                "sampled_action_count": 3,
                "symbols": list(CAMPAIGN_SYMBOLS),
                "note": "fixture: sampled against an independent synthetic source",
            }
        )
    )
    return path


def _assemble(tmp_path: Path, **overrides: Any) -> Any:
    store = overrides.pop("store", None) or write_store(tmp_path / "store")
    out = overrides.pop("out", tmp_path / "delivery")
    kwargs: dict[str, Any] = {
        "delivery_id": "fixture-2026Q3-sixsym-daily",
        "provenance": dict(PROVENANCE),
        "provider_price_basis": "unadjusted_as_traded",
        "attestation_path": _sampled_attestation(tmp_path),
        "classified_moves_path": None,
        "supersedes": None,
    }
    kwargs.update(overrides)
    return assemble_delivery(store, out, **kwargs)


def test_the_verdict_does_not_depend_on_how_intake_json_is_spelled(
    tmp_path: Path,
) -> None:
    """Serialisation invariance ONLY — it re-reads the assembler's own output.

    Named for what it proves after Astra's E2: `copytree` + re-serialise cannot be an
    independent equivalent, because a derivation mistake would be copied into both sides.
    The independence test is the next one.
    """

    result = _assemble(tmp_path)
    assembled = verify_intake(result.delivery)

    hand_built = tmp_path / "hand-built"
    shutil.copytree(result.delivery, hand_built)
    # Re-serialise INTAKE.json the way a person would type it: different key order and
    # indentation, identical content. The verdict must not depend on the spelling.
    document = json.loads((hand_built / "INTAKE.json").read_text())
    (hand_built / "INTAKE.json").write_text(json.dumps(document, indent=4, sort_keys=False))
    manual = verify_intake(hand_built)

    assert assembled.certified is True, [f.kind for f in assembled.findings]
    assert manual.certified == assembled.certified
    assert manual.certification_digest == assembled.certification_digest
    assert [f.kind for f in manual.findings] == [f.kind for f in assembled.findings]


def _independent_intake(store: Path, attestation: Path) -> dict[str, Any]:
    """Build the delivery document from the SOURCES, without reading assembler output.

    Deliberately not a call into `data_assemble`: the CSV is split on commas here and the
    counts come from line arithmetic, so a mistake in the module's derivation cannot be
    reproduced by the thing checking it (Astra, E2).
    """

    symbols: dict[str, Any] = {}
    for symbol in sorted(CAMPAIGN_SYMBOLS):
        bars = (store / "bars" / f"{symbol}.csv").read_bytes()
        actions = (store / "corporate_actions" / f"{symbol}.json").read_bytes()
        rows = [line for line in bars.decode().splitlines() if line][1:]
        symbols[symbol] = {
            "window": {
                "start": rows[0].split(",")[0],
                "end": rows[-1].split(",")[0],
            },
            "bars_sha256": hashlib.sha256(bars).hexdigest(),
            "bar_count": len(rows),
            "corporate_actions_sha256": hashlib.sha256(actions).hexdigest(),
            "corporate_action_count": len(json.loads(actions)),
            # derived here from the raw JSON and string date comparison, not from ActionKind
            # or the module's parser, so a mistake in the assembler's derivation cannot be
            # reproduced by the thing checking it
            "no_split_in_window": not [
                action
                for action in json.loads(actions)
                if action["kind"] == "split"
                and rows[0].split(",")[0] <= action["ex_date"] <= rows[-1].split(",")[0]
            ],
        }

    window = json.loads((store / "HOLDOUTS.json").read_text())["windows"][0]
    holdout_map = []
    for symbol in sorted(CAMPAIGN_SYMBOLS):
        supplied = symbols[symbol]["window"]
        lo = date.fromisoformat(window["start"])
        holdout_map.append(
            {
                "symbol": symbol,
                "name": f"{symbol.lower()}-seen-{supplied['start']}",
                "start": supplied["start"],
                "end": (lo - timedelta(days=1)).isoformat(),
                "status": "seen",
            }
        )
        holdout_map.append(
            {
                "symbol": symbol,
                "name": f"{symbol.lower()}-clean-{window['start']}",
                "start": window["start"],
                "end": window["end"],
                "status": "clean",
                "reason": window["reason"],
            }
        )

    return {
        "schema_version": 2,
        "delivery_id": "fixture-2026Q3-sixsym-daily",
        "supersedes": None,
        "interval": "1d",
        "adjustment_policy": "unadjusted_as_traded",
        "provider_price_basis": "unadjusted_as_traded",
        "provenance": dict(PROVENANCE),
        "symbols": symbols,
        "corporate_action_attestation": json.loads(attestation.read_text()),
        "classified_moves": [],
        "holdout_map": holdout_map,
    }


def test_the_delivery_matches_an_equivalent_built_independently_of_the_assembler(
    tmp_path: Path,
) -> None:
    """The acceptance criterion, rebuilt from the sources rather than copied from the output.

    Compared whole: every per-symbol window, count and content digest, the holdout tiling,
    and the document itself. Content digests, not the certification digest — that one is a
    report digest, and two deliveries with equal report digests can still hold different
    bar bytes (#193).
    """

    store = write_store(tmp_path / "store")
    attestation = _sampled_attestation(tmp_path)
    result = _assemble(tmp_path, store=store, attestation_path=attestation)
    document = json.loads((result.delivery / "INTAKE.json").read_text())
    expected = _independent_intake(store, attestation)

    for symbol in CAMPAIGN_SYMBOLS:
        assert document["symbols"][symbol] == expected["symbols"][symbol], symbol
    assert document["holdout_map"] == expected["holdout_map"]
    assert document == expected

    # and the delivered bytes are the source bytes, not merely equally-digested ones
    for symbol in CAMPAIGN_SYMBOLS:
        assert (result.delivery / "bars" / f"{symbol}.csv").read_bytes() == (
            store / "bars" / f"{symbol}.csv"
        ).read_bytes()


def test_the_store_is_not_written_to(tmp_path: Path) -> None:
    store = write_store(tmp_path / "store")
    before = {
        p.relative_to(store): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(store.rglob("*"))
        if p.is_file()
    }
    _assemble(tmp_path, store=store)
    after = {
        p.relative_to(store): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(store.rglob("*"))
        if p.is_file()
    }
    assert after == before, "assemble wrote to the capture store"


@pytest.mark.parametrize("dropped", ["DIA", "QQQ"])
def test_a_partial_symbol_set_is_refused_naming_the_symbol(tmp_path: Path, dropped: str) -> None:
    """A five-symbol store is not a partial pass — the verifier refuses it and so does this."""

    store = write_store(tmp_path / "store")
    document = json.loads((store / "MANIFEST.json").read_text())
    del document["symbols"][dropped]
    (store / "MANIFEST.json").write_text(json.dumps(document, indent=2))
    with pytest.raises(AssembleRefusal) as caught:
        _assemble(tmp_path, store=store)
    assert dropped in caught.value.reason
    assert "missing required symbol" in caught.value.reason


def test_an_extra_symbol_is_refused(tmp_path: Path) -> None:
    store = write_store(tmp_path / "store")
    document = json.loads((store / "MANIFEST.json").read_text())
    document["symbols"]["VTI"] = document["symbols"]["SPY"]
    (store / "MANIFEST.json").write_text(json.dumps(document, indent=2))
    with pytest.raises(AssembleRefusal) as caught:
        _assemble(tmp_path, store=store)
    assert "VTI" in caught.value.reason and "outside the delivery contract" in caught.value.reason


@pytest.mark.parametrize("field", sorted(PROVENANCE))
def test_a_missing_provenance_flag_is_refused_naming_the_field(tmp_path: Path, field: str) -> None:
    """Provenance is asserted, never derived; the refusal has to say which flag is absent."""

    provenance = dict(PROVENANCE)
    provenance[field] = ""
    with pytest.raises(AssembleRefusal) as caught:
        _assemble(tmp_path, provenance=provenance)
    assert field.replace("_", "-") in caught.value.reason
    assert "cannot be derived" in caught.value.reason


def test_a_missing_attestation_is_refused(tmp_path: Path) -> None:
    with pytest.raises(AssembleRefusal) as caught:
        _assemble(tmp_path, attestation_path=None)
    assert "corporate_action_attestation" in caught.value.reason
    assert "code cannot do this half" in caught.value.reason


def test_a_byte_changed_in_the_bars_after_assembly_is_caught_at_verify(tmp_path: Path) -> None:
    """The digest in INTAKE.json is the delivery's own claim about its bytes."""

    result = _assemble(tmp_path)
    assert verify_intake(result.delivery).certified is True

    bars = result.delivery / "bars" / "SPY.csv"
    before = bars.read_bytes()
    rows = bars.read_text().splitlines()
    fields = rows[1].split(",")
    fields[-1] = str(float(fields[-1]) + 1)  # one volume digit, by position, not by literal
    rows[1] = ",".join(fields)
    bars.write_text("\n".join(rows) + "\n")
    assert bars.read_bytes() != before, "the mutation itself must change the bytes"

    from chronos.research.data_intake import IntakeUnverified

    with pytest.raises(IntakeUnverified) as caught:
        verify_intake(result.delivery)
    assert "bars_sha256 mismatch" in caught.value.reason


def test_a_timestamped_date_is_refused_against_the_store_file(tmp_path: Path) -> None:
    store = write_store(tmp_path / "store")
    bars = store / "bars" / "GLD.csv"
    rows = bars.read_text().splitlines()
    rows[1] = rows[1].replace(rows[1].split(",")[0], rows[1].split(",")[0] + "T00:00:00", 1)
    bars.write_text("\n".join(rows) + "\n")
    with pytest.raises(AssembleRefusal) as caught:
        _assemble(tmp_path, store=store)
    assert caught.value.path == bars, "the refusal must name the store file, not the delivery"
    assert "the verifier would refuse" in caught.value.reason


def test_the_manifest_and_the_bytes_must_agree_on_the_window(tmp_path: Path) -> None:
    store = write_store(tmp_path / "store")
    document = json.loads((store / "MANIFEST.json").read_text())
    document["symbols"]["TLT"]["bars"]["end"] = "2030-01-01"
    (store / "MANIFEST.json").write_text(json.dumps(document, indent=2))
    with pytest.raises(AssembleRefusal) as caught:
        _assemble(tmp_path, store=store)
    assert "disagrees with its bytes" in caught.value.reason


def test_classified_moves_default_to_empty_and_load_from_a_file(tmp_path: Path) -> None:
    result = _assemble(tmp_path)
    assert json.loads((result.delivery / "INTAKE.json").read_text())["classified_moves"] == []

    moves = tmp_path / "moves.json"
    moves.write_text(
        json.dumps([{"symbol": "SPY", "session_date": "2024-02-01", "reason": "fixture move"}])
    )
    second = _assemble(tmp_path, out=tmp_path / "delivery-2", classified_moves_path=moves)
    document = json.loads((second.delivery / "INTAKE.json").read_text())
    assert document["classified_moves"][0]["reason"] == "fixture move"


def test_supersedes_is_null_unless_supplied(tmp_path: Path) -> None:
    result = _assemble(tmp_path)
    assert json.loads((result.delivery / "INTAKE.json").read_text())["supersedes"] is None
    second = _assemble(tmp_path, out=tmp_path / "delivery-2", supersedes="b" * 64)
    assert json.loads((second.delivery / "INTAKE.json").read_text())["supersedes"] == "b" * 64


def test_the_holdout_map_tiles_every_supplied_session_exactly_once(tmp_path: Path) -> None:
    """The store's windows schema has no status; the delivery's tiling needs one per span.

    Derived, not demanded: a declared holdout window is `clean` (that is what declaring one
    says), and the remainder of the supplied range is `seen`, because it has been available
    to research all along. `seen` under-claims and `clean` over-claims, and only one of
    those errors is recoverable.
    """

    store = write_store(tmp_path / "store")
    store_windows = json.loads((store / "HOLDOUTS.json").read_text())["windows"]
    result = _assemble(tmp_path, store=store)
    document = json.loads((result.delivery / "INTAKE.json").read_text())
    spans = document["holdout_map"]

    for symbol in CAMPAIGN_SYMBOLS:
        window = document["symbols"][symbol]["window"]
        mine = sorted(
            (date.fromisoformat(s["start"]), date.fromisoformat(s["end"]), s["status"])
            for s in spans
            if s["symbol"] == symbol
        )
        assert mine, symbol
        assert mine[0][0].isoformat() == window["start"], f"{symbol}: tiling starts late"
        assert mine[-1][1].isoformat() == window["end"], f"{symbol}: tiling ends early"
        for (_, previous_end, _s), (next_start, _e, _t) in pairwise(mine):
            assert next_start == previous_end + timedelta(days=1), (
                f"{symbol}: spans are not contiguous across {previous_end}"
            )
        # The store declares exactly one window; it must be the ONLY clean span, at its own
        # bounds, and everything the owner did not reserve must read `seen`.
        declared = [w for w in store_windows if symbol in w["symbols"]]
        assert len(declared) == 1, symbol
        clean = [(a, b) for a, b, status in mine if status == "clean"]
        assert clean == [
            (date.fromisoformat(declared[0]["start"]), date.fromisoformat(declared[0]["end"]))
        ], f"{symbol}: the clean span does not match the declared window"
        assert {status for *_, status in mine} == {"clean", "seen"}, (
            f"{symbol}: the remainder of the supplied range must read `seen` — calling it "
            "`clean` would claim an untouched reserve that does not exist"
        )


def test_a_mid_range_holdout_is_tiled_seen_clean_seen(tmp_path: Path) -> None:
    """The generator reserves the tail, which never exercises the remainder AFTER a window.

    A window an owner declared in the middle of the range must produce three spans, and the
    sessions on both sides of it must read `seen` — the trailing side is a separate branch
    from the leading one and it was untested until a mutation of it changed no test.
    """

    store = write_store(tmp_path / "store")
    document = json.loads((store / "HOLDOUTS.json").read_text())
    document["windows"] = [
        {
            "name": "mid-range-reserve",
            "start": "2024-02-05",
            "end": "2024-02-16",
            "symbols": list(CAMPAIGN_SYMBOLS),
            "reason": "fixture: reserved mid-range, so both remainders exist",
        }
    ]
    (store / "HOLDOUTS.json").write_text(json.dumps(document))

    result = _assemble(tmp_path, store=store)
    spans = json.loads((result.delivery / "INTAKE.json").read_text())["holdout_map"]
    mine = [(s["start"], s["end"], s["status"]) for s in spans if s["symbol"] == "SPY"]
    # Spans are calendar-day ranges, so the boundaries are the declared window's own
    # neighbours rather than the neighbouring sessions: contiguity on dates is what makes
    # "every supplied session is claimed exactly once" true without consulting the calendar.
    assert mine == [
        (START.isoformat(), "2024-02-04", "seen"),
        ("2024-02-05", "2024-02-16", "clean"),
        ("2024-02-17", END.isoformat(), "seen"),
    ]


def test_burned_is_never_derived(tmp_path: Path) -> None:
    """It means a reserve was consumed, and §2 wants a reason. Code cannot supply one."""

    result = _assemble(tmp_path)
    spans = json.loads((result.delivery / "INTAKE.json").read_text())["holdout_map"]
    assert not [s for s in spans if s["status"] == "burned"]


def test_a_missing_action_file_is_refused_naming_the_symbol(tmp_path: Path) -> None:
    """Astra's lane-C trap: store.read_actions() returns () for a MISSING file.

    The capture writes bars and manifest only, so this is the common case rather than an
    edge case — and an absent stream must never become an authored empty one.
    """

    store = write_store(tmp_path / "store")
    (store / "corporate_actions" / "DIA.json").unlink()
    with pytest.raises(AssembleRefusal) as caught:
        _assemble(tmp_path, store=store)
    assert "DIA" in caught.value.reason
    assert "will not create an empty file" in caught.value.reason
    assert not (tmp_path / "delivery").exists(), "a refusal must not leave a half-built delivery"


def test_an_empty_action_file_assembles_and_is_reported_never_silently(tmp_path: Path) -> None:
    """An empty array is an owner statement — reviewed, no actions — so it assembles.

    But it is indistinguishable downstream from an absent stream, so the result names the
    symbols it applies to rather than passing over them.
    """

    store = write_store(tmp_path / "store")
    set_actions(store, "TLT", "[]\n")
    result = _assemble(tmp_path, store=store)
    assert "TLT" in result.owner_declared_no_actions
    assert (
        json.loads((result.delivery / "INTAKE.json").read_text())["symbols"]["TLT"][
            "corporate_action_count"
        ]
        == 0
    )


def _cli_argv(store: Path, out: Path, attestation: Path) -> list[str]:
    return [
        "data",
        "assemble",
        "--store",
        str(store),
        "--out",
        str(out),
        "--delivery-id",
        "fixture-2026Q3-sixsym-daily",
        "--provider-price-basis",
        "unadjusted_as_traded",
        "--attestation",
        str(attestation),
        "--source-id",
        PROVENANCE["source_id"],
        "--source-receipt-sha256",
        PROVENANCE["source_receipt_sha256"],
        "--retrieved-at",
        PROVENANCE["retrieved_at"],
        "--retrieval-method",
        PROVENANCE["retrieval_method"],
        "--license-note",
        PROVENANCE["license_note"],
    ]


def test_the_cli_names_owner_declared_empty_streams_on_the_one_success_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`never silently` has to hold at the surface an operator actually reads."""

    store = write_store(tmp_path / "store")
    set_actions(store, "TLT", "[]\n")
    code = main(_cli_argv(store, tmp_path / "delivery", _sampled_attestation(tmp_path)))

    output = capsys.readouterr().out
    assert code == 0
    assert output.count("\n") == 1, "the data commands print exactly one line"
    assert output.startswith("ASSEMBLED ")
    declared = output.split("owner-declared-no-actions: ", 1)[1].split(";", 1)[0]
    assert "TLT" in declared.split(", "), output


def test_the_cli_refuses_a_missing_action_file_with_exit_2_and_the_symbol(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = write_store(tmp_path / "store")
    (store / "corporate_actions" / "DIA.json").unlink()
    code = main(_cli_argv(store, tmp_path / "delivery", _sampled_attestation(tmp_path)))

    output = capsys.readouterr().out
    assert code == 2
    assert output.startswith("REFUSED ")
    assert "DIA" in output


def _tree(root: Path) -> dict[str, str]:
    """Every entry under root, not only the files that were there before.

    The previous snapshot compared a dict built from the files it found on BOTH sides, so a
    delivery written inside the store added thirteen entries and compared equal (Astra, H1).
    """

    out: dict[str, str] = {}
    for item in sorted(root.rglob("*")):
        key = str(item.relative_to(root))
        out[key] = (
            hashlib.sha256(item.read_bytes()).hexdigest() if item.is_file() else "<directory>"
        )
    return out


@pytest.mark.parametrize("nested", ["delivery", "bars/delivery", "."])
def test_an_out_inside_the_store_is_refused_before_anything_is_written(
    tmp_path: Path, nested: str
) -> None:
    """ "Read-only on the store" has to survive the placement that contradicts it."""

    store = write_store(tmp_path / "store")
    before = _tree(store)
    with pytest.raises(AssembleRefusal) as caught:
        _assemble(tmp_path, store=store, out=store / nested)
    assert "resolves inside the store" in caught.value.reason
    assert _tree(store) == before, "a refused assembly still changed the store"


def test_an_out_reaching_the_store_through_a_symlink_is_refused(tmp_path: Path) -> None:
    """Resolved, not compared as text — an alias is the same write by another name."""

    store = write_store(tmp_path / "store")
    alias = tmp_path / "alias"
    alias.symlink_to(store, target_is_directory=True)
    before = _tree(store)
    with pytest.raises(AssembleRefusal) as caught:
        _assemble(tmp_path, store=store, out=alias / "delivery")
    assert "resolves inside the store" in caught.value.reason
    assert _tree(store) == before


def test_sources_changed_between_the_parse_and_the_copy_cannot_reach_the_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The delivery is the bytes that were validated, not whatever the files hold later.

    Astra's H2 probe removed a final bar from DIA between the parse and the copy: assemble
    reported success and `data verify` then refused a `bar_count mismatch` against a count
    assemble had itself measured. The write now publishes the in-memory snapshot, so the
    same interference leaves a delivery that is self-consistent and certifies.

    BOTH of DIA's sources are changed, not only the bars (Astra's re-review): the action
    stream is a separate read and a separate write, so a guard that moves only the bars
    would keep passing if the actions path regressed to a second read. The two are asserted
    independently, because a delivery can be self-consistent on one and stale on the other.
    """

    store = write_store(tmp_path / "store")
    bars_source = store / "bars" / "DIA.csv"
    actions_source = store / "corporate_actions" / "DIA.json"
    validated_bars = bars_source.read_bytes()
    validated_actions = actions_source.read_bytes()

    # `_holdout_map` runs after every file is parsed and before anything is written — the
    # exact gap another writer would land in.
    original = data_assemble._holdout_map

    def interfere(*args: Any, **kwargs: Any) -> Any:
        rows = bars_source.read_text().splitlines()
        bars_source.write_text("\n".join(rows[:-1]) + "\n")  # a concurrent capture, mid-assembly
        actions = json.loads(validated_actions)
        actions.append(
            {
                "symbol": "DIA",
                "kind": "cash_dividend",
                "ex_date": "2024-06-03",
                "value": 1.25,
                "source": "synthetic",
                "note": "written by another writer, after this file was validated",
            }
        )
        actions_source.write_text(json.dumps(actions))
        return original(*args, **kwargs)

    monkeypatch.setattr(data_assemble, "_holdout_map", interfere)
    result = _assemble(tmp_path, store=store)

    assert bars_source.read_bytes() != validated_bars, "the bars interference must have happened"
    assert actions_source.read_bytes() != validated_actions, (
        "the actions interference must have happened"
    )
    assert (result.delivery / "bars" / "DIA.csv").read_bytes() == validated_bars, (
        "the delivery published bar bytes nothing had validated"
    )
    assert (result.delivery / "corporate_actions" / "DIA.json").read_bytes() == (
        validated_actions
    ), "the delivery published action bytes nothing had validated"
    report = verify_intake(result.delivery)
    assert report.certified is True, [f.kind for f in report.findings]


@pytest.mark.parametrize(
    ("block", "field", "value"),
    [
        ("bars", "rows", 999999),
        ("bars", "sha256", "0" * 64),
        ("bars", "start", "2020-01-02"),
        ("bars", "end", "2030-01-02"),
        ("corporate_actions", "count", 999999),
        ("corporate_actions", "sha256", "0" * 64),
    ],
)
def test_a_manifest_witness_disagreeing_with_the_bytes_is_refused(
    tmp_path: Path, block: str, field: str, value: Any
) -> None:
    """The store's own record of these bytes is checked against the bytes (Astra, H3).

    The delivery's fields are still derived from the snapshot — this is the separate
    question of whether the store agrees with itself, and a store that does not is not one
    to build a delivery from.
    """

    store = write_store(tmp_path / "store")
    manifest = json.loads((store / "MANIFEST.json").read_text())
    manifest["symbols"]["DIA"][block][field] = value
    (store / "MANIFEST.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))

    with pytest.raises(AssembleRefusal) as caught:
        _assemble(tmp_path, store=store)
    assert "DIA" in caught.value.reason
    assert f"{block}.{field}" in caught.value.reason
    assert not (tmp_path / "delivery").exists()


def test_an_attestation_file_holding_json_null_is_refused_naming_the_flag(
    tmp_path: Path,
) -> None:
    """A supplied filename is not yet a supplied §4 attestation (Astra, A1)."""

    path = tmp_path / "null-attestation.json"
    path.write_text("null")
    with pytest.raises(AssembleRefusal) as caught:
        _assemble(tmp_path, attestation_path=path)
    assert "corporate_action_attestation must be a JSON object, got null" in caught.value.reason
    assert "--attestation" in caught.value.reason


def test_the_missing_attestation_refusal_names_the_flag_that_supplies_it(
    tmp_path: Path,
) -> None:
    with pytest.raises(AssembleRefusal) as caught:
        _assemble(tmp_path, attestation_path=None)
    assert "--attestation <path>" in caught.value.reason
    assert "--corporate-action-attestation" not in caught.value.reason


def test_a_classified_moves_file_that_is_not_an_array_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "classified.json"
    path.write_text(json.dumps({"QQQ": "not a list"}))
    with pytest.raises(AssembleRefusal) as caught:
        _assemble(tmp_path, classified_moves_path=path)
    assert "classified_moves must be a JSON array, got dict" in caught.value.reason
    assert "--classified-moves" in caught.value.reason


def test_a_missing_provider_price_basis_is_refused_naming_the_flag(tmp_path: Path) -> None:
    """Schema 2's one field this command cannot derive (ADR-0059).

    The store records `adjusted: false`, but that is the capture's claim about the bytes, not
    the vendor's account of how they were produced — which is the distinction the ADR exists
    to keep. A default would put the assumption back.
    """

    with pytest.raises(AssembleRefusal) as caught:
        _assemble(tmp_path, provider_price_basis=None)
    assert "provider_price_basis is absent" in caught.value.reason
    assert "--provider-price-basis" in caught.value.reason
    assert not (tmp_path / "delivery").exists()


def test_a_basis_outside_the_vocabulary_is_refused_with_the_allowed_list(tmp_path: Path) -> None:
    """The enum is imported from certification.py, so this list cannot drift from it."""

    with pytest.raises(AssembleRefusal) as caught:
        _assemble(tmp_path, provider_price_basis="raw")
    assert "'raw' is not one of" in caught.value.reason
    for member in ProviderPriceBasis:
        assert member.value in caught.value.reason


def test_an_inadmissible_basis_assembles_and_verify_is_the_one_that_refuses(
    tmp_path: Path,
) -> None:
    """assemble does not pre-judge admission — that reasoning belongs with the verdict.

    `ibkr_trades_split_adjusted` is IN the vocabulary and is refused by `data verify`, because
    a split after the delivered window rescales the series and no in-window check can see it.
    Duplicating that refusal here would be a second place for it to drift; assembling and
    letting the verifier speak keeps one authority for the contract.
    """

    result = _assemble(tmp_path, provider_price_basis="ibkr_trades_split_adjusted")
    document = json.loads((result.delivery / "INTAKE.json").read_text())
    assert document["provider_price_basis"] == "ibkr_trades_split_adjusted"

    from chronos.research.data_intake import IntakeUnverified

    with pytest.raises(IntakeUnverified) as caught:
        verify_intake(result.delivery)
    assert "ibkr_trades_split_adjusted cannot satisfy adjustment_policy" in caught.value.reason


def test_no_split_in_window_is_derived_per_symbol_from_the_shipped_actions(
    tmp_path: Path,
) -> None:
    """True iff no split ex-date falls inside that symbol's delivered window.

    The generator splits QQQ inside the range and leaves the other five alone, so the derived
    value is not constant across symbols — a constant would pass a check that only compared
    it with itself.
    """

    store = write_store(tmp_path / "store", seed=7, start=date(2024, 1, 2), end=date(2024, 12, 31))
    result = _assemble(tmp_path, store=store)
    document = json.loads((result.delivery / "INTAKE.json").read_text())
    derived = {
        symbol: document["symbols"][symbol]["no_split_in_window"] for symbol in CAMPAIGN_SYMBOLS
    }
    assert derived["QQQ"] is False, "QQQ splits inside this range"
    assert all(value is True for symbol, value in derived.items() if symbol != "QQQ"), derived
    assert verify_intake(result.delivery).certified is True


def test_the_declaration_follows_the_WINDOW_not_merely_the_action_file(tmp_path: Path) -> None:
    """The same symbol, the same split, moved outside the delivered window: True.

    A value derived from the action file alone would answer False here and contradict the
    delivery it describes. The pairing is the informative part: with the ex-date outside the
    window the declaration is honestly `true`, and the bars' unexplained discontinuity is
    then caught by certification as an UNCLASSIFIED_MATERIAL_MOVE — the declaration is
    evidence, never an acceptance path (§4).
    """

    store = write_store(tmp_path / "store")
    actions = json.loads((store / "corporate_actions" / "QQQ.json").read_text())
    moved = 0
    for action in actions:
        if action["kind"] == "split":
            action["ex_date"] = "2024-06-03"  # after this delivery's window
            moved += 1
    assert moved == 1, "the fixture must carry exactly one split to move"
    set_actions(store, "QQQ", json.dumps(actions))

    result = _assemble(tmp_path, store=store)
    document = json.loads((result.delivery / "INTAKE.json").read_text())
    assert document["symbols"]["QQQ"]["no_split_in_window"] is True

    report = verify_intake(result.delivery)
    assert report.certified is False
    assert [(f.kind.value, f.symbol) for f in report.findings] == [
        ("UNCLASSIFIED_MATERIAL_MOVE", "QQQ")
    ]
