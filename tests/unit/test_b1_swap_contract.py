"""B1 swap rehearsal (SIM-HARNESS U5): the swap to the real paper adapter is config-only.

B1 puts ``OfficialIBKRBroker`` on a paper gateway in place of the demo broker. This module
rehearses everything about that swap that can be proven without a gateway, and pins the
parts a Protocol-only contract would miss (the adapter's contract with the order plane is
wider than ``chronos.broker.base.Broker``):

1. the EXACT ``.env`` block of ``docs/ops/m4-read-only-gate-session-checklist.md`` §3.2 is
   read from the document itself, loaded through ``Settings``, and leaves both transmission
   conjunctions False (and the two controls that flip them fail the way the docs say);
2. ``chronos.runtime.build_runtime`` really runs end to end on that shape with the adapter's
   construction replaced by a recording stand-in, and constructs ``OfficialIBKRBroker`` once,
   with a call that binds to the REAL constructor's signature and supplies every hook it takes
   (``live_kill_switch``, ``on_connection_uncertain``, ``on_managed_account_scope_change``),
   wired to the latch the submission boundary and the connection manager share;
3. those hooks have their safety effect: a connection drop between claim and send makes the
   send guard refuse, and a managed-account scope change drops readiness.

No connection is ever opened: a socket tripwire fails the test on any attempt, the real adapter
class is never instantiated (only its signature and source are read), and ``ibapi`` is not
needed. Every path the runtime writes (database, kill-switch file, session baseline) is a
temporary one and ``configure_logging`` is stubbed, because it would otherwise leave the
``chronos`` logger non-propagating for every later test. Tests only; nothing here can reach a
venue, and ``ALLOW_ORDER_TRANSMIT`` / ``ALLOW_LIVE_TRADING`` stay false in the loaded shape.
"""

from __future__ import annotations

import ast
import inspect
import io
import re
import socket
import sys
import textwrap
import types
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from dotenv import dotenv_values
from pydantic import ValidationError
from tests.support.order_fakes import FIXED_NOW, FakeBroker

import chronos.runtime as runtime_module
from chronos.broker.base import BrokerSafetyError
from chronos.broker.callbacks import CallbackBridge
from chronos.broker.demo import DemoBroker
from chronos.broker.official_ibkr import OfficialIBKRBroker, verify_environment_port
from chronos.config.settings import Settings
from chronos.control.modes import ExecutionCapability, TradingMode, resolve_mode_lock
from chronos.domain.enums import (
    BrokerAdapter,
    BrokerMode,
    ConnectionState,
    DataQuality,
    DisplayEnvironment,
    IBEnvironment,
    ReconciliationStatus,
)
from chronos.domain.models import ConnectionStatus

_REPO = Path(__file__).resolve().parents[2]
_CHECKLIST = _REPO / "docs" / "ops" / "m4-read-only-gate-session-checklist.md"

#: The keys of checklist §3.2, exactly. ``AUTONOMY_MANDATE_FILE`` is deliberately NOT a key
#: there: the block carries it only as a comment ("stays unset").
_B1_KEYS = frozenset(
    {
        "BROKER_MODE",
        "BROKER_ADAPTER",
        "IB_ENVIRONMENT",
        "IB_HOST",
        "IB_PORT",
        "IB_CLIENT_ID",
        "IB_ACCOUNT_ID",
        "ALLOW_ORDER_TRANSMIT",
        "ALLOW_LIVE_TRADING",
        "SYMBOL_ALLOWLIST",
    }
)
#: Variables that, if exported, silently beat the ``.env`` (process environment outranks it).
_AMBIENT = sorted(
    _B1_KEYS
    | {
        "AUTONOMY_MANDATE_FILE",
        "IB_ACCOUNT_ALLOWLIST",
        "IB_DATA_CLIENT_ID",
        "CHRONOS_CAPTURE_PEPPER",
    }
)
#: The hooks ``runtime.build_runtime`` hands the official adapter (``runtime.py:321-326``).
_ADAPTER_HOOKS = frozenset(
    {"live_kill_switch", "on_connection_uncertain", "on_managed_account_scope_change"}
)


def _b1_env_block() -> str:
    """The fenced ``dotenv`` block under checklist heading ``### 3.2``, verbatim."""

    text = _CHECKLIST.read_text(encoding="utf-8")
    section = text.split("### 3.2", 1)[1]
    match = re.search(r"```dotenv\n(.*?)```", section, re.S)
    assert match is not None, "checklist §3.2 no longer carries a dotenv block"
    return match.group(1)


