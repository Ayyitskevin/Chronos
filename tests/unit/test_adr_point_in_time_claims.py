"""ADRs and operator docs must not cache facts that move underneath them (#169).

`tests/unit/test_operator_skill_contract.py` already forbids point-in-time claims
in `SKILL.md`. This extends the same idea to `docs/adr/` and the operator-facing
`docs/*.md`, for the two classes that actually went stale on this repository:

- **A current schema version.** ADR-0055 carried "`SCHEMA_VERSION` 12 is
  unchanged". Migration 0012 took the version to 13 and the sentence was false
  from that commit; a doc sweep found it three ADRs later (#168). ADR-0057 makes
  the identical point as "`SCHEMA_VERSION` untouched" and cannot go stale — that
  is the form these tests push toward.
- **A backend port literal.** `docs/BACKUP_AND_RECOVERY.md` gave an operator a
  `curl` against `127.0.0.1:8000` while the backend's default is 8765, so the
  documented way to clear a recovery hold connected to nothing (#168, F-2).

## Two limits, stated rather than discovered

The transition exemption is **line-scoped**: a stale claim sharing a line with a
transition is not flagged, because the transition exempts the whole line. Contrived,
and the line scoping is what keeps the exemption simple to read.

``_LOOPBACK_PORT`` recognises ``127.0.0.1`` and ``localhost`` only. ``0.0.0.0:8000``,
a bare hostname, and a space after the colon are invisible to it. That is the width
of this check, not an oversight — widening it is a separate change with its own
false-positive question.

The copula list is finite, so a sentence that reaches the number through a noun —
"the ``SCHEMA_VERSION`` constant is 13" — is not flagged. The listed forms are the
ones an ADR author writes; this one is recorded so the next reader knows the edge
exists rather than inferring coverage the pattern does not have.

## What is deliberately NOT forbidden

"Schema v11 makes binding atomic and replayable" (ADR-0036) is a heading about
what *that ADR did*. It is permanently true and there are four like it. The rule
therefore binds ``SCHEMA_VERSION`` — the name of the live constant — and not the
phrase "schema vN", because only the former asserts a current value.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from chronos.config.settings import Settings

REPO_ROOT = Path(__file__).resolve().parents[2]
ADR_DIR = REPO_ROOT / "docs" / "adr"

#: ``SCHEMA_VERSION`` followed by a number: an assertion about the live constant's
#: current value. Punctuation, backticks and the copulas an author actually writes
#: are spanned, so "`SCHEMA_VERSION` 12", "SCHEMA_VERSION = 13", "SCHEMA_VERSION: 13",
#: "SCHEMA_VERSION is 12" and "SCHEMA_VERSION is now 13" all match.
#:
#: The first version of this pattern was ``[^0-9A-Za-z]{0,6}``, which cannot span the
#: letters in "is" — while this module's own docstring claimed it could. A guard whose
#: description overstates its reach is the same defect the guard exists to refuse, so
#: the copulas are listed here explicitly and the claim above is the tested one.
_SCHEMA_VERSION_NUMBER = re.compile(
    r"SCHEMA_VERSION`?(?:[^0-9A-Za-z]{0,3}(?:is|now|at|stays|=|:)){0,3}[^0-9A-Za-z]{0,3}\d"
)

#: The one form that names numbers and still cannot go stale: a transition, as in
#: "migration 0008, SCHEMA_VERSION 8 -> 9". That records what a past migration did
#: and stays true forever, so it is allowed. Only an assertion about the CURRENT
#: value ages, which is the whole distinction this module draws.
_SCHEMA_VERSION_TRANSITION = re.compile(r"SCHEMA_VERSION[^\n]{0,12}\d+\s*(?:->|\u2192)\s*\d+")

#: A loopback host:port in prose or a shell example.
_LOOPBACK_PORT = re.compile(r"(?:127\.0\.0\.1|localhost):(\d+)")

#: Ports a doc may legitimately name that are not this backend. 11434 is the
#: local model server (Ollama) reached by the worker, a different process with
#: its own default — see ADR-0050 and docs/model_worker.md.
_NON_BACKEND_PORTS = frozenset({"11434"})


def _docs() -> list[Path]:
    """Every ADR, plus the operator-facing docs at the top of `docs/`."""

    return sorted([*ADR_DIR.glob("ADR-*.md"), *(REPO_ROOT / "docs").glob("*.md")])


def test_the_document_set_is_not_empty() -> None:
    """An empty glob would make every assertion below vacuously true.

    Each glob is asserted separately. A single combined floor passes while one of
    the two contributes nothing — 57 ADRs alone clear any total this test would
    reasonably set, so losing all 53 top-level docs would not be noticed.
    """

    adrs = sorted(ADR_DIR.glob("ADR-*.md"))
    top_level = sorted((REPO_ROOT / "docs").glob("*.md"))

    assert len(adrs) > 20, f"only {len(adrs)} ADRs matched; the docs/adr glob is wrong"
    assert len(top_level) > 20, f"only {len(top_level)} top-level docs matched; that glob is wrong"
    assert set(_docs()) == set(adrs) | set(top_level)


@pytest.mark.parametrize("document", _docs(), ids=lambda p: p.name)
def test_no_document_states_a_current_schema_version(document: Path) -> None:
    """The number moves with the next migration; the claim does not move with it.

    Say "`SCHEMA_VERSION` untouched" or "no migration" instead — ADR-0057 does,
    and it is the only phrasing here that survived migration 0012.
    """

    offenders = [
        f"{document.relative_to(REPO_ROOT)}:{number}: {line.strip()}"
        for number, line in enumerate(document.read_text(encoding="utf-8").splitlines(), 1)
        if _SCHEMA_VERSION_NUMBER.search(line) and not _SCHEMA_VERSION_TRANSITION.search(line)
    ]
    assert not offenders, "a document names SCHEMA_VERSION's current value:\n" + "\n".join(
        offenders
    )


@pytest.mark.parametrize("document", _docs(), ids=lambda p: p.name)
def test_no_document_names_a_backend_port_that_disagrees_with_the_default(
    document: Path,
) -> None:
    """Compared against the setting, never against another literal.

    A second copy of "8765" in this file would agree with itself while both
    drifted from `Settings`. The default is read from the field so that moving it
    moves this check too.
    """

    backend_port = str(Settings.model_fields["backend_port"].default)
    offenders = [
        f"{document.relative_to(REPO_ROOT)}:{number}: {line.strip()}"
        for number, line in enumerate(document.read_text(encoding="utf-8").splitlines(), 1)
        for port in _LOOPBACK_PORT.findall(line)
        if port != backend_port and port not in _NON_BACKEND_PORTS
    ]
    assert not offenders, (
        f"a document names a loopback port that is neither the backend default "
        f"({backend_port}) nor a known other service:\n" + "\n".join(offenders)
    )
