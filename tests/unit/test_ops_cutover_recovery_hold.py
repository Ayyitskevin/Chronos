"""C1R-F2: step 7 of the N3 cutover runbook gates on the recovery hold it is going to meet.

The cutover archive (step 3) moves only ``chronos.db`` and its ``-wal``/``-shm``. The ADR-0054
installation marker, ``state_generation.json``, stays beside the kill-switch file, and the fresh
keyed store has no ``installation_identity`` row: ``create_all`` leaves that table empty, and only
migration 0012 writes the adoption sentinel. Marker present and row absent is
``DATABASE_REPLACED``, so the first start of the keyed backend is held (read-only, unreconciled,
no autonomy runtime) while ``/health`` still answers 200 (kimi GONOGO-READ-V2 P1). Fix shape (a),
Kevin K-20261024-006: the runbook EXPECTS the hold, proves its reason, has Kevin acknowledge it
with a note, restarts, and proves the hold is gone before steps 8 and 9.

Two kinds of test. The text pins hold the gate's sentences in step 7, in order, and an in-test
negative control shows that removing the expect-the-hold, acknowledge or prove-cleared part is
caught. The behavioural tests run the gate's own commands, extracted from the runbook, against a
SYNTHETIC store and state directory under ``tmp_path``: the real app through its real lifespan
(the seam ``tests/safety/test_restore_recovery_hold.py`` uses), the suite's derived persistence
key (never printed), the demo broker, no network. The expected-output blocks the runbook quotes
are compared with what those commands print.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import re
import subprocess
import sys
import textwrap
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from tests.safety.test_restore_recovery_hold import _mandate

from chronos.api import auth
from chronos.api.dependencies import require_lease_holder, require_writer
from chronos.api.main import create_app
from chronos.api.routes import autonomy as autonomy_routes
from chronos.api.routes import live as live_routes
from chronos.cli.campaign_preflight import cmd_campaign_preflight
from chronos.cli.campaign_status import _recovery_status
from chronos.config.settings import Settings, get_settings
from chronos.operations import cutover_archive as cutover
from chronos.orders.recovery_hold import RecoveryHoldReason
from chronos.orders.state_generation import DEFAULT_MARKER_NAME

ROOT = Path(__file__).resolve().parents[2]
RUNBOOK = ROOT / "docs" / "ops" / "N3-CUTOVER.md"
_STAMP = "20261024T000000Z"


def _flat(text: str) -> str:
    return " ".join(text.split())


def _step(text: str, start: str, end: str) -> str:
    return text[text.index(start) : text.index(end)]


def _step7(text: str | None = None) -> str:
    return _step(
        text if text is not None else RUNBOOK.read_text(encoding="utf-8"), "7. **", "8. **Bind"
    )


#: The one header form the runbook may use: curl reads the header from a pipe the shell fills, so
#: the token is in no process's argv (G1). ``$(<file)`` is a shell builtin read; ``printf`` is a
#: builtin; the only argv curl carries is ``/dev/fd/N``.
_HEADER_FROM_FILE = "-H @<(printf 'X-Chronos-Token: %s\\n' \"$(<data/backend_api_token)\")"


#: The one line run in the operator's own shell before the first token-bearing command (P2-1,
#: P2-2): `builtin` skips a function named `type`; `type` reports `command` and `curl` as the
#: shell resolves them (a function, an alias, a hashed path or a PATH wrapper all show).
_SHELL_CHECK = "builtin type command curl printf"

#: The guard every token-bearing curl wears (P1-1): a subshell that turns xtrace off before curl is
#: expanded, ``command`` so an alias or function named ``curl`` cannot interpose, and ``-q`` as
#: curl's FIRST argument (curl ignores it anywhere else), so no ambient ``.curlrc`` can switch on
#: ``verbose``/``trace``.
_GUARD = "( set +x; command curl -q "
_TOKEN_CURL_FLAGS_NEVER = (" -v", "--verbose", "--trace", "--config", " -K", " -x ")


def _curl_commands(text: str) -> list[str]:
    """Every logical ``curl`` command in the bash fences, backslash continuations joined."""

    commands = []
    for info, body in _fences(text):
        if info != "bash":
            continue
        for logical in re.sub(r"\\\n\s*", " ", body).splitlines():
            if re.match(r"\s*(\(\s*(set \+x;\s*)?)?(command\s+)?curl\b", logical):
                commands.append(logical)
    return commands


def _token_curl_guard_problems(text: str) -> list[str]:
    """Token-bearing curl commands that lack the guard or carry an output-enabling flag."""

    problems = []
    for command in _curl_commands(text):
        if "backend_api_token" not in command:
            continue
        if not command.startswith(_GUARD):
            problems.append(f"no `( set +x; command curl -q ` guard: {command[:70]}")
        if not command.endswith(" )"):
            problems.append(f"the guard subshell is not closed on this command: {command[-30:]}")
        for flag in _TOKEN_CURL_FLAGS_NEVER:
            if flag in command:
                problems.append(f"{flag.strip()} on a token-bearing curl: {command[:70]}")
    return problems


def _token_in_curl_argv_problems(text: str) -> list[str]:
    """Curl commands that put the token file's contents where curl's argv can carry them."""

    problems = []
    for command in _curl_commands(text):
        if re.search(r"\$\(\s*cat\b[^)]*token", command):
            problems.append(f"$(cat <token file>) in a curl command: {command[:80]}")
        for match in re.finditer("backend_api_token", command):
            if command[: match.end() + 3][-len(_HEADER_FROM_FILE) :] != _HEADER_FROM_FILE:
                problems.append(f"token file outside the fd-fed header form: {command[:80]}")
    return problems


# --- the gate's text --------------------------------------------------------------------------

#: Each part of the gate, as the sentences (whitespace-normalized) the runbook must carry in
#: step 7. Together they cover every line the gate adds; a removed line breaks at least one.
_GATE: dict[str, tuple[str, ...]] = {
    "health-200-is-not-proof": (
        'That 200 proves only that the process answers: `/health` says `"status":"ok"` under a '
        "recovery hold too.",
        "**Steps 8 and 9 wait for the gate below.**",
    ),
    "expect-the-hold": (
        "**Expect the recovery hold, reason `database_replaced` (ADR-0054). "
        "It is not an incident.**",
        "Step 3 moved only `chronos.db` and its `-wal`/`-shm`.",
        "The installation marker `state_generation.json` beside the kill-switch file survives, and "
        "a fresh store has no `installation_identity` row.",
        "A marker with no row reads as a database replaced beneath surviving state files, so the "
        "backend boots held: read-only, unreconciled, no autonomy runtime.",
        "Read the hold from `<checkout>`.",
        "The token comes from the backend's own token file (`BACKEND_TOKEN_FILE`, "
        "`data/backend_api_token` by default; use your path if it differs): the shell reads the "
        "file and hands curl a header file through process substitution.",
        "That needs **bash or zsh** (dash and sh refuse it with a syntax error before curl runs) "
        "and **curl 7.55 or newer** (`-H @file`).",
        "What the two token-bearing commands neutralise: the token is never in any process's argv "
        "(`ps` and `/proc/<pid>/cmdline` show only a `/dev/fd/N` path); the subshell's `set +x` "
        "keeps an xtrace you already have on from printing it; `command` keeps an alias or shell "
        "function named `curl` (one that adds `-v`, say) from running; and `-q`, as curl's FIRST "
        "argument, keeps an ambient `.curlrc` (one holding `verbose`, say) from printing it.",
        "What stays with you: add `-v`, `--trace`, `--trace-ascii`, `--config` or `-K` to either "
        "command and curl prints the token. So does anything that changes which `command` or "
        "which `curl` runs: a function named `command` (also from `BASH_ENV`, or "
        "`ZDOTDIR/.zshenv`), a hashed `curl` (`hash -p` in bash, `hash curl=` in zsh), another "
        "`curl` earlier in your `PATH`, or a function or alias named `printf`, which receives "
        "the token inside the process substitution.",
        "The check below shows those; it cannot see a shell that also redefines `builtin` or "
        "`type`. Do not.",
        "The log is your `LOG_FILE` (`logs/chronos.log` by default).",
        "Check the shell first. In the shell you will paste into, run:",
        "It must print exactly three lines: `command is a shell builtin`, `curl is <path>` and "
        "`printf is a shell builtin`, where `<path>` is your system's curl in a directory only "
        "root can write (`/usr/bin/curl`, say).",
        "**STOP, and run nothing below, if** `command` or `printf` is a function or an alias, "
        "`curl` is a function, an alias or `hashed`, the path is under your home or in any "
        "directory you can write, or there is any other line.",
        "( set +x; command curl -q -sS -w ' HTTP %{http_code}\\n' " + _HEADER_FROM_FILE + " "
        "http://127.0.0.1:<port>/autonomy/alerts )",
        "It must print these four lines, then one log line whose message begins `This boot cannot "
        "prove it does not follow a restore (database_replaced)`:",
        "`GET /autonomy/alerts` only reads, but it sits behind the same writer check as every "
        "mutating route: its 409 is the hold's refusal, and its 200 later means writes are "
        "allowed.",
        "**Anything else is not the cutover's state: stop the backend (`systemctl --user stop "
        "chronos-backend`), acknowledge nothing, and stop here.**",
        "That includes another reason in the log or the 409 (`state_directory_lost`, "
        "`marker_unreadable`, `installation_mismatch`, `restore_pending`), another startup fault "
        "beside `recovery_unverified`, and no hold at all: `startup_faults: none` here means the "
        "marker was not where the backend looks, so the live-safety state this host ran with is "
        "missing.",
        "`python -m chronos.cli campaign status` and `campaign preflight` cannot show this reason: "
        "until the restart after your acknowledgement they report the fresh store's missing "
        "`installation_identity` row as UNVERIFIED.",
        "That restart writes the row from the marker's id (ADR-0054, amended 2026-10-25), and from "
        "then on both read the pair as consistent: `recovery hold: CLEAR` and "
        "`PASS [ADR-0054] state identity`.",
    ),
    "acknowledge": (
        "**Then you acknowledge it: your act, with a typed note** saying this is the expected N3 "
        "database replacement.",
        "In a new shell, run the check above again first (same STOP).",
        "The same token file, in the same guarded form:",
        "( set +x; command curl -q -fsS -X POST "
        "http://127.0.0.1:<port>/live/recovery/acknowledge \\ " + _HEADER_FROM_FILE + " \\ "
        "-H 'Content-Type: application/json' \\",
        'Expect `{"acknowledged":true,"reason":"database_replaced","binding":"<64 hex>",'
        '"effective":"next start"}`.',
        "A `reason` other than `database_replaced`: stop.",
        "The acknowledgement binds this exact observation and takes effect at the next start, so "
        "this process stays held.",
        "It does not disengage the kill switch, arm, or activate a mandate.",
    ),
    "prove-cleared": (
        "**Restart, then prove the hold is gone, before step 8.**",
        "`systemctl --user restart chronos-backend`, then `systemctl --user is-active "
        "chronos-backend` → `active`, then the `/health` and `/autonomy/alerts` commands above "
        "again, but not the `grep`: the log still holds the held boot's `recovery_hold_engaged` "
        "line, which proves nothing now. They must print:",
        "`startup_faults: none`: the hold is cleared.",
        "`HTTP 200`: writes are allowed (the body lists unacknowledged alerts; it may hold the "
        "mandate alert below instead of `[]`).",
        "`reconciliation: RECONCILED` with `reconciliation=RUNNING`: startup reconciliation ran "
        "and the refresher runs (`STARTING` in the first seconds: re-run).",
        "`autonomy=NOT_EXPECTED` is right until step 9, for one of two reasons: no "
        "`AUTONOMY_MANDATE_FILE` is configured, or the configured mandate still carries the "
        "unkeyed fingerprint.",
        "Then the log says `Persistent mandate is scoped to a different account; autonomy stays "
        'inert`, and the alert list holds `"kind":"autonomy.mandate_invalid"`.',
        "Step 9 re-authors the mandate; autonomy starts after it.",
        "**Anything else (a startup fault, a 409, `reconciliation` not `RECONCILED`, "
        "`autonomy=RUNNING`: a grant active before step 8 proved the key) means stop the backend "
        "and stop here.**",
        "A bare `/health` 200 is not this proof.",
        "Steps 8 and 9 start only after it passes.",
    ),
}

#: Each part's lead-in, which starts its own paragraph inside list item 7, in this order.
_LEAD_INS = (
    ("health-200-is-not-proof", "That 200 proves only"),
    ("expect-the-hold", "**Expect the recovery hold"),
    ("acknowledge", "**Then you acknowledge it"),
    ("prove-cleared", "**Restart, then prove the hold is gone"),
)

_STEP8_RETAKES_THE_GATE = (
    "then start again, pass step 7's recovery-hold gate again (the new store holds no "
    "acknowledgement, so the hold comes back), and repeat this step."
)


def _gate_problems(step7: str) -> list[str]:
    """Every gate sentence missing from step 7, named by its part."""

    flat = _flat(step7)
    return [
        f"{part}: {sentence[:72]}"
        for part, sentences in _GATE.items()
        for sentence in sentences
        if _flat(sentence) not in flat
    ]


def _without_part(step7: str, part: str) -> str:
    """Step 7 with one part of the gate cut out, from its lead-in to the next one."""

    leads = [lead for _, lead in _LEAD_INS]
    index = [name for name, _ in _LEAD_INS].index(part)
    start = step7.index(leads[index])
    end = step7.index(leads[index + 1]) if index + 1 < len(leads) else len(step7)
    return step7[:start] + step7[end:]


def test_c1rf2_step_7_carries_the_whole_gate_in_order() -> None:
    step7 = _step7()
    assert _gate_problems(step7) == []
    starts = [step7.index("\n\n   " + lead) for _, lead in _LEAD_INS]
    assert starts == sorted(starts), "the parts run: 200 is not proof, expect, acknowledge, prove"
    # the gate sits after the backend's first start and before step 8 (the slice ends there)
    assert step7.index("systemctl --user start chronos-backend") < starts[0]


def test_c1rf2_negative_control_each_removed_part_is_caught() -> None:
    """The pin is not vacuous: cut any one part out and the check names exactly that part."""

    step7 = _step7()
    for part, _ in _LEAD_INS:
        problems = _gate_problems(_without_part(step7, part))
        assert problems, part
        assert {problem.split(":", 1)[0] for problem in problems} == {part}, problems


def test_c1rf2_step_8_retakes_the_gate_after_a_re_archive() -> None:
    step8 = _step(RUNBOOK.read_text(encoding="utf-8"), "8. **Bind", "9. **")
    assert _flat(_STEP8_RETAKES_THE_GATE) in _flat(step8)


def test_c1rf2_the_gate_commands_never_print_the_token() -> None:
    """The token reaches only a request header, through the shell; nothing echoes it."""

    gate = _step7()[_step7().index("That 200 proves only") :]
    blocks = [body for info, body in _fences(gate) if info == "bash"]
    assert len(blocks) == 3, "the shell check, the read block and the acknowledgement block"
    assert blocks[0] == _SHELL_CHECK + "\n"
    for block in blocks[1:]:
        assert "backend_api_token" in block
        uses = block.count("backend_api_token")
        assert block.count(_HEADER_FROM_FILE) == uses
        assert block.count("set +x") == uses
        for word in (" -v", "--verbose", "--trace", "set -x", "echo ", "tee "):
            assert word not in block, word
        # the only printf is the header line the fd-fed form carries
        assert block.count("printf ") == uses
    # the header and the default file are the code's own
    assert auth._TOKEN_HEADER == "X-Chronos-Token"
    assert Settings.model_fields["backend_token_file"].default == Path("data/backend_api_token")
    assert Settings.model_fields["log_file"].default == Path("logs/chronos.log")


def test_g1_no_curl_in_the_runbook_has_the_token_in_its_argv() -> None:
    """G1: `-H "X-Chronos-Token: $(cat <token file>)"` put the token in curl's argv (ps, /proc)."""

    runbook = RUNBOOK.read_text(encoding="utf-8")
    assert len(_curl_commands(runbook)) >= 2, "the pin must see the gate's curl commands"
    assert _token_in_curl_argv_problems(runbook) == []
    assert "$(cat data/backend_api_token)" not in runbook


