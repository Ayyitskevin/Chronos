"""`data check` — the per-symbol gates over a partial capture, and never a verdict.

Two properties carry this file. The first is that the findings are the verifier's own: the
gates come from `certification.gate_symbol_bars`, the same function `certify_export` calls,
so a test that pins a finding code is pinning the verifier's code and not a copy. The
second is negative and is the reason the command exists at all — it must never produce
anything a reader could mistake for a certification, so there is a test that says so about
the output, the result object and the filesystem.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import date
from pathlib import Path

import pytest

from chronos.cli.main import main
from chronos.research.data_check import CheckRefusal, available_symbols, check_store
from chronos.research.synth_store import generate_store

START = date(2024, 1, 2)
END = date(2024, 3, 28)


def full_store(root: Path, *, seed: int = 7) -> Path:
    generate_store(root, seed=seed, start=START, end=END)
    return root


def one_symbol_store(root: Path, symbol: str = "DIA", *, with_actions: bool = False) -> Path:
    """A capture that holds ONE symbol — the shape of Kevin's first pull.

    Built by copying one symbol out of the generator's store rather than by hand, so the
    bytes under test are the same bytes the six-symbol path uses.
    """

    source = full_store(root / "source")
    store = root / "partial"
    (store / "bars").mkdir(parents=True)
    shutil.copyfile(source / "bars" / f"{symbol}.csv", store / "bars" / f"{symbol}.csv")
    if with_actions:
        (store / "corporate_actions").mkdir(parents=True)
        shutil.copyfile(
            source / "corporate_actions" / f"{symbol}.json",
            store / "corporate_actions" / f"{symbol}.json",
        )
    return store


def _tree(root: Path) -> dict[str, str]:
    return {
        str(item.relative_to(root)): (
            hashlib.sha256(item.read_bytes()).hexdigest() if item.is_file() else "<directory>"
        )
        for item in sorted(root.rglob("*"))
    }


def test_a_one_symbol_store_is_checked_and_reports_no_findings(tmp_path: Path) -> None:
    """The point of the lane: DIA alone can be judged, months before the other five exist."""

    store = one_symbol_store(tmp_path)
    result = check_store(store)

    assert [item.symbol for item in result.symbols] == ["DIA"]
    checked = result.symbols[0]
    assert checked.bar_count > 0
    assert checked.coverage == pytest.approx(1.0)
    assert result.finding_count == 0
    assert checked.action_count is None, "an absent action file is absent, not empty"


def test_the_exit_code_counts_findings_and_judges_nothing(tmp_path: Path) -> None:
    """0 and 1 are a finding count, not a verdict — and 2 is 'unreadable', as elsewhere."""

    store = one_symbol_store(tmp_path)
    assert main(["data", "check", "--store", str(store), "--symbol", "DIA"]) == 0

    rows = (store / "bars" / "DIA.csv").read_text().splitlines()
    del rows[20:26]  # six sessions the exchange held and this capture now lacks
    (store / "bars" / "DIA.csv").write_text("\n".join(rows) + "\n")
    assert main(["data", "check", "--store", str(store), "--symbol", "DIA"]) == 1

    assert main(["data", "check", "--store", str(tmp_path / "nowhere")]) == 2


def test_the_findings_are_the_verifiers_own_codes(tmp_path: Path) -> None:
    """A gap produces MISSING_SESSION, from `gate_symbol_bars`, not from a lookalike here."""

    store = one_symbol_store(tmp_path)
    rows = (store / "bars" / "DIA.csv").read_text().splitlines()
    dropped = rows[20].split(",")[0]
    del rows[20]
    (store / "bars" / "DIA.csv").write_text("\n".join(rows) + "\n")

    findings = check_store(store).symbols[0].findings
    # One session missing out of this fixture's 61 puts coverage at 0.9836, under the frozen
    # 0.995 floor — so the honest answer is both codes, and pinning only the first would be
    # pinning a tidier report than the verifier gives.
    assert [f.kind.value for f in findings] == ["MISSING_SESSION", "COVERAGE_BELOW_FLOOR"]
    assert findings[0].session_date is not None
    assert findings[0].session_date.isoformat() == dropped
    assert findings[0].symbol == "DIA"


def test_a_timestamped_date_cell_is_refused_naming_the_file(tmp_path: Path) -> None:
    """The verifier's own parser refuses it, so this refuses it against the store file.

    Reported as a refusal rather than a finding because there is no `FindingKind` for it —
    inventing one would widen the frozen vocabulary to make a dry run look tidier.
    """

    store = one_symbol_store(tmp_path)
    path = store / "bars" / "DIA.csv"
    rows = path.read_text().splitlines()
    fields = rows[1].split(",")
    fields[0] = f"{fields[0]}T00:00:00Z"
    rows[1] = ",".join(fields)
    path.write_text("\n".join(rows) + "\n")

    with pytest.raises(CheckRefusal) as caught:
        check_store(store)
    assert caught.value.path == path
    assert "the verifier would refuse these bars" in caught.value.reason


def test_an_adjusted_close_column_is_refused(tmp_path: Path) -> None:
    store = one_symbol_store(tmp_path)
    path = store / "bars" / "DIA.csv"
    rows = path.read_text().splitlines()
    rows[0] = rows[0] + ",adj_close"
    for index in range(1, len(rows)):
        rows[index] = rows[index] + "," + rows[index].split(",")[4]
    path.write_text("\n".join(rows) + "\n")

    with pytest.raises(CheckRefusal) as caught:
        check_store(store)
    assert "adjusted-close column" in caught.value.reason


def test_a_manifest_witness_disagreeing_with_the_bytes_is_refused(tmp_path: Path) -> None:
    """The same cross-check `data assemble` runs, on whatever subset the store holds."""

    store = full_store(tmp_path / "store")
    manifest = json.loads((store / "MANIFEST.json").read_text())
    manifest["symbols"]["DIA"]["bars"]["rows"] = 999999
    (store / "MANIFEST.json").write_text(json.dumps(manifest))

    with pytest.raises(CheckRefusal) as caught:
        check_store(store, ("DIA",))
    assert "DIA" in caught.value.reason
    assert "bars.rows" in caught.value.reason


def test_a_symbol_the_store_does_not_hold_is_refused_naming_what_it_holds(
    tmp_path: Path,
) -> None:
    store = one_symbol_store(tmp_path)
    with pytest.raises(CheckRefusal) as caught:
        check_store(store, ("SPY",))
    assert "no bars for SPY" in caught.value.reason
    assert "DIA" in caught.value.reason


def test_any_subset_is_checkable_and_the_six_symbol_identity_is_untouched(
    tmp_path: Path,
) -> None:
    """Two of six here; `data verify` still demands all six of a real delivery."""

    store = full_store(tmp_path / "store")
    result = check_store(store, ("DIA", "GLD"))
    assert [item.symbol for item in result.symbols] == ["DIA", "GLD"]

    from chronos.research.data_intake import CAMPAIGN_SYMBOLS

    assert CAMPAIGN_SYMBOLS == ("QQQ", "SPY", "IWM", "DIA", "GLD", "TLT")


def test_the_split_is_reconciled_against_the_symbols_own_action_file(tmp_path: Path) -> None:
    """With the action stream present the split reconciles; without it, it cannot.

    Both halves are asserted, because "no findings" means something different in each case
    and an operator who cannot tell them apart has been misled by a clean run.
    """

    with_actions = one_symbol_store(tmp_path / "a", "QQQ", with_actions=True)
    assert check_store(with_actions).finding_count == 0
    # QQQ's stream over this window: one split plus one quarterly dividend. Pinned as a
    # literal rather than read back off the file the command just read.
    assert check_store(with_actions).symbols[0].action_count == 2

    without = one_symbol_store(tmp_path / "b", "QQQ", with_actions=False)
    result = check_store(without)
    assert result.symbols[0].action_count is None
    assert [f.kind.value for f in result.symbols[0].findings] == ["UNCLASSIFIED_MATERIAL_MOVE"]


def test_the_command_emits_no_verdict_and_no_digest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The negative property the whole command rests on.

    A partial capture cannot satisfy the frozen criteria, so anything verdict-shaped here —
    the word, a `Verdict`, a certification digest — would be a second acceptance surface
    next to the real one. Asserted on the output AND on the result object.
    """

    store = one_symbol_store(tmp_path)
    code = main(["data", "check", "--store", str(store), "--symbol", "DIA"])
    output = capsys.readouterr().out

    assert code == 0
    for forbidden in ("CERTIFIED", "NOT_CERTIFIED", "UNVERIFIED", "verdict", "digest"):
        assert forbidden not in output, f"{forbidden!r} in a dry-run's output"
    assert "not a certification" in output

    result = check_store(store)
    fields = {name for item in result.symbols for name in item.__slots__}
    assert not {name for name in fields if "verdict" in name or "digest" in name}
    assert not hasattr(result, "verdict")
    assert not hasattr(result, "certification_digest")


