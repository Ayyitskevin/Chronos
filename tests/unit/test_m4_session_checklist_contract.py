"""docs/ops/m4-read-only-gate-session-checklist.md cannot drift from VCP §7 or the source.

The checklist is owner-facing material for the real-gateway read-only gate. It must name
every evidence category the gate names, cite only commands, scripts, documents and source
paths that exist, name only settings flags that exist with the safe defaults it claims,
say plainly that no gateway has ever been connected and that it has not been rehearsed, and
mark as ``[GAP]`` only mechanisms the repository really lacks. Each pin reads the plan, the
source or the tree at test time — nothing is hard-coded — so a VCP edit, a renamed script,
a new Settings default, or a tool that quietly appears fails here, not in an owner session.

Daybreak's HOLD at 6ab2016 added two safety pins: the checklist may never call
``scripts/paper_soak_report.py`` read-only (its ``Database.initialize()`` creates a schema on
an empty target) and every database command it labels read-only must open read-only on a
snapshot; and the ``ALLOW_LIVE_TRADING`` sentence must carry its qualifier — a bare true is
refused, the full live conjunction is accepted by design (``tests/unit/test_settings.py``).
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from chronos.config.settings import Settings
from chronos.domain.enums import BrokerMode

ROOT = Path(__file__).resolve().parents[2]
CHECKLIST = ROOT / "docs" / "ops" / "m4-read-only-gate-session-checklist.md"
PLAN = ROOT / "docs" / "VISION_COMPLETION_PLAN.md"
SEARCH_ROOTS = ("src", "scripts", "tests", "docs", ".claude/skills")

_GATE_CATEGORIES = re.compile(r"capture sanitized evidence for (.*?)\.")
_CITED_PATH = re.compile(r"`((?:docs|src|scripts|tests|\.claude)/[A-Za-z0-9_./-]+)`")
_CLI_COMMAND = re.compile(r"`python -m chronos\.cli ([^`]+)`")
_FLAG_ROW = re.compile(r"^\| `([A-Z_]+)` \| (.+?) \| (.+?) \|$", re.M)
# The marker is written `[GAP]`; the first backticked token after it names the absent thing.
_GAP_TOKEN = re.compile(r"\[GAP\]`?[^`]*`([^`]+)`")


def _checklist() -> str:
    return CHECKLIST.read_text(encoding="utf-8")


def _collapsed(text: str) -> str:
    return " ".join(text.split())


def _section(text: str, start: str, stop: str) -> str:
    begin = text.index(start)
    end = text.index(stop, begin)
    return text[begin:end]


def _gap_lines() -> list[str]:
    """Every [GAP] line after the preamble (the preamble defines the marker, once)."""

    body = _checklist().split("\n## 1.", 1)[1]
    return [line for line in body.splitlines() if "[GAP]" in line]


def _gap_tokens() -> set[str]:
    """One absent mechanism per [GAP] marker: the first backticked token after each."""

    tokens: set[str] = set()
    for line in _gap_lines():
        found = [match.group(1) for match in _GAP_TOKEN.finditer(line)]
        assert len(found) == line.count("[GAP]"), (
            f"a [GAP] marker names no backticked mechanism: {line!r}"
        )
        tokens.update(found)
    return tokens


def _under_a_gap_path(path: str, gaps: set[str]) -> bool:
    clean = path.rstrip("/")
    return any(clean == gap.rstrip("/") or clean.startswith(gap.rstrip("/") + "/") for gap in gaps)


# ------------------------------------------------ (a) every §7 category, parsed from the plan


def _plan_categories() -> list[str]:
    plan = _collapsed(PLAN.read_text(encoding="utf-8"))
    match = _GATE_CATEGORIES.search(plan)
    assert match is not None, "VCP §7 gate paragraph not found"
    items = [item.strip() for item in match.group(1).split(", ")]
    items[-1] = items[-1].removeprefix("and ")
    return items


def test_every_gate_category_is_a_row_of_the_per_session_capture_table() -> None:
    categories = _plan_categories()
    assert len(categories) >= 10, categories
    table = _collapsed(_section(_checklist(), "### 4.2", "### 4.3"))
    for category in categories:
        assert f"| {category} |" in table, category


# ------------------------------------------------ (b) every cited command and path exists


def test_every_cited_repository_path_exists_unless_declared_a_gap() -> None:
    cited = set(_CITED_PATH.findall(_checklist()))
    assert cited, "no repository paths cited"
    gaps = {token for token in _gap_tokens() if "/" in token}
    for path in sorted(cited):
        if _under_a_gap_path(path, gaps):
            continue
        assert (ROOT / path).exists(), path


def test_every_cited_cli_command_parses_and_its_options_exist() -> None:
    commands = _CLI_COMMAND.findall(_checklist())
    assert commands, "no chronos.cli commands cited"
    for command in commands:
        tokens = command.split()
        subcommand = tokens[0]
        completed = subprocess.run(
            [sys.executable, "-m", "chronos.cli", subcommand, "--help"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 0, (command, completed.stderr)
        for option in (token for token in tokens[1:] if token.startswith("--")):
            assert option in completed.stdout, (subcommand, option)


# ------------------------------------------------ (c) every named flag is real, default safe


def test_every_flag_in_the_flags_table_is_a_settings_field_with_the_claimed_default() -> None:
    rows = _FLAG_ROW.findall(_section(_checklist(), "## 2.", "## 3."))
    assert rows, "flags table not found"
    expected_defaults = {"`false`": False, "unset": None, "`demo`": BrokerMode.DEMO}
    for name, _required, default_cell in rows:
        field = Settings.model_fields.get(name.lower())
        assert field is not None, name
        assert default_cell in expected_defaults, (name, default_cell)
        assert field.default == expected_defaults[default_cell], (name, field.default)


# ------------------------------------------------ (d) the two honesty sentences


def test_the_checklist_says_no_gateway_was_ever_connected_and_it_is_unrehearsed() -> None:
    text = _collapsed(_checklist())
    assert "no real IBKR gateway — paper or live — has ever been connected to Chronos" in text
    assert "This checklist has not been rehearsed against a real gateway" in text


# ------------------------------------------------ (e) every [GAP] names something absent


def _tree_files() -> list[Path]:
    files: list[Path] = []
    for root in SEARCH_ROOTS:
        for path in (ROOT / root).rglob("*"):
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
                files.append(path)
    return files


def test_every_gap_line_names_a_mechanism_the_repository_lacks() -> None:
    tokens = _gap_tokens()
    assert tokens, "no [GAP] lines"
    own = {CHECKLIST.resolve(), Path(__file__).resolve()}
    files = [path for path in _tree_files() if path.resolve() not in own]
    for token in sorted(tokens):
        if "/" in token:
            assert not (ROOT / token).exists(), f"[GAP] path exists now: {token}"
            continue
        holders = [
            path.relative_to(ROOT).as_posix()
            for path in files
            if token in path.read_text(encoding="utf-8", errors="ignore")
        ]
        assert holders == [], f"[GAP] mechanism {token!r} now appears in {holders}"


# ------------------------------------------ (f) the database check is genuinely read-only


def test_the_soak_report_is_never_called_read_only_and_its_side_effect_is_stated() -> None:
    lines = _checklist().splitlines()
    mentions = [index for index, line in enumerate(lines) if "paper_soak_report" in line]
    assert mentions, "the checklist no longer mentions scripts/paper_soak_report.py"
    for index in mentions:
        window = " ".join(lines[max(0, index - 2) : index + 3]).lower()
        assert "read-only" not in window and "read only" not in window, lines[index]
    assert "initializes a schema on an empty target" in _collapsed(_checklist())


def test_every_database_command_opens_read_only_on_a_snapshot() -> None:
    commands = [line for line in _checklist().splitlines() if "sqlite3 " in line]
    assert commands, "no sqlite3 command in the checklist (the read-only check is gone)"
    for line in commands:
        assert "-readonly" in line or "mode=ro" in line, line
        assert "snapshot" in line, line


# ------------------------------------------ (g) the live-flag sentence keeps its qualifier


def test_the_live_flag_sentence_carries_its_qualifier() -> None:
    text = _collapsed(_checklist())
    assert "`ALLOW_LIVE_TRADING=true` by itself" in text
    assert "full live conjunction" in text
    assert "does not enable anything" not in text


# The source proves a CONFIGURATION property (validation accepts; live_transmission_possible is
# True), never that a process starts or fails to start: build_runtime can still fail after
# settings load (database, adapter construction, broker connection, account scope).
_PROCESS_CLAIMS = (
    "the process starts",
    "process never starts",
    "starts live-capable",
    "the process does not start",
    "refuses to start",
)


def test_the_live_flag_passage_claims_a_configuration_property_not_a_process_outcome() -> None:
    text = _collapsed(_checklist())
    start = text.index("`ALLOW_LIVE_TRADING=true` by itself")
    passage = text[start : text.index("A present, valid `AUTONOMY_MANDATE_FILE`", start)]
    for claim in _PROCESS_CLAIMS:
        assert claim not in passage, claim
    assert "`live_transmission_possible` is true" in passage
    assert "says nothing about whether a process starts" in passage
    # The port is the adapter's check, not a settings conjunct.
    assert "a live port" not in passage
    assert "`verify_environment_port`" in passage