def test_g1_negative_control_the_old_argv_form_is_caught() -> None:
    runbook = RUNBOOK.read_text(encoding="utf-8")
    old = '-H "X-Chronos-Token: $(cat data/backend_api_token)"'
    assert _HEADER_FROM_FILE in runbook
    assert len(_token_in_curl_argv_problems(runbook.replace(_HEADER_FROM_FILE, old))) >= 2
    # a different spelling of the same leak is caught too
    other = runbook.replace(_HEADER_FROM_FILE, '-H "X-Chronos-Token: $(< data/backend_api_token)"')
    assert _token_in_curl_argv_problems(other)
    assert _token_in_curl_argv_problems(
        runbook.replace(_HEADER_FROM_FILE, '-H "X-Chronos-Token: $(cat  "$F"/token)"')
    )


def test_p11_every_token_bearing_curl_is_guarded_against_xtrace_and_curlrc() -> None:
    """P1-1: `-q` first and `set +x` in one subshell; no flag that makes curl print the request."""

    runbook = RUNBOOK.read_text(encoding="utf-8")
    token_curls = [c for c in _curl_commands(runbook) if "backend_api_token" in c]
    assert len(token_curls) == 2, "the read and the acknowledgement"
    assert _token_curl_guard_problems(runbook) == []
    for command in token_curls:
        assert command.startswith(_GUARD) and command.endswith(" )")


