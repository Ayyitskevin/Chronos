"""Every variable `.env.example` advertises must actually be read by something.

A configuration variable that looks like a control and controls nothing is the
same shape as the four inert kernel defects this repository was burned by
(R-24..R-27): wired, documented, and structurally unable to act. The config layer
had five of them until 2026-08-02, and one — `PAPER_ACCOUNT_ALLOWLIST` — was a
phantom of a *real* safety gate. `chronos.control.modes` genuinely refuses paper
execution when the paper allowlist is empty, but it is fed from
`settings.ib_account_allowlist`; an operator who set the phantom variable and
believed they had allowlisted an account had changed nothing.

Fail-closed meant the worst case was a refusal rather than a wrong account. That
is luck about which direction the mistake pointed, not a property of the design,
and it is exactly the reasoning R-25 shows to be unreliable — that one WAS the
fail-open member of the set.

This test is structural rather than a review habit for the same reason the
transmit-site inventory is: the next phantom will be added by someone who did not
read this docstring.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_EXAMPLE = REPO_ROOT / ".env.example"
SETTINGS_FILE = REPO_ROOT / "src" / "chronos" / "config" / "settings.py"
SEARCH_ROOTS = (REPO_ROOT / "src", REPO_ROOT / "scripts")

#: `KEY=value` at the start of a line. Comments and blanks are ignored, so the
#: explanatory prose above each block costs nothing here.
_ASSIGNMENT = re.compile(r"^([A-Z][A-Z0-9_]*)=", re.MULTILINE)


def _declared_variables() -> list[str]:
    return _ASSIGNMENT.findall(ENV_EXAMPLE.read_text(encoding="utf-8"))


def _python_sources() -> list[str]:
    texts: list[str] = []
    for root in SEARCH_ROOTS:
        for path in root.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            texts.append(path.read_text(encoding="utf-8", errors="replace"))
    return texts


def _is_read(name: str, settings_text: str, sources: list[str]) -> bool:
    """A variable counts as read if pydantic-settings maps it, or code names it.

    pydantic-settings resolves `FOO_BAR` to a field named `foo_bar` case-
    insensitively, so a matching field declaration is the mapping. Direct
    `os.environ` / `os.getenv` lookups are the other legitimate route — several
    variables here are read that way on purpose (the demo profile, the opt-in
    smoke-test switch, the monitor's own settings) and must not be flagged.
    """

    if re.search(rf"^\s+{name.lower()}\s*:", settings_text, re.MULTILINE):
        return True
    return any(name in text for text in sources)


def test_env_example_declares_no_variable_that_nothing_reads() -> None:
    settings_text = SETTINGS_FILE.read_text(encoding="utf-8")
    sources = _python_sources()

    phantoms = [
        name for name in _declared_variables() if not _is_read(name, settings_text, sources)
    ]

    assert not phantoms, (
        "these variables are advertised in .env.example but are read by nothing — "
        "not a Settings field, not an os.environ lookup. Setting them does nothing, "
        "which is how a phantom safety control gets born (see this module's "
        f"docstring and R-24..R-27): {sorted(phantoms)}"
    )


def test_the_five_removed_phantoms_stay_gone() -> None:
    """A named regression guard for the specific set removed on 2026-08-02.

    The general test above would catch them again, but naming them keeps the
    reason discoverable from a failure message rather than from git history.
    """

    removed = {
        "PLATFORM_HALT_FILE",
        "PLATFORM_AUDIT_FILE",
        "PLATFORM_LEDGER_DB",
        "PLATFORM_RISK_POLICY",
        "PAPER_ACCOUNT_ALLOWLIST",
    }
    declared = set(_declared_variables())
    returned = removed & declared
    assert not returned, (
        "these were removed on 2026-08-02 because nothing read them; "
        "PAPER_ACCOUNT_ALLOWLIST in particular impersonated the real gate fed by "
        f"IB_ACCOUNT_ALLOWLIST. Re-adding one means wiring it first: {sorted(returned)}"
    )


def test_the_real_account_allowlist_is_still_advertised() -> None:
    """The phantom's removal must not have taken the genuine control with it."""

    assert "IB_ACCOUNT_ALLOWLIST" in set(_declared_variables())