@pytest.fixture(autouse=True)
def _no_socket_is_ever_opened(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tripwire: this module may not open a network connection, whatever it exercises."""

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("the B1 rehearsal attempted a network connection")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


@pytest.fixture
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """No ambient copy of any B1 key: the process environment would outrank the file."""

    for name in _AMBIENT:
        monkeypatch.delenv(name, raising=False)


def _write_env(directory: Path, *extra_lines: str, name: str = ".env") -> Path:
    path = directory / name
    path.write_text(_b1_env_block() + "".join(f"{line}\n" for line in extra_lines))
    return path


def _b1_settings(directory: Path, *extra_lines: str, **overrides: object) -> Settings:
    """``Settings`` from the §3.2 block (+ appended lines). Later lines win, as in a real file."""

    return Settings(_env_file=str(_write_env(directory, *extra_lines)), **overrides)  # type: ignore[arg-type]


def _runtime_settings(directory: Path, *extra_lines: str) -> Settings:
    """The B1 shape plus ONLY temporary file locations, so a build writes nothing in the repo."""

    return _b1_settings(
        directory,
        *extra_lines,
        database_url=f"sqlite:///{directory / 'chronos.db'}",
        live_kill_switch_file=directory / "live_kill_switch.json",
        session_baseline_file=directory / "session_baseline.json",
        log_file=directory / "chronos.log",
    )


# ---------------------------------------------------------------------------
# 1. The documented shape, and what it loads as.
# ---------------------------------------------------------------------------


def test_the_checklist_env_block_is_exactly_the_documented_b1_shape() -> None:
    block = _b1_env_block()
    values = {k: v for k, v in dotenv_values(stream=io.StringIO(block)).items() if v is not None}

    assert set(values) == _B1_KEYS
    assert values["BROKER_MODE"] == "ibkr"
    assert values["BROKER_ADAPTER"] == "official_ibkr"
    assert values["IB_ENVIRONMENT"] == "paper"
    assert values["IB_HOST"] == "127.0.0.1"
    assert values["IB_PORT"] in {"7497", "4002"}
    assert values["IB_CLIENT_ID"] == "17"
    assert re.fullmatch(r"D[UF]\d{4,}", values["IB_ACCOUNT_ID"])
    assert (values["ALLOW_ORDER_TRANSMIT"], values["ALLOW_LIVE_TRADING"]) == ("false", "false")
    assert values["SYMBOL_ALLOWLIST"] == "AAPL,MSFT,SPY"
    # The mandate file is only ever a comment here, and no key looks like a credential.
    assert not any(line.startswith("AUTONOMY_MANDATE_FILE") for line in block.splitlines())
    assert "AUTONOMY_MANDATE_FILE stays unset" in block
    assert not [k for k in values if re.search(r"PASS|SECRET|TOKEN|PEPPER|USER|PWD", k)]


@pytest.mark.usefixtures("clean_environment")
def test_the_b1_env_loads_as_paper_official_ibkr_with_transmission_impossible(
    tmp_path: Path,
) -> None:
    settings = _b1_settings(tmp_path)

    assert settings.broker_mode is BrokerMode.IBKR
    assert settings.broker_adapter is BrokerAdapter.OFFICIAL_IBKR
    assert settings.ib_environment is IBEnvironment.PAPER
    assert (settings.ib_host, settings.ib_port, settings.ib_client_id) == ("127.0.0.1", 7497, 17)
    assert settings.ib_data_client_id != settings.ib_client_id
    assert settings.symbol_allowlist == ("AAPL", "MSFT", "SPY")
    assert settings.allow_order_transmit is False
    assert settings.allow_live_trading is False
    assert settings.autonomy_mandate_file is None
    assert settings.ib_account_allowlist == ()
    assert settings.transmission_possible is False
    assert settings.live_transmission_possible is False


@pytest.mark.usefixtures("clean_environment")
def test_only_the_two_flags_keep_transmission_impossible(tmp_path: Path) -> None:
    """Controls: the False above comes from the flags, so it can flip, and live is refused."""

    transmitting = _b1_settings(tmp_path, "ALLOW_ORDER_TRANSMIT=true")
    assert transmitting.transmission_possible is True
    assert transmitting.live_transmission_possible is False

    with pytest.raises(ValidationError, match="ALLOW_LIVE_TRADING=true requires the full live"):
        _b1_settings(tmp_path, "ALLOW_LIVE_TRADING=true")


@pytest.mark.usefixtures("clean_environment")
def test_the_paper_mode_lock_denies_the_b1_shape_and_names_what_it_needs(tmp_path: Path) -> None:
    settings = _b1_settings(tmp_path)

    def lock(*, enabled: bool, allowlist: tuple[str, ...]):
        return resolve_mode_lock(
            requested_mode=TradingMode.PAPER,
            paper_account_allowlist=allowlist,
            broker_reported_account_id=settings.ib_account_id,
            broker_reported_environment_is_paper=True,
            order_transmission_enabled=enabled,
        )

    denied = lock(enabled=settings.allow_order_transmit, allowlist=settings.ib_account_allowlist)
    assert denied.may_submit_paper is False
    assert denied.capability is ExecutionCapability.NO_ORDERS
    assert "order transmission is disabled (ALLOW_ORDER_TRANSMIT is false)" in denied.denial_reasons
    assert "paper account allowlist is empty" in denied.denial_reasons

    # Control: exactly those two things, and nothing else, open the paper lock.
    opened = lock(enabled=True, allowlist=(settings.ib_account_id,))
    assert opened.may_submit_paper is True
    assert opened.paper_account_id == settings.ib_account_id


@pytest.mark.usefixtures("clean_environment")
def test_the_adapters_own_preconditions_accept_the_b1_shape(tmp_path: Path) -> None:
    """What the real constructor checks before it would connect (account id, allowlist, port)."""

    settings = _b1_settings(tmp_path)

    verify_environment_port(settings.ib_environment, settings.ib_port)  # must not raise
    assert settings.ib_account_id.strip()
    assert not settings.ib_account_allowlist or settings.ib_account_id in (
        settings.ib_account_allowlist
    )
    # Controls: the port check bites, in both directions.
    for live_port in (7496, 4001):
        with pytest.raises(BrokerSafetyError, match="requires a paper port"):
            verify_environment_port(IBEnvironment.PAPER, live_port)
    with pytest.raises(BrokerSafetyError, match="requires a live port"):
        verify_environment_port(IBEnvironment.LIVE, 7497)


@pytest.mark.parametrize(
    ("name", "value", "check"),
    [
        ("BROKER_MODE", "demo", lambda s: s.broker_mode is BrokerMode.DEMO),
        ("ALLOW_ORDER_TRANSMIT", "true", lambda s: s.transmission_possible is True),
    ],
)
def test_an_exported_variable_silently_beats_the_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, value: str, check: object
) -> None:
    """The process environment outranks ``.env``: B1 must start from a clean environment."""

    for ambient in _AMBIENT:
        monkeypatch.delenv(ambient, raising=False)
    monkeypatch.setenv(name, value)

    settings = _b1_settings(tmp_path)

    assert check(settings)  # type: ignore[operator]
    # And the file's own value was not what won.
    assert dotenv_values(stream=io.StringIO(_b1_env_block()))[name] != value


# ---------------------------------------------------------------------------
# 2. The real constructor's whole surface, and what runtime hands it.
# ---------------------------------------------------------------------------


def test_the_real_constructors_hook_surface_is_exactly_what_the_runtime_wires() -> None:
    parameters = list(inspect.signature(OfficialIBKRBroker.__init__).parameters.values())[1:]

    assert parameters[0].name == "settings"
    assert parameters[0].default is inspect.Parameter.empty
    hooks = {p.name: p for p in parameters[1:]}
    assert set(hooks) == _ADAPTER_HOOKS, "the adapter grew or lost a constructor hook"
    for parameter in hooks.values():
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
        assert parameter.default is None

    bridge = inspect.signature(CallbackBridge.__init__).parameters
    assert {"on_connection_uncertain", "on_managed_account_scope_change"} <= set(bridge)


def _calls_with_keywords(source: str, callee: str) -> list[dict[str, str]]:
    """Keyword arguments (name -> simple-name value) of every ``callee(...)`` call in ``source``."""

    found: list[dict[str, str]] = []
    for node in ast.walk(ast.parse(textwrap.dedent(source))):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == callee:
            found.append(
                {
                    keyword.arg: keyword.value.id
                    for keyword in node.keywords
                    if keyword.arg is not None and isinstance(keyword.value, ast.Name)
                }
            )
    return found


def test_the_adapter_forwards_both_hooks_into_the_callback_bridge() -> None:
    """Closes the chain statically: runtime -> constructor -> bridge -> classifier."""

    adapter = _calls_with_keywords(inspect.getsource(OfficialIBKRBroker.__init__), "CallbackBridge")
    assert len(adapter) == 1
    assert adapter[0]["on_connection_uncertain"] == "on_connection_uncertain"
    assert adapter[0]["on_managed_account_scope_change"] == "on_managed_account_scope_change"

    classifier = _calls_with_keywords(
        inspect.getsource(CallbackBridge.__init__), "UnsolicitedObservationClassifier"
    )
    assert len(classifier) == 1
    assert classifier[0]["invalidate"] == "on_connection_uncertain"


# ---------------------------------------------------------------------------
# 3. build_runtime on the B1 shape, with the adapter's construction replaced.
# ---------------------------------------------------------------------------


class _PaperSessionBroker(FakeBroker):
    """A venue-free stand-in that answers like a connected PAPER session at startup.

    ``build_bound_runtime_scope`` requires, of the startup observations: one connected
    account, ``last_successful_sync == account.as_of``, environment PAPER and market-data
    quality UNKNOWN. The real adapter's compliance with that is only provable at first
    contact (S1); this stand-in states the expectation the runtime holds it to.
    """

    def __init__(self, account_id: str) -> None:
        super().__init__(account_id=account_id)
        self.account = account_id

    async def connection_status(self) -> ConnectionStatus:
        return ConnectionStatus(
            state=ConnectionState.CONNECTED,
            environment=DisplayEnvironment.PAPER,
            connected=True,
            account_id=self.account,
            data_quality=DataQuality.UNKNOWN,
            last_successful_sync=FIXED_NOW,
            managed_accounts=(self.account,),
        )


def _recording(kind: str) -> type[_PaperSessionBroker]:
    """A stand-in class that records exactly how the runtime constructed it."""

    class Recording(_PaperSessionBroker):
        instances: list[Recording] = []  # noqa: RUF012 - per-class record, reset by construction

        def __init__(self, settings: Settings, **hooks: object) -> None:
            super().__init__(settings.ib_account_id)
            self.settings = settings
            self.hooks = hooks
            type(self).instances.append(self)

    Recording.__name__ = Recording.__qualname__ = kind
    return Recording


@pytest.fixture
def stubbed_runtime(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """``build_runtime`` without its process-global side effects."""

    monkeypatch.setattr(runtime_module, "configure_logging", lambda *args, **kwargs: None)
    yield


def _build(monkeypatch: pytest.MonkeyPatch, settings: Settings):
    monkeypatch.setattr(runtime_module, "get_settings", lambda: settings)
    return runtime_module.build_runtime(register_atexit=False)


@pytest.mark.usefixtures("clean_environment", "stubbed_runtime")
def test_build_runtime_selects_the_official_adapter_and_wires_every_hook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _runtime_settings(tmp_path)
    official = _recording("OfficialIBKRBroker")
    monkeypatch.setattr("chronos.broker.official_ibkr.OfficialIBKRBroker", official)

    runtime = _build(monkeypatch, settings)
    try:
        # Exactly one construction, of the stand-in for the official adapter.
        (built,) = official.instances
        assert runtime.broker is built
        assert built.settings is settings
        assert set(built.hooks) == _ADAPTER_HOOKS
        assert all(hook is not None for hook in built.hooks.values())
        # The call the runtime made binds to the REAL constructor: the stand-in accepts
        # anything, so this is what makes "the right constructor wiring" a real claim.
        inspect.signature(OfficialIBKRBroker.__init__).bind(None, settings, **built.hooks)

        latch = runtime.reconciliation_readiness
        boundary = runtime.order_management.submission_boundary
        assert built.hooks["on_connection_uncertain"] == latch.invalidate
        assert built.hooks["on_managed_account_scope_change"] == latch.invalidating_transition
        # One kill switch and one latch are shared by the adapter, the boundary and the
        # connection manager (private attributes pinned on purpose: this IS the wiring).
        assert built.hooks["live_kill_switch"] is runtime.live_kill_switch
        assert boundary._live_kill_switch is runtime.live_kill_switch
        assert boundary._reconciliation_readiness is latch
        assert runtime.connection._readiness is latch

        # The startup scope locks everything, and the conjunctions are still False.
        assert runtime.runtime_scope.submission_locked is True
        assert runtime.runtime_scope.live_trading_locked is True
        assert latch.snapshot().status is ReconciliationStatus.PENDING
        assert runtime.settings.transmission_possible is False
        assert runtime.settings.live_transmission_possible is False
    finally:
        runtime.close()


@pytest.mark.usefixtures("clean_environment", "stubbed_runtime")
def test_the_wired_hooks_have_their_safety_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    official = _recording("OfficialIBKRBroker")
    monkeypatch.setattr("chronos.broker.official_ibkr.OfficialIBKRBroker", official)
    runtime = _build(monkeypatch, _runtime_settings(tmp_path))
    try:
        (built,) = official.instances
        latch = runtime.reconciliation_readiness

        def reconcile() -> int:
            generation = latch.begin_reconciliation("rehearsal")
            assert latch.complete(
                expected_generation=generation,
                status=ReconciliationStatus.RECONCILED,
                reason="rehearsal proof",
                reconciled_at=datetime.now(UTC),
            )
            return generation

        # Control: with no drop, a claimed submission is authorized at the send guard.
        generation = reconcile()
        with latch.submission_guard(expected_generation=generation) as claimed:
            assert claimed is True
            with latch.at_send() as authorized:
                assert authorized is True

        # A connection drop between claim and send makes the guard (what the adapter
        # enters around its send) refuse, and readiness is no longer RECONCILED.
        generation = reconcile()
        with latch.submission_guard(expected_generation=generation) as claimed:
            assert claimed is True
            built.hooks["on_connection_uncertain"]("simulated gateway drop")  # type: ignore[operator]
            with latch.at_send() as authorized:
                assert authorized is False
        snapshot = latch.snapshot()
        assert snapshot.status is ReconciliationStatus.PENDING
        assert snapshot.reason == "simulated gateway drop"

        # A managed-account scope change holds readiness locked while the scope is replaced.
        generation = reconcile()
        with built.hooks["on_managed_account_scope_change"]("scope changed"):  # type: ignore[operator]
            pass
        snapshot = latch.snapshot()
        assert snapshot.status is ReconciliationStatus.PENDING
        assert snapshot.reason == "scope changed"
        assert snapshot.generation > generation
    finally:
        runtime.close()


@pytest.mark.usefixtures("clean_environment", "stubbed_runtime")
@pytest.mark.parametrize("selector", ["demo", "ib_async"])
def test_other_selectors_choose_other_brokers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, selector: str
) -> None:
    """Control: the selection can fail, so the test above is not a tautology."""

    official = _recording("OfficialIBKRBroker")
    monkeypatch.setattr("chronos.broker.official_ibkr.OfficialIBKRBroker", official)
    ib_async = _recording("IBKRBroker")
    stub = types.ModuleType("chronos.broker.ibkr")
    stub.IBKRBroker = ib_async  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "chronos.broker.ibkr", stub)

    line = "BROKER_MODE=demo" if selector == "demo" else "BROKER_ADAPTER=ib_async"
    runtime = _build(monkeypatch, _runtime_settings(tmp_path, line))
    try:
        assert official.instances == []
        if selector == "demo":
            assert isinstance(runtime.broker, DemoBroker)
            assert ib_async.instances == []
        else:
            (built,) = ib_async.instances
            assert runtime.broker is built
            # Not the B1 target: it takes only the connection-uncertainty hook.
            assert set(built.hooks) == {"on_connection_uncertain"}
    finally:
        runtime.close()


# ---------------------------------------------------------------------------
# 4. Where the pepper lives: never in Settings, never in the B1 env.
# ---------------------------------------------------------------------------


def test_the_capture_pepper_is_not_a_setting_and_not_in_the_b1_env() -> None:
    assert not [name for name in Settings.model_fields if "pepper" in name.lower()]
    assert "PEPPER" not in _b1_env_block().upper()

    # .env.example offers it only as a commented line; nothing in src ever reads it.
    example = (_REPO / ".env.example").read_text(encoding="utf-8").splitlines()
    assert "#CHRONOS_CAPTURE_PEPPER=" in example
    assert not [line for line in example if line.startswith("CHRONOS_CAPTURE_PEPPER")]
    offenders = [
        str(path.relative_to(_REPO))
        for path in (_REPO / "src" / "chronos").rglob("*.py")
        if "CHRONOS_CAPTURE_PEPPER" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []
