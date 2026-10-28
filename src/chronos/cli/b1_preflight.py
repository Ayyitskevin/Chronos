"""Offline B1 preflight: ``python -m chronos.cli b1-preflight`` (B1 readiness U-d).

Run by the owner, on the capture host, before the first read-only B1 session. It answers, without
connecting to anything, whether this host's private ``.env`` and local state are what the B1
runbook (``docs/ops/b1-swap-runbook.md``) requires, and says what to fix when they are not.

**It reads secrets; it never prints, logs or writes one.** The private ``.env`` holds the capture
pepper (``CHRONOS_CAPTURE_PEPPER``) and must hold no IBKR credential. The command opens that file
once, through one descriptor, with the same admission as every other secret file in Chronos
(:func:`chronos.utils.secure_files.read_authority_file` in ``SECRET`` mode: not a symlink, a
regular file owned by this user, mode exactly 0600, one link, checked on the descriptor it then
reads). Parsed values stay in local variables of :func:`run_b1_preflight`; every finding names a
KEY or a condition id, never a value. The account id, the pepper and the database URL are never
echoed.

**It makes no network effect.** It opens no socket, imports no broker adapter and no broker
library (``ibapi`` is located and its source is READ as a file, never imported), and its
port check reads ``/proc/net/tcp*`` (advisory: it cannot attest which endpoint a later socket
uses). The Read-Only API checkbox in TWS/Gateway cannot be observed without an order call, so it
is reported UNVERIFIABLE, never CLEAR.

Exit codes follow ``db-preflight``: 0 CLEAR (nothing that ran would refuse; the verdict names
what was not checked), 78 REFUSE (a condition is unmet: fix and re-run), 1 UNDECIDED (a check
could not run). An unexpected error is one UNDECIDED line naming the exception class only.
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import importlib.metadata
import importlib.util
import io
import json
import os
import re
import sqlite3
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any

from dotenv import dotenv_values
from pydantic import TypeAdapter, ValidationError
from pydantic_settings import SettingsConfigDict

from chronos.cli.db_preflight import Report as DatabaseReport
from chronos.cli.db_preflight import run_preflight as run_database_preflight
from chronos.config.persistence_key import (
    PERSISTENCE_KEY_ENV,
    PersistenceKeyError,
    count_key_lines,
    resolve_persistence_key,
)
from chronos.config.settings import LIVE_API_PORTS, PAPER_API_PORTS, Settings
from chronos.control.halt import HaltStore
from chronos.domain.accounts import is_paper_account_id
from chronos.domain.enums import BrokerAdapter, BrokerMode, IBEnvironment
from chronos.utils.identifiers import account_fingerprint
from chronos.utils.secure_files import AuthorityMode, UnsafeAuthorityFile, read_authority_file

EXIT_CLEAR = 0
EXIT_UNDECIDED = 1
#: ``EX_CONFIG``: what refuses is the host's configuration, not this command.
EXIT_REFUSED = 78

_PAPER_PORTS = PAPER_API_PORTS  # one source of truth: chronos.config.settings (B1 U-c)
_LIVE_PORTS = LIVE_API_PORTS
_CHECKLIST_PLACEHOLDER_ACCOUNT = "DU1234567"
_DEFAULT_DATABASE_URL = "sqlite:///data/chronos.db"
_PEPPER_SHAPE = re.compile(r"[0-9a-f]{64}")
_AMBIENT_PREFIXES = ("BROKER_", "IB_", "ALLOW_", "AUTONOMY_", "CHRONOS_")
#: Settings loads case-insensitively, so an exported name outranks the file in any case it folds
_AMBIENT_FOLDED = tuple(prefix.casefold() for prefix in _AMBIENT_PREFIXES)
_CREDENTIAL_KEY = re.compile(r"(PASS(WORD)?|USER(NAME)?|LOGIN|SECRET|TOKEN)", re.IGNORECASE)
_PROC_NET = Path("/proc/net")
_ENV_LABEL = "chronos-preflight env file"
_TCP_LISTEN = "0A"

#: condition id -> what the runbook requires (the runbook's table carries the same ids AND text;
#: ``tests/unit/test_b1_env_preflight.py`` fails if either drifts).
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
    "P13": "IB_ACCOUNT_ALLOWLIST is absent or empty (a transmission-authority input)",
}

_NOT_CHECKED = (
    "the gateway login and its paper banner, the Read-Only API checkbox (owner visual check), "
    "market-data entitlements, that the account id is YOUR paper account, the pepper's "
    "provenance, and anything a connection would show (that is b1-probe, U-g)"
)


# --- the env-file conditions (pure on text; the test module pins them) -------------------------


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

    ambient = sorted(k for k in (environ or {}) if k.casefold().startswith(_AMBIENT_FOLDED))
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
        # Settings itself enforces several of the conditions at load (since B1 U-c the port,
        # client id and account shape too): report the refusal under its own condition id
        condition = _condition_of_load_refusal(settings)
        # Settings stops at its FIRST refusal, whichever validator raised it (a U-c shape
        # rule, the live conjunction, a field that would not parse, a client-id clash), so
        # every condition that needs no constructed Settings is evaluated on the file's own
        # values for EVERY load refusal (Daybreak build-uc r2 P2-1, r3 P2-3), and reported
        # once each: the refusal keeps its own bounded message under its condition id, and the
        # raw pass adds every OTHER condition it found (P1 alone has no raw form).
        found.append((condition, settings))
        found.extend((cid, message) for cid, message in _raw_findings(values) if cid != condition)
        return sorted(set(found), key=_order)

    if not (
        settings.broker_mode.value == "ibkr"
        and settings.broker_adapter.value == "official_ibkr"
        and settings.ib_environment.value == "paper"
    ):
        found.append(("P2", _MODE_MESSAGE))
    if settings.ib_host != "127.0.0.1" or settings.ib_port not in _PAPER_PORTS:
        found.append(("P3", _PORT_MESSAGE))
    account = settings.ib_account_id
    if not is_paper_account_id(account) or account.strip() == _CHECKLIST_PLACEHOLDER_ACCOUNT:
        found.append(("P4", _ACCOUNT_MESSAGE))
    if (
        settings.allow_order_transmit
        or settings.allow_live_trading
        or settings.transmission_possible
        or settings.live_transmission_possible
    ):
        found.append(("P5", _TRANSMIT_MESSAGE))
    if settings.ib_account_allowlist:
        found.append(("P13", _ALLOWLIST_MESSAGE))
    return sorted(set(found), key=_order)


_PORT_MESSAGE = "IB_HOST is not 127.0.0.1 or IB_PORT is not a paper port"
_ACCOUNT_MESSAGE = "IB_ACCOUNT_ID is not a paper-shaped id or is the placeholder"


_MODE_MESSAGE = "broker mode/adapter/environment are not ibkr/official_ibkr/paper"
_TRANSMIT_MESSAGE = "a transmission flag or derived conjunction is true"
_CLIENT_IDS_MESSAGE = (
    "IB_CLIENT_ID is 0 or equals IB_DATA_CLIENT_ID, or SYMBOL_ALLOWLIST is empty (or one of "
    "them would not parse)"
)
_ALLOWLIST_MESSAGE = (
    "IB_ACCOUNT_ALLOWLIST is not empty: it is a transmission-authority input and stays "
    "empty for the read-only campaign (it is added only by a separate paper-"
    "transmission decision)"
)
_UNPARSEABLE = object()


def _raw_field(values: Mapping[str, str], name: str) -> Any:
    """The file's value for one Settings field, parsed with THAT FIELD's own annotation and
    validators (so the rules cannot drift from Settings'); the field default when absent;
    ``_UNPARSEABLE`` when the value would not parse (callers fail closed on it)."""

    info = Settings.model_fields[name]
    lowered = {key.lower(): raw for key, raw in values.items()}
    if name not in lowered:
        return info.default
    annotation: Any = (
        Annotated[(info.annotation, *info.metadata)] if info.metadata else info.annotation
    )
    try:
        return TypeAdapter(annotation).validate_python(lowered[name])
    except ValidationError:
        return _UNPARSEABLE


def _raw_findings(values: Mapping[str, str]) -> list[tuple[str, str]]:
    """Every condition that needs no constructed Settings, from the raw file values.

    Used after ANY Settings load refusal: Settings stops at its FIRST refusal, so without this
    an early refusal (a U-c shape rule, the live conjunction, an unparseable field, a client-id
    clash) would hide independent findings (Daybreak build-uc r1 P2-1, r2 P2-1, r3 P2-3). The
    conditions decided here are P2, P3, P4, P5, P8 and P13; P6, P7 and P9-P12 are decided before
    the load, and P1 is the refusal itself, so no condition can hide behind another. An
    unparseable value fails closed into its condition. Messages never carry a value.
    """

    found: list[tuple[str, str]] = []
    if (
        _raw_field(values, "broker_mode") is not BrokerMode.IBKR
        or _raw_field(values, "broker_adapter") is not BrokerAdapter.OFFICIAL_IBKR
        or _raw_field(values, "ib_environment") is not IBEnvironment.PAPER
    ):
        found.append(("P2", _MODE_MESSAGE))
    port = _raw_field(values, "ib_port")
    if _raw_field(values, "ib_host") != "127.0.0.1" or port not in _PAPER_PORTS:
        found.append(("P3", _PORT_MESSAGE))
    account = _raw_field(values, "ib_account_id")
    if (
        not isinstance(account, str)
        or not is_paper_account_id(account)
        or account.strip() == _CHECKLIST_PLACEHOLDER_ACCOUNT
    ):
        found.append(("P4", _ACCOUNT_MESSAGE))
    if any(
        _raw_field(values, flag) is not False
        for flag in ("allow_order_transmit", "allow_live_trading")
    ):
        found.append(("P5", _TRANSMIT_MESSAGE))
    client_id = _raw_field(values, "ib_client_id")
    data_client_id = _raw_field(values, "ib_data_client_id")
    symbols = _raw_field(values, "symbol_allowlist")
    if (
        _UNPARSEABLE in (client_id, data_client_id, symbols)
        or client_id == 0
        or client_id == data_client_id
        or not symbols
    ):
        found.append(("P8", _CLIENT_IDS_MESSAGE))
    if _raw_field(values, "ib_account_allowlist") != ():
        found.append(("P13", _ALLOWLIST_MESSAGE))
    return found


def _condition_of_load_refusal(message: str) -> str:
    if re.search(r"IB_DATA_CLIENT_ID|SYMBOL_ALLOWLIST|IB_CLIENT_ID=0", message):
        return "P8"
    if re.search(r"requires a (paper|live) port", message):
        return "P3"
    if "IB_ACCOUNT_ID" in message and "transmission" not in message:
        return "P4"
    return "P1"


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
        # the bounded rule text only; never the structured input (include_input=False)
        return "Settings refused the file: " + error.errors(include_input=False)[0]["msg"][:200]


# --- the report ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Finding:
    """``LEVEL [check] text``; levels: CLEAR REFUSE UNDECIDED ADVISORY UNVERIFIABLE INFO."""

    level: str
    check: str
    text: str

    def __str__(self) -> str:
        return f"{self.level} [{self.check}] {self.text}"


@dataclass
class B1Report:
    findings: list[Finding] = field(default_factory=list)

    def add(self, level: str, check: str, text: str) -> None:
        self.findings.append(Finding(level, check, text))

    def verdict(self) -> tuple[str, int]:
        refused = [f.check for f in self.findings if f.level == "REFUSE"]
        undecided = [f.check for f in self.findings if f.level == "UNDECIDED"]
        if refused:
            return (
                f"B1-PREFLIGHT REFUSED ({len(refused)}: {', '.join(dict.fromkeys(refused))}) — "
                "fix each REFUSE line and re-run; do not start a B1 session",
                EXIT_REFUSED,
            )
        if undecided:
            return (
                f"B1-PREFLIGHT UNDECIDED ({len(undecided)}: {', '.join(undecided)}) — a check "
                "could not run; nothing is known about it",
                EXIT_UNDECIDED,
            )
        checked = ", ".join(dict.fromkeys(f.check for f in self.findings if f.level == "CLEAR"))
        return (
            f"B1-PREFLIGHT CLEAR — checked: {checked}; NOT checked: {_NOT_CHECKED}",
            EXIT_CLEAR,
        )


# --- the checks ------------------------------------------------------------------------------


def run_b1_preflight(
    *,
    env_file: Path,
    environ: Mapping[str, str],
    halt_file: Path,
    proc_net: Path = _PROC_NET,
) -> B1Report:
    """Run every offline check; never raises for a check's own failure (it becomes a line)."""

    report = B1Report()
    try:
        contents = read_authority_file(env_file, label=_ENV_LABEL, mode=AuthorityMode.SECRET)
    except FileNotFoundError:
        report.add(
            "REFUSE",
            "env-file",
            f"{_ENV_LABEL} at {env_file}: not found; create the private .env first "
            "(runbook step 6)",
        )
    except UnsafeAuthorityFile as unsafe:
        report.add(
            "REFUSE",
            "env-file",
            f"{unsafe}; nothing was read from it (runbook step 6: a regular file you own, "
            "mode 0600, one link, not a symlink)",
        )
    else:
        _env_lines(report, contents.text, environ)
    _guarded(report, "ibapi", _ibapi_lines)
    _guarded(report, "ports", lambda r: _port_lines(r, proc_net))
    _guarded(report, "halt", lambda r: _halt_lines(r, halt_file))
    report.add(
        "UNVERIFIABLE",
        "read-only-api",
        "the TWS/Gateway 'Read-Only API' checkbox cannot be observed without an order call, "
        "which this command never makes: owner visual check (runbook step 3)",
    )
    return report


def _guarded(report: B1Report, check: str, run: Callable[[B1Report], None]) -> None:
    try:
        run(report)
    except Exception as error:  # a check that cannot run is UNDECIDED, never a traceback
        report.add("UNDECIDED", check, f"the check could not run ({type(error).__name__})")


def _env_lines(report: B1Report, text: str, environ: Mapping[str, str]) -> None:
    findings = preflight_b1_env(text, environ=environ)
    for condition, message in findings:
        report.add("REFUSE", condition, message)
    if not findings:
        report.add(
            "CLEAR",
            "env",
            f"P1-P13 met ({len(CONDITIONS)} conditions; P11 by the admission: a regular, "
            "owner-only 0600 file read through one descriptor)",
        )
    # KEYSRC: the backend refuses a key named more than once (it counts the same lines), and the
    # last line is the one the settings below would load: say so here, before any scope verdict
    named = count_key_lines(text)
    if named > 1:
        report.add(
            "REFUSE",
            "key-source",
            f"{PERSISTENCE_KEY_ENV} is named on {named} lines of the file (any case, export forms "
            "and empty values included); the backend refuses to start unless exactly one names it: "
            "keep one (the value is not shown)",
        )
    values = {k: v for k, v in dotenv_values(stream=io.StringIO(text)).items() if v is not None}
    settings = _load_settings(values)
    if isinstance(settings, str):
        report.add("INFO", "database", "not checked: the file's settings did not load (P1/P8)")
        return
    _guarded(report, "database", lambda r: _database_lines(r, settings))
    _guarded(report, "kill-switch", lambda r: _kill_switch_lines(r, settings))


def _ibapi_lines(report: B1Report) -> None:
    """Locate the installed ``ibapi`` and read ``EWrapper.error``'s signature from its SOURCE.

    Never imports it: a package import runs its code, and ``ibapi`` opens sockets. The shape
    decides R-ARITY (B1R-F2) before any connection: the adapter's ``error(*args)`` reads
    ``(reqId, errorCode, errorString[, json])`` and drops a NON-int first value.
    """

    spec = importlib.util.find_spec("ibapi")
    if spec is None or not spec.submodule_search_locations:
        report.add(
            "REFUSE",
            "ibapi",
            "the official TWS API (ibapi) is not installed in this interpreter: install it from "
            "IBKR's distribution and record its version (runbook step 5)",
        )
        return
    package = Path(next(iter(spec.submodule_search_locations)))
    try:
        version = f"version {importlib.metadata.version('ibapi')}"
    except importlib.metadata.PackageNotFoundError:
        version = "version not in the package metadata (record what `pip show ibapi` prints)"
    wrapper = package / "wrapper.py"
    try:
        source = wrapper.read_text(encoding="utf-8")
    except OSError:
        report.add("UNDECIDED", "ibapi", f"ibapi found ({version}) but wrapper.py is unreadable")
        return
    params = _error_parameters(source)
    if params is None:
        report.add(
            "UNDECIDED",
            "ibapi",
            f"ibapi {version}: EWrapper.error has an unrecognized signature; compare it with "
            "the adapter's error(*args) before connecting (K4 #1)",
        )
    elif params[:3] == ["reqId", "errorCode", "errorString"] and len(params) <= 4:
        report.add(
            "CLEAR",
            "ibapi",
            f"ibapi {version}: EWrapper.error({', '.join(params)}) is the shape the adapter reads",
        )
    elif len(params) >= 2 and params[1] == "errorTime":
        report.add(
            "REFUSE",
            "ibapi",
            f"B1R-F2: the installed ibapi ({version}) calls EWrapper.error({', '.join(params)}); "
            "the adapter would read the error TIME as the code, so connection-loss codes would "
            "never drop readiness. Do not connect until the adapter reads this shape (K4 #1)",
        )
    else:
        report.add(
            "UNDECIDED",
            "ibapi",
            f"ibapi {version}: EWrapper.error({', '.join(params)}) is not a shape the adapter "
            "is known to read (K4 #1)",
        )


def _error_parameters(source: str) -> list[str] | None:
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ClassDef) and node.name == "EWrapper":
            for item in ast.iter_child_nodes(node):  # direct members, no `.body` read
                if isinstance(item, ast.FunctionDef) and item.name == "error":
                    arguments = item.args
                    if arguments.vararg or arguments.kwarg or arguments.kwonlyargs:
                        return None
                    names = [a.arg for a in [*arguments.posonlyargs, *arguments.args]]
                    return names[1:] if names[:1] == ["self"] else None
    return None


