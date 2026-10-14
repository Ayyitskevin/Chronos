"""B1 U-g: ``b1-probe``, the CONNECTED stage, proven ONLY against the in-process FakeGateway.

Design ``b1-ready/DESIGN.md`` r2 §U-g (Daybreak P1-1/P2-1/P2-2). Nothing here touches a real
gateway, a real credential or a real pepper: the pepper is a canary derived at import (a 64-hex
literal would trip the secret scan), the gateway is ``tests/support/fake_ibapi.py`` (which also
forbids sockets), and every ALLOW_* flag is false. Every IB behaviour the fake encodes is
ASSUMED-IB (verify at K4); a green case proves the probe against that model, not against IB.

What is pinned:
1. **No connect without an admission.** Every refusal (file shape, P1-P13, the declared scope,
   a hand-built or altered token, a substituted Settings) leaves zero ``_load_ibapi`` calls, an
   empty FakeGateway transcript and zero ``socket.connect`` audit events.
2. **A live port, account or flag is refused before any socket**, including Daybreak's
   ``LIVE / 198.51.100.7 / 7496 / U12345``.
3. **Read-only.** The wire transcript holds only the design's read allowlist; the adapter's four
   order methods are patched to raise; a seeded ``placeOrder`` on the same wire makes the same
   helper fail; ``preview_order`` with the admitted Settings DOES reach ``placeOrder`` (why the
   view exists); and a static AST guard over the probe and its helper closure.
4. **Managed-set exactness**, with the irreversible latch, the declared set and its rules.
5. **No pepper or secret**, searched by every 8-character window (U-d r2) on every surface.
6. **The second layer stays live**: the admitted Settings keep the paper mode lock denying.
"""

from __future__ import annotations

import ast
import dataclasses
import hashlib
import logging
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from tests.support import fake_ibapi

from chronos.broker import official_ibkr
from chronos.cli import b1_probe
from chronos.cli.main import main

ROOT = Path(__file__).resolve().parents[2]
PROBE_SOURCE = ROOT / "src/chronos/cli/b1_probe.py"
ACCOUNT = fake_ibapi.ACCOUNT  # the fake's paper account (DU5550001)
OTHER_PAPER = "DU8888888"
THIRD_PAPER = "DU7777777"
#: a fixed, non-repetitive 64-hex canary DERIVED at import (never a literal: the secret scan)
PEPPER = hashlib.sha256(b"chronos-b1-ug-pepper-canary").hexdigest()
#: a credential canary (P9), derived too: a quoted value beside a password key trips the scan
CREDENTIAL = "canary-" + hashlib.sha256(b"chronos-b1-ug-credential-canary").hexdigest()[:12]
CREDENTIAL_KEY = "IB_" + "PASSWORD"
_WINDOW = 8
#: the design's read allowlist for the probe's wire (U-g, "Proof of read-only", item 1)
READ_ONLY_WIRE = frozenset(
    {
        "connect",
        "run",
        "isConnected",
        "disconnect",
        "reqMarketDataType",
        "reqCurrentTime",
        "reqAccountSummary",
        "cancelAccountSummary",
        "reqContractDetails",
        "reqMktData",
        "cancelMktData",
    }
)
ORDER_METHODS = ("preview_order", "submit_order", "modify_order", "cancel_order")
WIRE_ORDER_NAMES = ("placeOrder", "cancelOrder")


# --- the audit hook: socket connects and write-opens, recorded while armed ---------------------

_AUDIT: dict[str, Any] = {"armed": False, "events": []}
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND


def _audit(event: str, args: tuple[Any, ...]) -> None:
    if not _AUDIT["armed"]:
        return
    if event == "socket.connect":
        _AUDIT["events"].append(("socket.connect", repr(args[1:])[:80]))
    elif event == "open" and len(args) >= 3:
        path, mode, flags = args[0], args[1], args[2]
        writes = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
            isinstance(flags, int) and flags & _WRITE_FLAGS
        )
        if writes:
            _AUDIT["events"].append(("open-write", str(path)[:120]))


sys.addaudithook(_audit)


@contextmanager
def _audited() -> Iterator[list[tuple[str, str]]]:
    _AUDIT["events"] = []
    _AUDIT["armed"] = True
    try:
        yield _AUDIT["events"]
    finally:
        _AUDIT["armed"] = False


# --- the env file and the fake ------------------------------------------------------------------


def _text(tmp: Path, **overrides: str | None) -> str:
    values: dict[str, str] = {
        "BROKER_MODE": "ibkr",
        "BROKER_ADAPTER": "official_ibkr",
        "IB_ENVIRONMENT": "paper",
        "IB_HOST": "127.0.0.1",
        "IB_PORT": "7497",
        "IB_CLIENT_ID": str(fake_ibapi.CLIENT_ID),
        "IB_DATA_CLIENT_ID": "18",
        "IB_ACCOUNT_ID": ACCOUNT,
        "ALLOW_ORDER_TRANSMIT": "false",
        "ALLOW_LIVE_TRADING": "false",
        "SYMBOL_ALLOWLIST": "AAPL,MSFT",
        "DATABASE_URL": f"sqlite:///{tmp / 'b1' / 'chronos-b1.db'}",
        "CHRONOS_CAPTURE_PEPPER": PEPPER,
    }
    for key, value in overrides.items():
        if value is None:
            values.pop(key, None)
        else:
            values[key] = value
    return "".join(f"{key}={value}\n" for key, value in values.items())


