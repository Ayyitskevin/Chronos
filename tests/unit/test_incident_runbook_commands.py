"""docs/INCIDENT_RESPONSE.md is untested code until you run it (VCP §6 finding 2).

Finding 2: "the incident runbook invokes the deterministic-platform halt while the live
order plane has a separate kill switch". Chronos has two stop mechanisms on two planes —
`python -m chronos.cli halt` for the deterministic platform (`chronos.execution` /
`chronos.risk`) and `POST /live/kill` for the live order plane (`chronos.orders`) — and an
operator following a playbook step that says only "Halt." stops one of them.

These pins hold the runbook to the code it cites, mechanically:

- every ``python -m chronos.cli …`` line in a fenced block parses (``--help`` exits 0) and
  every option it names is one the subcommand accepts;
- every HTTP route it cites is registered on the real routers, with that method;
- every ``src/…py`` path it cites exists; every state file it names is the code's default;
- every ledger table and column its ``sqlite3`` queries name exists in the ledger schema;
- every playbook's first step stops BOTH planes, naming both commands;
- the closing procedure names both re-enable paths, and says which grants authority;
- the immediate-actions list is numbered consecutively (it once went 1, 2, 3, 2).

A runbook that drifts from the CLI, the routes or the schema fails here rather than at
2am.
"""

from __future__ import annotations

import re
import shlex
import subprocess
import sys
from pathlib import Path

from chronos.api.routes import account, autonomy, health, live, orders, strategy, terminal
from chronos.cli.main import DEFAULT_AUDIT_PATH, DEFAULT_HALT_PATH
from chronos.config.settings import Settings
from chronos.orders.state_generation import DEFAULT_MARKER_NAME

ROOT = Path(__file__).resolve().parents[2]
RUNBOOK = ROOT / "docs" / "INCIDENT_RESPONSE.md"
LEDGER_SCHEMA = ROOT / "src" / "chronos" / "execution" / "sqlite_ledger.py"

_FENCE = re.compile(r"```bash\n(.*?)```", re.S)
_ROUTE = re.compile(r"`(GET|POST) (/[A-Za-z0-9_/\-]+)`")
_CURL = re.compile(r"curl\s.*?-X\s+(GET|POST)\s+http://127\.0\.0\.1:\d+(/[A-Za-z0-9_/\-]+)", re.S)
_SOURCE_PATH = re.compile(r"`(src/chronos/[A-Za-z0-9_/]+\.py)(?::\d+(?:-\d+)?)?`")
_CREATE_TABLE = re.compile(r"CREATE TABLE IF NOT EXISTS (\w+) \((.*?)\);", re.S)


def _text() -> str:
    return RUNBOOK.read_text(encoding="utf-8")


def _section(text: str, heading: str) -> str:
    level = heading.split(" ", 1)[0]
    start = text.index(heading + "\n")
    rest = text[start + len(heading) + 1 :]
    stop = re.search(rf"^#{{1,{len(level)}}} ", rest, re.M)
    return rest[: stop.start()] if stop else rest


def _bash_lines() -> list[str]:
    """Logical command lines from every fenced bash block, continuations joined."""

    lines: list[str] = []
    for block in _FENCE.findall(_text()):
        joined = re.sub(r"\\\n\s*", " ", block)
        lines.extend(line.strip() for line in joined.splitlines() if line.strip())
    return lines


def _cli_invocations() -> list[list[str]]:
    """Every `python -m chronos.cli …` the runbook cites — fenced blocks and inline code."""

    candidates = [line for line in _bash_lines() if line.startswith("python -m chronos.cli")]
    candidates.extend(re.findall(r"`(python -m chronos\.cli [^`]+)`", _text()))
    invocations = []
    for line in candidates:
        command = re.split(r"\s[>|]", line, maxsplit=1)[0]  # drop redirections and pipes
        invocations.append(shlex.split(command)[3:])  # tokens after `python -m chronos.cli`
    return invocations


def _sqlite_queries() -> list[str]:
    """The SQL inside every `sqlite3 … "…"` in a fenced block, newlines collapsed."""

    queries = []
    for block in _FENCE.findall(_text()):
        for sql in re.findall(r'sqlite3[^"]*"(.*?)"', block, re.S):
            queries.append(" ".join(sql.split()))
    return queries


# ------------------------------------------------------------------------------- CLI commands


def test_the_runbook_cites_cli_commands_at_all() -> None:
    assert len(_cli_invocations()) >= 4, _cli_invocations()