def test_p11_negative_control_each_unguarded_form_is_caught() -> None:
    runbook = RUNBOOK.read_text(encoding="utf-8")
    assert _token_curl_guard_problems(runbook) == []
    mutants = {
        "no -q": runbook.replace("command curl -q -sS", "command curl -sS"),
        "-q not first": runbook.replace("command curl -q -sS", "command curl -sS -q"),
        "no set +x": runbook.replace("( set +x; command curl -q -fsS", "( command curl -q -fsS"),
        "no command (alias/function can interpose), alerts": runbook.replace(
            "( set +x; command curl -q -sS", "( set +x; curl -q -sS"
        ),
        "no command (alias/function can interpose), ack": runbook.replace(
            "( set +x; command curl -q -fsS", "( set +x; curl -q -fsS"
        ),
        "command after -q": runbook.replace("command curl -q -sS", "curl -q command -sS"),
        "unclosed subshell": runbook.replace("/autonomy/alerts )", "/autonomy/alerts"),
        "-v": runbook.replace("curl -q -sS", "curl -q -v -sS"),
        "--trace-ascii": runbook.replace("curl -q -fsS", "curl -q --trace-ascii - -fsS"),
        "--config": runbook.replace("curl -q -fsS", "curl -q --config x -fsS"),
        "-K": runbook.replace("curl -q -sS", "curl -q -K x -sS"),
        "6b8c755 text": runbook.replace("command curl", "curl"),
        "fed2d81 text": runbook.replace(_GUARD, "curl ").replace("alerts )", "alerts"),
    }
    for name, mutant in mutants.items():
        assert mutant != runbook, name
        assert _token_curl_guard_problems(mutant), name


