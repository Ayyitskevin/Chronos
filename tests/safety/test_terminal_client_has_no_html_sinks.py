"""The terminal client's "text, never markup" rule, executed instead of asserted (M8a).

``terminal.js`` opens with the claim this whole milestone leans on:

    Every server string is text. Nothing here assigns ``innerHTML``,
    ``outerHTML``, ``insertAdjacentHTML`` or ``document.write``; text reaches the
    DOM only via ``textContent``.

That is a **comment**. Until this file existed nothing enforced it and no test
read the client's source at all, so the property held exactly as long as ~1300
lines of hand-written DOM code went un-regressed. A panel renderer added under
time pressure could reintroduce an HTML sink and every existing test would still
pass, because they drive behaviour through a fake DOM that is perfectly happy to
be handed markup. ``chronos.terminal.views`` makes the same argument about its
own module and answers it structurally; this is the client's half of it.

What the sink actually costs is worth stating, because "the terminal is a window"
makes it sound theoretical. The strings this page draws include model- and
worker-authored narrative (R-30) and operator-typed notes, neither of which
Chronos wrote. Same-origin script executing in this document is script running in
the page that talks to the process holding the broker session — and a browser
session credential is a planned follow-up, at which point the distance between
"injected text is seen" and "injected text acts" is one ``innerHTML``.

Two layers, and this is the first. The second is the Content-Security-Policy
``chronos.api.main`` emits over ``/terminal``, which makes the *next* sink inert
rather than exploitable. Neither replaces the other: a header can be dropped by a
proxy or a future refactor of the middleware, and a policy nobody can violate is
cheaper to keep than one nobody can exploit.

## Why comments are stripped before the scan

The file's own honest description of what it avoids names every sink this test
looks for. Scanning the raw text would fail on the truthful comment, and the
obvious fix — deleting the comment — would trade the documentation for the test.
So the scanners run over a comment-stripped copy. That stripping is itself a
place a sink could hide (over-strip a regex literal and the code after it
disappears), so it is pinned below by synthetic sources whose sinks sit exactly
where a naive stripper would lose them.

## Honest residual

``_strip_js_comments`` is a heuristic tokenizer, not a JavaScript parser: it
tracks strings, template literals and regex literals well enough for a file with
no bundler and no exports, and it is checked against the shipped client on every
run. It is not a general-purpose tool and should not be used as one. The tests
below therefore assert both directions — that the real client is clean *and* that
the stripper still surfaces a planted sink — because a scanner that quietly stops
finding things is the same class of problem as a panel that quietly stops
refreshing.
"""

from __future__ import annotations

import re
from pathlib import Path

_CLIENT_DIR = Path(__file__).resolve().parents[2] / "src" / "chronos" / "terminal" / "static"
_JS = _CLIENT_DIR / "terminal.js"
_CSS = _CLIENT_DIR / "terminal.css"
_HTML = _CLIENT_DIR / "index.html"

#: Every way a string can stop being text and start being code or markup. Named
#: rather than numbered so a failure reads as the rule that was broken.
_SINKS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("innerHTML", re.compile(r"\binnerHTML\b")),
    ("outerHTML", re.compile(r"\bouterHTML\b")),
    ("insertAdjacentHTML", re.compile(r"\binsertAdjacentHTML\b")),
    ("document.write", re.compile(r"\bdocument\s*\.\s*write")),
    ("eval", re.compile(r"\beval\s*\(")),
    ("the Function constructor", re.compile(r"\bnew\s+Function\b|\bFunction\s*\(")),
    ("a javascript: URL", re.compile(r"(?i)javascript\s*:")),
    ("an on* handler set as an attribute", re.compile(r"""(?i)\bsetAttribute\s*\(\s*["'`]\s*on""")),
)

#: An absolute or scheme-relative reference to somewhere that is not this origin.
#: The page must work with the machine offline, and an outbound request from the
#: process holding the broker session is not a styling decision.
_EXTERNAL_ORIGIN = re.compile(r"(?i)\bhttps?:|(?<![:\w])//[a-z0-9]")

