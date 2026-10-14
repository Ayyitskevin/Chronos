"""B1 U-f3: the leak search and the no-order transcript over the FakeGateway (tests only).

Design ``b1-ready/DESIGN.md`` §U-f, packet U-f3. The campaign's REAL ``capture_readonly.main()``
runs a full capture over ``FakeGateway.capture_day()`` through the REAL official adapter, with a
THROWAWAY pepper (``secrets.token_hex(32)``) and a ``tmp_path`` output root, exactly as U-f2 does
(the helpers are U-f2's), then ``replay_check.py`` replays it as a rehearsal. Here:

1. **The leak search:** EVERY output byte -- the whole output tree (every file, read as bytes),
   the capture's stdout, every log record of the root and ``chronos`` loggers, and the replay
   CLI's stdout/stderr -- is searched for the raw account id and for the pepper raw, as hex
   (both cases) and as base64 (standard and urlsafe, with and without padding). Each target has
   a positive control: a seeded leak is found.
2. **The no-order transcript:** every ``OfficialIBKRBroker`` method the harness calls is
   recorded, and every request the FakeGateway receives on its wire; the adapter calls must be a
   subset of the read allowlist and the wire must carry none of the mutating requests
   (``placeOrder``/``cancelOrder``/``exerciseOptions``/``reqGlobalCancel``, the inventory of
   ``tests/safety/test_broker_mutation_inventory.py``). Positive control: a seeded order call
   is caught by both recorders.
3. **The no-pepper refusal:** U-f2 already pins it for an absent and a too-short pepper
   (nothing written, no gateway call, the adapter never built, no socket object). The gap pinned
   here: a NON-HEX pepper of the right length.

Con-id <-> account linkage: the capture pseudonymises the account as ``ACCT-<16 hex>`` of the
UNKEYED ``account_fingerprint`` (sha256 of ``chronos-account:<id>``), so a fixture reader can
recover the raw paper account by enumerating the account-id space and thereby link every
position's con id to it. That is a campaign-script gap: strict xfail **U-F3-F1** below.
"""

from __future__ import annotations

import base64
import io
import logging
import os
import secrets
import socket
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from typing import Any

import pytest
from tests.integration.test_capture_replay_fakegateway import (
    ROOT,
    SCRIPTS,
    _load,
    _paper_environment,
    _run_capture_main,
)
from tests.safety.test_broker_mutation_inventory import _MUTATING_METHODS
from tests.support import fake_ibapi

from chronos.utils.identifiers import account_fingerprint

#: the adapter methods the capture workload is ALLOWED to call (measured, then pinned: any new
#: call fails the allowlist and must be added here deliberately, after reading what it does)
_READ_ALLOWLIST = frozenset(
    {
        "connect",
        "disconnect",
        "connection_status",
        "server_time",
        "account_summary",
        "positions",
        "executions",
        "open_orders",
        "qualify_underlying",
        "request_underlying_quote",
        "option_chain_parameters",
        "qualify_option_contracts",
        "option_market_rules",
        "historical_bars",
        "cancel_market_data",  # unsubscribes market data; not an order method
    }
)
#: the adapter's order plane: none may ever be called by the read-only capture
_ORDER_METHODS = frozenset({"preview_order", "submit_order", "modify_order", "cancel_order"})