def _check_problems(step7: str) -> list[str]:
    """The shell check must precede each token-bearing block and carry its STOP and residuals."""

    flat = _flat(step7)
    problems = []
    check_at = step7.find("```bash\n   " + _SHELL_CHECK + "\n")
    read_at = step7.find("curl -fsS http://127.0.0.1:<port>/health | python")
    ack_at = step7.find("( set +x; command curl -q -fsS -X POST")
    if not 0 <= check_at < read_at < ack_at:
        problems.append("the shell check does not precede the read block")
    stop_at = flat.find("**STOP, and run nothing below, if**")
    if not 0 <= stop_at <= _flat(step7[:read_at]).__len__():
        problems.append("the STOP does not sit before the read block")
    if "In a new shell, run the check above again first (same STOP)." not in flat:
        problems.append("the acknowledgement does not repeat the check")
    for phrase in (
        "a function named `command` (also from `BASH_ENV`, or `ZDOTDIR/.zshenv`)",
        "a hashed `curl` (`hash -p` in bash, `hash curl=` in zsh)",
        "another `curl` earlier in your `PATH`",
        "or a function or alias named `printf`, which receives the token inside the process "
        "substitution",
        "it cannot see a shell that also redefines `builtin` or `type`",
        "`printf is a shell builtin`",
        "`command` or `printf` is a function or an alias",
        "`curl` is a function, an alias or `hashed`",
        "the path is under your home or in any directory you can write",
    ):
        if phrase not in flat:
            problems.append(f"missing: {phrase[:60]}")
    return problems


