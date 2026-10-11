"""B1 env preflight: every NON-SECRET condition ``docs/ops/b1-swap-runbook.md`` requires of the
private ``.env``, checked on a candidate file's TEXT with dummy values.

``preflight_b1_env`` takes text (and optionally a file mode and an environment mapping); it
never opens a path, never reads the repository's ``.env``, never connects, and its findings
name KEYS and condition ids only, never a value (the pepper's shape is checked, its digits are
not echoed). The tests drive it with dummy values built from the checklist's own §3.2 block;
nothing here is, or looks like, a credential. A tripwire fails any test that reads the real
``.env`` or opens a socket.

Each condition has an id (``P1``...). The runbook lists the same ids in its preflight table and
``test_the_runbook_and_the_preflight_list_the_same_conditions`` fails if the two drift.
Tests only; ``ALLOW_ORDER_TRANSMIT`` and ``ALLOW_LIVE_TRADING`` stay false in every shape loaded.
"""

from __future__ import annotations

import builtins
import io
import re
import socket
import stat
from collections.abc import Mapping
from pathlib import Path

import pytest
from dotenv import dotenv_values
from pydantic import ValidationError

from chronos.config.settings import Settings

_REPO = Path(__file__).resolve().parents[2]
_CHECKLIST = _REPO / "docs" / "ops" / "m4-read-only-gate-session-checklist.md"
_RUNBOOK = _REPO / "docs" / "ops" / "b1-swap-runbook.md"
_REAL_DOTENV = _REPO / ".env"

_PAPER_PORTS = frozenset({7497, 4002})
_PAPER_ACCOUNT = re.compile(r"D[UF]\d{4,}")
_CHECKLIST_PLACEHOLDER_ACCOUNT = "DU1234567"
_DEFAULT_DATABASE_URL = "sqlite:///data/chronos.db"
_PEPPER_SHAPE = re.compile(r"[0-9a-f]{64}")
_AMBIENT_PREFIXES = ("BROKER_", "IB_", "ALLOW_", "AUTONOMY_")
_CREDENTIAL_KEY = re.compile(r"(PASS(WORD)?|USER(NAME)?|LOGIN|SECRET|TOKEN)", re.IGNORECASE)

#: condition id -> what the runbook requires (the runbook's table carries the same ids).
CONDITIONS: dict[str, str] = {
    "P1": "the file settings load (Settings accepts them) with no process-env override",
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
    "P12": "no BROKER_*/IB_*/ALLOW_*/AUTONOMY_* variable is exported (it would outrank the file)",
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
    """``Settings`` whose only source is the init kwargs: no process environment, no env file."""

    @classmethod
    def settings_customise_sources(  # type: ignore[no-untyped-def]
        cls, settings_cls, init_settings, env_settings, dotenv_settings, file_secret_settings
    ):
        return (init_settings,)


def _load_settings(values: Mapping[str, str]) -> Settings | str:
    """Build Settings from the file's own values only (never os.environ, never an env file)."""

    try:
        return _FileOnlySettings(**{k.lower(): v for k, v in values.items()})  # type: ignore[arg-type]
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


@pytest.fixture(autouse=True)
def _never_the_real_dotenv_and_never_a_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tripwire: no test here may read the repository's real ``.env`` or open a connection."""

    real_open = builtins.open

    def guarded_open(file: object, *args: object, **kwargs: object) -> object:
        try:
            if Path(str(file)).resolve() == _REAL_DOTENV.resolve():
                raise AssertionError("the preflight tests tried to read the real .env")
        except OSError:
            pass
        return real_open(file, *args, **kwargs)  # type: ignore[call-overload]

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("the preflight tests attempted a network connection")

    monkeypatch.setattr(builtins, "open", guarded_open)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


# --- the good shape ----------------------------------------------------------------------------


def test_the_checklist_block_with_dummy_values_meets_every_condition() -> None:
    assert preflight_b1_env(_good_text(), file_mode=0o600, environ={}) == []


def test_the_good_shape_really_leaves_both_conjunctions_false() -> None:
    values = {k: v for k, v in dotenv_values(stream=io.StringIO(_good_text())).items() if v}
    settings = _FileOnlySettings(**{k.lower(): v for k, v in values.items()})  # type: ignore[arg-type]
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


# --- it never reads a real .env, and the preflight is pure on its text -------------------------


def test_the_preflight_does_not_depend_on_the_process_environment_or_a_real_env_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IB_PORT", "7496")
    monkeypatch.setenv("BROKER_MODE", "demo")
    # the process environment is the caller's `environ` argument, never os.environ:
    assert preflight_b1_env(_good_text(), environ={}) == []


def test_the_tripwire_really_stops_a_read_of_the_real_dotenv() -> None:
    with pytest.raises(AssertionError, match=r"real \.env"), builtins.open(_REAL_DOTENV):
        pass


def test_the_preflight_source_names_no_path_to_open() -> None:
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


# --- the runbook and the preflight agree -------------------------------------------------------


def test_the_runbook_and_the_preflight_list_the_same_conditions() -> None:
    text = _RUNBOOK.read_text(encoding="utf-8")
    table = text.split("## Preflight conditions", 1)[1].split("\n## ", 1)[0]
    documented = dict(re.findall(r"^\| (P\d+) \| (.+?) \|", table, re.M))
    assert set(documented) == set(CONDITIONS)


def test_the_runbook_cites_this_test_and_never_asks_for_a_value_in_a_command() -> None:
    text = _RUNBOOK.read_text(encoding="utf-8")
    assert "tests/unit/test_b1_env_preflight.py" in text
    assert "NOT RUN" in text
    for line in text.splitlines():
        assert not re.search(r"(PASSWORD|SECRET)=\S", line), line
