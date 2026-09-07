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
from chronos.research.data_check import CheckRefusal, check_store
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