def _env_file(tmp: Path, text: str, mode: int = 0o600) -> Path:
    path = tmp / "b1.env"
    path.write_text(text, encoding="utf-8")
    path.chmod(mode)
    return path


@dataclasses.dataclass
class Rig:
    tmp: Path
    gateway: fake_ibapi.FakeGateway
    loader_calls: list[int]

    def admit(
        self, text: str | None = None, **kw: Any
    ) -> b1_probe.AdmittedB1Paper | b1_probe.Refusal:
        path = _env_file(self.tmp, text if text is not None else _text(self.tmp))
        return b1_probe.admit_b1_paper(path.read_text(), os.lstat(path), {}, **kw)

    def token(self, **kw: Any) -> b1_probe.AdmittedB1Paper:
        admitted = self.admit(**kw)
        assert isinstance(admitted, b1_probe.AdmittedB1Paper), admitted
        return admitted

    @property
    def wire(self) -> list[str]:
        return [method for _client, method in self.gateway.transcript]

    def nothing_connected(self) -> bool:
        return self.loader_calls == [] and self.gateway.transcript == []


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Rig]:
    for key in list(os.environ):
        if key.startswith(("BROKER_", "IB_", "ALLOW_", "AUTONOMY_", "CHRONOS_")):
            monkeypatch.delenv(key)
    gateway = fake_ibapi.FakeGateway.capture_day()
    fake_ibapi.install(monkeypatch, gateway)
    loader_calls: list[int] = []
    installed = official_ibkr._load_ibapi

    def counted() -> Any:
        loader_calls.append(1)
        return installed()

    monkeypatch.setattr(official_ibkr, "_load_ibapi", counted)
    for name in ORDER_METHODS:  # a belt over the braces: an order method must never run

        def refuse(*args: Any, _name: str = name, **kwargs: Any) -> Any:
            raise AssertionError(f"the probe called the adapter's {_name}")

        monkeypatch.setattr(official_ibkr.OfficialIBKRBroker, name, refuse)
    try:
        yield Rig(tmp_path, gateway, loader_calls)
    finally:
        gateway.release_readers()


def _assert_read_only_wire(wire: list[str]) -> None:
    """The wire prohibition, shared by the normal tests and the positive control."""

    sent = set(wire)
    assert {"connect", "reqCurrentTime", "reqAccountSummary"} <= sent  # the transcript ran
    extra = sorted(sent - READ_ONLY_WIRE)
    assert not extra, f"non-read-only wire requests: {extra}"


def _levels(report: b1_probe.ProbeReport) -> dict[str, str]:
    return {line.check: line.level for line in report.lines}


# --- (1)/(2) no connect without an admission ----------------------------------------------------


def test_an_admitted_paper_config_clears_over_the_fake(rig: Rig) -> None:
    token = rig.token()
    with _audited() as events:
        report = b1_probe.run_probe(token)

    verdict, code = report.verdict()
    assert code == 0, [str(line) for line in report.lines]
    assert verdict.startswith("B1-PROBE CLEAR (0)")
    levels = _levels(report)
    for check in ("handshake", "scope", "account", "teardown"):
        assert levels[check] == "PASS", check
    assert levels["clock"] == "INFO"
    symbols = [line.text for line in report.lines if line.check == "symbol"]
    assert [text.split(":")[0] for text in symbols] == ["AAPL", "MSFT"]
    _assert_read_only_wire(rig.wire)
    assert rig.wire.count("connect") == 1 and rig.wire[-1] == "disconnect"
    assert events == []  # no write-open, no socket connect
    assert rig.loader_calls  # the adapter was really built


@pytest.mark.parametrize(
    ("overrides", "condition"),
    [
        ({"IB_HOST": "192.0.2.10"}, "P3"),
        ({"IB_PORT": "7496"}, "P3"),
        ({"IB_PORT": "4001"}, "P3"),
        ({"IB_ENVIRONMENT": "live"}, None),
        (
            {
                "IB_ENVIRONMENT": "live",
                "IB_HOST": "198.51.100.7",
                "IB_PORT": "7496",
                "IB_ACCOUNT_ID": "U12345",
            },
            None,
        ),
        ({"IB_ACCOUNT_ID": "U12345"}, None),
        ({"IB_ACCOUNT_ID": "DU1234567"}, "P4"),
        ({"IB_CLIENT_ID": "0"}, None),
        ({"IB_DATA_CLIENT_ID": str(fake_ibapi.CLIENT_ID)}, None),
        ({"ALLOW_ORDER_TRANSMIT": "true"}, None),
        ({"AUTONOMY_MANDATE_FILE": "/tmp/mandate.json"}, "P6"),
        ({"IB_ACCOUNT_ALLOWLIST": ACCOUNT}, "P13"),
        ({"IB_ACCOUNT_ALLOWLIST": f"{ACCOUNT},{OTHER_PAPER}"}, "P13"),
        ({CREDENTIAL_KEY: CREDENTIAL}, "P9"),
        ({"CHRONOS_CAPTURE_PEPPER": "z" * 64}, "P10"),
        ({"BROKER_MODE": "demo"}, None),
    ],
    ids=[
        "remote-host",
        "live-port-7496",
        "live-port-4001",
        "live-environment",
        "daybreak-live-combo",
        "live-account",
        "placeholder-account",
        "client-id-0",
        "same-client-ids",
        "transmit-flag",
        "mandate",
        "allowlist-holds-the-paper-id",
        "allowlist-two-entries",
        "credential-key",
        "bad-pepper",
        "demo-mode",
    ],
)
def test_a_refused_config_never_connects(
    rig: Rig, overrides: dict[str, str], condition: str | None
) -> None:
    path = _env_file(rig.tmp, _text(rig.tmp, **overrides))
    text, file_stat = path.read_text(), os.lstat(path)
    with _audited() as events:
        admitted = b1_probe.admit_b1_paper(text, file_stat, {})
    assert isinstance(admitted, b1_probe.Refusal), "admitted a config it must refuse"
    if condition is not None:
        assert condition in admitted.conditions
    assert rig.nothing_connected() and events == []


