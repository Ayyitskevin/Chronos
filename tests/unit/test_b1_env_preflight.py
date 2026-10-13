"""B1 env preflight: every NON-SECRET condition ``docs/ops/b1-swap-runbook.md`` requires of the
private ``.env``, checked on a candidate file's TEXT with dummy values.

Since B1 U-d the checker is ``chronos.cli.b1_preflight.preflight_b1_env`` (src), run by
``python -m chronos.cli b1-preflight``; this module imports it and stays its pin.

``preflight_b1_env`` takes text (and optionally a file mode and an environment mapping); it
never opens a path, never reads ANY ``.env`` (not the working directory's, not the home
directory's), never connects, and its findings name KEYS and condition ids only, never a value
(the pepper's shape is checked, its digits are not echoed). The tests drive it with dummy values
built from the checklist's own §3.2 block; nothing here is, or looks like, a credential.

How "never reads a ``.env``" is proved (r1; the first version only claimed it): ``Settings`` is
a pydantic-settings class, and building one OPENS ``.env`` from the working directory while it
assembles its sources, even if only the init arguments are then used. So the checker builds a
subclass whose ``model_config`` has ``env_file=None``, whose only source is the init arguments,
and which is also given ``_env_file=None``. The proof is an AUDIT HOOK (``sys.addaudithook``),
which sees every ``open`` event from ``io.open``, ``Path.read_text``/``open`` and ``os.open``
(a ``builtins.open`` patch sees none of the last three): the tests plant sentinel ``.env`` files
carrying a canary in the working directory and in ``$HOME`` and fail on the first open of any
file named ``.env``; a positive control shows the same hook DOES see plain ``Settings`` read one.
The hook cannot be removed once added, so it stays registered for the session and does nothing
unless a test switches it on. An AST check of the function bodies cannot see a library's read,
and is only a second guard. A socket tripwire fails any test that opens a connection.

Each condition has an id (``P1``...). The runbook lists the same ids AND the same condition text
in its preflight table; ``test_the_runbook_and_the_preflight_list_the_same_conditions`` fails if
either drifts.
Tests only; ``ALLOW_ORDER_TRANSMIT`` and ``ALLOW_LIVE_TRADING`` stay false in every shape loaded.
"""

from __future__ import annotations

import io
import os
import re
import socket
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from dotenv import dotenv_values
from pydantic import ValidationError

from chronos.config.settings import Settings

_REPO = Path(__file__).resolve().parents[2]
_CHECKLIST = _REPO / "docs" / "ops" / "m4-read-only-gate-session-checklist.md"
_RUNBOOK = _REPO / "docs" / "ops" / "b1-swap-runbook.md"

# The preflight itself lives in src since B1 U-d (``python -m chronos.cli b1-preflight``); this
# module stays its pin, unchanged in what it asserts.
from chronos.cli.b1_preflight import (  # noqa: E402
    _AMBIENT_PREFIXES,
    _CHECKLIST_PLACEHOLDER_ACCOUNT,
    CONDITIONS,
    _FileOnlySettings,
    _load_settings,
    preflight_b1_env,
)


def _b1_block() -> str:
    section = _CHECKLIST.read_text(encoding="utf-8").split("### 3.2", 1)[1]
    match = re.search(r"```dotenv\n(.*?)```", section, re.S)
    assert match is not None, "checklist §3.2 no longer carries a dotenv block"
    return match.group(1)


def _good_text() -> str:
    """The checklist's block with dummy replacements plus the two lines the runbook adds."""

    block = _b1_block().replace(_CHECKLIST_PLACEHOLDER_ACCOUNT, "DU7654321")
    return (
        block
        + "DATABASE_URL=sqlite:////srv/chronos-b1/chronos-b1.db\n"
        + "CHRONOS_CAPTURE_PEPPER="
        + "ab" * 32
        + "\n"
    )


def _ids(text: str, **kwargs: object) -> list[str]:
    return [item[0] for item in preflight_b1_env(text, **kwargs)]  # type: ignore[arg-type]