def _reword(text: str, phrase: str, replacement: str) -> str:
    """``text`` with ``phrase`` replaced wherever its words sit, whatever the line wrapping."""

    pattern = r"\s+".join(re.escape(word) for word in phrase.split())
    assert re.search(pattern, text), phrase
    return re.sub(pattern, replacement, text, count=1)


def test_p2_the_shell_check_precedes_each_token_command_and_carries_its_stop() -> None:
    step7 = _step7()
    assert _check_problems(step7) == []
    assert step7.count("builtin type command curl") == 1


def test_p2_negative_control_each_missing_piece_is_caught() -> None:
    step7 = _step7()
    assert _check_problems(step7) == []
    mutants = {
        "5f3cec2 text (no check, residuals unnamed)": step7.replace(
            "builtin type command curl", "true"
        ),
        "check moved after the read block": step7.replace(
            "```bash\n   " + _SHELL_CHECK + "\n   ```", "```bash\n   true\n   ```"
        ),
        "STOP dropped": _reword(step7, "**STOP, and run nothing below, if**", "Note:"),
        "no repeat before the acknowledgement": _reword(
            step7, "In a new shell, run the check above again first (same STOP).", ""
        ),
        "function command not named": _reword(step7, "a function named `command`", "a function"),
        "printf residual not named": _reword(
            step7, "or a function or alias named `printf`", "or a thing"
        ),
        "printf not in the check": step7.replace(_SHELL_CHECK, "builtin type command curl"),
        "printf not in the STOP": _reword(
            step7, "`command` or `printf` is a function", "`command` is a function"
        ),
        "hashed curl not named": _reword(step7, "a hashed `curl`", "a thing"),
        "hash forms not named": _reword(
            step7, "(`hash -p` in bash, `hash curl=` in zsh)", "(hashing)"
        ),
        "builtin/type residual dropped": _reword(
            step7, "it cannot see a shell that also redefines `builtin` or `type`", "it sees all"
        ),
    }
    for name, mutant in mutants.items():
        assert mutant != step7, name
        assert _check_problems(mutant), name


