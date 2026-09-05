"""The `docs/ops/` unit templates ship a shape, never someone's configuration.

These templates are the kind of file where a convenience default survives unread:
nothing imports them, nothing runs them, and CI never installs them, so the only
thing standing between "a reviewed shape" and "an operator's filled-in unit with a
live flag in it" is this test.

Each rule below is one the templates would otherwise merely assert about themselves
in a comment. `docs/ops/README.md` states them for a human; this states them for the
gate.
"""

from __future__ import annotations

import configparser
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
OPS = ROOT / "docs/ops"
TEMPLATES = sorted(OPS.glob("*.service"))

#: Set to true, any of these turns an inert template into a forwarding one. The
#: flags themselves are owner-only (docs/AGENT_PROTOCOL.md §9).
_FORWARD_TRUE = re.compile(r"_FORWARD\s*=\s*(?:true|1|yes|on)\b", re.IGNORECASE)

#: Settings that make the order plane live-capable. `Settings` reads the
#: environment case-insensitively with no prefix, so these names are exactly what
#: a unit file would carry.
_LIVE_CAPABLE = ("ALLOW_ORDER_TRANSMIT", "ALLOW_LIVE_TRADING")

#: `Environment=`/`EnvironmentFile=` values are the only place a path belongs, and
#: every one of them must be rooted at systemd's `%h` specifier followed by a
#: placeholder. Anything else is a real path someone left behind.
_PATH_DIRECTIVES = ("WorkingDirectory", "EnvironmentFile", "ExecStart", "ReadWritePaths")


def test_there_are_templates_to_check() -> None:
    """Guard the guard: an empty glob would make every test below vacuous."""

    assert TEMPLATES, "docs/ops/*.service is empty; the rules below would check nothing"


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda path: path.name)
def test_a_template_parses_as_a_systemd_unit(template: Path) -> None:
    """Sections systemd requires, present and spelled right."""

    # `interpolation=None`: systemd's `%h` specifier is not Python's `%(name)s`,
    # and the default interpolation refuses it. `strict=False`: systemd allows
    # repeated keys (ReadWritePaths, Environment) and configparser does not.
    # `optionxform = str`: directive names are case-sensitive in unit files.
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str  # type: ignore[method-assign]
    parser.read_string(template.read_text(encoding="utf-8"))
    assert {"Unit", "Service", "Install"} <= set(parser.sections())
    assert parser["Service"].get("ExecStart", "").strip()


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda path: path.name)
def test_a_template_carries_no_absolute_path_outside_a_placeholder(template: Path) -> None:
    """A real path in a template is a template that was filled in and committed."""

    offenders: list[str] = []
    for raw in template.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() not in _PATH_DIRECTIVES:
            continue
        for token in value.split():
            if token.startswith("/"):
                offenders.append(line)
                break
    assert not offenders, f"absolute path outside a placeholder: {offenders}"


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda path: path.name)
def test_a_template_never_enables_forwarding(template: Path) -> None:
    """`*_FORWARD` is an owner act; a shipped template must not pre-make it."""

    text = template.read_text(encoding="utf-8")
    assert not _FORWARD_TRUE.search(text), "a template sets a *_FORWARD flag true"


def test_the_worker_template_unsets_the_forwarding_variable() -> None:
    """`UnsetEnvironment=` is the only thing that makes the inert default a guarantee.

    `EnvironmentFile=` overrides `Environment=` (systemd.exec(5)), so a unit that
    merely stated `CHRONOS_WORKER_FORWARD=false` could be flipped by an edit to the
    private environment file — which is not in this repository and which no test can
    see. `UnsetEnvironment=` is applied last and removes the variable outright, so
    the worker falls back to its own default of False, and turning forwarding on
    becomes an edit to the reviewed unit.
    """

    worker = OPS / "chronos-worker.service"
    assert worker.exists(), "the worker template is the one this rule is about"
    directives = [
        line.strip()
        for line in worker.read_text(encoding="utf-8").splitlines()
        if not line.strip().startswith("#")
    ]
    assert "UnsetEnvironment=CHRONOS_WORKER_FORWARD" in directives


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda path: path.name)
def test_a_template_names_no_live_capable_setting(template: Path) -> None:
    """Absence is the control: these must not appear, set to anything at all."""

    for raw in template.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("#"):
            continue
        for name in _LIVE_CAPABLE:
            assert name not in line, f"{template.name} names {name}"
        if re.search(r"\bIB_ENVIRONMENT\s*=\s*LIVE\b", line, re.IGNORECASE):
            raise AssertionError(f"{template.name} sets IB_ENVIRONMENT=LIVE")
        broker = re.search(r"\bBROKER_MODE\s*=\s*(\S+)", line, re.IGNORECASE)
        if broker is not None:
            assert broker.group(1).lower() == "demo", (
                f"{template.name} sets BROKER_MODE={broker.group(1)}; the campaign is demo-only"
            )