#: ``src`` and ``href`` in the shell, which ``index.html`` documents as living
#: under exactly one prefix.
_SHELL_REFERENCE = re.compile(r"""(?i)\b(?:src|href)\s*=\s*["']([^"']*)["']""")

_CSS_URL = re.compile(r"""(?i)\burl\(\s*["']?([^"')]*)""")

_REGEX_MAY_FOLLOW = frozenset("(,=:[!&|?{};+-*%~^<>") | {""}
_REGEX_MAY_FOLLOW_KEYWORD = frozenset(
    {
        "return",
        "typeof",
        "instanceof",
        "in",
        "of",
        "new",
        "delete",
        "void",
        "case",
        "do",
        "else",
        "yield",
        "await",
        "throw",
    }
)


# ------------------------------------------------------------------- stripping


def _string_end(source: str, start: int) -> int:
    """The index just past the string, template or template substitution at ``start``.

    Template substitutions are walked as code so a nested template inside
    ``${...}`` closes against its own backtick rather than against the outer
    one — which is the mis-parse that would leave the remainder of a line being
    read as code and a ``//`` inside a string eating it.
    """

    quote = source[start]
    index = start + 1
    end = len(source)
    while index < end:
        char = source[index]
        if char == "\\":
            index += 2
            continue
        if char == quote:
            return index + 1
        if quote == "`" and char == "$" and source[index + 1 : index + 2] == "{":
            depth = 1
            index += 2
            while index < end and depth:
                inner = source[index]
                if inner in "\"'`":
                    index = _string_end(source, index)
                    continue
                if inner == "{":
                    depth += 1
                elif inner == "}":
                    depth -= 1
                index += 1
            continue
        index += 1
    return end


def _regex_end(source: str, start: int) -> int:
    """The index just past the regex literal at ``start``.

    Character classes are tracked because ``[/]`` is a legal way to write a
    slash, and an unterminated literal is treated as a lone ``/`` rather than
    swallowing the rest of the file.
    """

    index = start + 1
    end = len(source)
    in_class = False
    while index < end:
        char = source[index]
        if char == "\\":
            index += 2
            continue
        if char == "[":
            in_class = True
        elif char == "]":
            in_class = False
        elif char == "\n":
            return start + 1
        elif char == "/" and not in_class:
            index += 1
            while index < end and source[index].isalpha():
                index += 1
            return index
        index += 1
    return start + 1


def _regex_starts_here(emitted: str, previous: str) -> bool:
    """Whether a ``/`` opens a regex literal rather than dividing something."""

    if previous in _REGEX_MAY_FOLLOW:
        return True
    word = re.search(r"([A-Za-z_$]+)\s*$", emitted[-32:])
    return bool(word and word.group(1) in _REGEX_MAY_FOLLOW_KEYWORD)


def _strip_js_comments(source: str) -> str:
    """Remove ``//`` and ``/* */`` comments and nothing else.

    Line numbers are preserved — a block comment leaves behind as many newlines
    as it spanned — so a failure below can name the line in the real file rather
    than a line in a copy nobody can open.
    """

    out: list[str] = []
    index = 0
    end = len(source)
    previous = ""
    while index < end:
        char = source[index]
        following = source[index + 1 : index + 2]
        if char == "/" and following == "/":
            stop = source.find("\n", index)
            index = end if stop == -1 else stop
            continue
        if char == "/" and following == "*":
            stop = source.find("*/", index + 2)
            stop = end if stop == -1 else stop + 2
            out.append(" " + "\n" * source.count("\n", index, stop))
            index = stop
            continue
        if char in "\"'`":
            stop = _string_end(source, index)
            out.append(source[index:stop])
            previous = source[stop - 1]
            index = stop
            continue
        if char == "/" and _regex_starts_here("".join(out), previous):
            stop = _regex_end(source, index)
            out.append(source[index:stop])
            previous = "/"
            index = stop
            continue
        out.append(char)
        if not char.isspace():
            previous = char
        index += 1
    return "".join(out)