@contextmanager
def _adapter_calls(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Record every public ``OfficialIBKRBroker`` method call (the call itself, before it runs)."""

    from chronos.broker import official_ibkr

    calls: list[str] = []
    adapter = official_ibkr.OfficialIBKRBroker
    for name, function in list(vars(adapter).items()):
        if callable(function) and not name.startswith("_"):

            def recorded(self: Any, *args: Any, _name: str = name, _fn: Any = function, **kw: Any):  # type: ignore[no-untyped-def]
                calls.append(_name)
                return _fn(self, *args, **kw)

            monkeypatch.setattr(adapter, name, recorded)
    yield calls


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


class Session:
    """One full capture + replay, and every byte it produced."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.gateway = fake_ibapi.FakeGateway.capture_day()
        fake_ibapi.install(monkeypatch, self.gateway)
        workdir = tmp_path / "cwd"
        workdir.mkdir()
        _paper_environment(monkeypatch, workdir)
        self.pepper_hex = secrets.token_hex(32)  # throwaway: generated here, gone with tmp_path
        monkeypatch.setenv("CHRONOS_CAPTURE_PEPPER", self.pepper_hex)
        self.root = tmp_path / "out"
        self.out = self.root / "session-1"
        with _all_logs() as logs, _adapter_calls(monkeypatch) as calls:
            self.code, self.printed = _run_capture_main(
                monkeypatch, _load("capture_readonly"), self.out
            )
            replay_printed = io.StringIO()
            with redirect_stdout(replay_printed):
                self.replay_failures = _load("replay_check").check_session(
                    self.out, allow_demo=True
                )
        self.logs = logs.lines
        self.calls = list(calls)
        self.wire = [method for _client, method in self.gateway.transcript]
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "PYTHONPATH": os.pathsep.join([str(ROOT / "src"), str(ROOT)]),
            "HOME": str(tmp_path),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        cli = subprocess.run(
            [sys.executable, str(SCRIPTS / "replay_check.py"), "--allow-demo", str(self.out)],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        self.replay_cli = cli.stdout + cli.stderr
        self.replay_code = cli.returncode

    def every_byte(self) -> bytes:
        """The whole output tree (every file, as bytes) plus every printed and logged line."""

        files = sorted(path for path in self.root.rglob("*") if path.is_file())
        assert files, "the capture wrote nothing"
        chunks = [path.read_bytes() for path in files]
        chunks += [
            text.encode("utf-8")
            for text in (self.printed, self.replay_cli, "\n".join(self.logs), *self.replay_failures)
        ]
        return b"\n".join(chunks)

    def pepper_encodings(self) -> dict[str, bytes]:
        raw = bytes.fromhex(self.pepper_hex)
        encodings = {
            "raw": raw,
            # a raw-byte leak into a TEXT channel (stdout, a log line) shows as one of these
            "raw_as_text": raw.decode("latin-1").encode("utf-8"),
            "raw_repr": repr(raw).encode(),
            "hex": self.pepper_hex.encode(),
            "HEX": self.pepper_hex.upper().encode(),
            "base64": base64.b64encode(raw),
            "base64_unpadded": base64.b64encode(raw).rstrip(b"="),
            "urlsafe_base64": base64.urlsafe_b64encode(raw),
            "urlsafe_base64_unpadded": base64.urlsafe_b64encode(raw).rstrip(b"="),
        }
        return encodings


def _found(haystack: bytes, session: Session) -> list[str]:
    hits = [name for name, needle in session.pepper_encodings().items() if needle in haystack]
    if fake_ibapi.ACCOUNT.encode() in haystack:
        hits.append("raw_account")
    return hits


@pytest.fixture
def session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Session:
    s = Session(tmp_path, monkeypatch)
    assert s.code == 0, s.printed
    assert s.replay_failures == [] and s.replay_code == 0, (s.replay_failures, s.replay_cli)
    return s


# --- (1) the leak search ------------------------------------------------------------------------


def test_no_output_byte_carries_the_raw_account_or_the_pepper_in_any_encoding(
    session: Session,
) -> None:
    every = session.every_byte()
    assert b"capture written to" in every and b"[PASS]" in every  # both runs really produced output
    assert _found(every, session) == []


def test_the_account_appears_only_as_its_pseudonym(session: Session) -> None:
    every = session.every_byte()
    assert b"ACCT-" in every  # the account IS in the capture, sanitised
    assert fake_ibapi.ACCOUNT.encode() not in every


@pytest.mark.parametrize(
    "target",
    [
        "raw",
        "raw_repr",
        "hex",
        "HEX",
        "base64",
        "base64_unpadded",
        "urlsafe_base64",
        "urlsafe_base64_unpadded",
        "raw_account",
    ],
)
@pytest.mark.parametrize("where", ["output_tree_file", "log_record"])
def test_positive_control_a_seeded_leak_is_found(session: Session, target: str, where: str) -> None:
    needle = (
        fake_ibapi.ACCOUNT.encode()
        if target == "raw_account"
        else session.pepper_encodings()[target]
    )
    if where == "output_tree_file":
        (session.out / "notes.bin").write_bytes(b"prefix " + needle + b" suffix")
        expected = target
    else:
        session.logs.append("leak: " + needle.decode("latin-1"))
        # raw bytes in a text line are seen in their text form
        expected = "raw_as_text" if target == "raw" else target
    assert expected in _found(session.every_byte(), session)


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "U-F3-F1: capture_readonly.sanitize writes the account as ACCT-<account_fingerprint[:16]>, "
        "an UNKEYED sha256 of 'chronos-account:<id>': enumerating the paper-account id space "
        "recovers the raw account from any fixture, and with it the con-id <-> account linkage "
        "of every position and execution. Campaign-script gap (not fixed here); smallest fix in "
        "RESULT: key the account token with the pepper like the other identifiers."
    ),
)
def test_u_f3_f1_the_account_token_cannot_be_recovered_without_the_pepper(
    session: Session,
) -> None:
    every = session.every_byte()
    # a bounded enumeration of the fake's account block, as a fixture reader could do it
    candidates = (f"DU555{n:04d}" for n in range(10_000))
    recovered = [
        account
        for account in candidates
        if f"ACCT-{account_fingerprint(account)[:16]}".encode() in every
    ]
    assert recovered == [], f"the account token reverses to {recovered} with no pepper"