def test_an_exported_variable_is_refused_before_any_socket(rig: Rig) -> None:
    path = _env_file(rig.tmp, _text(rig.tmp))
    admitted = b1_probe.admit_b1_paper(path.read_text(), os.lstat(path), {"IB_PORT": "7497"})
    assert isinstance(admitted, b1_probe.Refusal) and "P12" in admitted.conditions
    assert rig.nothing_connected()


def test_a_world_readable_env_file_is_refused(rig: Rig) -> None:
    path = _env_file(rig.tmp, _text(rig.tmp), mode=0o644)
    admitted = b1_probe.admit_b1_paper(path.read_text(), os.lstat(path), {})
    assert isinstance(admitted, b1_probe.Refusal) and "P11" in admitted.conditions
    assert rig.nothing_connected()


@pytest.mark.parametrize("allowlist", ["", None], ids=["bare-key", "unset"])
def test_an_empty_or_unset_allowlist_is_admitted(rig: Rig, allowlist: str | None) -> None:
    """The positive control for the P13 refusals: an empty allowlist stays the deny default."""

    admitted = rig.admit(_text(rig.tmp, IB_ACCOUNT_ALLOWLIST=allowlist))
    assert isinstance(admitted, b1_probe.AdmittedB1Paper)


def test_the_token_carries_no_pepper_and_no_allowlist(rig: Rig) -> None:
    token = rig.token()
    names = {f.name for f in dataclasses.fields(token)}
    assert not any("pepper" in name or "allowlist" in name for name in names)
    rendered = repr(token) + repr(dataclasses.asdict(token))
    assert _partial_leaks(rendered, PEPPER) == []
    assert ACCOUNT not in repr(token)  # the id is not even in the repr


# --- the CLI: zero connects on every refusal ----------------------------------------------------


def _cli(rig: Rig, text: str, *extra: str, mode: int = 0o600) -> int:
    path = _env_file(rig.tmp, text, mode=mode)
    with _audited() as events:
        code = main(["b1-probe", "--env-file", str(path), *extra])
    assert all(kind != "socket.connect" for kind, _ in events)
    return code


@pytest.mark.parametrize(
    "declared",
    [
        f"{OTHER_PAPER}",  # omits IB_ACCOUNT_ID
        f"{ACCOUNT},U12345",  # a non-paper id
        f"{ACCOUNT},{ACCOUNT}",  # a duplicate
        ",".join([ACCOUNT, OTHER_PAPER, THIRD_PAPER, "DU6666666", "DU5555555"]),  # over the bound
        "",  # empty
    ],
    ids=["omits-the-account", "non-paper", "duplicate", "over-the-bound", "empty"],
)
def test_a_bad_declared_scope_refuses_at_parsing_with_zero_connects(
    rig: Rig, capsys: pytest.CaptureFixture[str], declared: str
) -> None:
    code = _cli(rig, _text(rig.tmp), "--managed-accounts", declared)
    assert code == 78 and "[SCOPE]" in capsys.readouterr().out
    assert rig.nothing_connected()