def _strip_html_comments(source: str) -> str:
    """The same courtesy for ``index.html``, whose header explains its own rules."""

    return re.sub(
        r"<!--.*?-->",
        lambda match: "\n" * match.group(0).count("\n"),
        source,
        flags=re.DOTALL,
    )


def _strip_css_comments(source: str) -> str:
    return re.sub(
        r"/\*.*?\*/",
        lambda match: "\n" * match.group(0).count("\n"),
        source,
        flags=re.DOTALL,
    )


# -------------------------------------------------------------------- scanning


def _sinks_in(label: str, text: str) -> list[str]:
    """Every sink in ``text``, reported as ``file:line — the rule broken``."""

    found: list[str] = []
    for name, pattern in _SINKS:
        for match in pattern.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            found.append(f"{label}:{line} — {name} ({match.group(0)!r})")
    return found


def _tags(html: str) -> list[str]:
    return re.findall(r"<[a-zA-Z][^>]*>", html)


# ------------------------------------------------------------ the shipped client


def test_the_shipped_client_assigns_no_markup_and_evaluates_no_string() -> None:
    """The comment at the top of ``terminal.js``, turned into a failing build.

    Every entry in :data:`_SINKS` is a way for a string the backend did not
    author — a model's refusal narrative, an operator's own note — to stop being
    characters on a screen and become instructions to a browser sitting
    same-origin with the order routes.
    """

    offenders = _sinks_in("terminal.js", _strip_js_comments(_JS.read_text(encoding="utf-8")))
    offenders += _sinks_in("index.html", _strip_html_comments(_HTML.read_text(encoding="utf-8")))
    offenders += _sinks_in("terminal.css", _strip_css_comments(_CSS.read_text(encoding="utf-8")))
    assert offenders == [], "the terminal client grew an HTML or eval sink:\n" + "\n".join(
        offenders
    )


def test_the_shell_declares_no_inline_event_handler() -> None:
    """``onclick=`` in markup is script in an attribute, and the CSP forbids it.

    Worth its own test rather than folding into the scan above: an inline handler
    is the one sink that needs no JavaScript file to reach, and it is also the
    one that would silently stop working the day the Content-Security-Policy in
    ``chronos.api.main`` starts being enforced — a handler that fails closed in
    the browser is still a handler nobody meant to ship.
    """

    html = _strip_html_comments(_HTML.read_text(encoding="utf-8"))
    offenders = [tag for tag in _tags(html) if re.search(r"(?i)\son[a-z]+\s*=", tag)]
    assert offenders == [], f"the shell declares an inline event handler: {offenders}"


def test_the_client_loads_nothing_from_another_origin() -> None:
    """Offline is a property of this page, not an accident of the network.

    A CDN request would be an outbound connection from the process that holds the
    broker session, and it would make the operator's window depend on a third
    party being up at the moment they most need to look at it. ``index.html``
    already says so in prose; this is the same claim in a form that fails.
    """

    sources = {
        "terminal.js": _strip_js_comments(_JS.read_text(encoding="utf-8")),
        "terminal.css": _strip_css_comments(_CSS.read_text(encoding="utf-8")),
        "index.html": _strip_html_comments(_HTML.read_text(encoding="utf-8")),
    }
    offenders = [
        f"{label}:{text.count(chr(10), 0, match.start()) + 1} — {match.group(0)!r}"
        for label, text in sources.items()
        for match in _EXTERNAL_ORIGIN.finditer(text)
    ]
    assert offenders == [], "the terminal client references an external origin:\n" + "\n".join(
        offenders
    )

    # And every reference it *does* make stays under the one prefix index.html
    # documents, so "same-origin" is a path and not just an absent hostname.
    references = _SHELL_REFERENCE.findall(sources["index.html"])
    assert references, "the shell references nothing at all, so this test proved nothing"
    assert [ref for ref in references if not ref.startswith("/terminal/static/")] == []

    assert _CSS_URL.findall(sources["terminal.css"]) == []

    # The same pattern, shown finding what it is for. An origin check that
    # matched nothing would pass on a page full of CDN tags.
    for planted in (
        '<script src="https://cdn.example.com/x.js"></script>',
        "@import url(http://fonts.example.com/f.css);",
        'fetch("//metrics.example.com/beacon");',
    ):
        assert _EXTERNAL_ORIGIN.search(planted), planted


