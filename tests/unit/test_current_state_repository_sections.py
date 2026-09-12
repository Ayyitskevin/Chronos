"""The generated current-state page also carries the repository's milestone state.

VCP §5 asks for ONE generated current-state page so that "historical documents retain
history but cannot present old milestone state as current truth". The committed
capability matrix (PR #118) is that page's first half; this file pins the second half —
everything a reader used to get from HANDOFF.md/TASKS.md prose, derived from the tree:

- volatile facts (default branch, HEAD, test counts) are COMMANDS, never copied values
  (docs/AGENT_PROTOCOL.md: "copied values are how sixteen skills went stale");
- the D / ADR / R watermarks, by the §7 scans;
- RISK_REGISTER.md status counts and every OPEN row;
- the VCP §6 findings with a status read mechanically from the plan's own markers, where
  anything the reader cannot classify is UNKNOWN — never closed;
- the public ``chronos.auditlog`` names (what this week added, visible to a future reader);
- the forwarding flags as DECLARED in source (present, default), never their values.

The generator stays a pure function of committed source: no git, no environment. That is
why the page carries the command for HEAD rather than HEAD.
"""

from __future__ import annotations

import hashlib
import importlib.util
import re
from pathlib import Path
from types import ModuleType

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

import chronos.auditlog as auditlog_pkg

ROOT = Path(__file__).resolve().parents[2]
GENERATOR = ROOT / "scripts" / "build_current_state.py"
CURRENT_STATE = ROOT / "docs" / "generated" / "CURRENT_STATE.md"
DECISIONS = ROOT / "DECISIONS.md"
REGISTER = ROOT / "RISK_REGISTER.md"
PLAN = ROOT / "docs" / "VISION_COMPLETION_PLAN.md"
ADR_DIR = ROOT / "docs" / "adr"
MAKEFILE = ROOT / "Makefile"
HANDOFF = ROOT / "HANDOFF.md"
TASKS = ROOT / "TASKS.md"


def _generator() -> ModuleType:
    spec = importlib.util.spec_from_file_location("build_current_state", GENERATOR)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _page() -> str:
    return CURRENT_STATE.read_text(encoding="utf-8")


def _section(page: str, heading: str) -> str:
    """The text under ``heading`` up to the next heading of the same or higher level."""

    level = heading.split(" ", 1)[0]
    start = page.index(heading + "\n")
    rest = page[start + len(heading) + 1 :]
    stop = re.search(rf"^#{{1,{len(level)}}} ", rest, re.M)
    return rest[: stop.start()] if stop else rest


# --------------------------------------------------------------- volatile facts are commands


def test_volatile_facts_are_commands_never_values() -> None:
    page = _page()
    volatile = _section(page, "### Volatile facts — run, do not copy")
    assert "`git ls-remote --symref origin HEAD`" in volatile
    assert "`git rev-parse HEAD`" in volatile
    assert "`make gates`" in volatile
    # No copied HEAD (a 40-hex WORD; the 64-hex fingerprint cells cannot match it) and no
    # copied test count anywhere on the page.
    assert not re.search(r"\b[0-9a-f]{40}\b", page), "a 40-hex word looks like a copied HEAD"
    assert not re.search(r"\b\d+ passed\b", page)
    assert not re.search(r"\d+ / \d+ / \d+", page)


# --------------------------------------------------------------------------- ID watermarks


def _scan_max(text: str, pattern: str) -> int:
    return max(int(match) for match in re.findall(pattern, text, re.M))


def test_id_watermarks_match_the_protocol_scans() -> None:
    page = _page()
    d_max = _scan_max(DECISIONS.read_text(encoding="utf-8"), r"^\| D-(\d+)")
    r_max = _scan_max(REGISTER.read_text(encoding="utf-8"), r"^\| R-(\d+)")
    adr_max = max(re.findall(r"ADR-\d{4}", "\n".join(p.name for p in ADR_DIR.iterdir())))
    watermarks = _section(page, "### ID watermarks (protocol §7 scans)")
    assert f"| `DECISIONS.md` D-nn | D-{d_max} |" in watermarks, watermarks
    assert f"| `docs/adr/` ADR-nnnn | {adr_max} |" in watermarks, watermarks
    assert f"| `RISK_REGISTER.md` R-nn | R-{r_max} |" in watermarks, watermarks
    # The scans are shown verbatim under the numbers so the reader re-runs them instead of
    # trusting: a fenced block, because pipes in a table cell stop being copy-pasteable.
    d_scan = "grep -oE '^\\| D-[0-9]+'  DECISIONS.md      | grep -oE '[0-9]+' | sort -n | tail -1"
    r_scan = "grep -oE '^\\| R-[0-9]+'  RISK_REGISTER.md  | grep -oE '[0-9]+' | sort -n | tail -1"
    assert d_scan in watermarks
    assert "ls docs/adr/ | grep -oE 'ADR-[0-9]{4}' | sort | tail -1" in watermarks
    assert r_scan in watermarks
    assert "this table is a reading, not a reservation" in watermarks