def test_nothing_is_written_anywhere(tmp_path: Path) -> None:
    """Read-only, measured over the WHOLE tree — the store and everything beside it.

    A complete snapshot, including new entries: a run that added a report file next to the
    store would compare equal under a snapshot that only revisited the files it knew about.
    """

    store = full_store(tmp_path / "store")
    (tmp_path / "beside").mkdir()
    before = _tree(tmp_path)

    assert main(["data", "check", "--store", str(store)]) == 0
    check_store(store, ("DIA",))

    assert _tree(tmp_path) == before, "data check wrote something"


# ------------------------------------------------------------- filename convention


def test_a_lower_case_bars_filename_is_refused_naming_the_file_and_the_canonical_name(
    tmp_path: Path,
) -> None:
    """`bars/dia.csv` is not a subject the gates can judge, and the refusal says why.

    The store layout is `bars/<SYMBOL>.csv` (`histdata.store.bars_path`; the campaign symbols
    are upper-case), and this module already reads a stem as the upper-cased symbol. Before
    this test the two halves disagreed: `available_symbols` folded `dia` to `DIA` and
    `check_store` reopened `DIA.csv`, which does not exist — a FileNotFoundError traceback
    for an operator who saved one file in lower case. There is deliberately no
    case-insensitive resolution: which file is canonical is not a choice this command makes.
    """

    store = one_symbol_store(tmp_path)
    (store / "bars" / "DIA.csv").rename(store / "bars" / "dia.csv")

    with pytest.raises(CheckRefusal) as caught:
        check_store(store)
    assert caught.value.path == store / "bars" / "dia.csv"
    assert "bars/DIA.csv" in caught.value.reason
    assert "upper-case" in caught.value.reason