def test_every_cited_cli_command_parses_and_its_options_exist() -> None:
    for tokens in _cli_invocations():
        positional = [token for token in tokens if not token.startswith("-")]
        assert positional, tokens
        subcommand = positional[0]
        completed = subprocess.run(
            [sys.executable, "-m", "chronos.cli", subcommand, "--help"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 0, (tokens, completed.stderr)
        for option in (token for token in tokens if token.startswith("--")):
            assert option in completed.stdout, (subcommand, option, completed.stdout)


def test_cli_commands_rely_on_the_defaults_the_runbook_names() -> None:
    text = _text()
    assert f"`{DEFAULT_HALT_PATH}`" in text, DEFAULT_HALT_PATH
    assert f"data/{Path(DEFAULT_AUDIT_PATH).name}" in text
    for tokens in _cli_invocations():
        assert "--halt-file" not in tokens and "--audit-file" not in tokens, tokens


# ------------------------------------------------------------------------------ HTTP routes


def _registered_routes() -> set[tuple[str, str]]:
    registered: set[tuple[str, str]] = set()
    for module in (live, terminal, health, orders, account, autonomy, strategy):
        for name in ("router", "session_router"):
            router = getattr(module, name, None)
            if router is None:
                continue
            for route in router.routes:
                for method in getattr(route, "methods", set()):
                    registered.add((method, route.path))
    return registered


def test_every_cited_http_route_is_registered_with_that_method() -> None:
    text = _text()
    cited = set(_ROUTE.findall(text)) | set(_CURL.findall(text))
    assert ("POST", "/live/kill") in cited
    assert ("POST", "/live/kill/disengage") in cited
    assert ("POST", "/terminal/mandate/revoke") in cited
    registered = _registered_routes()
    for method, path in sorted(cited):
        assert (method, path) in registered, (method, path)


# ------------------------------------------------------------- cited paths and state files


def test_every_cited_source_path_exists() -> None:
    cited = set(_SOURCE_PATH.findall(_text()))
    assert cited, "no source paths cited"
    for path in sorted(cited):
        assert (ROOT / path).is_file(), path


def test_cited_state_files_are_the_code_defaults() -> None:
    text = _text()
    kill_file = Settings.model_fields["live_kill_switch_file"].default
    assert f"`{kill_file}`" in text, kill_file
    assert f"`data/{DEFAULT_MARKER_NAME}`" in text, DEFAULT_MARKER_NAME
    assert "`live_kill_switch_file`" in text  # the settings name, so an operator can find it


# ------------------------------------------------------------------------- ledger queries


def _ledger_schema() -> dict[str, set[str]]:
    schema: dict[str, set[str]] = {}
    for table, body in _CREATE_TABLE.findall(LEDGER_SCHEMA.read_text(encoding="utf-8")):
        columns = set()
        for line in body.splitlines():
            token = line.strip().split(" ")[0].strip(",")
            if (
                token
                and token.isidentifier()
                and token.upper()
                not in {
                    "PRIMARY",
                    "FOREIGN",
                    "UNIQUE",
                    "CHECK",
                }
            ):
                columns.add(token)
        schema[table] = columns
    return schema


def test_every_ledger_table_and_column_the_runbook_queries_exists() -> None:
    schema = _ledger_schema()
    assert "intents" in schema and "fills" in schema
    queries = [query for query in _sqlite_queries() if "SELECT" in query.upper()]
    assert queries, "no sqlite3 queries cited"
    for query in queries:
        tables = re.findall(r"\b(?:FROM|JOIN)\s+(\w+)", query, re.I)
        assert tables, query
        for table in tables:
            assert table in schema, (table, query)
        selected = re.search(r"SELECT\s+(.*?)\s+FROM", query, re.I | re.S)
        assert selected is not None, query
        for column in re.findall(r"\b([a-z_]+)\b", selected.group(1)):
            if column in {"f", "i"} or "." in column:
                continue
            assert any(column in columns for columns in schema.values()), (column, query)


# ---------------------------------------------------------------- both planes, every step


def _playbooks(text: str) -> dict[str, str]:
    playbooks = _section(text, "## Playbooks")
    names = re.findall(r"^### (.+)$", playbooks, re.M)
    assert len(names) >= 6, names
    return {name: _section(playbooks, f"### {name}") for name in names}


def test_every_playbook_first_step_stops_both_planes_by_name() -> None:
    for name, body in _playbooks(_text()).items():
        first = re.search(r"^1\. (.+?)(?=^\d+\. |\Z)", body, re.M | re.S)
        assert first is not None, name
        step = first.group(1)
        assert "/live/kill" in step, (name, step)
        assert "chronos.cli halt" in step, (name, step)
        assert "both planes" in step.lower(), (name, step)


def test_the_immediate_actions_name_both_planes_and_are_numbered_consecutively() -> None:
    immediate = _section(_text(), "## Immediate actions (any incident)")
    numbers = [int(n) for n in re.findall(r"^(\d+)\. ", immediate, re.M)]
    assert numbers == list(range(1, len(numbers) + 1)), numbers
    assert "/live/kill" in immediate and "chronos.cli halt" in immediate
    assert "`chronos.orders`" in immediate and "`chronos.execution`" in immediate
    # The backend-down case is stated: the kill switch is HTTP-only.
    assert "backend is not running" in immediate, "backend-down guidance missing"
    assert "AUTONOMY_MANDATE_FILE" in immediate


def test_the_closing_procedure_names_both_re_enable_paths_and_which_grants_authority() -> None:
    closing = _section(_text(), "## After any incident")
    assert "python -m chronos.cli rearm --note" in closing
    assert "POST /live/kill/disengage" in closing
    assert "POST /live/arm" in closing
    assert "grants authority" in closing or "grant authority" in closing