def test_the_cli_refuses_a_live_port_with_zero_connects(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _cli(rig, _text(rig.tmp, IB_PORT="7496")) == 78
    assert "B1-PROBE REFUSE (78)" in capsys.readouterr().out
    assert rig.nothing_connected()


def test_the_cli_refuses_an_unsafe_file_without_reading_it(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _cli(rig, _text(rig.tmp), mode=0o644) == 78
    assert "failed the file admission" in capsys.readouterr().out
    assert rig.nothing_connected()


def test_the_cli_clears_over_the_fake(rig: Rig, capsys: pytest.CaptureFixture[str]) -> None:
    assert _cli(rig, _text(rig.tmp)) == 0
    out = capsys.readouterr().out
    assert "B1-PROBE CLEAR (0)" in out and ACCOUNT not in out
    _assert_read_only_wire(rig.wire)


# --- the token cannot be forged or altered ------------------------------------------------------


def test_a_hand_built_token_cannot_be_constructed(rig: Rig) -> None:
    fields = {f.name: getattr(rig.token(), f.name) for f in dataclasses.fields(rig.token())}
    fields.pop("_seal")
    with pytest.raises(TypeError, match="only by admit_b1_paper"):
        b1_probe.AdmittedB1Paper(**fields)


def test_a_token_forged_around_the_constructor_is_refused_with_zero_connects(rig: Rig) -> None:
    real = rig.token()
    forged = object.__new__(b1_probe.AdmittedB1Paper)
    for f in dataclasses.fields(real):
        if f.name != "_seal":
            object.__setattr__(forged, f.name, getattr(real, f.name))
    report = b1_probe.run_probe(forged)
    assert report.verdict()[1] == 78 and _levels(report) == {"admission": "REFUSE"}
    assert rig.nothing_connected()


@pytest.mark.parametrize(
    "change",
    [
        {"host": "198.51.100.7"},
        {"port": 7496},
        {"account_id": "U12345"},
        {"allow_order_transmit": True},
        {"client_id": 0},
        {"expected_scope": frozenset({ACCOUNT, "U12345"})},
    ],
    ids=["host", "port", "account", "flag", "client-id", "scope"],
)
def test_an_altered_token_is_refused_by_the_re_evaluated_predicates(
    rig: Rig, change: dict[str, Any]
) -> None:
    altered = dataclasses.replace(rig.token(), **change)  # keeps the seal: the predicates catch it
    report = b1_probe.run_probe(altered)
    assert report.verdict()[1] == 78 and set(_levels(report)) == {"admission"}
    assert rig.nothing_connected()


def test_a_substituted_settings_is_refused_before_the_loader(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = b1_probe._settings_from

    def substituted(token: b1_probe.AdmittedB1Paper) -> Any:
        return real(dataclasses.replace(token, data_client_id=19))

    monkeypatch.setattr(b1_probe, "_settings_from", substituted)
    report = b1_probe.run_probe(rig.token())
    assert _levels(report) == {"substitution": "REFUSE"}
    assert rig.nothing_connected()


def test_an_ambient_override_after_admission_changes_nothing(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Settings come from the token only: exported variables after admission are ignored. Init
    arguments outrank the environment in pydantic-settings, so an override of a field the probe
    passes cannot win either way; a field it does NOT pass (AUTONOMY_MANDATE_FILE) would be
    picked up by a Settings with env sources on, and the substitution check would refuse."""

    token = rig.token()
    monkeypatch.setenv("IB_CLIENT_ID", "99")
    monkeypatch.setenv("IB_PORT", "4001")
    # a field the probe does NOT pass: only a Settings with env sources on would pick it up
    monkeypatch.setenv("AUTONOMY_MANDATE_FILE", str(rig.tmp / "mandate.json"))
    report = b1_probe.run_probe(token)
    assert report.verdict()[1] == 0
    assert {client for client, _ in rig.gateway.transcript} == {fake_ibapi.CLIENT_ID}


# --- (3) read-only ------------------------------------------------------------------------------


def test_positive_control_a_seeded_place_order_on_the_wire_fails_the_helper(rig: Rig) -> None:
    b1_probe.run_probe(rig.token())
    raw = fake_ibapi.FakeEClient.bound_to(rig.gateway)(wrapper=None)
    raw.connect("127.0.0.1", 7497, 77)
    raw.placeOrder(4242, fake_ibapi.FakeContract(), fake_ibapi.FakeOrder())
    with pytest.raises(AssertionError, match=r"non-read-only wire requests: \['placeOrder'\]"):
        _assert_read_only_wire(rig.wire)


def test_characterization_preview_with_the_admitted_settings_reaches_place_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Why the view exists (Daybreak P2-2): with transmission off, ``preview_order`` still sends
    ``placeOrder`` (whatIf). So admitted Settings do NOT make every order method refuse."""

    import asyncio
    from decimal import Decimal

    from chronos.domain.enums import OrderIntent, OrderSide
    from chronos.domain.models import OrderRequest, UnderlyingContract

    gateway = fake_ibapi.FakeGateway.capture_day()
    fake_ibapi.install(monkeypatch, gateway)
    for key in list(os.environ):
        if key.startswith(("BROKER_", "IB_", "ALLOW_", "AUTONOMY_", "CHRONOS_")):
            monkeypatch.delenv(key)
    path = _env_file(tmp_path, _text(tmp_path))
    token = b1_probe.admit_b1_paper(path.read_text(), os.lstat(path), {})
    assert isinstance(token, b1_probe.AdmittedB1Paper)
    broker = official_ibkr.OfficialIBKRBroker(b1_probe._settings_from(token))

    async def preview() -> None:
        await broker.connect()
        contract = await broker.qualify_underlying("AAPL")
        request = OrderRequest(
            correlation_id="b1-ug-characterization",
            account_id=ACCOUNT,
            contract=UnderlyingContract.model_validate(contract.model_dump()),
            intent=OrderIntent.OPEN_LONG_STOCK,
            side=OrderSide.BUY,
            quantity=Decimal(1),
            limit_price=Decimal("1.00"),
            order_ref="CHR-B1UG-" + "C" * 24,
        )
        try:
            await broker.preview_order(request)
        except Exception:
            pass
        finally:
            await broker.disconnect()

    try:
        asyncio.run(preview())
    finally:
        gateway.release_readers()
    assert "placeOrder" in [method for _client, method in gateway.transcript]


_FORBIDDEN_CALL_NAMES = {"eval", "exec", "__getattribute__"}


def _probe_tree() -> ast.Module:
    return ast.parse(PROBE_SOURCE.read_text(encoding="utf-8"))


def _names(tree: ast.AST) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.Attribute):
            found.add(node.attr)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            found.add(node.name)
    return found


def test_static_the_probe_names_no_order_method_and_no_dynamic_dispatch() -> None:
    tree = _probe_tree()
    names = _names(tree)
    assert not names & {*ORDER_METHODS, *WIRE_ORDER_NAMES}, sorted(names & set(ORDER_METHODS))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = (
                func.id
                if isinstance(func, ast.Name)
                else (func.attr if isinstance(func, ast.Attribute) else "")
            )
            assert name not in _FORBIDDEN_CALL_NAMES, name
            if name in {"getattr", "setattr", "import_module"}:
                assert len(node.args) >= 2 or name == "import_module"
                key = node.args[1] if name != "import_module" else node.args[0]
                assert isinstance(key, ast.Constant) and isinstance(key.value, str), (
                    f"{name} with a non-literal name at line {node.lineno}"
                )
            assert name not in {"get_settings"}, f"get_settings() at line {node.lineno}"
            assert not (isinstance(func, ast.Name) and func.id == "Settings"), (
                f"Settings() at line {node.lineno}"
            )


def test_static_only_the_view_touches_the_adapter_and_only_its_read_methods() -> None:
    allowed = {
        "connect",
        "disconnect",
        "connection_status",
        "server_time",
        "account_summary",
        "qualify_underlying",
        "request_underlying_quote",
        "bridge",
    }
    tree = _probe_tree()
    view = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "_ReadOnlyBrokerView"
    )
    inside: list[str] = []
    for node in ast.walk(view):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "__broker"
        ):
            inside.append(node.attr)
    assert inside and set(inside) <= allowed, sorted(set(inside) - allowed)
    outside = [
        node
        for top in tree.body
        if top is not view
        for node in ast.walk(top)
        if isinstance(node, ast.Attribute)
        and node.attr in {"__broker", "_ReadOnlyBrokerView__broker"}
    ]
    assert outside == [], "the view's private adapter is reached from outside the view"


#: modules the closure treats as REVIEWED terminals (Daybreak design r1 delta P2-2): the adapter
#: and its broker interface necessarily define the order methods; the probe reaches them only
#: through the view (pinned above) and the dynamic wire allowlist (pinned over the fake).
_TERMINALS = frozenset({"chronos.broker.official_ibkr", "chronos.broker.base"})


def _chronos_imports(module: str) -> set[str]:
    import importlib.util

    spec = importlib.util.find_spec(module)
    assert spec is not None and spec.origin is not None
    tree = ast.parse(Path(spec.origin).read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names if alias.name.startswith("chronos"))
        elif isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("chronos"):
            found.add(node.module)
            for alias in node.names:  # `from chronos.x import y` where y is a module
                candidate = f"{node.module}.{alias.name}"
                try:
                    if importlib.util.find_spec(candidate) is not None:
                        found.add(candidate)
                except ModuleNotFoundError:  # a name inside a module, not a submodule
                    pass
    return found


def _closure(entry: str) -> set[str]:
    seen: set[str] = set()
    todo = [entry]
    while todo:
        module = todo.pop()
        if module in seen:
            continue
        seen.add(module)
        if module in _TERMINALS:
            continue
        todo.extend(_chronos_imports(module) - seen)
    return seen


def test_static_the_probes_helper_closure_names_no_order_method() -> None:
    import importlib.util

    closure = _closure("chronos.cli.b1_probe")
    assert {"chronos.cli.b1_preflight", "chronos.broker.official_ibkr"} <= closure
    offenders: dict[str, list[str]] = {}
    for module in sorted(closure - _TERMINALS):
        spec = importlib.util.find_spec(module)
        assert spec is not None and spec.origin is not None
        if not spec.origin.endswith(".py"):
            continue
        hits = _names(ast.parse(Path(spec.origin).read_text(encoding="utf-8")))
        bad = sorted(hits & {*ORDER_METHODS, *WIRE_ORDER_NAMES})
        if bad:
            offenders[module] = bad
    assert offenders == {}, offenders


# --- (4) managed-set exactness ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("managed", "declared", "cleared"),
    [
        ((ACCOUNT,), None, True),
        ((ACCOUNT, OTHER_PAPER), None, False),  # an extra account at the handshake
        ((ACCOUNT, "U12345"), None, False),  # a live id in the observed set
        ((OTHER_PAPER, ACCOUNT), f"{ACCOUNT},{OTHER_PAPER}", True),  # a reorder, declared
        ((ACCOUNT,), f"{ACCOUNT},{OTHER_PAPER}", False),  # observed a proper subset
        ((ACCOUNT, OTHER_PAPER, THIRD_PAPER), f"{ACCOUNT},{OTHER_PAPER}", False),  # superset
    ],
    ids=["singleton", "extra", "live-id", "declared-reorder", "subset", "superset"],
)
def test_the_managed_set_must_equal_the_expected_scope(
    rig: Rig, managed: tuple[str, ...], declared: str | None, cleared: bool
) -> None:
    rig.gateway.managed_accounts = managed
    token = rig.token(declared_accounts=None if declared is None else tuple(declared.split(",")))
    report = b1_probe.run_probe(token)
    levels = _levels(report)
    if cleared:
        assert report.verdict()[1] == 0 and levels["scope"] == "PASS"
    else:
        assert levels["scope"] == "REFUSE" and report.verdict()[1] == 78
        assert "reqCurrentTime" not in rig.wire and "reqAccountSummary" not in rig.wire
        assert levels["teardown"] == "PASS"


