"""SKB CLI tests (AI Quant plan B2).

Drives the read-only ``chronos skb`` surface through ``main(argv)`` and pins exit
codes and output for ``stats`` and ``query`` (each output format, a couple of
filters). The CLI loads the committed store — it is a browse surface over the
knowledge base and places no orders.
"""

from __future__ import annotations

import json

import pytest

from chronos.cli.main import main


def test_skb_stats_reports_corpus_and_counts(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["skb", "stats"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["pine_scripts"] == 42
    assert payload["derived_strategies"] == 2
    assert payload["by_disposition"] == {
        "ported": 2,
        "deferred": 4,
        "blocked_on": 1,
        "rejected": 35,
    }
    assert sum(payload["by_family"].values()) == 42


def test_skb_query_ids_format(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["skb", "query", "--disposition", "deferred", "--format", "ids"]) == 0
    captured = capsys.readouterr()
    ids = captured.out.split()
    assert ids == ["00", "02", "0A", "16"]
    assert "4 script(s)" in captured.err


def test_skb_query_json_format(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["skb", "query", "--ported", "--format", "json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert {r["catalog_number"] for r in rows} == {"01", "11"}
    assert all(r["disposition"] == "ported" for r in rows)


def test_skb_query_table_default_lists_rows(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["skb", "query", "--disposition", "blocked_on"]) == 0
    captured = capsys.readouterr()
    assert "08" in captured.out
    assert "requires_rewrite" in captured.out
    assert "1 script(s)" in captured.err


def test_skb_query_tradable_long_includes_bidirectional(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["skb", "query", "--tradable", "long", "--format", "ids"]) == 0
    long_ids = set(capsys.readouterr().out.split())
    assert main(["skb", "query", "--direction", "bidirectional", "--format", "ids"]) == 0
    bidir_ids = set(capsys.readouterr().out.split())
    assert bidir_ids
    assert bidir_ids <= long_ids


def test_skb_query_conjunctive_filters(capsys: pytest.CaptureFixture[str]) -> None:
    assert (
        main(
            [
                "skb",
                "query",
                "--disposition",
                "deferred",
                "--direction",
                "short",
                "--format",
                "ids",
            ]
        )
        == 0
    )
    assert capsys.readouterr().out.split() == ["02"]


def test_skb_query_rejects_out_of_vocab_choice(capsys: pytest.CaptureFixture[str]) -> None:
    # argparse rejects an unknown disposition with a non-zero SystemExit.
    with pytest.raises(SystemExit) as excinfo:
        main(["skb", "query", "--disposition", "not_a_real_disposition"])
    assert excinfo.value.code != 0


def test_skb_subcommand_is_required(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["skb"])
    assert excinfo.value.code != 0