def test_a_store_holding_both_cases_of_one_symbol_is_refused_as_ambiguous(
    tmp_path: Path,
) -> None:
    """`DIA.csv` next to `dia.csv`: two files claim one symbol, and neither is chosen."""

    store = one_symbol_store(tmp_path)
    shutil.copyfile(store / "bars" / "DIA.csv", store / "bars" / "dia.csv")

    with pytest.raises(CheckRefusal) as caught:
        check_store(store)
    assert "ambiguous" in caught.value.reason
    assert "DIA.csv" in caught.value.reason
    assert "dia.csv" in caught.value.reason


def test_the_filename_refusal_fires_before_any_gate_and_the_cli_reports_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A conforming SPY.csv sits beside the offending file, and no gate runs for it either."""

    store = one_symbol_store(tmp_path)
    (store / "bars" / "DIA.csv").rename(store / "bars" / "dia.csv")
    shutil.copyfile(tmp_path / "source" / "bars" / "SPY.csv", store / "bars" / "SPY.csv")

    code = main(["data", "check", "--store", str(store)])
    output = capsys.readouterr().out
    assert code == 2
    assert output.startswith("REFUSED "), output
    assert "CHECKED" not in output, output  # the refusal precedes every per-symbol gate
    assert "dia.csv" in output and "bars/DIA.csv" in output


def test_upper_case_stems_are_the_symbols_the_store_holds(tmp_path: Path) -> None:
    """Positive control: a convention-conforming store is read exactly as before."""

    store = full_store(tmp_path / "store")
    assert available_symbols(store) == ("DIA", "GLD", "IWM", "QQQ", "SPY", "TLT")


# ------------------------------------------------- filename convention: the extension


@pytest.mark.parametrize("near_miss", ["DIA.CSV", "Dia.Csv", "dia.CSV"])
def test_1_a_near_miss_extension_is_refused_naming_the_canonical_file(
    tmp_path: Path, near_miss: str
) -> None:
    """Contract 1. `bars/DIA.CSV` used to fall outside the `*.csv` glob and vanish: the store
    reported "GATES RUN over 5 symbol(s) … 0 finding(s)", exit 0, with DIA silently absent.
    A file that is `<STEM>.csv` case-insensitively but not exactly `<STEM>.csv` with an
    upper-case stem is now refused by the same grouping F-3 added, before any gate."""

    store = one_symbol_store(tmp_path)
    (store / "bars" / "DIA.csv").rename(store / "bars" / near_miss)

    with pytest.raises(CheckRefusal) as caught:
        available_symbols(store)
    assert caught.value.path == store / "bars" / near_miss
    assert "bars/DIA.csv" in caught.value.reason


def test_1_cli_refuses_a_near_miss_extension_with_exit_2_before_any_gate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Contract 1, through the CLI: one REFUSED line, exit 2, no CHECKED line — with a
    conforming SPY.csv beside the near miss, so "before any gate" is observable."""

    store = one_symbol_store(tmp_path)
    (store / "bars" / "DIA.csv").rename(store / "bars" / "DIA.CSV")
    shutil.copyfile(tmp_path / "source" / "bars" / "SPY.csv", store / "bars" / "SPY.csv")

    code = main(["data", "check", "--store", str(store)])
    output = capsys.readouterr().out
    assert code == 2, output
    lines = output.splitlines()
    assert len(lines) == 1, output  # ONE line: the refusal, and nothing before or after it
    assert lines[0].startswith("REFUSED "), output
    assert sum(line.startswith("REFUSED ") for line in lines) == 1, output
    assert "CHECKED" not in output, output
    assert "DIA.CSV" in output and "bars/DIA.csv" in output, output


def test_2_a_near_miss_beside_the_canonical_file_is_refused_as_ambiguous(tmp_path: Path) -> None:
    """Contract 2. `DIA.csv` and `DIA.CSV` both claim DIA; the ambiguity refusal names both."""

    store = one_symbol_store(tmp_path)
    shutil.copyfile(store / "bars" / "DIA.csv", store / "bars" / "DIA.CSV")

    with pytest.raises(CheckRefusal) as caught:
        available_symbols(store)
    assert "ambiguous" in caught.value.reason
    assert "DIA.csv" in caught.value.reason and "DIA.CSV" in caught.value.reason


def test_3_a_conforming_store_is_checked_exactly_as_before(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Contract 3. Six CHECKED lines, exit 0, and the report text is unchanged by the extension
    rule — the positive control that the widened scan changed nothing for a conforming store."""

    store = full_store(tmp_path / "store")
    assert available_symbols(store) == ("DIA", "GLD", "IWM", "QQQ", "SPY", "TLT")
    code = main(["data", "check", "--store", str(store)])
    output = capsys.readouterr().out
    assert code == 0, output
    # The complete canonical report for the seed-7 store over START..END, compared whole:
    # six CHECKED lines (sorted symbols, 61 sessions each, coverage to four decimals, the
    # generator's action counts — one quarterly dividend each, GLD none, QQQ plus its split)
    # and the one summary line. Any drift in wording, precision, order or count fails here.
    expected = "".join(
        f"CHECKED {symbol}: 61 bars 2024-01-02..2024-03-28, coverage 1.0000, {actions} action(s), "
        "manifest witnesses checked, 0 finding(s)\n"
        for symbol, actions in (
            ("DIA", 1),
            ("GLD", 0),
            ("IWM", 1),
            ("QQQ", 2),
            ("SPY", 1),
            ("TLT", 1),
        )
    ) + (
        f"GATES RUN over 6 symbol(s) in {store}: 0 finding(s). This is not a certification — "
        "a delivery of all six symbols still has to pass data verify.\n"
    )
    assert output == expected, output


def test_4_non_csv_files_in_bars_are_still_ignored(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Contract 4. The refusal is NOT widened to files that are not `.csv` under any casing."""

    store = one_symbol_store(tmp_path)
    (store / "bars" / "notes.txt").write_text("operator notes\n")
    (store / "bars" / "README").write_text("not bars\n")

    assert available_symbols(store) == ("DIA",)
    assert main(["data", "check", "--store", str(store)]) == 0
    assert "GATES RUN over 1 symbol(s)" in capsys.readouterr().out


# ------------------------------------------------ manifest lists a symbol the bytes lack


def test_a_manifest_listed_symbol_with_no_bars_file_is_refused_before_any_gate(
    tmp_path: Path,
) -> None:
    """The store's own record disagrees with its bytes — the mirror of the witness refusal.

    A listed symbol whose bars file is PRESENT but disagrees is refused today; a listed
    symbol whose bars file is ABSENT was silently skipped: five symbols checked, exit 0,
    and DIA gone. Store-level: it is refused whichever symbol was requested.
    """

    store = full_store(tmp_path / "store")
    (store / "bars" / "DIA.csv").unlink()

    with pytest.raises(CheckRefusal) as caught:
        check_store(store)
    assert caught.value.path == store / "MANIFEST.json"
    assert "DIA" in caught.value.reason
    assert "bars/DIA.csv" in caught.value.reason
    assert "disagrees with its bytes" in caught.value.reason

    with pytest.raises(CheckRefusal) as caught_subset:  # not just the missing one
        check_store(store, ("SPY",))
    assert "bars/DIA.csv" in caught_subset.value.reason


def test_the_cli_refuses_a_manifest_listed_symbol_with_no_bars_and_checks_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = full_store(tmp_path / "store")
    (store / "bars" / "DIA.csv").unlink()

    code = main(["data", "check", "--store", str(store)])
    output = capsys.readouterr().out
    assert code == 2
    assert output.startswith("REFUSED "), output
    assert "CHECKED" not in output, output  # the refusal precedes every per-symbol gate
    assert "bars/DIA.csv" in output


def test_a_symbol_the_manifest_does_not_list_is_still_checked_without_witnesses(
    tmp_path: Path,
) -> None:
    """The other direction is not a refusal: bytes the manifest never claimed are disclosed."""

    store = full_store(tmp_path / "store")
    shutil.copyfile(store / "bars" / "DIA.csv", store / "bars" / "AAPL.csv")

    result = check_store(store, ("AAPL",))
    (item,) = result.symbols
    assert item.symbol == "AAPL"
    assert item.manifest_checked is False