def _database_path(settings: Settings) -> Path:
    # P7 has already required an absolute sqlite file URL ("sqlite:////abs/path").
    return Path(settings.database_url.removeprefix("sqlite:///"))


def _database_lines(report: B1Report, settings: Settings) -> None:
    inner = DatabaseReport()
    run_database_preflight(
        inner, database_url=settings.database_url, ledger=None, evidence=False, settings=settings
    )
    refused = [line.text for line in inner.lines if line.level == "REFUSE"]
    undecided = [line.text for line in inner.lines if line.level == "UNDECIDED"]
    if refused:
        report.add("REFUSE", "database", "db-preflight would refuse: " + "; ".join(refused))
    elif undecided:
        report.add(
            "UNDECIDED", "database", "db-preflight could not decide: " + "; ".join(undecided)
        )
    else:
        report.add("CLEAR", "database", "db-preflight checks pass (identity, mode, schema)")
    _scope_lines(report, _database_path(settings), settings)


def _scope_lines(report: B1Report, path: Path, settings: Settings) -> None:
    # N3: the backend resolves the persistence key before it creates or binds anything, so a
    # file without a well-formed key cannot start whatever the database holds. Resolved from
    # the FILE's own settings (never os.environ); the value is never echoed.
    try:
        key = resolve_persistence_key(settings)
    except PersistenceKeyError:
        report.add(
            "REFUSE",
            "scope",
            f"{PERSISTENCE_KEY_ENV} is absent from the file or not 64 lowercase hex digits; the "
            "backend would refuse to start (the value is not shown)",
        )
        return
    account = settings.ib_account_id
    if not path.exists():
        report.add(
            "CLEAR",
            "scope",
            "absent: the first backend start creates the file and binds it to this paper account",
        )
        return
    uri = f"file:{path}?mode=ro"
    with contextlib.closing(sqlite3.connect(uri, uri=True)) as connection:
        try:
            row = connection.execute(
                "SELECT broker_mode, environment, account_fingerprint "
                "FROM database_scope WHERE id = 1"
            ).fetchone()
        except sqlite3.Error:
            report.add("UNDECIDED", "scope", "the database's scope row could not be read")
            return
    if row is None:
        report.add(
            "CLEAR",
            "scope",
            "unbound: it binds to this paper account at the first backend start (a file that "
            "already holds account data without a scope would refuse then; not checked here)",
        )
    elif tuple(row) == ("ibkr", "paper", account_fingerprint(account, key=key)):
        report.add("CLEAR", "scope", "already bound to this paper account")
    else:
        report.add(
            "REFUSE",
            "scope",
            "this database is already bound to a different broker scope or a different "
            "persistence key than this file's: find which before changing anything (a new "
            "DATABASE_URL starts a separate store, and if the key changed, one bound under a key "
            "that may not be escrowed)",
        )


