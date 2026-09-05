"""Source-measured SKB properties (issue #181).

These fields are measurements, not derivations, so the tests that matter are the
ones that keep a measurement honest: every citation must still match the Pine line
it names, only the five scripts actually read may carry a value, and the corpus
identity hash must be untouched by any of it.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from chronos.skb import query as skb_query
from chronos.skb.compiler import (
    REPO_ROOT,
    STORE_PATH,
    SKBCompileError,
    _corpus_hash,
    compile_skb,
    load_store,
)
from chronos.skb.schema import PineScriptEntry, Timeframe, TimeframeBinding
from chronos.skb.source_properties import SOURCE_PROPERTIES, LineCitation, MeasuredProperties

_GOLDEN_CORPUS_HASH = "94482faffd7205055363beb209b0ee86611a3588917b3637490f5af79fe67d8a"
_MEASURED_CATALOG_NUMBERS = {"00", "01", "02", "0A", "16"}


@pytest.mark.parametrize("catalog_number", sorted(SOURCE_PROPERTIES))
def test_every_citation_still_matches_its_pine_line(catalog_number: str) -> None:
    """The anti-fabrication guard: a citation that drifts fails, it does not decay.

    Each claim names a file, a line and a token. If an edit to the corpus shifts a
    line, or a token was mis-transcribed, this fails loudly rather than leaving a
    plausible-looking provenance string that points at nothing.
    """

    measured = SOURCE_PROPERTIES[catalog_number]
    path = REPO_ROOT / "research/pine" / measured.filename
    assert path.is_file(), f"cited Pine file does not exist: {measured.filename}"
    source_lines = path.read_text(encoding="utf-8").splitlines()

    citations = measured.position_citations + measured.timeframe_citations
    assert citations, "a measured entry must carry evidence"
    for citation in citations:
        assert 1 <= citation.line <= len(source_lines), (
            f"{measured.filename}:{citation.line} is out of range (file has "
            f"{len(source_lines)} lines)"
        )
        actual = source_lines[citation.line - 1]
        assert citation.contains in actual, (
            f"{measured.filename}:{citation.line} no longer contains "
            f"{citation.contains!r} — it reads: {actual.strip()!r}"
        )


def test_position_claim_rests_on_a_flatness_gate_not_on_pyramiding() -> None:
    """Each one-position claim must cite the entry gate, not only the declaration.

    ``pyramiding`` is the misleading field here — four of the five declare
    ``pyramiding = 3`` while still holding one position — so a claim of
    ``max_concurrent_positions == 1`` is only supported by a flatness condition on
    the entry. This pins the reasoning, not just the answer.
    """

    for catalog_number, measured in SOURCE_PROPERTIES.items():
        if measured.max_concurrent_positions != 1:
            continue
        gates = [c for c in measured.position_citations if "position_size == 0" in c.contains]
        assert gates, (
            f"catalog {catalog_number} claims one position but cites no "
            f"`strategy.position_size == 0` entry gate"
        )


def test_only_the_scripts_that_were_read_carry_a_value() -> None:
    """37 scripts stay unmeasured; 'unknown' must keep meaning 'nobody looked'."""

    store = compile_skb(REPO_ROOT)
    measured = {e.catalog_number for e in store.pine_scripts if e.source_property_citation}
    assert measured == _MEASURED_CATALOG_NUMBERS

    for entry in store.pine_scripts:
        if entry.catalog_number in _MEASURED_CATALOG_NUMBERS:
            assert entry.max_concurrent_positions == 1
            assert entry.timeframe_binding is TimeframeBinding.CHART_TF
        else:
            assert entry.max_concurrent_positions is None
            assert entry.timeframe_binding is TimeframeBinding.UNKNOWN
            assert entry.source_property_citation == ""


def test_corpus_hash_is_a_function_of_catalog_identity_alone() -> None:
    """Adding fields cannot move the corpus hash — proved on the mechanism.

    The golden pin alone would only show the value happens to match today. This
    also shows *why*: ``_corpus_hash`` reads catalog number and sha256 and nothing
    else, so any number of new per-entry fields leaves it fixed.
    """

    bare = [{"catalog_number": "01", "sha256": "ab"}, {"catalog_number": "02", "sha256": "cd"}]
    enriched = [
        {**bare[0], "max_concurrent_positions": 1, "timeframe_binding": "chart_tf"},
        {**bare[1], "max_concurrent_positions": 3, "timeframe_binding": "pinned"},
    ]
    assert _corpus_hash(bare) == _corpus_hash(enriched)

    store = compile_skb(REPO_ROOT)
    assert store.corpus_hash == _GOLDEN_CORPUS_HASH
    assert load_store(REPO_ROOT / STORE_PATH).corpus_hash == _GOLDEN_CORPUS_HASH


def test_an_interval_without_a_pinned_binding_is_rejected() -> None:
    """A concrete interval is only meaningful when the script pins one."""

    store = compile_skb(REPO_ROOT)
    entry = next(e for e in store.pine_scripts if e.catalog_number == "01")
    fields = entry.model_dump()

    fields["timeframe"] = Timeframe.DAILY.value
    fields["timeframe_binding"] = TimeframeBinding.CHART_TF.value
    with pytest.raises(ValidationError, match="requires timeframe_binding=pinned"):
        PineScriptEntry.model_validate(fields)

    fields["timeframe_binding"] = TimeframeBinding.PINNED.value
    assert PineScriptEntry.model_validate(fields).timeframe is Timeframe.DAILY


def test_compile_fails_closed_on_a_catalog_number_the_corpus_lacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patched = dict(SOURCE_PROPERTIES)
    patched["ZZ"] = SOURCE_PROPERTIES["01"]
    monkeypatch.setattr("chronos.skb.compiler.SOURCE_PROPERTIES", patched)
    with pytest.raises(SKBCompileError, match="absent from the corpus: ZZ"):
        compile_skb(REPO_ROOT)


def test_compile_fails_closed_when_a_citation_names_the_wrong_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Citing the right catalog number but the wrong file must not compile."""

    wrong = MeasuredProperties(
        filename="02_markov_regime_bear_plus.pine",
        max_concurrent_positions=1,
        timeframe_binding=TimeframeBinding.CHART_TF,
        position_citations=(LineCitation(1086, "strategy.position_size == 0"),),
        timeframe_citations=(LineCitation(712, "timeframe.period"),),
        note="deliberately mismatched",
    )
    monkeypatch.setattr(
        "chronos.skb.compiler.SOURCE_PROPERTIES", {**SOURCE_PROPERTIES, "01": wrong}
    )
    with pytest.raises(SKBCompileError, match="source_properties cites"):
        compile_skb(REPO_ROOT)


def test_query_exposes_both_fields_and_never_matches_an_unmeasured_script() -> None:
    store = compile_skb(REPO_ROOT)

    one_position = skb_query.query_scripts(store, max_concurrent_positions=1)
    assert {e.catalog_number for e in one_position} == _MEASURED_CATALOG_NUMBERS

    chart_tf = skb_query.query_scripts(store, timeframe_binding=TimeframeBinding.CHART_TF)
    assert {e.catalog_number for e in chart_tf} == _MEASURED_CATALOG_NUMBERS

    unmeasured = skb_query.query_scripts(store, timeframe_binding=TimeframeBinding.UNKNOWN)
    assert len(unmeasured) == store.pine_script_count - len(_MEASURED_CATALOG_NUMBERS)

    # An unmeasured script must not be swept up by a filter on a measured value.
    assert skb_query.query_scripts(store, max_concurrent_positions=0) == ()

    counts = skb_query.timeframe_binding_counts(store)
    assert counts[TimeframeBinding.CHART_TF.value] == len(_MEASURED_CATALOG_NUMBERS)
    assert counts[TimeframeBinding.UNKNOWN.value] == 37
    assert counts[TimeframeBinding.PINNED.value] == 0
