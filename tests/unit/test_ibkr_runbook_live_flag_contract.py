"""docs/IBKR_RUNBOOK.md's live-flag sentence and docs/ops/README.md's index say what is.

Daybreak's DOC-2 review found the runbook saying "`ALLOW_LIVE_TRADING=true` does not enable
anything: settings validation raises and the process refuses to start". A bare true flag is
refused, but the full live conjunction is accepted by design (`validate_safety_and_ranges`,
``src/chronos/config/settings.py``) and ``live_transmission_possible`` is then True — the
executable proof is ``tests/unit/test_settings.py``. An operator reading the absolute
sentence would believe the flag inert; it is not. These pins hold the runbook to the
qualifier and hold the qualifier to the validator's own words.

``docs/ops/README.md`` defined the directory solely as service-unit templates; operator
checklists now also live there. The index pin is conditional on presence: every markdown
file in ``docs/ops/`` other than the README must be named by the README, and the README must
already name the M4 checklist so the pin is exercised even while that file is on another
branch (a vacuous pass would prove nothing).
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUNBOOK = ROOT / "docs" / "IBKR_RUNBOOK.md"
SETTINGS = ROOT / "src" / "chronos" / "config" / "settings.py"
SETTINGS_TESTS = ROOT / "tests" / "unit" / "test_settings.py"
OPS = ROOT / "docs" / "ops"
OPS_README = OPS / "README.md"

ABSOLUTE_FORM = "does not enable anything"
QUALIFIER = "`ALLOW_LIVE_TRADING=true` by itself"
PASSAGE_END = "Never put IBKR usernames"
M4_CHECKLIST = "m4-read-only-gate-session-checklist.md"
# The source proves a CONFIGURATION property (validation accepts; live_transmission_possible is
# True), never that a process starts or fails to start: build_runtime can still fail after
# settings load (database, adapter construction, broker connection, account scope).
PROCESS_CLAIMS = (
    "the process starts",
    "process never starts",
    "starts live-capable",
    "the process does not start",
    "refuses to start",
)


def _collapsed(path: Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").split())


def _live_flag_passage() -> str:
    text = _collapsed(RUNBOOK)
    start = text.index(QUALIFIER)
    return text[start : text.index(PASSAGE_END, start)]


# ------------------------------------------------ (a) the runbook's live-flag sentence


def test_the_runbook_does_not_carry_the_absolute_live_flag_sentence() -> None:
    assert ABSOLUTE_FORM not in _collapsed(RUNBOOK)


def test_the_runbook_qualifies_the_live_flag_by_the_conjunction() -> None:
    text = _collapsed(RUNBOOK)
    assert QUALIFIER in text
    assert "full live conjunction" in text
    assert "`validate_safety_and_ranges`" in text
    assert "`tests/unit/test_settings.py`" in text


def test_the_live_flag_passage_claims_a_configuration_property_not_a_process_outcome() -> None:
    passage = _live_flag_passage()
    for claim in PROCESS_CLAIMS:
        assert claim not in passage, claim
    assert "`live_transmission_possible` is true" in passage
    assert "says nothing about whether a process starts" in passage


def test_the_qualifier_matches_the_validator_and_its_executable_proof() -> None:
    settings = SETTINGS.read_text(encoding="utf-8")
    assert "def validate_safety_and_ranges(" in settings
    assert "requires the full live conjunction" in settings
    assert "def live_transmission_possible(" in settings
    tests = SETTINGS_TESTS.read_text(encoding="utf-8")
    assert "def test_full_live_conjunction_is_accepted_and_live_transmission_possible(" in tests
    # The port is NOT a settings conjunct: the adapter refuses a mismatch at construction.
    assert (
        "ib_port"
        not in settings[settings.index("def validate_safety_and_ranges(") :].split("def ", 2)[1]
    )
    adapter = (ROOT / "src" / "chronos" / "broker" / "official_ibkr.py").read_text(encoding="utf-8")
    assert "def verify_environment_port(" in adapter
    assert "`verify_environment_port`" in _collapsed(RUNBOOK)


# ------------------------------------------------ (b) docs/ops/README.md indexes checklists


def test_the_ops_readme_admits_operator_checklists_and_names_the_m4_one() -> None:
    readme = _collapsed(OPS_README)
    assert "Operator checklists also live here" in readme
    assert f"`{M4_CHECKLIST}`" in readme  # positive control for the conditional pin below


def test_every_checklist_present_in_docs_ops_is_named_by_the_readme() -> None:
    readme = OPS_README.read_text(encoding="utf-8")
    for path in sorted(OPS.glob("*.md")):
        if path.name == "README.md":
            continue
        assert f"`{path.name}`" in readme, f"docs/ops/{path.name} is not indexed in the README"