#: ``sys.addaudithook`` hooks cannot be removed: register once, act only when a test switches
#: the mode on. ``raise``: the first open of any file NAMED ``.env`` fails the test;
#: ``record``: opens are noted (for the positive control and the explicit "nothing seen" proof).
_DOTENV_WATCH: dict[str, object] = {"mode": "off", "seen": []}


def _audit_dotenv_open(event: str, args: tuple[object, ...]) -> None:
    if event != "open" or _DOTENV_WATCH["mode"] == "off":
        return
    target = args[0]
    if not isinstance(target, str | bytes | os.PathLike):
        return  # an int file descriptor, not a path
    name = os.fsdecode(target)
    if os.path.basename(name) != ".env":
        return
    cast_seen = _DOTENV_WATCH["seen"]
    assert isinstance(cast_seen, list)
    cast_seen.append(name)
    if _DOTENV_WATCH["mode"] == "raise":
        raise AssertionError(f"a .env was opened: {name}")


if not getattr(sys, "_b1_preflight_dotenv_hook", False):
    sys.addaudithook(_audit_dotenv_open)
    sys._b1_preflight_dotenv_hook = True  # type: ignore[attr-defined]


@contextmanager
def _watching(mode: str) -> Iterator[list[str]]:
    seen: list[str] = []
    previous = (_DOTENV_WATCH["mode"], _DOTENV_WATCH["seen"])
    _DOTENV_WATCH["mode"], _DOTENV_WATCH["seen"] = mode, seen
    try:
        yield seen
    finally:
        _DOTENV_WATCH["mode"], _DOTENV_WATCH["seen"] = previous