def _kill_switch_lines(report: B1Report, settings: Settings) -> None:
    """Display the kill-switch state from its JSON file, read here rather than through
    ``chronos.orders.kill_switch``: importing anything under ``chronos.orders`` runs that
    package's ``__init__``, which loads the order plane and with it ``chronos.broker``, and this
    command imports no broker module. Display only: the authority is ``LiveKillSwitch.read()``
    at startup, which also consults the installation's generation marker (not read here)."""

    path = Path(settings.live_kill_switch_file)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        engaged = payload["engaged"] if isinstance(payload, dict) else None
    except FileNotFoundError:
        report.add(
            "INFO",
            "kill-switch",
            "no kill-switch file: a fresh install reads DISENGAGED (startup reads ENGAGED if "
            "this installation wrote one before: its generation marker is not read here)",
        )
        return
    except (OSError, ValueError, KeyError, TypeError):
        engaged = None
    if not isinstance(engaged, bool):
        report.add(
            "ADVISORY",
            "kill-switch",
            "the kill-switch file is unreadable, so startup reads it as ENGAGED (fail closed); "
            "inspect it before the session (docs/IBKR_RUNBOOK.md)",
        )
    elif engaged:
        report.add("INFO", "kill-switch", "ENGAGED (no order is sent in a read-only session)")
    else:
        report.add("INFO", "kill-switch", "disengaged (no order is sent in a read-only session)")