def test_watermark_helper_reads_max_not_last_row(tmp_path: Path) -> None:
    module = _generator()
    register = tmp_path / "RISK_REGISTER.md"
    register.write_text(
        "| ID | Risk | Sev | Status | Notes |\n| --- | --- | --- | --- | --- |\n"
        "| R-09 | nine | M | OPEN | n |\n"
        "| R-11 | eleven | M | OPEN | n |\n"
        "| R-10 | ten | M | OPEN | n |\n",
        encoding="utf-8",
    )
    assert module._max_row_id(register, "R") == 11


# --------------------------------------------------------------------------- risk register


def _register_rows() -> list[tuple[str, str, str, str]]:
    rows = []
    for line in REGISTER.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^\| (R-\d+[^ |]*) \| ([^|]+) \| ([^|]+) \| ([^|]+) \|", line)
        if match:
            rows.append(tuple(cell.strip() for cell in match.groups()))
    return rows  # type: ignore[return-value]


def _status_phrase(cell: str) -> str:
    return re.sub(r"\s*\(.*$", "", cell).strip()


def test_register_status_counts_and_open_rows_are_derived_from_the_table() -> None:
    page = _page()
    rows = _register_rows()
    assert rows, "the register table did not parse"
    counts: dict[str, int] = {}
    for _, _, _, status in rows:
        counts[_status_phrase(status)] = counts.get(_status_phrase(status), 0) + 1
    register = _section(page, "### Risk register")
    for phrase, count in sorted(counts.items()):
        assert f"| {phrase} | {count} |" in register, (phrase, count, register)
    assert f"| all rows | {len(rows)} |" in register
    open_rows = [
        (rid, risk, sev) for rid, risk, sev, status in rows if _status_phrase(status) == "OPEN"
    ]
    assert open_rows, "no OPEN rows in the register — the pin needs at least one to bite"
    for rid, risk, sev in open_rows:
        assert f"| {rid} | {risk} | {sev} |" in register, rid
    # A MITIGATED row is not listed as open, and the page says MITIGATED is not CLOSED.
    mitigated = next(rid for rid, _, _, status in rows if _status_phrase(status) == "MITIGATED")
    assert f"| {mitigated} |" not in register.split("Open rows")[1]
    assert "MITIGATED` is not `CLOSED" in page or "MITIGATED is not CLOSED" in register


def test_status_phrase_strips_the_parenthetical_qualifier() -> None:
    module = _generator()
    assert module._status_phrase("MITIGATED (posture restated 2026-07-25)") == "MITIGATED"
    assert module._status_phrase("MITIGATED IN CODE") == "MITIGATED IN CODE"
    assert module._status_phrase("OPEN") == "OPEN"


# ------------------------------------------------------------------------ VCP §6 findings


def test_plan_findings_carry_a_mechanical_status_with_unknown_as_the_fallback() -> None:
    page = _page()
    findings = _section(page, "### Vision plan §6 findings")
    rows = re.findall(r"^\| (\d) \| (.+?) \| ([A-Z_]+) \| (.*?) \|$", findings, re.M)
    assert [int(number) for number, *_ in rows] == [1, 2, 3, 4, 5, 6, 7, 8], rows
    status = {int(number): state for number, _, state, _ in rows}
    # Read from the plan as committed today: struck + "addressed" marker = ADDRESSED; with an
    # unstruck "Still open from this finding" = ADDRESSED_WITH_RESIDUAL; unstruck = OPEN.
    assert status[6] == "ADDRESSED"
    assert status[3] == "ADDRESSED_WITH_RESIDUAL"
    assert status[5] == "ADDRESSED_WITH_RESIDUAL"
    for number in (1, 2, 4, 7, 8):
        assert status[number] == "OPEN", (number, status[number])
    assert "UNKNOWN is never closed" in findings
    assert "CLOSED" not in {state for state in status.values()}


def test_finding_status_rules_on_synthetic_text() -> None:
    module = _generator()
    classify = module._finding_status
    assert classify("Plain unstruck finding text.") == "OPEN"
    assert classify("~~Old text.~~ **Addressed 2026-08-13 (A1):** fixed.") == "ADDRESSED"
    assert (
        classify(
            "~~Old text.~~ **Addressed 2026-08-13:** fixed. Still open from this finding: half."
        )
        == "ADDRESSED_WITH_RESIDUAL"
    )
    # A struck "Still open" is retired text, not a residual.
    assert (
        classify(
            "~~Old.~~ **Evidence half addressed 2026-08-14.** ~~Still open from this finding: x.~~"
        )
        == "ADDRESSED"
    )
    # Struck through with no marker saying why: the reader must not infer closure.
    assert classify("~~Old text with no reason given.~~") == "UNKNOWN"
    assert classify("") == "UNKNOWN"
    assert classify("   \n  ") == "UNKNOWN"
    for text in ("", "~~x~~", "garbage ~~ half struck"):
        assert classify(text) not in {"ADDRESSED", "ADDRESSED_WITH_RESIDUAL", "CLOSED"}


@pytest.mark.parametrize(
    "marker",
    [
        "**Unaddressed: waiting on the owner.**",
        "**Not addressed: waiting.**",
        "**Not addressed 2026-09-01: still waiting.**",
        "**Partially addressed 2026-09-01 (half the fields).**",
        "**Never addressed 2026-09-01.**",
        "**unaddressed 2026-09-01**",
        "**not addressed**",
        "**partially addressed**",
        "**The owner later addressed this in conversation.**",
        "**Status note, not a closure: observed 2026-09-03.**",
        # Daybreak's re-verification of #223: a denylist of negating words let these through.
        "**No longer addressed 2026-09-01.**",
        "**Partly addressed 2026-09-01.**",
        "**Still not addressed 2026-09-01 (waiting on D-80).**",
        "**Never addressed 2026-09-01 (owner declined).**",
        "**Not half addressed 2026-09-01.**",
        "**Half addressed 2026-09-01.**",
        "**Owner-declined half addressed 2026-09-01.**",
        "**addressed 2026-09-01 (lowercase lead).**",
    ],
)
def test_negative_and_unqualified_markers_stay_unknown(marker: str) -> None:
    """Daybreak's HOLD on #221: the substring 'addressed' inside '**Unaddressed: …**' or
    '**Not addressed: …**' classified as ADDRESSED. Only a bold marker that BEGINS with
    Addressed/Closed, or that reads '<qualifier> addressed <date>' with no negating qualifier,
    is positive. Everything else on a struck finding is UNKNOWN — never closed."""

    module = _generator()
    assert module._finding_status(f"~~Old statement.~~ {marker}") == "UNKNOWN", marker
    assert module._is_positive_marker(marker.strip("*")) is False, marker


@pytest.mark.parametrize(
    "marker",
    [
        "**Addressed 2026-08-13 (A1; R-49):**",
        "**Closed 2026-08-13 (D-1):**",
        "**Kill-engaged half addressed 2026-09-03 (D-63/ADR-0049, R-66):**",
        "**Read-only and unreconciled addressed 2026-09-04 (D-69/ADR-0054, R-72):**",
        "**Evidence half addressed 2026-08-14 (A2) — finding 6 is now closed on both halves.**",
        "**Addressed: fixed in the same PR.**",
        "**Addressed (A1; R-49).**",
        "**Closed — superseded by ADR-0060.**",
    ],
)
def test_the_plan_s_own_positive_markers_are_recognised(marker: str) -> None:
    module = _generator()
    assert module._is_positive_marker(marker.strip("*")) is True, marker
    assert module._finding_status(f"~~Old statement.~~ {marker}") == "ADDRESSED", marker


_POSITIVE_FORMS = (
    "Addressed 2026-08-13 (A1; R-49):",
    "Closed 2026-08-13 (D-1):",
    "Kill-engaged half addressed 2026-09-03 (D-63/ADR-0049, R-66):",
    "Read-only and unreconciled addressed 2026-09-04 (D-69/ADR-0054, R-72):",
    "Evidence half addressed 2026-08-14 (A2):",
)