@pytest.fixture(autouse=True)
def _no_dotenv_is_opened_and_no_socket(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Every test here runs with the audit hook in ``raise`` mode and a socket tripwire."""

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("the preflight tests attempted a network connection")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    with _watching("raise"):
        yield


_CANARY = "canary-must-never-be-read"


@pytest.fixture
def sentinel_dotenvs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """A ``.env`` in the working directory and one in ``$HOME``, each carrying a canary.

    The canary ``IB_DATA_CLIENT_ID=17`` collides with the good shape's ``IB_CLIENT_ID=17``: if
    anything reads either file, the checker's verdict changes (P8), not just the audit hook.
    """

    work, home = tmp_path / "work", tmp_path / "home"
    paths = {"cwd": work / ".env", "home": home / ".env"}
    with _watching("off"):  # creating the sentinels is not a read of them
        for directory in (work, home):
            directory.mkdir()
        for path in paths.values():
            path.write_text(f"IB_DATA_CLIENT_ID=17\nCANARY_VALUE={_CANARY}\n", encoding="utf-8")
    monkeypatch.chdir(work)
    monkeypatch.setenv("HOME", str(home))
    return paths


# --- the good shape ----------------------------------------------------------------------------


def test_the_checklist_block_with_dummy_values_meets_every_condition() -> None:
    assert preflight_b1_env(_good_text(), file_mode=0o600, environ={}) == []


def test_the_good_shape_really_leaves_both_conjunctions_false() -> None:
    values = {k: v for k, v in dotenv_values(stream=io.StringIO(_good_text())).items() if v}
    settings = _load_settings(values)
    assert isinstance(settings, Settings)
    assert settings.transmission_possible is False
    assert settings.live_transmission_possible is False
    assert settings.autonomy_mandate_file is None


# --- each condition fails on its own broken shape (and only that condition) --------------------


def _set(text: str, key: str, value: str | None) -> str:
    lines = [ln for ln in text.splitlines() if not ln.startswith(f"{key}=")]
    if value is not None:
        lines.append(f"{key}={value}")
    return "\n".join(lines) + "\n"


@pytest.mark.parametrize(
    ("label", "mutate", "expected"),
    [
        ("demo mode", lambda t: _set(t, "BROKER_MODE", "demo"), ["P2"]),
        ("other adapter", lambda t: _set(t, "BROKER_ADAPTER", "ib_async"), ["P2"]),
        ("a live live-port", lambda t: _set(t, "IB_PORT", "7496"), ["P3"]),
        ("a gateway live-port", lambda t: _set(t, "IB_PORT", "4001"), ["P3"]),
        ("the gateway paper port is fine", lambda t: _set(t, "IB_PORT", "4002"), []),
        ("a non-local host", lambda t: _set(t, "IB_HOST", "10.0.0.5"), ["P3"]),
        ("a live-shaped account", lambda t: _set(t, "IB_ACCOUNT_ID", "U1234567"), ["P4"]),
        ("a blank account", lambda t: _set(t, "IB_ACCOUNT_ID", ""), ["P4"]),
        ("a too-short paper id", lambda t: _set(t, "IB_ACCOUNT_ID", "DU12"), ["P4"]),
        ("the checklist placeholder", lambda t: _set(t, "IB_ACCOUNT_ID", "DU1234567"), ["P4"]),
        ("DF is a paper prefix", lambda t: _set(t, "IB_ACCOUNT_ID", "DF7654321"), []),
        ("transmit on", lambda t: _set(t, "ALLOW_ORDER_TRANSMIT", "true"), ["P5"]),
        # Daybreak build-uc r2 P2-1: the live-conjunction refusal (P1) must not hide the flag
        # itself (P5): every load refusal reports the independent raw-file conditions too
        ("live on alone", lambda t: _set(t, "ALLOW_LIVE_TRADING", "true"), ["P1", "P5"]),
        ("a mandate", lambda t: _set(t, "AUTONOMY_MANDATE_FILE", "/srv/mandate.json"), ["P6"]),
        ("no database url", lambda t: _set(t, "DATABASE_URL", None), ["P7"]),
        (
            "the shared default database",
            lambda t: _set(t, "DATABASE_URL", "sqlite:///data/chronos.db"),
            ["P7"],
        ),
        (
            "a relative database",
            lambda t: _set(t, "DATABASE_URL", "sqlite:///relative/b1.db"),
            ["P7"],
        ),
        ("an in-memory database", lambda t: _set(t, "DATABASE_URL", "sqlite:///:memory:"), ["P7"]),
        ("colliding client ids", lambda t: _set(t, "IB_DATA_CLIENT_ID", "17"), ["P8"]),
        ("an empty allowlist", lambda t: _set(t, "SYMBOL_ALLOWLIST", ""), ["P8"]),
        ("a credential-like key", lambda t: t + "IB_PASSWORD=dummy\n", ["P9"]),
        ("a username key", lambda t: t + "IB_USERNAME=dummy\n", ["P9"]),
        ("no pepper", lambda t: _set(t, "CHRONOS_CAPTURE_PEPPER", None), ["P10"]),
        ("a short pepper", lambda t: _set(t, "CHRONOS_CAPTURE_PEPPER", "abcd"), ["P10"]),
        ("an upper-case pepper", lambda t: _set(t, "CHRONOS_CAPTURE_PEPPER", "AB" * 32), ["P10"]),
        # P13 (DESIGN r2): IB_ACCOUNT_ALLOWLIST is a transmission-authority input; empty forms only
        (
            "the paper id on the account allowlist",
            lambda t: _set(t, "IB_ACCOUNT_ALLOWLIST", "DU7654321"),
            ["P13"],
        ),
        (
            "a JSON account allowlist",
            lambda t: _set(t, "IB_ACCOUNT_ALLOWLIST", '["DU7654321"]'),
            ["P13"],
        ),
        ("a bare account allowlist is fine", lambda t: _set(t, "IB_ACCOUNT_ALLOWLIST", ""), []),
        ("an empty JSON account allowlist", lambda t: _set(t, "IB_ACCOUNT_ALLOWLIST", "[]"), []),
    ],
)
def test_each_condition_fails_on_its_own_broken_shape(
    label: str, mutate: object, expected: list[str]
) -> None:
    text = mutate(_good_text())  # type: ignore[operator]
    assert text != _good_text() or not expected, label
    assert _ids(text) == expected, label


def test_a_loose_file_mode_is_refused() -> None:
    assert _ids(_good_text(), file_mode=0o644) == ["P11"]
    assert _ids(_good_text(), file_mode=0o100600) == []  # S_IFREG | 0600 as os.stat reports it


def test_an_exported_variable_that_would_outrank_the_file_is_refused() -> None:
    assert _ids(_good_text(), environ={"PATH": "/usr/bin", "IB_PORT": "7496"}) == ["P12"]
    assert _ids(_good_text(), environ={"ALLOW_ORDER_TRANSMIT": "false"}) == ["P12"]
    assert _ids(_good_text(), environ={"PATH": "/usr/bin", "HOME": "/home/x"}) == []


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        # Daybreak build-uc r1 P2-1: the U-c load refusal (the port) must not hide P5 and P13
        (
            {
                "IB_PORT": "7496",
                "ALLOW_ORDER_TRANSMIT": "true",
                "IB_ACCOUNT_ALLOWLIST": "DU7654321",
            },
            ["P3", "P5", "P13"],
        ),
        ({"IB_PORT": "4001", "BROKER_ADAPTER": "ib_async"}, ["P2", "P3"]),
        ({"IB_ACCOUNT_ID": "U1234567", "ALLOW_ORDER_TRANSMIT": "true"}, ["P4", "P5"]),
        # Daybreak build-uc r2 P2-1: the raw pass runs for EVERY load refusal, not only U-c's.
        # A refusal that has no raw form (P1, P8) is kept beside the raw findings.
        (  # the paper-transmission rule refuses first (P1); the blank account is P4
            {
                "IB_ACCOUNT_ID": "",
                "ALLOW_ORDER_TRANSMIT": "true",
                "IB_ACCOUNT_ALLOWLIST": "DU7654321",
            },
            ["P1", "P4", "P5", "P13"],
        ),
        (  # an unparseable port is a field error (P1) and, raw, not a paper port (P3)
            {
                "IB_PORT": "seven",
                "ALLOW_ORDER_TRANSMIT": "true",
                "IB_ACCOUNT_ALLOWLIST": "DU7654321",
            },
            ["P1", "P3", "P5", "P13"],
        ),
        (  # an unparseable flag is a field error (P1) and, raw, fails closed as P5
            {
                "ALLOW_ORDER_TRANSMIT": "maybe",
                "IB_PORT": "7496",
                "IB_ACCOUNT_ALLOWLIST": "DU7654321",
            },
            ["P1", "P3", "P5", "P13"],
        ),
        (  # a client-id collision refuses at load (P8); the flag and the allowlist stay visible
            {
                "IB_DATA_CLIENT_ID": "17",
                "ALLOW_ORDER_TRANSMIT": "true",
                "IB_ACCOUNT_ALLOWLIST": "DU7654321",
            },
            ["P5", "P8", "P13"],
        ),
    ],
)
def test_a_load_refusal_still_reports_every_independent_condition(
    changes: dict[str, str], expected: list[str]
) -> None:
    text = _good_text()
    for key, value in changes.items():
        text = _set(text, key, value)
    assert _ids(text) == expected


def test_several_broken_conditions_are_all_reported_in_order() -> None:
    text = _set(
        _set(_set(_good_text(), "IB_PORT", "7496"), "DATABASE_URL", None), "IB_ACCOUNT_ID", "U1"
    )
    assert _ids(text, file_mode=0o644) == ["P3", "P4", "P7", "P11"]


# --- the findings never leak a value -----------------------------------------------------------


def test_no_finding_echoes_a_value_from_the_file() -> None:
    sentinels = {
        "IB_ACCOUNT_ID": "ACCT-SENTINEL-5550001",
        "IB_HOST": "203.0.113.77",
        "DATABASE_URL": "sqlite:///leaky/relative-sentinel.db",
        "CHRONOS_CAPTURE_PEPPER": "zz-pepper-sentinel",
        "IB_LOGIN": "opaque-sentinel-value",
        "AUTONOMY_MANDATE_FILE": "/srv/mandate-sentinel.json",
    }
    text = _good_text()
    for key, value in sentinels.items():
        text = _set(text, key, value)
    findings = preflight_b1_env(text, file_mode=0o644, environ={"IB_PORT": "exported-sentinel"})
    assert {item[0] for item in findings} >= {"P4", "P6", "P7", "P9", "P10", "P11", "P12"}
    rendered = " | ".join(message for _id, message in findings)
    for value in (*sentinels.values(), "exported-sentinel"):
        assert value not in rendered, "a value leaked into a finding"


def test_a_refused_file_reports_settings_without_its_values() -> None:
    findings = preflight_b1_env(_set(_good_text(), "ALLOW_LIVE_TRADING", "true"))
    # P1 (the live conjunction) plus the raw P5 the refusal must not hide (Daybreak r2 P2-1)
    assert [item[0] for item in findings] == ["P1", "P5"]
    assert "ALLOW_LIVE_TRADING=true requires the full live conjunction" in findings[0][1]
    assert "DU7654321" not in findings[0][1]


# --- it never reads a .env, and the preflight is pure on its text -----------------------------


def test_the_preflight_does_not_depend_on_the_process_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IB_PORT", "7496")
    monkeypatch.setenv("BROKER_MODE", "demo")
    monkeypatch.setenv("IB_DATA_CLIENT_ID", "17")
    # the process environment is the caller's `environ` argument, never os.environ:
    assert preflight_b1_env(_good_text(), environ={}) == []


def test_a_dotenv_in_the_working_directory_and_in_home_is_never_opened_or_used(
    sentinel_dotenvs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The proof for the reader's P1: sentinel files with a canary are present, an audit hook
    watches every ``open`` event, the checker is run on a good shape and on a broken one, and
    nothing named ``.env`` is opened and the canary changes no verdict."""

    monkeypatch.setenv("IB_DATA_CLIENT_ID", "17")  # an exported canary as well
    with _watching("record") as seen:
        good = preflight_b1_env(_good_text(), file_mode=0o600, environ={})
        broken = _ids(_set(_good_text(), "IB_PORT", "7496"), file_mode=0o600)
        refused = _ids(_set(_good_text(), "ALLOW_LIVE_TRADING", "true"))
    assert seen == [], seen
    assert good == []  # the canary's colliding client id would have produced P8
    assert broken == ["P3"]
    assert refused == ["P1", "P5"]  # the raw P5 beside the P1 refusal (Daybreak r2 P2-1)
    with _watching("off"):  # this test's own look at the sentinels is not the checker's read
        for path in sentinel_dotenvs.values():
            assert _CANARY in path.read_text(encoding="utf-8")  # untouched, and still there


def test_the_loader_cannot_open_a_dotenv_even_without_the_env_file_argument(
    sentinel_dotenvs: dict[str, Path],
) -> None:
    """Belt and braces: ``env_file=None`` in the class config, not only the init argument."""

    kwargs = {"broker_mode": "ibkr", "ib_account_id": "DU7654321"}
    with _watching("record") as seen:
        settings = _FileOnlySettings(**kwargs)  # type: ignore[arg-type]
    assert seen == []
    assert settings.ib_data_client_id == 18  # the default; the canary's 17 was not read


def test_the_audit_hook_really_sees_plain_settings_read_a_dotenv(
    sentinel_dotenvs: dict[str, Path],
) -> None:
    """Positive control for the proof above (and a reproduction of the reader's P1)."""

    with (
        _watching("record") as seen,
        pytest.raises(ValidationError, match="IB_DATA_CLIENT_ID must differ"),
    ):
        Settings(broker_mode="ibkr", ib_account_id="DU7654321")  # type: ignore[arg-type]
    assert all(Path(item).name == ".env" for item in seen)
    assert seen, "the hook did not see pydantic-settings open the .env"


def test_the_audit_hook_blocks_every_way_of_opening_a_dotenv(
    sentinel_dotenvs: dict[str, Path],
) -> None:
    """Unlike a ``builtins.open`` patch, it also sees ``Path.read_text`` and ``os.open``."""

    attempts: dict[str, Callable[[Path], object]] = {
        "open": lambda p: open(p, encoding="utf-8").close(),
        "io.open": lambda p: io.open(p, encoding="utf-8").close(),  # noqa: UP020
        "Path.read_text": lambda p: p.read_text(encoding="utf-8"),
        "Path.open": lambda p: p.open(encoding="utf-8").close(),
        "os.open": lambda p: os.close(os.open(p, os.O_RDONLY)),
    }
    for name, attempt in attempts.items():
        with pytest.raises(AssertionError, match=r"a \.env was opened"):
            attempt(sentinel_dotenvs["home"])
        assert name


def test_the_hook_ignores_other_files(tmp_path: Path) -> None:
    other = tmp_path / ".env.example"
    with _watching("off"):
        other.write_text("X=1\n", encoding="utf-8")
    assert other.read_text(encoding="utf-8") == "X=1\n"  # raise mode is on; not a ".env"


def test_the_preflight_source_names_no_path_to_open() -> None:
    """A second guard only: an AST check cannot see a library's read (that is what P1 was)."""

    import ast
    import inspect

    source = inspect.getsource(preflight_b1_env) + inspect.getsource(_load_settings)
    tree = ast.parse(source)
    called = {
        node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    }
    assert not called & {"open", "read_text", "read_bytes", "connect", "create_connection"}
    assert "_FileOnlySettings(" in source
    assert "_env_file=None" in source


# --- the runbook and the preflight agree -------------------------------------------------------


def _runbook() -> str:
    return _RUNBOOK.read_text(encoding="utf-8")


def _section(text: str, heading: str) -> str:
    return text.split(heading, 1)[1].split("\n## ", 1)[0]


def test_the_runbook_and_the_preflight_list_the_same_conditions() -> None:
    """Same ids AND the same condition text: rewording or inverting one side fails."""

    table = _section(_runbook(), "## Preflight conditions")
    documented = {
        key: " ".join(text.split())
        for key, text in re.findall(r"^\| (P\d+) \| (.+?) \| [^|]* \|$", table, re.M)
    }
    assert documented == {key: " ".join(text.split()) for key, text in CONDITIONS.items()}


def test_the_runbook_cites_this_test_and_asks_for_no_value_on_a_command_line() -> None:
    text = _runbook()
    assert "tests/unit/test_b1_env_preflight.py" in text
    assert "NOT RUN" in text
    for line in text.splitlines():
        assert not re.search(r"(PASSWORD|SECRET)=\S", line), line


def test_no_runbook_command_prints_a_value() -> None:
    """The reader's P3-1: an env listing shows names only; the pepper goes straight to the file."""

    lines = _runbook().splitlines()
    for number, line in enumerate(lines, start=1):
        if "env | grep" in line:
            assert "cut -d= -f1" in line, f"line {number} lists environment VALUES: {line}"
        if "token_hex" in line:
            assert ">> .env" in line or ">>.env" in line, f"line {number} prints the pepper: {line}"


def test_the_exported_prefixes_match_between_the_runbook_step_and_the_preflight() -> None:
    step = _section(_runbook(), "### Step 1")
    grep = re.search(r"grep -E '\^\(([^)]*)\)'", step)
    assert grep is not None
    assert tuple(grep.group(1).split("|")) == _AMBIENT_PREFIXES


def test_the_rollback_returns_to_demo_and_leaves_the_b1_database_alone() -> None:
    """The reader's P2: BROKER_MODE=demo with the B1 DATABASE_URL still set would refuse to boot."""

    rollback = _section(_runbook(), "## Rollback")
    step2 = rollback.split("2. **", 1)[1].split("3. **", 1)[0]
    assert "BROKER_MODE=demo" in step2
    assert re.search(r"(remove|comment out)[^\n]*DATABASE_URL", step2, re.I), step2
    assert "sqlite:///data/chronos.db" in step2  # the verification prints the demo database
    step3 = rollback.split("3. **", 1)[1].split("4. **", 1)[0]
    assert "untouched" in step3 or "leave" in step3.lower()
    assert "different file" in step3