def _halt_lines(report: B1Report, halt_file: Path) -> None:
    state = HaltStore(halt_file).read()
    reason = state.reason.value if state.reason is not None else "none"
    status = "halted" if state.halted else "armed"
    report.add("INFO", "halt", f"platform {status} (reason: {reason})")


def _listening_ports(proc_net: Path) -> set[int]:
    ports: set[int] = set()
    for name in ("tcp", "tcp6"):
        table = proc_net / name
        if not table.exists():
            continue
        for line in table.read_text(encoding="ascii", errors="replace").splitlines()[1:]:
            fields = line.split()
            if len(fields) > 3 and fields[3] == _TCP_LISTEN and ":" in fields[1]:
                ports.add(int(fields[1].rsplit(":", 1)[1], 16))
    return ports


def _port_lines(report: B1Report, proc_net: Path) -> None:
    if not (proc_net / "tcp").exists() and not (proc_net / "tcp6").exists():
        report.add("ADVISORY", "ports", f"not checked: no TCP tables under {proc_net}")
        return
    listening = _listening_ports(proc_net)
    live = sorted(listening & _LIVE_PORTS)
    paper = sorted(listening & _PAPER_PORTS)
    if live:
        report.add(
            "REFUSE",
            "ports",
            f"a LIVE API port is listening ({', '.join(map(str, live))}): you may be in a live "
            "session; stop and log in to paper (runbook stop rule)",
        )
    if paper:
        report.add(
            "ADVISORY",
            "ports",
            f"a paper API port is listening ({', '.join(map(str, paper))}); advisory: this "
            "cannot attest which endpoint a later connection uses",
        )
    elif not live:
        report.add(
            "ADVISORY",
            "ports",
            "no paper API port (7497/4002) is listening yet: start TWS/Gateway in paper before "
            "the session (runbook step 3)",
        )