def test_p11_the_runbook_no_longer_claims_nothing_prints_the_token() -> None:
    """The claim was false under xtrace, curl -v/--trace and an ambient .curlrc."""

    runbook = RUNBOOK.read_text(encoding="utf-8")
    step7 = _flat(_step7(runbook))
    assert "nothing prints it" not in _flat(runbook)
    assert "never printed" not in step7
    assert "bash or zsh" in step7 and "curl 7.55 or newer" in step7
    # a runbook that goes back to the claim is caught by the gate text pin as well
    wrong = runbook.replace("What stays with you", "Nothing prints it")
    assert _gate_problems(_step7(wrong))


def test_g2_the_restart_read_leaves_out_the_held_boots_log_line() -> None:
    """G2: re-running the `grep` after the restart prints the held boot's stale line again."""

    prove = _step7()[_step7().index("**Restart, then prove the hold is gone") :]
    assert _flat("again, but not the `grep`") in _flat(prove)
    assert "held boot's `recovery_hold_engaged` line" in _flat(prove)
    # a restart paragraph that tells the reader to re-run everything above is caught
    wrong = _step7().replace("again, but not the `grep`", "again")
    assert _gate_problems(wrong) and all(
        problem.startswith("prove-cleared") for problem in _gate_problems(wrong)
    )


def test_c1rf2_the_routes_the_gate_reads_are_the_ones_it_says() -> None:
    """`/autonomy/alerts` is a GET behind `require_writer`; the acknowledgement is not."""

    routes = {
        (route.path, method): route
        for router in (autonomy_routes.router, live_routes.router)
        for route in router.routes
        if isinstance(route, APIRoute)
        for method in route.methods
    }

    def calls(route: APIRoute) -> set[object]:
        return {dependency.call for dependency in route.dependant.dependencies}

    assert require_writer in calls(routes[("/autonomy/alerts", "GET")])
    acknowledge = routes[("/live/recovery/acknowledge", "POST")]
    assert require_lease_holder in calls(acknowledge) and require_writer not in calls(acknowledge)


def test_c1rf2_each_part_starts_its_own_paragraph() -> None:
    step7 = _step7()
    for _, lead in _LEAD_INS:
        assert "\n\n   " + lead in step7, lead
    # and the gate's last sentence ends step 7, a blank line before step 8 like every step
    assert step7.endswith(" Steps 8 and 9 start only after it passes.\n\n")


# --- the gate, run on a synthetic cutover -------------------------------------------------------


@pytest.fixture()
def demo_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    """A synthetic deployment: store, state files, token and log all under ``tmp_path``."""

    monkeypatch.chdir(ROOT)
    monkeypatch.setenv("BROKER_MODE", "demo")
    monkeypatch.setenv("ALLOW_ORDER_TRANSMIT", "false")
    monkeypatch.setenv("ALLOW_LIVE_TRADING", "false")
    monkeypatch.setenv("DEMO_PROFILE", "empty_account")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'chronos.db'}")
    monkeypatch.setenv("LOG_FILE", str(tmp_path / "logs" / "chronos.log"))
    monkeypatch.setenv("BACKEND_TOKEN_FILE", str(tmp_path / "backend_api_token"))
    monkeypatch.setenv("LIVE_KILL_SWITCH_FILE", str(tmp_path / "live_kill_switch.json"))
    monkeypatch.setenv("SESSION_BASELINE_FILE", str(tmp_path / "session_drawdown.json"))
    monkeypatch.delenv("AUTONOMY_MANDATE_FILE", raising=False)
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def _boot() -> TestClient:
    get_settings.cache_clear()
    return TestClient(create_app())


def _token(root: Path) -> dict[str, str]:
    return {"X-Chronos-Token": (root / "backend_api_token").read_text(encoding="utf-8").strip()}


def _archive_per_cutover(root: Path) -> list[str]:
    """Step 3's effect on the directory: the store family moves to ``.archived-<stamp>`` names."""

    family = ["chronos.db", *("chronos.db" + suffix for suffix in cutover.SIDECAR_SUFFIXES)]
    moved = []
    for name in family:
        if (root / name).exists():
            (root / name).rename(root / f"{name}{cutover.ARCHIVED_INFIX}{_STAMP}")
            moved.append(name)
    return moved


def _fences(text: str) -> list[tuple[str, str]]:
    """Each fenced block as ``(info string, dedented body)``, read line by line."""

    blocks: list[tuple[str, str]] = []
    info: str | None = None
    body: list[str] = []
    for line in text.splitlines():
        fence = re.fullmatch(r"\s*```(\w*)", line)
        if fence is None:
            if info is not None:
                body.append(line)
        elif info is None:
            info, body = fence.group(1), []
        else:
            assert fence.group(1) == "", f"a closing fence carries an info string: {line!r}"
            blocks.append((info, textwrap.dedent("\n".join(body) + "\n")))
            info = None
    assert info is None, "a fence never closes"
    return blocks