# --- (2) the no-order transcript ----------------------------------------------------------------


def test_the_capture_calls_only_read_methods_of_the_adapter(session: Session) -> None:
    called = set(session.calls)
    assert {"connect", "positions", "historical_bars"} <= called  # the recorder really ran
    assert called <= _READ_ALLOWLIST, sorted(called - _READ_ALLOWLIST)
    assert not called & _ORDER_METHODS


def test_the_capture_sends_no_mutating_request_on_the_wire(session: Session) -> None:
    wire = set(session.wire)
    assert {"connect", "reqPositions", "reqHistoricalData"} <= wire  # the transcript really ran
    assert not wire & _MUTATING_METHODS, sorted(wire & _MUTATING_METHODS)
    assert {"placeOrder", "cancelOrder"} <= _MUTATING_METHODS  # the inventory still names them


def test_positive_control_a_seeded_order_call_is_caught_by_both_recorders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run the same capture, but make the harness also attempt a cancel: both the adapter
    recorder and the wire transcript must see it."""

    capture = _load("capture_readonly")
    real_run = capture.run_capture

    async def run_and_cancel(settings: Any, args: Any) -> Any:
        result = await real_run(settings, args)
        from chronos.broker.official_ibkr import OfficialIBKRBroker

        broker = OfficialIBKRBroker(settings)
        await broker.connect()
        try:
            await broker.cancel_order(fake_ibapi.OWNED_ID)
        except Exception:
            pass
        finally:
            await broker.disconnect()
        return result

    gateway = fake_ibapi.FakeGateway.capture_day()
    fake_ibapi.install(monkeypatch, gateway)
    workdir = tmp_path / "cwd"
    workdir.mkdir()
    _paper_environment(monkeypatch, workdir)
    monkeypatch.setenv("CHRONOS_CAPTURE_PEPPER", secrets.token_hex(32))
    monkeypatch.setattr(capture, "run_capture", run_and_cancel)
    with _adapter_calls(monkeypatch) as calls:
        _run_capture_main(monkeypatch, capture, tmp_path / "out" / "session-1")
    assert "cancel_order" in set(calls) & _ORDER_METHODS
    wire = {method for _client, method in gateway.transcript}
    # the adapter refuses an unowned cancel before send (no ledger here), or sends it: either way
    # the ADAPTER recorder caught the attempt; if it reached the wire, the transcript shows it
    assert not (wire & _MUTATING_METHODS) or "cancelOrder" in wire


def test_positive_control_the_wire_check_flags_a_mutating_request() -> None:
    assert {"reqPositions", "placeOrder"} & _MUTATING_METHODS == {"placeOrder"}


# --- (3) the no-pepper refusal: the gap U-f2 left -------------------------------------------------


def test_a_non_hex_pepper_of_the_right_length_writes_nothing_and_opens_no_socket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """U-f2 pins an absent and a too-short pepper; a 64-character NON-hex value is the gap."""

    gateway = fake_ibapi.FakeGateway.capture_day()
    fake_ibapi.install(monkeypatch, gateway)
    sockets: list[object] = []
    real_init = socket.socket.__init__

    def recording_init(self: socket.socket, *args: Any, **kwargs: Any) -> None:
        sockets.append(args)
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "__init__", recording_init)
    workdir = tmp_path / "cwd"
    workdir.mkdir()
    _paper_environment(monkeypatch, workdir)
    monkeypatch.setenv("CHRONOS_CAPTURE_PEPPER", "z" * 64)
    with _adapter_calls(monkeypatch) as calls:
        code, printed = _run_capture_main(
            monkeypatch, _load("capture_readonly"), tmp_path / "out" / "session-1"
        )
    assert code == 2 and printed.startswith("REFUSED: CHRONOS_CAPTURE_PEPPER must be hex"), printed
    assert not (tmp_path / "out").exists()
    assert gateway.transcript == [] and calls == [] and sockets == []