@given(
    prefix=st.from_regex(r"[A-Za-z][A-Za-z\-']{0,14}( [A-Za-z][A-Za-z\-']{0,14})?", fullmatch=True)
)
@settings(max_examples=200, deadline=None)
def test_any_prefix_on_a_positive_marker_yields_unknown_by_construction(prefix: str) -> None:
    """The grammar is anchored: a marker is positive only when it BEGINS with one of the plan's
    closure forms. So `Not`, `No longer`, `Partly`, `Still not`, `Never`, `Un`, or any word at
    all in front of a positive marker falls through to UNKNOWN — by construction, not because
    the word is on a list. Hypothesis draws the word; the plan's own words are included."""

    module = _generator()
    for form in _POSITIVE_FORMS:
        assert module._is_positive_marker(form) is True, form
        prefixed = f"{prefix} {form}"
        assert module._is_positive_marker(prefixed) is False, prefixed
        assert module._finding_status(f"~~Old statement.~~ **{prefixed}**") == "UNKNOWN", prefixed


def test_a_missing_findings_section_yields_unknown_rows_not_silence(tmp_path: Path) -> None:
    module = _generator()
    plan = tmp_path / "plan.md"
    plan.write_text("# A plan with no section 6\n\n## 7. Something else\n", encoding="utf-8")
    findings = module._plan_findings(plan)
    assert findings and all(item["status"] == "UNKNOWN" for item in findings)


# -------------------------------------------------------------------- auditlog public names


def test_auditlog_public_names_are_listed_in_declared_order() -> None:
    page = _page()
    names = _section(page, "### `chronos.auditlog` public names")
    listed = re.findall(r"`([A-Za-z_]+)`", names.split("from `chronos.auditlog.__all__`")[0])
    assert listed == list(auditlog_pkg.__all__), (listed, auditlog_pkg.__all__)
    assert "from `chronos.auditlog.__all__`" in names


# ----------------------------------------------------------------------- forwarding flags


def test_forwarding_flags_are_reported_as_declared_never_as_values() -> None:
    page = _page()
    flags = _section(page, "### Forwarding flags — declared, never read here")
    bridge = "| `CHRONOS_TV_BRIDGE_FORWARD` | `src/chronos/bridge/config.py` | `False` | not read"
    worker = "| `CHRONOS_WORKER_FORWARD` | `worker/config.py` | `False` | not read"
    assert bridge in flags
    assert worker in flags
    assert not re.search(r"FORWARD=(true|false|1|0)", page, re.I)
    assert "reads no environment" in flags


def test_forwarding_flag_declarations_are_read_from_source_by_ast(tmp_path: Path) -> None:
    module = _generator()
    source = tmp_path / "config.py"
    source.write_text(
        "def load(environ):\n"
        "    return X(forward=_parse_bool(environ, 'CHRONOS_DEMO_FORWARD', default=True))\n",
        encoding="utf-8",
    )
    assert module._declared_flag_default(source, "CHRONOS_DEMO_FORWARD") == "True"
    assert module._declared_flag_default(source, "CHRONOS_ABSENT_FORWARD") == "not declared"


# --------------------------------------------------------------------------- state inputs


def test_state_inputs_are_fingerprinted_and_named() -> None:
    module = _generator()
    page = _page()
    inputs = _section(page, "### State inputs")
    for path in module.STATE_SOURCE_PATHS:
        digest = hashlib.sha256((ROOT / path).read_bytes()).hexdigest()
        assert f"| {path} | `{digest}` |" in inputs, path
    for required in (
        "DECISIONS.md",
        "RISK_REGISTER.md",
        "docs/VISION_COMPLETION_PLAN.md",
        "src/chronos/auditlog/__init__.py",
        "src/chronos/bridge/config.py",
        "worker/config.py",
    ):
        assert Path(required) in [Path(p) for p in module.STATE_SOURCE_PATHS], required


# --------------------------------------------------------- wiring: make target and pointers


def test_make_current_state_regenerates_the_page() -> None:
    makefile = MAKEFILE.read_text(encoding="utf-8")
    match = re.search(r"^current-state:\n((?:\t.+\n)+)", makefile, re.M)
    assert match, "no `current-state:` target in the Makefile"
    assert "scripts/build_current_state.py" in match.group(1)
    assert "--check" not in match.group(1)


def test_handoff_and_tasks_point_at_the_generated_page() -> None:
    pointer = "docs/generated/CURRENT_STATE.md"
    for document in (HANDOFF, TASKS):
        text = document.read_text(encoding="utf-8")
        lines = [line for line in text.splitlines() if pointer in line]
        assert len(lines) == 1, (document.name, lines)
        assert "generated" in lines[0]
