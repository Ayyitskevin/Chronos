"""The terminal's command surface: one frozen registry, one tolerant grammar (M8a).

ADR-0018 §§5-6. Two ideas from tyche (Apache-2.0; attribution in ``NOTICE``) are
adapted here, and the second is the one that matters:

- **the tolerant grammar** — ``<symbol?> … <command?> <args…>``, in which the
  *last* registry-resolving token wins and unrecognized text degrades instead of
  erroring;
- **the registry as the single source of truth** — the panel surface, the help
  text, and the client's completions are all derived from :data:`COMMANDS`, so
  there is exactly one place a command can be said to exist.

Chronos inverts where that registry lives. In tyche it is TypeScript compiled
into the browser bundle; here it is Python, served as JSON. The consequence is
the point: the panel surface cannot drift from what the backend can answer,
because the backend is what declares it, and the declaration is checked by
``mypy --strict`` and ``pytest`` rather than by a second toolchain.

## Why this file never upper-cases the line

midas's parser upper-cases the whole input and then treats the first token as a
symbol. Applied to Chronos that is not a style difference, it is data loss:
``ESZ5`` survives it, but an argument, a date, or an OCC-style option token does
not necessarily, and once the line has been folded there is no way to recover
what the operator typed. So the rule here is narrow and absolute: **tokens are
matched case-insensitively and returned verbatim.** ``resolve`` upper-cases a
*copy* of the token to look it up; nothing else in this module transforms user
text at all. :attr:`ParsedCommand.symbol` and :attr:`ParsedCommand.args` are
slices of the line as typed.

## Why nothing here can raise

A terminal that raises on a typo teaches the operator to stop typing, and the
operator is the last supervisor in this system. An unresolvable line is a *fact
about the line*, reported as ``command is None`` and rendered by the client as
"unknown command", not an exception and not an error state. :func:`parse` has no
failure mode: every string, including empty, whitespace, control characters, and
markup, produces a :class:`ParsedCommand`.

## What was rejected

**A description panel.** tyche's grammar sends a bare symbol to ``DES``, and the
obvious port would have been a ``DES`` command here. There is nothing behind it:
Chronos has no historical-bar route and no ``Broker`` bars method (ADR-0018
residual 1), and no contract-detail endpoint. Declaring ``DES`` would declare a
panel no route can feed, which is precisely the restraint ADR-0018 asks for. A
bare symbol therefore parses to ``command=None`` with the symbol **kept**, so the
client can hold it as the pending symbol rather than silently discarding what the
operator typed.

**A symbol narrowing on any panel.** Every command ships ``takes_symbol=False``,
and that is a statement about storage, not about the grammar. The rows behind
these panels are keyed by account fingerprint, not by instrument:
``AutonomyProposalQueueRow`` — which feeds both JRNL and QUE — stores the raw
payload plus a stage and a refusal and has no symbol column at all;
``AutonomyOwnerAlertRow`` and ``AutonomySessionCounterRow`` likewise. A panel
advertising ``SPY JRNL`` today would be advertising a filter the database cannot
serve, and a filter that silently does nothing is worse than no filter. The
grammar still extracts the symbol, and the field still exists, so that when a
symbol-keyed read model lands, flipping one boolean turns the narrowing on across
the grammar, the JSON payload, the help text, and the client at once. That is the
whole reason the registry is the single source of truth.

## What this module is not

Nothing here reaches execution. A command names a *panel*, a panel is a read
model, and no token in this file maps to a broker call, an order field, or a
transmit site. The terminal's owner-actions reach execution only by the browser
calling the existing routes in ``chronos.api.routes.orders``; this module has no
opinion about them and no name for them.

Every string in the registry is a constant authored here. None of it comes from
a model, an evidence bundle, or any external text — which is what makes it safe
for the client to render, and what distinguishes it from model narrative, which
is displayed as text and never as markup (ADR-0018 residual 6).

## Honest residuals

1. **A command token shadows an identically-named ticker.** ``ALRT`` is a
   command, so it can never be read as a symbol. Harmless while no panel takes a
   symbol; it becomes a real collision the day one does, and the fix is an escape
   form, not a rename.
2. **A lowercase ticker is not a symbol.** ``spy sys`` yields no symbol. Symbol
   recognition requires the conventional upper-case ticker form, because the
   alternative — promoting any bare word — makes every typo an instrument.
3. **A symbol after the command lands in ``args``.** ``SPY JRNL`` is the
   canonical Bloomberg-style order; ``JRNL SPY`` puts ``SPY`` in ``args`` rather
   than in ``symbol``. Deliberate: "everything after the command is an argument"
   is a rule an operator can hold in their head, and a parser that hunts for
   symbols on both sides of the command is one whose output cannot be predicted
   from the line.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

#: The longest token still considered a possible instrument. An OCC-style option
#: symbol is 21 characters; beyond this a token is prose, and treating prose as
#: an instrument is how a parser starts inventing things.
MAX_SYMBOL_LENGTH = 32

#: Conventional ticker form: upper-case, starts with a letter, dots and slashes
#: tolerated so ``BRK.B``, ``ESZ5``, and a ``/ES`` futures root all survive.
_SYMBOL_PATTERN = re.compile(r"^/?[A-Z][A-Z0-9]*(?:[./][A-Z0-9]+)*$")


@dataclass(frozen=True, slots=True)
class TerminalCommand:
    """One command, and therefore one panel, and therefore one read model.

    Frozen because the registry is a constant: a command that can be mutated at
    runtime is a command the help text, the client, and the routes can disagree
    about.
    """

    #: The canonical token, upper-case. What HELP shows and what the client
    #: echoes back.
    code: str
    #: Short human title for the panel header.
    title: str
    #: The panel this command opens. One panel per command, and a panel only
    #: exists here if a route can feed it (ADR-0018 §6).
    panel: str
    #: Additional tokens that resolve to this command. Checked for collisions
    #: against every other code and alias at import.
    aliases: tuple[str, ...] = ()
    #: Whether the panel *accepts* a symbol as a narrowing. False across the
    #: board in M8a — see the module docstring; this is a claim about what the
    #: stored rows can be filtered by, not about what the grammar can parse.
    takes_symbol: bool = False
    #: What the panel answers, in the operator's terms. Rendered by HELP, which
    #: makes it a claim about the read model behind ``panel`` and not a blurb: a
    #: clause naming a fact that model does not carry is the panel surface
    #: drifting from what the backend can answer, which is the drift keeping the
    #: registry in Python was meant to make impossible.
    summary: str = ""
    #: Lines that work *today*. Since no panel takes an argument yet, that is
    #: the code and its aliases; inventing an example with an argument nothing
    #: parses would be the kind of plausible fiction this repository refuses.
    examples: tuple[str, ...] = ()


#: The command surface, entire. Seven panels, each backed by state the backend
#: already holds. Additions belong here and nowhere else.
COMMANDS: tuple[TerminalCommand, ...] = (
    TerminalCommand(
        code="SYS",
        title="Supervision status",
        panel="system",
        aliases=("STATUS", "SYSTEM"),
        summary=(
            "Whether the supervisor is awake and whether it is allowed to act: proposal-queue "
            "depth, unacknowledged alert count, kill switch, and live arming. An unknown value "
            "is shown as unknown, never as zero."
        ),
        examples=("SYS", "STATUS"),
    ),
    TerminalCommand(
        code="MAND",
        title="Active mandate",
        panel="mandate",
        aliases=("MANDATE",),
        summary=(
            "The authority in force: instrument scope, promotion level, capital and loss "
            "limits, when the owner activated it, and when it expires. No activation means no "
            "authority established — which the panel states rather than showing an empty scope."
        ),
        examples=("MAND", "MANDATE"),
    ),
    TerminalCommand(
        code="JRNL",
        title="Decision journal",
        panel="journal",
        aliases=("JOURNAL",),
        summary=(
            "Where each judged proposal stopped and why: the cycle stage reached and the "
            "refusal recorded, most recent first. Refusal detail is model- or Chronos-authored "
            "text and is displayed as text only."
        ),
        examples=("JRNL", "JOURNAL"),
    ),
    TerminalCommand(
        code="CNTR",
        title="Session counters",
        panel="counters",
        aliases=("COUNTERS",),
        summary=(
            "What the supervisor itself observed this session: realized loss, peak-to-trough "
            "drawdown, orders submitted, cancellations, replacements, turnover. Absent "
            "counters mean nothing has been recorded, never 'no losses yet'."
        ),
        examples=("CNTR", "COUNTERS"),
    ),
    TerminalCommand(
        code="QUE",
        title="Proposal queue",
        panel="queue",
        aliases=("QUEUE", "PROPOSALS"),
        summary=(
            "How many proposals are waiting for a cycle to judge them, against the bound, and "
            "how the recently judged ones came out. A queued proposal has been received, which "
            "authorizes nothing."
        ),
        examples=("QUE", "QUEUE"),
    ),
    TerminalCommand(
        code="ALRT",
        title="Owner alerts",
        panel="alerts",
        aliases=("ALERTS",),
        summary=(
            "Alerts the owner has not acknowledged: severity, kind, when it was raised, "
            "whether it was actually delivered, and how many times the condition recurred. "
            "Acknowledging is an owner action, and it never deletes the alert."
        ),
        examples=("ALRT", "ALERTS"),
    ),
    TerminalCommand(
        code="HELP",
        title="Command reference",
        panel="help",
        aliases=("?",),
        summary=(
            "This registry, rendered: every command, its aliases, the panel it opens, and what "
            "that panel answers. Derived from the same declaration the routes are, so it "
            "cannot describe a command that does not exist."
        ),
        examples=("HELP", "?"),
    ),
)


def _index_registry(
    commands: Sequence[TerminalCommand],
) -> tuple[dict[str, TerminalCommand], dict[str, TerminalCommand]]:
    """Build the code and alias lookups, refusing an incoherent registry.

    Raises rather than resolving ambiguously, and raises at *import* because
    :data:`COMMANDS` is a literal: the only way to reach these errors is to edit
    the registry wrongly, and a terminal whose tokens mean two things is worse
    than a terminal that does not start. Codes are indexed before aliases so an
    alias can never shadow a code declared later in the tuple.
    """

    by_code: dict[str, TerminalCommand] = {}
    for command in commands:
        code = command.code.upper()
        if not code:
            raise ValueError("a command with an empty code cannot be typed")
        if not command.panel:
            raise ValueError(f"{code} declares no panel, so nothing can render it")
        if code in by_code:
            raise ValueError(f"duplicate command code: {code}")
        by_code[code] = command

    by_alias: dict[str, TerminalCommand] = {}
    for command in commands:
        for alias in command.aliases:
            token = alias.upper()
            if not token:
                raise ValueError(f"{command.code} declares an empty alias")
            shadowed = by_code.get(token)
            if shadowed is not None and shadowed is not command:
                raise ValueError(f"alias {token!r} of {command.code} is the code of a command")
            claimed = by_alias.get(token)
            if claimed is not None and claimed is not command:
                raise ValueError(
                    f"alias collision: {token!r} is claimed by both {claimed.code} "
                    f"and {command.code}"
                )
            by_alias[token] = command

    return by_code, by_alias


_BY_CODE, _BY_ALIAS = _index_registry(COMMANDS)


def looks_like_symbol(token: str) -> bool:
    """Whether a token has the conventional instrument form.

    Deliberately conservative. Requiring the upper-case form is what keeps
    ``SECF apple``-shaped input from turning a typo into an instrument, and the
    cost — a lowercase ticker is not recognized — is disclosed rather than
    papered over by promoting bare words.
    """

    return len(token) <= MAX_SYMBOL_LENGTH and _SYMBOL_PATTERN.match(token) is not None


def resolve(token: str) -> TerminalCommand | None:
    """Resolve one token to its command, code first and then aliases.

    Case-insensitive by upper-casing a copy of the token; the caller's string is
    never modified. Returns ``None`` for anything unknown, which is a fact and
    not an error.
    """

    key = token.strip().upper()
    if not key:
        return None
    found = _BY_CODE.get(key)
    if found is not None:
        return found
    return _BY_ALIAS.get(key)


def registry_payload() -> list[dict[str, object]]:
    """The registry as JSON-serializable data, for the client to render.

    Tuples become lists because JSON has no tuple, and the client is the only
    consumer. This is the whole contract the browser needs: it fetches this and
    draws the command surface from it, so a command added here appears in the
    client's help and completions without a client change.
    """

    return [
        {
            "code": command.code,
            "title": command.title,
            "panel": command.panel,
            "aliases": list(command.aliases),
            "takes_symbol": command.takes_symbol,
            "summary": command.summary,
            "examples": list(command.examples),
        }
        for command in COMMANDS
    ]


@dataclass(frozen=True, slots=True)
class ParsedCommand:
    """What one typed line meant, as far as the grammar can tell.

    All four fields are always present. ``command`` is ``None`` when nothing
    resolved — the client's cue to say "unknown command" — and ``symbol`` and
    ``args`` are verbatim slices of the line, never folded or rewritten.
    """

    #: The resolved command, or ``None`` when no token resolved.
    command: TerminalCommand | None = None
    #: The first symbol-shaped token appearing before the command, verbatim.
    #: Empty when there was none.
    symbol: str = ""
    #: Every token after the command, verbatim and in order.
    args: tuple[str, ...] = ()
    #: The line as typed, stripped of surrounding whitespace. Kept so the client
    #: can echo exactly what was entered.
    raw: str = ""


def parse(line: str) -> ParsedCommand:
    """Parse a typed line. Total: every input maps to a :class:`ParsedCommand`.

    The grammar, stated completely:

    - tokens are split on whitespace;
    - the **last** token that resolves in the registry is the command, so typing
      a second command replaces the first rather than erroring — ``SYS MAND``
      opens the mandate;
    - the first symbol-shaped token *before* the command becomes ``symbol``. A
      token that itself resolves is never a symbol, so the superseded ``SYS`` in
      ``SYS MAND`` is dropped rather than mistaken for a ticker;
    - every token *after* the command becomes ``args``, verbatim;
    - other tokens before the command are tolerated and ignored, which is what
      lets Bloomberg-style noise (``SPY US Equity SYS``) parse as ``SPY SYS``
      without a keyword table to maintain;
    - with no command at all, args are the tokens following the symbol, so a
      bare symbol yields a symbol and nothing else, and unrecognized prose is
      returned whole for the client to echo.
    """

    raw = line.strip()
    if not raw:
        return ParsedCommand()

    tokens = raw.split()

    command: TerminalCommand | None = None
    command_index = -1
    for index, token in enumerate(tokens):
        found = resolve(token)
        if found is not None:
            command = found
            command_index = index

    head = tokens if command_index < 0 else tokens[:command_index]
    symbol = ""
    symbol_index = -1
    for index, token in enumerate(head):
        if resolve(token) is None and looks_like_symbol(token):
            symbol = token
            symbol_index = index
            break

    anchor = command_index if command_index >= 0 else symbol_index
    args = tuple(tokens[anchor + 1 :])

    return ParsedCommand(command=command, symbol=symbol, args=args, raw=raw)