def _gate_blocks() -> tuple[str, str, str, str]:
    """The read block, the held output, the acknowledgement block and the cleared output."""

    gate = _step7()[_step7().index("That 200 proves only") :]
    blocks = _fences(gate)
    assert [info for info, _ in blocks] == ["bash", "bash", "", "bash", ""], blocks
    assert blocks[0][1] == _SHELL_CHECK + "\n", "the shell check block comes first"
    return blocks[1][1], blocks[2][1], blocks[3][1], blocks[4][1]


def _health_lines(health_body: str) -> list[str]:
    """The read block's ``/health`` filter, exactly as the runbook gives it, over a real body."""

    read_block = _gate_blocks()[0]
    program = re.search(r"python -I -c '(.*?)'\n", read_block, re.S)
    assert program is not None
    done = subprocess.run(
        [sys.executable, "-I", "-c", program.group(1)],
        input=health_body,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    return done.stdout.splitlines()


def _alerts_line(client: TestClient, root: Path) -> str:
    """What the read block's ``curl -sS -w ' HTTP %{http_code}\\n'`` line prints."""

    response = client.get("/autonomy/alerts", headers=_token(root))
    return f"{response.text} HTTP {response.status_code}"


def _last_hold_log_line(root: Path) -> str:
    """The read block's ``grep`` line, run as written in the directory the log path is under."""

    read_block = _gate_blocks()[0]
    (line,) = [entry for entry in read_block.splitlines() if entry.startswith("grep ")]
    done = subprocess.run(
        ["bash", "-c", line], cwd=root, capture_output=True, text=True, timeout=60, check=False
    )
    return done.stdout.strip()


def test_c1rf2_a_synthetic_cutover_boots_held_then_clears_after_the_acknowledgement(
    demo_env: Path,
) -> None:
    _, held_output, ack_block, cleared_output = _gate_blocks()

    # the pre-cutover installation: its writer seeded the marker and the identity row
    with _boot():
        pass
    assert (demo_env / DEFAULT_MARKER_NAME).exists()

    # step 3: only the store family moves; the marker is not in the cutover's namespace
    namespace = {"chronos.db", *("chronos.db" + suffix for suffix in cutover.LIVE_NAME_SUFFIXES)}
    assert DEFAULT_MARKER_NAME not in namespace
    assert _archive_per_cutover(demo_env) == ["chronos.db"]
    assert (demo_env / DEFAULT_MARKER_NAME).exists()
    assert not (demo_env / "chronos.db").exists()

    # step 7's start: held, for exactly the reason the runbook expects
    with _boot() as client:
        state = client.app.state.backend  # type: ignore[attr-defined]
        assert state.recovery_hold is not None
        assert state.recovery_hold.reason is RecoveryHoldReason.DATABASE_REPLACED
        assert client.get("/health").status_code == 200  # the bare 200 the gate refuses to trust
        measured = [*_health_lines(client.get("/health").text), _alerts_line(client, demo_env)]
        assert measured == held_output.splitlines()
        # a real write route refuses too, not only the read the runbook uses
        assert (
            client.post("/live/arm", json={"phrase": "x"}, headers=_token(demo_env)).status_code
            == 409
        )
        log_line = _last_hold_log_line(demo_env)
        assert json.loads(log_line)["message"].startswith(
            "This boot cannot prove it does not follow a restore (database_replaced)"
        )

        # the acknowledgement, with the runbook's own body
        (body,) = re.findall(r"-d '(\{.*\})'", ack_block)
        response = client.post(
            "/live/recovery/acknowledge", json=json.loads(body), headers=_token(demo_env)
        )
        assert response.status_code == 200
        expected = re.escape(
            '{"acknowledged":true,"reason":"database_replaced","binding":"<64 hex>",'
            '"effective":"next start"}'
        ).replace(re.escape("<64 hex>"), "[0-9a-f]{64}")
        assert re.fullmatch(expected, response.text), response.text
        assert state.may_write is False  # this process stays held
        assert _alerts_line(client, demo_env).endswith(" HTTP 409")

    # the restart: cleared, writes allowed, reconciled, autonomy inert for the named reason
    with _boot() as client:
        state = client.app.state.backend  # type: ignore[attr-defined]
        assert state.recovery_hold is None and state.may_write is True
        measured = [*_health_lines(client.get("/health").text), _alerts_line(client, demo_env)]
        assert measured == cleared_output.splitlines()
        assert getattr(client.app.state, "autonomy", None) is None  # no mandate configured


def test_c1rf2_an_unkeyed_mandate_stays_inert_after_the_gate_for_the_named_reason(
    demo_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Before step 9 the configured grant still carries the old fingerprint: inert, and said."""

    with _boot():
        pass
    _archive_per_cutover(demo_env)
    with _boot() as client:
        client.post(
            "/live/recovery/acknowledge",
            json={"note": "synthetic N3 cutover"},
            headers=_token(demo_env),
        ).raise_for_status()
    mandate = _mandate(datetime.now(UTC)).model_copy(update={"account_fingerprint": "0" * 64})
    mandate_file = demo_env / "mandate.json"
    mandate_file.write_text(mandate.model_dump_json(), encoding="utf-8")
    mandate_file.chmod(0o600)
    monkeypatch.setenv("AUTONOMY_MANDATE_FILE", str(mandate_file))
    monkeypatch.setenv("AUTONOMY_ALERT_FILE", str(demo_env / "owner_alerts.jsonl"))
    with _boot() as client:
        assert client.app.state.backend.recovery_hold is None  # type: ignore[attr-defined]
        assert getattr(client.app.state, "autonomy", None) is None
        health = _health_lines(client.get("/health").text)
        assert health == _gate_blocks()[3].splitlines()[:3]
        alerts = client.get("/autonomy/alerts", headers=_token(demo_env))
        assert alerts.status_code == 200
        assert '"kind":"autonomy.mandate_invalid"' in alerts.text
    log = (demo_env / "logs" / "chronos.log").read_text(encoding="utf-8")
    assert "Persistent mandate is scoped to a different account; autonomy stays inert" in log


def test_c1rf2_a_re_archived_store_brings_the_hold_back(demo_env: Path) -> None:
    """Step 8's wrong-key path: the acknowledgement lived in the store that moved aside."""

    with _boot():
        pass
    _archive_per_cutover(demo_env)
    with _boot() as client:
        client.post(
            "/live/recovery/acknowledge",
            json={"note": "synthetic N3 cutover"},
            headers=_token(demo_env),
        ).raise_for_status()
    with _boot() as client:
        assert client.app.state.backend.recovery_hold is None  # type: ignore[attr-defined]
    for name in ("chronos.db", *("chronos.db" + suffix for suffix in cutover.SIDECAR_SUFFIXES)):
        if (demo_env / name).exists():
            (demo_env / name).rename(demo_env / f"{name}.wrong-key")
    with _boot() as client:
        hold = client.app.state.backend.recovery_hold  # type: ignore[attr-defined]
        assert hold is not None and hold.reason is RecoveryHoldReason.DATABASE_REPLACED


def test_c1rf2_campaign_status_cannot_show_this_reason(demo_env: Path) -> None:
    """The runbook's claim about `campaign status` and `campaign preflight`, measured at each step.

    UNVERIFIED while held, still UNVERIFIED in the acknowledging process (the note takes effect
    at the next start), then CLEAR and PASS after the restart writes the identity row (PGAP-I).
    """

    def preflight_line() -> str:
        arguments = argparse.Namespace(
            mandate=demo_env / "mandate.json",
            registry=demo_env / "registry.json",
            state_dir=demo_env,
            unit=demo_env / "worker.service",
            policy=demo_env / "policy.md",
            provider="local",
            model="model-tag",
            worker_backend_url="http://127.0.0.1:8765",
            worker_symbols="SPY",
            backend_symbols="SPY",
            backend_host="127.0.0.1",
            backend_port=8765,
            evidence=False,
        )
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            cmd_campaign_preflight(arguments)
        (line,) = [
            line for line in output.getvalue().splitlines() if "ADR-0054] state identity" in line
        ]
        return line

    unverified = (
        "FAIL [SHADOW_CAMPAIGN ADR-0054] state identity: UNVERIFIED; missing "
        "installation_identity row; boot the backend writer once, then re-run preflight"
    )
    with _boot():
        pass
    _archive_per_cutover(demo_env)
    with _boot() as client:
        status = _recovery_status(demo_env, demo_env / "chronos.db")
        assert (status.state.value, status.detail) == (
            "UNVERIFIED",
            "missing installation_identity row",
        )
        assert preflight_line() == unverified
        client.post(
            "/live/recovery/acknowledge",
            json={"note": "synthetic N3 cutover"},
            headers=_token(demo_env),
        ).raise_for_status()
        status = _recovery_status(demo_env, demo_env / "chronos.db")
        assert status.state.value == "UNVERIFIED"
        assert preflight_line() == unverified
    with _boot() as client:
        assert client.app.state.backend.recovery_hold is None  # type: ignore[attr-defined]
    result = _recovery_status(demo_env, demo_env / "chronos.db")
    assert (result.name, result.state.value, result.detail) == (
        "recovery hold",
        "CLEAR",
        "installation witnesses agree",
    )
    assert preflight_line() == (
        "PASS [ADR-0054] state identity: marker/database pair consistent; no recovery hold"
    )
