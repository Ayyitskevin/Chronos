"""The terminal's command registry and grammar (M8a, ADR-0018 §§5-6).

Two things are pinned here, and they fail differently.

The **registry** is the single source of truth for the panel surface, the help
text, and the client's completions. Its invariants are therefore asserted
*structurally* — no test lists the seven commands and checks the list, because a
test that enumerates the registry only proves the registry equals itself. What is
checked is that no code repeats, no alias is claimed twice, every entry can feed a
panel, and every advertised example actually parses back to the command that
advertises it.

The **grammar** is the only thing standing between an operator's typing and a
blank screen, so its failure mode matters more than its success: it must never
raise, and it must never fold the line. midas's parser upper-cases the whole
input, which is why an IBKR option or future symbol does not survive it; the
tests below pin that Chronos returns the operator's bytes back verbatim.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import FrozenInstanceError

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from chronos.terminal.commands import (
    COMMANDS,
    ParsedCommand,
    TerminalCommand,
    _index_registry,
    looks_like_symbol,
    parse,
    registry_payload,
    resolve,
)


def _tokens(command: TerminalCommand) -> list[str]:
    """Every token that should reach this command: its code and its aliases."""

    return [command.code, *command.aliases]


# --------------------------------------------------------------------------- #
# The registry
# --------------------------------------------------------------------------- #


def test_registry_is_not_empty_and_every_entry_is_typeable() -> None:
    assert COMMANDS
    for command in COMMANDS:
        assert command.code, "a command with no code cannot be typed"
        assert command.code == command.code.upper(), f"{command.code} is not canonical upper-case"
        assert command.title, f"{command.code} has no title to render"
        assert command.summary, f"{command.code} tells the operator nothing about itself"


def test_no_duplicate_codes() -> None:
    counts = Counter(command.code.upper() for command in COMMANDS)
    assert [code for code, seen in counts.items() if seen > 1] == []


def test_no_alias_collides_with_any_code_or_other_alias() -> None:
    counts = Counter(token.upper() for command in COMMANDS for token in _tokens(command))
    assert [token for token, seen in counts.items() if seen > 1] == []


def test_every_command_declares_a_panel_and_panels_are_distinct() -> None:
    panels = [command.panel for command in COMMANDS]
    assert all(panels), "a command whose panel is empty declares a panel nothing can render"
    assert len(set(panels)) == len(panels), "two commands claiming one panel is an ambiguity"


def test_indexing_rejects_a_duplicate_code() -> None:
    registry = (
        TerminalCommand(code="DUP", title="One", panel="one"),
        TerminalCommand(code="DUP", title="Two", panel="two"),
    )
    with pytest.raises(ValueError, match="duplicate command code"):
        _index_registry(registry)


def test_indexing_rejects_an_alias_that_shadows_another_commands_code() -> None:
    registry = (
        TerminalCommand(code="AAA", title="One", panel="one", aliases=("BBB",)),
        TerminalCommand(code="BBB", title="Two", panel="two"),
    )
    with pytest.raises(ValueError, match="is the code of a command"):
        _index_registry(registry)


def test_indexing_rejects_an_alias_claimed_by_two_commands() -> None:
    registry = (
        TerminalCommand(code="AAA", title="One", panel="one", aliases=("SHARED",)),
        TerminalCommand(code="BBB", title="Two", panel="two", aliases=("shared",)),
    )
    with pytest.raises(ValueError, match="alias collision"):
        _index_registry(registry)


def test_indexing_rejects_a_command_with_no_panel() -> None:
    registry = (TerminalCommand(code="AAA", title="One", panel=""),)
    with pytest.raises(ValueError, match="declares no panel"):
        _index_registry(registry)


def test_commands_are_frozen() -> None:
    with pytest.raises(FrozenInstanceError):
        COMMANDS[0].code = "NOPE"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        parse("SYS").symbol = "SPY"  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# Resolution
# --------------------------------------------------------------------------- #


def test_resolve_is_case_insensitive_for_every_code_and_alias() -> None:
    for command in COMMANDS:
        for token in _tokens(command):
            assert resolve(token) is command
            assert resolve(token.lower()) is command
            assert resolve(token.upper()) is command
            assert resolve(token.swapcase()) is command
            assert resolve(f"  {token.lower()}  ") is command


def test_resolve_returns_none_rather_than_raising_on_unknown_tokens() -> None:
    known = {token.upper() for command in COMMANDS for token in _tokens(command)}
    for token in ("", "   ", "NOTACOMMAND", "SY", "SYSS", "42", "<b>", "SYS MAND"):
        assert token.upper() not in known, "this case is meant to be unknown"
        assert resolve(token) is None


def test_registry_payload_is_json_serializable_and_complete() -> None:
    payload = registry_payload()
    round_tripped = json.loads(json.dumps(payload))
    assert round_tripped == payload

    assert [entry["code"] for entry in payload] == [command.code for command in COMMANDS]
    for entry in payload:
        assert entry["panel"], "the client cannot render a panel with no name"
        assert isinstance(entry["aliases"], list)
        assert isinstance(entry["examples"], list)
        assert isinstance(entry["takes_symbol"], bool)


def test_every_advertised_example_resolves_to_the_command_advertising_it() -> None:
    for command in COMMANDS:
        assert command.examples, f"{command.code} shows the operator no way to invoke it"
        for example in command.examples:
            assert parse(example).command is command, f"{command.code} advertises {example!r}"


def test_a_command_declaring_a_symbol_must_show_one_in_an_example() -> None:
    """Restraint, asserted rather than assumed.

    No panel in M8a takes a symbol: the rows behind them are keyed by account
    fingerprint and the proposal-queue row has no symbol column at all. This test
    does not pin ``takes_symbol is False`` — that would fight the day the
    narrowing becomes real. It pins the honesty rule instead: a command may only
    advertise a symbol once at least one of its worked examples carries one.
    """

    for command in COMMANDS:
        if not command.takes_symbol:
            continue
        assert any(parse(example).symbol for example in command.examples), (
            f"{command.code} claims to take a symbol but never shows one being given"
        )


# --------------------------------------------------------------------------- #
# The grammar
# --------------------------------------------------------------------------- #


def test_empty_and_whitespace_input_parse_to_an_empty_result() -> None:
    for line in ("", " ", "\t", "\n", "   \t\n  "):
        assert parse(line) == ParsedCommand()


def test_the_last_resolving_token_wins() -> None:
    assert parse("SYS MAND").command is resolve("MAND")
    assert parse("JRNL QUE ALRT").command is resolve("ALRT")
    assert parse("mandate status").command is resolve("SYS")


def test_a_superseded_command_token_is_not_mistaken_for_a_symbol() -> None:
    parsed = parse("SYS MAND")
    assert parsed.symbol == ""
    assert parsed.args == ()


def test_symbol_is_taken_from_before_the_command_and_args_from_after() -> None:
    parsed = parse("SPY JRNL since=2026-07-26 limit=5")
    assert parsed.command is resolve("JRNL")
    assert parsed.symbol == "SPY"
    assert parsed.args == ("since=2026-07-26", "limit=5")
    assert parsed.raw == "SPY JRNL since=2026-07-26 limit=5"


def test_args_keep_their_case_because_the_line_is_never_folded() -> None:
    parsed = parse("  jrnl   Since=2026-07-26T13:30:00Z   MixedCase  ")
    assert parsed.command is resolve("JRNL")
    assert parsed.args == ("Since=2026-07-26T13:30:00Z", "MixedCase")
    assert parsed.raw == "jrnl   Since=2026-07-26T13:30:00Z   MixedCase"


@pytest.mark.parametrize(
    "symbol",
    [
        "SPY",
        "BRK.B",
        "ESZ5",
        "/ES",
        "MESZ5",
        "SPY250117C00500000",
        "SPXW",
        "RDS.A",
        "BTC/USD",
    ],
)
def test_symbol_extraction_returns_the_operator_bytes_verbatim(symbol: str) -> None:
    """The property midas's parser loses: an instrument survives the parser.

    Upper-casing the whole line is what destroys an option or future symbol, so
    what is asserted is identity, not equality after normalization.
    """

    parsed = parse(f"{symbol} SYS")
    assert parsed.symbol == symbol
    assert parsed.command is resolve("SYS")
    assert looks_like_symbol(symbol)


def test_bare_symbol_yields_no_command_but_keeps_the_symbol() -> None:
    """Chronos declares no description panel, and says so instead of inventing one.

    tyche sends a bare symbol to DES. Chronos has no historical-bar route and no
    contract-detail route, so a DES command would be a panel no route can feed.
    The symbol is kept rather than discarded so the client can hold it.
    """

    parsed = parse("SPY")
    assert parsed.command is None
    assert parsed.symbol == "SPY"
    assert parsed.args == ()
    assert parsed.raw == "SPY"


def test_unrecognized_noise_before_the_command_is_tolerated() -> None:
    parsed = parse("SPY US Equity SYS")
    assert parsed.command is resolve("SYS")
    assert parsed.symbol == "SPY"
    assert parsed.args == ()


def test_unknown_input_is_reported_as_unknown_rather_than_raised() -> None:
    parsed = parse("frobnicate the gizmo")
    assert parsed.command is None
    assert parsed.symbol == ""
    assert parsed.args == ("frobnicate", "the", "gizmo")
    assert parsed.raw == "frobnicate the gizmo"


@pytest.mark.parametrize(
    "line",
    [
        "!!!",
        "?",
        "??",
        "-",
        "/",
        "//",
        "...",
        "SYS;DROP TABLE autonomy_owner_alerts",
        "<script>alert(1)</script>",
        "{'code': 'SYS'}",
        "\x00\x01\x02",
        "SYS " * 500,
        "A" * 5000,
        "🙂 SYS 🙂",
        "\u041cAND",  # a Cyrillic capital EM homoglyph, not the ASCII letter
    ],
)
def test_hostile_input_never_raises(line: str) -> None:
    parsed = parse(line)
    assert isinstance(parsed, ParsedCommand)
    assert parsed.command is None or parsed.command in COMMANDS


@settings(max_examples=400)
@given(st.text())
def test_parse_is_total_over_arbitrary_text(line: str) -> None:
    parsed = parse(line)
    assert parsed.raw == line.strip()
    assert parsed.command is None or parsed.command in COMMANDS
    assert all(token in line for token in parsed.args)
    assert parsed.symbol == "" or parsed.symbol in line


@settings(max_examples=200)
@given(st.text(), st.sampled_from([token for c in COMMANDS for token in (c.code, *c.aliases)]))
def test_a_trailing_command_token_always_wins(prefix: str, token: str) -> None:
    """Whatever precedes it, the last resolving token is the command."""

    assert parse(f"{prefix} {token}").command is resolve(token)