def test_the_scan_reads_a_file_that_still_contains_its_own_description() -> None:
    """The stripper must be doing work, and the code must survive it.

    Both halves have failed in other people's versions of this test: a scan whose
    patterns match nothing anywhere passes forever, and a stripper that ate the
    file passes just as quietly. So the raw client is asserted to contain exactly
    the comment the scan would trip on, and the stripped client is asserted to
    still contain the mechanism the comment describes.
    """

    raw = _JS.read_text(encoding="utf-8")
    assert _sinks_in("terminal.js", raw), "terminal.js no longer documents what it avoids"

    stripped = _strip_js_comments(raw)
    assert "textContent" in stripped, "the stripper removed the client along with its comments"
    assert stripped.count("\n") == raw.count("\n"), "line numbers no longer line up"


# ------------------------------------------------- the stripper, pinned in turn


def test_stripping_comments_does_not_hide_a_real_sink() -> None:
    """A sink placed exactly where a naive stripper would lose it is still found.

    Each case is a way the cheap version of this file goes wrong. A regex literal
    containing ``//`` looks like the start of a line comment; a string containing
    ``/*`` looks like the start of a block comment; a nested template literal
    ends the outer one early. In every one of them the sink sits *after* the
    trap, which is where the code that would have been swallowed lives.
    """

    cases = {
        "a line comment": "// we never touch innerHTML\nconst a = 1;\nel.innerHTML = a;",
        "a block comment": "/* innerHTML\n   and document.write */\nel.innerHTML = a;",
        "a regex holding a slash pair": "const re = /a\\/\\/b/;\nel.innerHTML = a;",
        "a string holding a comment opener": 'const s = "/* not a comment";\nel.innerHTML = s;',
        "a template holding a comment opener": "const s = `// not a comment`;\nel.innerHTML = s;",
        "a nested template": "const s = `${x ? `//` : `y`}`;\nel.innerHTML = s;",
        "a divided value": "const n = total / 2 / 3;\nel.innerHTML = n;",
    }
    for label, source in cases.items():
        found = _sinks_in("case", _strip_js_comments(source))
        assert found, f"the sink after {label} was stripped away with the comment"

    # And the comment on its own is not a finding, which is the whole reason the
    # stripper exists: the file is allowed to say what it refuses to do.
    assert _sinks_in("case", _strip_js_comments("// nothing here assigns innerHTML\n")) == []
    assert _sinks_in("case", _strip_js_comments("/* no eval(, no document.write */\n")) == []


def test_every_sink_pattern_catches_the_thing_it_is_named_for() -> None:
    """A pattern that matches nothing is a rule that is not being enforced.

    ``_SINKS`` is the list an editor reaches for when adding a rule, and a typo
    in a regex there is invisible: the scan keeps passing on a clean file whether
    the pattern works or not.
    """

    samples = (
        "node.innerHTML = value;",
        "node.outerHTML = value;",
        "node.insertAdjacentHTML('beforeend', value);",
        "document.write(value);",
        "eval(value);",
        "new Function(value)();",
        '<a href="javascript:alert(1)">x</a>',
        'node.setAttribute("onclick", value);',
    )
    unmatched = [
        name for name, pattern in _SINKS if not any(pattern.search(sample) for sample in samples)
    ]
    assert unmatched == [], f"these sink patterns match nothing: {unmatched}"