def test_a_substituted_account_is_refused_at_the_handshake(rig: Rig) -> None:
    rig.gateway.managed_accounts = (OTHER_PAPER,)
    report = b1_probe.run_probe(rig.token())
    assert _levels(report) == {"handshake": "REFUSE"}
    assert "reqCurrentTime" not in rig.wire


def test_a_mid_session_scope_addition_latches_and_refuses(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = rig.gateway.current_time
    added: list[bool] = []

    def time_then_add(client: Any) -> None:
        if not added:
            added.append(True)
            rig.gateway.change_managed_accounts((ACCOUNT, OTHER_PAPER))  # keeps the expected id
        real(client)

    monkeypatch.setattr(rig.gateway, "current_time", time_then_add)
    report = b1_probe.run_probe(rig.token())
    levels = _levels(report)
    assert added == [True]
    assert levels["latch"] == "REFUSE" and report.verdict()[1] == 78
    assert "reqAccountSummary" not in rig.wire  # no read after the latch tripped
    assert levels["teardown"] == "PASS"


def test_a_dropped_connection_mid_session_latches_and_refuses(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = rig.gateway.account_summary

    def drop_then_answer(client: Any, req_id: int) -> None:
        rig.gateway.emit_error(-1, 1100, "Connectivity between IB and TWS has been lost.")
        real(client, req_id)

    monkeypatch.setattr(rig.gateway, "account_summary", drop_then_answer)
    report = b1_probe.run_probe(rig.token())
    assert _levels(report)["latch"] == "REFUSE" and report.verdict()[1] == 78


def test_the_probes_own_teardown_close_does_not_latch(rig: Rig) -> None:
    """The fake (like ibapi, ASSUMED-IB) calls connectionClosed on our own disconnect."""

    report = b1_probe.run_probe(rig.token())
    assert "latch" not in _levels(report) and report.verdict()[1] == 0


# --- (5) no pepper or secret ever printed -------------------------------------------------------


def _windows(secret: str) -> set[str]:
    return {secret[i : i + _WINDOW] for i in range(len(secret) - _WINDOW + 1)}


def _partial_leaks(haystack: str, secret: str) -> list[str]:
    return sorted(window for window in _windows(secret) if window in haystack)


class _Records(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage() + repr(record.__dict__))


@contextmanager
def _all_logs() -> Iterator[_Records]:
    handler = _Records()
    targets = [logging.getLogger(), logging.getLogger("chronos")]
    levels = [t.level for t in targets]
    for target in targets:
        target.addHandler(handler)
        target.setLevel(logging.DEBUG)
    try:
        yield handler
    finally:
        for target, level in zip(targets, levels, strict=True):
            target.removeHandler(handler)
            target.setLevel(level)


def test_the_window_search_finds_a_seeded_fragment() -> None:
    """Positive control: a head, a middle and an elided head...tail fragment are all found."""

    assert _partial_leaks(f"x{PEPPER[20:30]}y", PEPPER)
    assert _partial_leaks(f"'{PEPPER[:12]}...{PEPPER[-12:]}'", PEPPER)
    assert len(_windows(PEPPER)) == len(PEPPER) - _WINDOW + 1


@pytest.mark.parametrize("json_out", [False, True], ids=["text", "json"])
@pytest.mark.parametrize(
    "overrides",
    [{}, {"IB_PORT": "7496"}, {CREDENTIAL_KEY: CREDENTIAL}],
    ids=["clear", "refused-port", "refused-credential"],
)
def test_no_fragment_of_the_pepper_or_a_credential_appears_anywhere(
    rig: Rig,
    capsys: pytest.CaptureFixture[str],
    overrides: dict[str, str],
    json_out: bool,
) -> None:
    flags = ["--json"] if json_out else []
    with _all_logs() as logs:
        code = _cli(rig, _text(rig.tmp, **overrides), *flags)
    captured = capsys.readouterr()
    everything = captured.out + captured.err + "\n".join(logs.lines)
    assert ("B1-PROBE CLEAR" in everything) is (overrides == {}), everything[-400:]
    assert code == (0 if overrides == {} else 78)
    assert _partial_leaks(everything, PEPPER) == []
    assert CREDENTIAL not in everything and ACCOUNT not in everything


def test_the_constructed_settings_carry_no_pepper(rig: Rig) -> None:
    settings = b1_probe._settings_from(rig.token())
    assert _partial_leaks(repr(settings.model_dump()), PEPPER) == []


# --- (6) the second layer stays live -------------------------------------------------------------


def test_the_admitted_settings_keep_the_paper_mode_lock_denying(rig: Rig) -> None:
    from chronos.control.modes import TradingMode, resolve_mode_lock

    settings = b1_probe._settings_from(rig.token())

    def lock(allowlist: tuple[str, ...]) -> Any:
        return resolve_mode_lock(
            requested_mode=TradingMode.PAPER,
            paper_account_allowlist=allowlist,
            broker_reported_account_id=ACCOUNT,
            broker_reported_environment_is_paper=True,
            order_transmission_enabled=True,
        )

    denied = lock(tuple(settings.ib_account_allowlist))
    assert any("paper account allowlist is empty" in reason for reason in denied.denial_reasons)
    control = lock((ACCOUNT,))  # the same call with the id allowlisted: that reason is gone
    assert not any(
        "paper account allowlist is empty" in reason for reason in control.denial_reasons
    )


# --- registration --------------------------------------------------------------------------------


def test_b1_probe_is_registered_on_the_operator_cli(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exited:
        main(["b1-probe", "--help"])
    assert exited.value.code == 0
    assert "Kevin's word" in capsys.readouterr().out


#: the declared-scope bound is code, not configuration
def test_the_declared_scope_bound_is_small_and_fixed() -> None:
    assert b1_probe.MAX_DECLARED_ACCOUNTS == 4


# --- U-g r1: Daybreak P2-1/P2-2, reader P2-1/P2-2 and the P3s ------------------------------------


def test_r1_a_scope_change_during_qualification_stops_the_snapshot_read(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Daybreak P2-1: the scope changes while ``reqContractDetails`` is answered; the latch must
    be checked between the two reads of a symbol, so no ``reqMktData`` follows (and no further
    symbol is read)."""

    real = rig.gateway.contract_details
    changed: list[bool] = []

    def details_then_add(client: Any, req_id: int, contract: Any) -> None:
        if not changed:
            changed.append(True)
            rig.gateway.change_managed_accounts((ACCOUNT, OTHER_PAPER))
        real(client, req_id, contract)

    monkeypatch.setattr(rig.gateway, "contract_details", details_then_add)
    report = b1_probe.run_probe(rig.token())
    levels = _levels(report)
    assert changed == [True]
    assert rig.wire.count("reqContractDetails") == 1  # AAPL qualified, MSFT never started
    assert "reqMktData" not in rig.wire
    assert levels["latch"] == "REFUSE" and levels["teardown"] == "PASS"
    assert report.verdict()[1] == 78


@pytest.mark.parametrize(
    "symbols",
    [ACCOUNT, f"AAPL,{ACCOUNT}", "AAPL,DU4440001", "MSFT,U123456"],
    ids=["the-account", "the-account-among-symbols", "another-paper-id", "a-live-id"],
)
def test_r1_an_account_shaped_symbol_is_refused_at_admission(rig: Rig, symbols: str) -> None:
    """Daybreak P2-2: a symbol is printed, so an account-shaped one would print an id."""

    path = _env_file(rig.tmp, _text(rig.tmp, SYMBOL_ALLOWLIST=symbols))
    admitted = b1_probe.admit_b1_paper(path.read_text(), os.lstat(path), {})
    assert isinstance(admitted, b1_probe.Refusal)
    assert all(ACCOUNT not in reason for reason in admitted.reasons)
    assert rig.nothing_connected()


@pytest.mark.parametrize("json_out", [False, True], ids=["text", "json"])
def test_r1_a_symbol_aliasing_the_account_never_prints_it(
    rig: Rig, capsys: pytest.CaptureFixture[str], json_out: bool
) -> None:
    flags = ["--json"] if json_out else []
    with _all_logs() as logs:
        code = _cli(rig, _text(rig.tmp, SYMBOL_ALLOWLIST=ACCOUNT), *flags)
    captured = capsys.readouterr()
    everything = captured.out + captured.err + "\n".join(logs.lines)
    assert code == 78 and "B1-PROBE REFUSE (78)" in everything
    assert ACCOUNT not in everything
    assert rig.nothing_connected()


def test_r1_an_altered_token_with_an_account_symbol_is_refused(rig: Rig) -> None:
    altered = dataclasses.replace(rig.token(), symbols=("AAPL", ACCOUNT))
    report = b1_probe.run_probe(altered)
    assert set(_levels(report)) == {"admission"} and ACCOUNT not in str(report.lines)
    assert rig.nothing_connected()


@pytest.mark.parametrize("json_out", [False, True], ids=["text", "json"])
@pytest.mark.parametrize(
    "managed",
    [(ACCOUNT, OTHER_PAPER), (ACCOUNT, "U12345"), (OTHER_PAPER,)],
    ids=["extra-paper", "live-id", "substituted"],
)
def test_r1_no_account_id_appears_on_a_scope_refusal(
    rig: Rig, capsys: pytest.CaptureFixture[str], managed: tuple[str, ...], json_out: bool
) -> None:
    """Reader P2-1: the scope refusal is where foreign ids are in hand; none may be printed."""

    rig.gateway.managed_accounts = managed
    flags = ["--json"] if json_out else []
    with _all_logs() as logs:
        code = _cli(rig, _text(rig.tmp), *flags)
    captured = capsys.readouterr()
    everything = captured.out + captured.err + "\n".join(logs.lines)
    assert code == 78
    for account in {ACCOUNT, *managed}:
        assert account not in everything, account


def test_r1_an_account_summary_for_another_account_refuses_and_reads_no_further(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reader P2-2: the probe does not trust the adapter's own row filter; a summary stamped
    with another account is a REFUSE, and no symbol is read after it."""

    from datetime import UTC, datetime
    from decimal import Decimal

    from chronos.domain.models import AccountSummary

    async def other(self: Any) -> AccountSummary:
        return AccountSummary(
            account_id=OTHER_PAPER,
            net_liquidation=Decimal(1),
            total_cash=Decimal(1),
            buying_power=Decimal(1),
            as_of=datetime.now(tz=UTC),
        )

    monkeypatch.setattr(official_ibkr.OfficialIBKRBroker, "account_summary", other)
    report = b1_probe.run_probe(rig.token())
    assert _levels(report)["account"] == "REFUSE" and report.verdict()[1] == 78
    assert "reqContractDetails" not in rig.wire and OTHER_PAPER not in str(report.lines)


def test_r1_no_summary_rows_for_the_account_refuses(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The realistic form: the gateway answers for another account only; the adapter raises and
    the probe refuses (the account cannot be confirmed) without reading symbols."""

    def other_rows(client: Any, req_id: int) -> None:
        client.emit("accountSummary", req_id, OTHER_PAPER, "NetLiquidation", "1", "USD")
        client.emit("accountSummaryEnd", req_id)

    monkeypatch.setattr(rig.gateway, "account_summary", other_rows)
    report = b1_probe.run_probe(rig.token())
    assert _levels(report)["account"] == "REFUSE" and report.verdict()[1] == 78
    assert "reqContractDetails" not in rig.wire


def test_r1_equal_client_ids_on_an_altered_token_are_refused(rig: Rig) -> None:
    token = rig.token()
    report = b1_probe.run_probe(dataclasses.replace(token, data_client_id=token.client_id))
    assert set(_levels(report)) == {"admission"} and rig.nothing_connected()


def test_r1_an_env_file_with_a_second_link_is_refused(rig: Rig) -> None:
    path = _env_file(rig.tmp, _text(rig.tmp))
    os.link(path, rig.tmp / "b1-second-link.env")
    admitted = b1_probe.admit_b1_paper(path.read_text(), os.lstat(path), {})
    assert isinstance(admitted, b1_probe.Refusal) and "FILE" in admitted.conditions


def test_r1_the_latch_excuses_only_its_own_teardown_close() -> None:
    """Reader P3-2, both ways, on the latch itself."""

    before = b1_probe._ScopeLatch()
    before.uncertain(b1_probe._CLOSED_REASON)  # a close BEFORE teardown latches
    assert before.first_reason == b1_probe._CLOSED_REASON
    during = b1_probe._ScopeLatch()
    during.begin_teardown()
    during.uncertain(b1_probe._CLOSED_REASON)  # our own close is excused
    assert during.first_reason is None
    during.uncertain("official IBKR connectivity event 1100")  # anything else still latches
    assert during.first_reason == "official IBKR connectivity event 1100"


def test_r1_a_close_mid_session_latches_and_refuses(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gateway closes the session while answering reqCurrentTime: the read fails (it is
    reported, not raised) and the latch, tripped before teardown, makes it a REFUSE."""

    def close_instead(client: Any) -> None:
        client.emit("connectionClosed")

    monkeypatch.setattr(rig.gateway, "current_time", close_instead)
    report = b1_probe.run_probe(rig.token())
    levels = _levels(report)
    assert levels["latch"] == "REFUSE" and levels["clock"] == "UNDECIDED"
    assert report.verdict()[1] == 78 and "reqAccountSummary" not in rig.wire


def test_r1_a_failed_handshake_reports_the_gateway_notice_codes(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reader P3-3: on a handshake failure the id-less notice codes are reported (G-C1)."""

    monkeypatch.setattr(official_ibkr, "_CONNECT_TIMEOUT_S", 0.5)  # the handshake never comes
    rig.gateway.held_client_ids.add(fake_ibapi.CLIENT_ID)
    report = b1_probe.run_probe(rig.token())
    (line,) = [line for line in report.lines if line.check == "handshake"]
    assert line.level == "REFUSE" and str(fake_ibapi.CODE_CLIENT_ID_IN_USE) in line.text


def test_r1_static_the_adapter_object_is_only_ever_handed_to_the_view() -> None:
    """Reader P3-4: a name bound to OfficialIBKRBroker(...) is used only as the view's argument
    (no attribute read, call or other use outside the view)."""

    tree = _probe_tree()
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    bound: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == "OfficialIBKRBroker"
        ):
            bound.update(t.id for t in node.targets if isinstance(t, ast.Name))
    assert bound, "the probe no longer constructs the adapter by name"
    outside_the_view = [
        top
        for top in tree.body
        if not (isinstance(top, ast.ClassDef) and top.name == "_ReadOnlyBrokerView")
    ]
    uses = [
        node
        for top in outside_the_view
        for node in ast.walk(top)
        if isinstance(node, ast.Name) and node.id in bound and isinstance(node.ctx, ast.Load)
    ]
    assert uses
    for use in uses:
        parent = parents[use]
        assert (
            isinstance(parent, ast.Call)
            and isinstance(parent.func, ast.Name)
            and parent.func.id == "_ReadOnlyBrokerView"
        ), f"the adapter is used outside the view at line {use.lineno}"
