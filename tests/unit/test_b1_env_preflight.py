"""B1 env preflight: every NON-SECRET condition ``docs/ops/b1-swap-runbook.md`` requires of the
private ``.env``, checked on a candidate file's TEXT with dummy values.

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
import stat
import sys
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from dotenv import dotenv_values
from pydantic import ValidationError
from pydantic_settings import SettingsConfigDict

from chronos.config.settings import Settings

_REPO = Path(__file__).resolve().parents[2]
_CHECKLIST = _REPO / "docs" / "ops" / "m4-read-only-gate-session-checklist.md"
_RUNBOOK = _REPO / "docs" / "ops" / "b1-swap-runbook.md"

_PAPER_PORTS = frozenset({7497, 4002})
_PAPER_ACCOUNT = re.compile(r"D[UF]\d{4,}")
_CHECKLIST_PLACEHOLDER_ACCOUNT = "DU1234567"
_DEFAULT_DATABASE_URL = "sqlite:///data/chronos.db"
_PEPPER_SHAPE = re.compile(r"[0-9a-f]{64}")
_AMBIENT_PREFIXES = ("BROKER_", "IB_", "ALLOW_", "AUTONOMY_", "CHRONOS_")
_CREDENTIAL_KEY = re.compile(r"(PASS(WORD)?|USER(NAME)?|LOGIN|SECRET|TOKEN)", re.IGNORECASE)

#: condition id -> what the runbook requires (the runbook's table carries the same ids).
CONDITIONS: dict[str, str] = {
    "P1": "the file's settings load (Settings accepts them) with no process-env override",
    "P2": "BROKER_MODE=ibkr, BROKER_ADAPTER=official_ibkr, IB_ENVIRONMENT=paper",
    "P3": "IB_HOST is 127.0.0.1 and IB_PORT is a paper port (7497 TWS or 4002 Gateway)",
    "P4": "IB_ACCOUNT_ID has the paper shape D[UF]+digits and is not the checklist placeholder",
    "P5": "ALLOW_ORDER_TRANSMIT and ALLOW_LIVE_TRADING are false and both conjunctions are false",
    "P6": "AUTONOMY_MANDATE_FILE is unset",
    "P7": "DATABASE_URL is set to its own absolute sqlite file, not the shared default",
    "P8": "IB_CLIENT_ID and IB_DATA_CLIENT_ID differ; SYMBOL_ALLOWLIST is non-empty",
    "P9": "no IBKR credential-like key is in the file",
    "P10": "CHRONOS_CAPTURE_PEPPER is present with the 64-hex shape (value never echoed)",
    "P11": "the file mode is 0600 (when a mode is supplied)",
    "P12": (
        "no BROKER_*/IB_*/ALLOW_*/AUTONOMY_*/CHRONOS_* variable is exported (it outranks the file)"
    ),
}


def preflight_b1_env(
    text: str,
    *,
    file_mode: int | None = None,
    environ: Mapping[str, str] | None = None,
) -> list[tuple[str, str]]:
    """Return ``(condition id, message)`` for each unmet condition; ``[]`` means all met.

    Pure on its inputs: parses ``text`` as dotenv and loads ``Settings`` from those values
    alone (``_FileOnlySettings``: no process environment, no env file); the caller's
    environment is only the ``environ`` mapping it passes. Never opens a path. Messages never
    contain a value.
    """

    found: list[tuple[str, str]] = []
    values = {k: v for k, v in dotenv_values(stream=io.StringIO(text)).items() if v is not None}

    ambient = sorted(k for k in (environ or {}) if k.startswith(_AMBIENT_PREFIXES))
    if ambient:
        found.append(("P12", f"exported variables outrank the file: {', '.join(ambient)}"))

    credential_keys = sorted(k for k in values if _CREDENTIAL_KEY.search(k))
    if credential_keys:
        found.append(("P9", f"credential-like keys in the file: {', '.join(credential_keys)}"))

    pepper = values.get("CHRONOS_CAPTURE_PEPPER")
    if pepper is None or not _PEPPER_SHAPE.fullmatch(pepper):
        found.append(("P10", "CHRONOS_CAPTURE_PEPPER is missing or not 64 lowercase hex digits"))

    if file_mode is not None and stat.S_IMODE(file_mode) != 0o600:
        found.append(("P11", f"file mode is {oct(stat.S_IMODE(file_mode))}, expected 0o600"))

    if "AUTONOMY_MANDATE_FILE" in values:
        found.append(("P6", "AUTONOMY_MANDATE_FILE is set in the file (must stay unset)"))

    database_url = values.get("DATABASE_URL", "")
    if not database_url:
        found.append(("P7", "DATABASE_URL is not set: the shared default would be used"))
    elif database_url == _DEFAULT_DATABASE_URL:
        found.append(("P7", "DATABASE_URL equals the shared default file"))
    elif not database_url.startswith("sqlite:////") or ":memory:" in database_url:
        found.append(("P7", "DATABASE_URL is not an absolute sqlite file (sqlite:////abs/path)"))

    settings = _load_settings(values)
    if isinstance(settings, str):
        # Settings itself enforces the client-id and allowlist rules: name them as P8
        condition = "P8" if re.search(r"IB_DATA_CLIENT_ID|SYMBOL_ALLOWLIST", settings) else "P1"
        found.append((condition, settings))
        return sorted(found, key=_order)

    if not (
        settings.broker_mode.value == "ibkr"
        and settings.broker_adapter.value == "official_ibkr"
        and settings.ib_environment.value == "paper"
    ):
        found.append(("P2", "broker mode/adapter/environment are not ibkr/official_ibkr/paper"))
    if settings.ib_host != "127.0.0.1" or settings.ib_port not in _PAPER_PORTS:
        found.append(("P3", "IB_HOST is not 127.0.0.1 or IB_PORT is not a paper port"))
    account = settings.ib_account_id
    if not _PAPER_ACCOUNT.fullmatch(account) or account == _CHECKLIST_PLACEHOLDER_ACCOUNT:
        found.append(("P4", "IB_ACCOUNT_ID is not a paper-shaped id or is the placeholder"))
    if (
        settings.allow_order_transmit
        or settings.allow_live_trading
        or settings.transmission_possible
        or settings.live_transmission_possible
    ):
        found.append(("P5", "a transmission flag or derived conjunction is true"))
    return sorted(set(found), key=_order)


def _order(item: tuple[str, str]) -> tuple[int, str]:
    return int(item[0][1:]), item[1]


class _FileOnlySettings(Settings):
    """``Settings`` that cannot read a ``.env`` and whose only source is the init arguments.

    ``env_file=None`` in the config stops the file being opened even if a caller forgets the
    ``_env_file=None`` argument; ``settings_customise_sources`` drops the process environment.
    """

    model_config = SettingsConfigDict(env_file=None)

    @classmethod
    def settings_customise_sources(  # type: ignore[no-untyped-def]
        cls, settings_cls, init_settings, env_settings, dotenv_settings, file_secret_settings
    ):
        return (init_settings,)


def _load_settings(values: Mapping[str, str]) -> Settings | str:
    """Build Settings from the file's own values only (never os.environ, never any .env)."""

    try:
        fields: dict[str, Any] = {k.lower(): v for k, v in values.items()}
        return _FileOnlySettings(_env_file=None, **fields)  # type: ignore[call-arg]
    except ValidationError as error:
        # the message names fields and rules, not values: keep only the first line
        return "Settings refused the file: " + error.errors()[0]["msg"][:200]


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
        ("live on alone", lambda t: _set(t, "ALLOW_LIVE_TRADING", "true"), ["P1"]),
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
    assert [item[0] for item in findings] == ["P1"]
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
    assert refused == ["P1"]
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
