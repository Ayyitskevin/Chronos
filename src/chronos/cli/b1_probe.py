"""Connected B1 probe: ``python -m chronos.cli b1-probe`` (B1 readiness U-g). RED.

The connected half of the B1 preflight (the offline half is ``b1-preflight``, U-d). It opens a
real brokerage session and reads account state, so **every real invocation is Kevin's act**:
it is run only on his word, on the capture host, against the paper gateway he provisioned. It
answers, in order and with codes, "why didn't it connect" and "which symbols lack data".

**It cannot connect without a passed, in-process admission.** :func:`admit_b1_paper` runs U-d's
P1-P13 on the private ``.env`` text (read once through the ``SECRET`` file admission), checks
the file's stat and the per-run declared account scope, and returns either a :class:`Refusal`
or an :class:`AdmittedB1Paper` token that only it can create (a module-private seal). The token
carries exactly the keys the probe needs: host, port, both client ids, the paper account id, the
symbols, the environment/mode/adapter, both transmission flags (false) and that no mandate is
set. **The pepper, any allowlist and every other key are dropped at projection**; the parsed
mapping lives only in local variables of the admission (memory is not zeroized: stated, not
claimed away). :func:`run_probe` is the only entry that connects. Its first statements re-check
the token's type and seal and re-evaluate every predicate on the token's own fields; then it
builds ONE ``Settings`` from the token alone (no process environment, no ``.env``), compares it
field by field with the token (the substitution check), and only then constructs the adapter
(``_load_ibapi``) and connects. A refusal at any step leaves zero loader and zero connect calls.

**It reads through a read-only view.** The probe sees only :class:`_ReadOnlyBrokerView`, which
holds the adapter privately and exposes ``connect``, ``disconnect``, ``connection_status``,
``server_time``, ``account_summary``, ``qualify_underlying`` and the snapshot quote. The view
exists because ``preview_order`` reaches the venue (``placeOrder`` with ``whatIf``) even with
transmission off, so admitted Settings alone do not make every order method refuse before the
wire.

**Managed scope is exact, with an irreversible latch.** After the handshake the gateway's
managed set must EQUAL the expected scope (``{IB_ACCOUNT_ID}``, or for one run the set declared
with ``--managed-accounts``), every id paper-shaped. Any later scope-change call or connection
uncertainty latches a failure, checked after every read and after teardown. The probe's own
teardown close is the one uncertainty event it expects.

**Output is condition codes and counts only**: never an account id, a value or the pepper.
Exit codes follow ``b1-preflight``: 0 CLEAR, 78 REFUSE, 1 UNDECIDED. It never reports the
Read-Only API checkbox as a PASS (it cannot be observed without an order call).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import json
import os
import re
import stat
import threading
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from dotenv import dotenv_values
from pydantic_settings import SettingsConfigDict

from chronos.cli.b1_preflight import (
    _CHECKLIST_PLACEHOLDER_ACCOUNT,
    EXIT_CLEAR,
    EXIT_REFUSED,
    EXIT_UNDECIDED,
    _load_settings,
    preflight_b1_env,
)
from chronos.config.settings import PAPER_API_PORTS, Settings
from chronos.domain.accounts import is_paper_account_id
from chronos.domain.enums import BrokerAdapter, BrokerMode, IBEnvironment
from chronos.domain.models import AccountSummary, MarketQuote, UnderlyingContract
from chronos.utils.secure_files import AuthorityMode, UnsafeAuthorityFile, read_authority_file

if TYPE_CHECKING:  # the adapter is imported where the probe connects, never at CLI import:
    # every other command (b1-preflight's no-broker pin) must not load a broker module
    from chronos.broker.official_ibkr import OfficialIBKRBroker

#: the most accounts one run may declare with --managed-accounts (a small fixed bound)
MAX_DECLARED_ACCOUNTS = 4
_ENV_LABEL = "chronos-probe env file"
#: the reason the callback bridge gives when a session closes (the probe's own teardown)
_CLOSED_REASON = "official IBKR connection closed"
_NOTICE_CODE = re.compile(r"^\[(-?\d+)\]")
_SEAL = object()


# --- the admission ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AdmittedB1Paper:
    """The only thing :func:`run_probe` connects with; created only by :func:`admit_b1_paper`."""

    host: str
    port: int
    client_id: int
    data_client_id: int
    account_id: str = field(repr=False)
    symbols: tuple[str, ...]
    environment: str
    broker_mode: str
    broker_adapter: str
    allow_order_transmit: bool
    allow_live_trading: bool
    mandate_absent: bool
    expected_scope: frozenset[str] = field(repr=False)
    _seal: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._seal is not _SEAL:
            raise TypeError("an AdmittedB1Paper is created only by admit_b1_paper")


@dataclass(frozen=True, slots=True)
class Refusal:
    """Why admission refused: condition ids and messages, never a value."""

    conditions: tuple[str, ...]
    reasons: tuple[str, ...]


def admit_b1_paper(
    env_text: str,
    file_stat: os.stat_result,
    environ: Mapping[str, str],
    *,
    declared_accounts: tuple[str, ...] | None = None,
) -> AdmittedB1Paper | Refusal:
    """Admit the B1 paper configuration, or refuse. Never opens a path; never connects.

    ``env_text`` is the private ``.env`` the caller read through the ``SECRET`` file admission;
    ``file_stat`` is that file's stat (re-checked here as a belt); ``environ`` is the process
    environment (P12: nothing relevant may be exported); ``declared_accounts`` is the per-run
    ``--managed-accounts`` list from argv, or None for the singleton ``{IB_ACCOUNT_ID}``.
    """

    found: list[tuple[str, str]] = list(_stat_findings(file_stat))
    found.extend(preflight_b1_env(env_text, file_mode=file_stat.st_mode, environ=environ))
    if found:
        return _refusal(found)
    values = {k: v for k, v in dotenv_values(stream=io.StringIO(env_text)).items() if v is not None}
    settings = _load_settings(values)
    del values
    if isinstance(settings, str):  # P1 passed above, so this is a contradiction: refuse
        return _refusal([("P1", settings)])
    account = settings.ib_account_id.strip()
    scope = _expected_scope(declared_accounts, account)
    if isinstance(scope, Refusal):
        return scope
    token = AdmittedB1Paper(
        host=settings.ib_host,
        port=int(settings.ib_port),
        client_id=int(settings.ib_client_id),
        data_client_id=int(settings.ib_data_client_id),
        account_id=account,
        symbols=tuple(settings.symbol_allowlist),
        environment=settings.ib_environment.value,
        broker_mode=settings.broker_mode.value,
        broker_adapter=settings.broker_adapter.value,
        allow_order_transmit=bool(settings.allow_order_transmit),
        allow_live_trading=bool(settings.allow_live_trading),
        mandate_absent=settings.autonomy_mandate_file is None,
        expected_scope=scope,
        _seal=_SEAL,
    )
    del settings
    problems = _predicate_failures(token)
    if problems:
        return _refusal([("ADMISSION", problem) for problem in problems])
    return token


def _refusal(found: list[tuple[str, str]]) -> Refusal:
    return Refusal(
        conditions=tuple(condition for condition, _ in found),
        reasons=tuple(f"[{condition}] {message}" for condition, message in found),
    )


def _stat_findings(file_stat: os.stat_result) -> Iterator[tuple[str, str]]:
    if not stat.S_ISREG(file_stat.st_mode):
        yield ("FILE", "the env file is not a regular file")
    if file_stat.st_uid != os.geteuid():
        yield ("FILE", "the env file is not owned by this user")
    if file_stat.st_nlink != 1:
        yield ("FILE", "the env file has more than one link")


def _expected_scope(declared: tuple[str, ...] | None, account: str) -> frozenset[str] | Refusal:
    """The exact managed set the gateway must report: ``{account}``, or the declared set."""

    if declared is None:
        return frozenset({account})
    problems: list[tuple[str, str]] = []
    if not declared:
        problems.append(("SCOPE", "--managed-accounts is empty"))
    if len(declared) > MAX_DECLARED_ACCOUNTS:
        problems.append(("SCOPE", f"--managed-accounts lists more than {MAX_DECLARED_ACCOUNTS}"))
    if len(set(declared)) != len(declared):
        problems.append(("SCOPE", "--managed-accounts lists an account twice"))
    if any(not is_paper_account_id(entry) for entry in declared):
        problems.append(("SCOPE", "--managed-accounts lists an id that is not paper-shaped"))
    if account not in declared:
        problems.append(("SCOPE", "--managed-accounts does not list IB_ACCOUNT_ID"))
    if problems:
        return _refusal(problems)
    return frozenset(declared)


def _predicate_failures(token: AdmittedB1Paper) -> list[str]:
    """Every admission predicate, evaluated on the token's own fields (twice: here and in
    :func:`run_probe`). Each failure names the predicate, never a value."""

    problems: list[str] = []
    if token.broker_mode != BrokerMode.IBKR.value:
        problems.append("BROKER_MODE is not ibkr")
    if token.broker_adapter != BrokerAdapter.OFFICIAL_IBKR.value:
        problems.append("BROKER_ADAPTER is not official_ibkr")
    if token.environment != IBEnvironment.PAPER.value:
        problems.append("IB_ENVIRONMENT is not paper")
    if token.host != "127.0.0.1":
        problems.append("IB_HOST is not exactly 127.0.0.1")
    if token.port not in PAPER_API_PORTS:
        problems.append("IB_PORT is not a paper port")
    if not is_paper_account_id(token.account_id) or (
        token.account_id == _CHECKLIST_PLACEHOLDER_ACCOUNT
    ):
        problems.append("IB_ACCOUNT_ID is not a paper-shaped id or is the placeholder")
    if token.client_id == 0 or token.data_client_id == 0:
        problems.append("a client id is 0")
    if token.client_id == token.data_client_id:
        problems.append("IB_CLIENT_ID equals IB_DATA_CLIENT_ID")
    if token.allow_order_transmit or token.allow_live_trading:
        problems.append("a transmission flag is true")
    if not token.mandate_absent:
        problems.append("AUTONOMY_MANDATE_FILE is set")
    if not token.symbols:
        problems.append("SYMBOL_ALLOWLIST is empty")
    scope = token.expected_scope
    if (
        token.account_id not in scope
        or not 1 <= len(scope) <= MAX_DECLARED_ACCOUNTS
        or any(not is_paper_account_id(entry) for entry in scope)
    ):
        problems.append("the expected managed scope is not a paper set holding IB_ACCOUNT_ID")
    return problems


# --- one Settings, from the token only --------------------------------------------------------


class _TokenOnlySettings(Settings):
    """``Settings`` whose only source is the init arguments: no process env, no ``.env``."""

    model_config = SettingsConfigDict(env_file=None)

    @classmethod
    def settings_customise_sources(  # type: ignore[no-untyped-def]
        cls, settings_cls, init_settings, env_settings, dotenv_settings, file_secret_settings
    ):
        return (init_settings,)


def _settings_from(token: AdmittedB1Paper) -> Settings:
    return _TokenOnlySettings(  # type: ignore[call-arg]
        _env_file=None,
        broker_mode=BrokerMode(token.broker_mode),
        broker_adapter=BrokerAdapter(token.broker_adapter),
        ib_environment=IBEnvironment(token.environment),
        ib_host=token.host,
        ib_port=token.port,
        ib_client_id=token.client_id,
        ib_data_client_id=token.data_client_id,
        ib_account_id=token.account_id,
        symbol_allowlist=token.symbols,
        ib_account_allowlist=(),
        allow_order_transmit=False,
        allow_live_trading=False,
    )


def _substitution_failures(settings: Settings, token: AdmittedB1Paper) -> list[str]:
    """The constructed Settings must say exactly what the token says (before any loader)."""

    expected: dict[str, object] = {
        "broker_mode": token.broker_mode,
        "broker_adapter": token.broker_adapter,
        "ib_environment": token.environment,
        "ib_host": token.host,
        "ib_port": token.port,
        "ib_client_id": token.client_id,
        "ib_data_client_id": token.data_client_id,
        "ib_account_id": token.account_id,
        "symbol_allowlist": token.symbols,
        "ib_account_allowlist": (),
        "allow_order_transmit": False,
        "allow_live_trading": False,
        "autonomy_mandate_file": None,
    }
    observed: dict[str, object] = {
        "broker_mode": settings.broker_mode.value,
        "broker_adapter": settings.broker_adapter.value,
        "ib_environment": settings.ib_environment.value,
        "ib_host": settings.ib_host,
        "ib_port": settings.ib_port,
        "ib_client_id": settings.ib_client_id,
        "ib_data_client_id": settings.ib_data_client_id,
        "ib_account_id": settings.ib_account_id,
        "symbol_allowlist": tuple(settings.symbol_allowlist),
        "ib_account_allowlist": tuple(settings.ib_account_allowlist),
        "allow_order_transmit": settings.allow_order_transmit,
        "allow_live_trading": settings.allow_live_trading,
        "autonomy_mandate_file": settings.autonomy_mandate_file,
    }
    problems = [
        f"{name} differs from the admission"
        for name in expected
        if expected[name] != observed[name]
    ]
    if settings.transmission_possible or settings.live_transmission_possible:
        problems.append("a transmission conjunction is true on the constructed Settings")
    return problems


# --- the read-only view and the scope latch ---------------------------------------------------


class _ReadOnlyBrokerView:
    """The only broker surface the probe sees: connection and read calls, nothing else."""

    __slots__ = ("__broker",)

    def __init__(self, broker: OfficialIBKRBroker) -> None:
        self.__broker = broker

    async def connect(self) -> None:
        await self.__broker.connect()

    async def disconnect(self) -> None:
        await self.__broker.disconnect()

    async def managed_accounts(self) -> tuple[str, ...]:
        status = await self.__broker.connection_status()
        return tuple(status.managed_accounts)

    async def server_time(self) -> datetime:
        return await self.__broker.server_time()

    async def account_summary(self) -> AccountSummary:
        return await self.__broker.account_summary()

    async def qualify_underlying(self, symbol: str) -> UnderlyingContract:
        return await self.__broker.qualify_underlying(symbol)

    async def snapshot_quote(self, contract: UnderlyingContract) -> MarketQuote:
        return await self.__broker.request_underlying_quote(contract)

    def notice_codes(self) -> tuple[int, ...]:
        """The codes of the gateway's id-less notices (G-C1), never their text."""

        codes: list[int] = []
        for notice in tuple(self.__broker.bridge.notices):
            match = _NOTICE_CODE.match(notice)
            if match:
                codes.append(int(match.group(1)))
        return tuple(codes)


class _ScopeLatch:
    """Set once by a managed-scope change or a connection uncertainty; never cleared."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tripped = threading.Event()
        self._first: str | None = None
        self._teardown = False

    def _trip(self, reason: str) -> None:
        with self._lock:
            if self._first is None:
                self._first = reason
            self._tripped.set()

    @contextlib.contextmanager
    def scope_changed(self, reason: str) -> Iterator[None]:
        self._trip(reason)
        yield

    def uncertain(self, reason: str) -> None:
        with self._lock:
            expected_close = self._teardown and reason == _CLOSED_REASON
        if not expected_close:
            self._trip(reason)

    def begin_teardown(self) -> None:
        with self._lock:
            self._teardown = True

    @property
    def first_reason(self) -> str | None:
        with self._lock:
            return self._first if self._tripped.is_set() else None


# --- the probe --------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProbeLine:
    level: str  # PASS | INFO | REFUSE | UNDECIDED
    check: str
    text: str

    def __str__(self) -> str:
        return f"[{self.level}] {self.check}: {self.text}"


@dataclass
class ProbeReport:
    lines: list[ProbeLine] = field(default_factory=list)

    def add(self, level: str, check: str, text: str) -> None:
        self.lines.append(ProbeLine(level, check, text))

    def verdict(self) -> tuple[str, int]:
        levels = {line.level for line in self.lines}
        if "REFUSE" in levels:
            return f"B1-PROBE REFUSE ({EXIT_REFUSED})", EXIT_REFUSED
        if "UNDECIDED" in levels or not self.lines:
            return f"B1-PROBE UNDECIDED ({EXIT_UNDECIDED})", EXIT_UNDECIDED
        return (
            f"B1-PROBE CLEAR ({EXIT_CLEAR}) — not checked: the Read-Only API checkbox (owner "
            "visual check), the clock-skew threshold (Q3, Kevin's)",
            EXIT_CLEAR,
        )


def run_probe(admitted: AdmittedB1Paper) -> ProbeReport:
    """Connect with the admission, read through the view, tear down. The ONLY connecting entry."""

    report = ProbeReport()
    if type(admitted) is not AdmittedB1Paper or getattr(admitted, "_seal", None) is not _SEAL:
        report.add("REFUSE", "admission", "not an admission made by admit_b1_paper; nothing ran")
        return report
    problems = _predicate_failures(admitted)
    if problems:
        for problem in problems:
            report.add("REFUSE", "admission", problem)
        return report
    settings = _settings_from(admitted)
    drift = _substitution_failures(settings, admitted)
    if drift:
        for problem in drift:
            report.add("REFUSE", "substitution", problem)
        return report
    from chronos.broker.base import BrokerError
    from chronos.broker.official_ibkr import OfficialIBKRBroker

    latch = _ScopeLatch()
    try:
        broker = OfficialIBKRBroker(
            settings,
            on_connection_uncertain=latch.uncertain,
            on_managed_account_scope_change=latch.scope_changed,
        )
    except BrokerError as error:
        report.add("REFUSE", "adapter", f"the adapter refused: {type(error).__name__}")
        return report
    asyncio.run(_session(_ReadOnlyBrokerView(broker), admitted, latch, report))
    return report


async def _session(
    view: _ReadOnlyBrokerView, token: AdmittedB1Paper, latch: _ScopeLatch, report: ProbeReport
) -> None:
    from chronos.broker.base import BrokerError

    connected = False
    try:
        try:
            await view.connect()
        except BrokerError as error:
            codes = ", ".join(str(code) for code in view.notice_codes()) or "none"
            report.add(
                "REFUSE",
                "handshake",
                f"{type(error).__name__}; gateway notice codes: {codes}",
            )
            return
        connected = True
        report.add("PASS", "handshake", "connected; the configured paper account is managed")
        if _latched(latch, report):
            return
        observed = frozenset(await view.managed_accounts())
        expected = token.expected_scope
        non_paper = sum(1 for account in observed if not is_paper_account_id(account))
        if observed != expected or non_paper:
            report.add(
                "REFUSE",
                "scope",
                f"the managed set is not exactly the expected scope (observed {len(observed)}, "
                f"expected {len(expected)}, not paper-shaped {non_paper}); no further reads",
            )
            return
        scope = (
            "the configured account only"
            if len(expected) == 1
            else (f"declared set of {len(expected)}")
        )
        report.add("PASS", "scope", f"scope: {scope}, exact")
        if _latched(latch, report):
            return
        server = await view.server_time()
        skew = (server - datetime.now(tz=UTC)).total_seconds()
        report.add(
            "INFO",
            "clock",
            f"serverTime minus local time: {skew:+.1f}s (threshold and warn/refuse: Q3, Kevin's)",
        )
        if _latched(latch, report):
            return
        summary = await view.account_summary()
        if summary.account_id == token.account_id:
            report.add("PASS", "account", "the account summary is for the configured account")
        else:
            report.add("REFUSE", "account", "the account summary is NOT for the configured account")
        if _latched(latch, report):
            return
        for symbol in token.symbols:
            try:
                contract = await view.qualify_underlying(symbol)
                quote = await view.snapshot_quote(contract)
            except BrokerError as error:
                code = getattr(error, "broker_error_code", None)
                report.add("INFO", "symbol", f"{symbol}: {type(error).__name__}, code {code}")
            else:
                prices = "present" if quote.bid is not None and quote.ask is not None else "absent"
                report.add(
                    "INFO",
                    "symbol",
                    f"{symbol}: qualified; quote {quote.data_quality.value}, bid/ask {prices}",
                )
            if _latched(latch, report):
                return
    finally:
        latch.begin_teardown()
        if connected:
            try:
                await view.disconnect()
            except BrokerError as error:
                report.add("REFUSE", "teardown", f"{type(error).__name__}: the reader may survive")
            else:
                report.add("PASS", "teardown", "disconnected; the reader thread stopped")
            _latched(latch, report)  # a failed handshake never had a session to latch


def _latched(latch: _ScopeLatch, report: ProbeReport) -> bool:
    reason = latch.first_reason
    if reason is None:
        return False
    if not any(line.check == "latch" for line in report.lines):
        report.add("REFUSE", "latch", f"managed scope or connection changed: {reason}")
    return True


# --- the command ------------------------------------------------------------------------------


def _declared(raw: str | None) -> tuple[str, ...] | None:
    if raw is None:
        return None
    return tuple(part.strip() for part in raw.split(","))


def cmd_b1_probe(args: argparse.Namespace) -> int:
    report = ProbeReport()
    try:
        path = Path(args.env_file)
        try:
            contents = read_authority_file(path, label=_ENV_LABEL, mode=AuthorityMode.SECRET)
        except FileNotFoundError:
            report.add("REFUSE", "env-file", "the private .env was not found; nothing was read")
        except UnsafeAuthorityFile:
            report.add(
                "REFUSE",
                "env-file",
                "the private .env failed the file admission (a regular file you own, mode 0600, "
                "one link, not a symlink); nothing was read",
            )
        else:
            admission = admit_b1_paper(
                contents.text,
                os.lstat(path),
                os.environ,
                declared_accounts=_declared(args.managed_accounts),
            )
            del contents
            if isinstance(admission, Refusal):
                for reason in admission.reasons:
                    report.add("REFUSE", "admission", reason)
            else:
                report = run_probe(admission)
        verdict, code = report.verdict()
    except Exception as error:  # one line, exit 1, never a traceback (it could carry a value)
        report = ProbeReport()
        verdict, code = f"B1-PROBE UNDECIDED ({EXIT_UNDECIDED}) — {type(error).__name__}", 1
    if args.json:
        payload = {
            "lines": [{"level": x.level, "check": x.check, "text": x.text} for x in report.lines],
            "verdict": verdict,
            "exit": code,
        }
        print(json.dumps(payload, indent=2))
        return code
    print(
        "Chronos b1-probe (CONNECTED, paper only, read-only calls; run only on Kevin's word; "
        "never prints an id or a value)"
    )
    for line in report.lines:
        print(line)
    print(verdict)
    return code


def add_b1_probe_command(sub: Any) -> None:
    """Register ``b1-probe`` on the operator CLI."""

    parser = sub.add_parser(
        "b1-probe",
        description=(
            "CONNECTED B1 paper probe. RED: it opens a real brokerage session, so run it only "
            "on Kevin's word. The private .env is admitted in-process first (P1-P13, paper port "
            "and account, flags false, no allowlist); a refusal never connects."
        ),
        help=(
            "CONNECTED B1 paper probe (RED: run only on Kevin's word); admits the private .env "
            "in-process, then makes read-only calls (exit 0 CLEAR, 78 REFUSE, 1 UNDECIDED)"
        ),
    )
    parser.add_argument("--env-file", default=".env", help="the private .env (default: ./.env)")
    parser.add_argument(
        "--managed-accounts",
        default=None,
        help=(
            "for this run only, the exact paper account set the gateway must manage "
            f"(comma-separated, must include IB_ACCOUNT_ID, at most {MAX_DECLARED_ACCOUNTS})"
        ),
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.set_defaults(func=cmd_b1_probe)