# --- the command -----------------------------------------------------------------------------


def cmd_b1_preflight(args: argparse.Namespace) -> int:
    try:
        report = run_b1_preflight(
            env_file=Path(args.env_file),
            environ=os.environ,
            halt_file=Path(args.halt_file),
            proc_net=Path(args.proc_net),
        )
        verdict, code = report.verdict()
    except Exception as error:  # one line, exit 1, never a traceback (it could carry a value)
        report = B1Report()
        verdict, code = f"B1-PREFLIGHT UNDECIDED (1) — {type(error).__name__}", EXIT_UNDECIDED
    if args.json:
        payload = {
            "findings": [
                {"level": f.level, "check": f.check, "text": f.text} for f in report.findings
            ],
            "verdict": verdict,
            "exit": code,
        }
        print(json.dumps(payload, indent=2))
        return code
    print(
        "Chronos b1-preflight (offline; reads the private .env, never prints a value, "
        "never connects)"
    )
    for finding in report.findings:
        print(finding)
    print(verdict)
    return code


def add_b1_preflight_command(sub: Any) -> None:
    """Register ``b1-preflight`` on the operator CLI."""

    parser = sub.add_parser(
        "b1-preflight",
        help=(
            "offline B1 readiness check of the private .env and local state (reads secrets, "
            "never prints one, never connects; exit 0 CLEAR, 78 REFUSE, 1 UNDECIDED)"
        ),
    )
    parser.add_argument("--env-file", default=".env", help="the private .env (default: ./.env)")
    parser.add_argument(
        "--halt-file", default="data/platform_halt.json", help="the platform halt state file"
    )
    parser.add_argument(
        "--proc-net", default=str(_PROC_NET), help="where the kernel's TCP tables are read"
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.set_defaults(func=cmd_b1_preflight)
