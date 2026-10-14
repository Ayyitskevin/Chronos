"""B1 U-f3: the leak search and the no-order transcript over the FakeGateway (tests only).

Design ``b1-ready/DESIGN.md`` §U-f, packet U-f3. The campaign's REAL ``capture_readonly.main()``
runs a full capture over ``FakeGateway.capture_day()`` through the REAL official adapter, with a
THROWAWAY pepper (``secrets.token_hex(32)``) and a ``tmp_path`` output root, exactly as U-f2 does
(the helpers are U-f2's), then ``replay_check.py`` replays it as a rehearsal. Here:

1. **The leak search:** EVERY output byte -- the whole output tree (every file, read as bytes,
   the manifest included), the capture's stdout AND stderr (from its module import onward: r2)
   and the in-process replay's stdout AND
   stderr (each captured at the file-descriptor level, so ``print``, ``sys.stderr`` and a raw
   ``os.write`` are all seen), every log record of the root and ``chronos`` loggers, the replay's
   failure strings, and the replay CLI's stdout/stderr -- is searched for the raw account id and
   for the pepper raw, as hex (both cases) and as base64 (standard and urlsafe, with and without
   padding). Positive control: every target, planted into every one of those channels on its
   own, is found (r1, Daybreak P2-1: capture stderr and the in-process replay were not searched).
2. **The no-order transcript:** every ``OfficialIBKRBroker`` method the harness calls is
   recorded, and every request the FakeGateway receives on its wire; the adapter calls must be a
   subset of the read allowlist and the wire must carry none of the mutating requests
   (``placeOrder``/``cancelOrder``/``exerciseOptions``/``reqGlobalCancel``, the inventory of
   ``tests/safety/test_broker_mutation_inventory.py``). The prohibitions are two assertion helpers
   the normal tests use; positive control (r1, Daybreak P2-2): a capture whose harness ALSO calls
   the adapter's ``cancel_order`` and sends a raw ``cancelOrder`` on the fake wire makes EACH
   helper raise for that call.
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
import logging
import os
import secrets
import socket
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from tests.integration.test_capture_replay_fakegateway import (
    LABEL,
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
        "transport_proof",  # #304: the read-only proof gateway_evidence is stamped from
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


#: the in-process channels captured at the file-descriptor level (a positive control can plant here)
_FD_CHANNELS = {"capture_stdout": 1, "capture_stderr": 2, "replay_stdout": 1, "replay_stderr": 2}


class Session:
    """One full capture + replay, and every byte it produced.

    ``plant`` (positive controls only): ``(channel, needle)`` written with ``os.write`` to that
    in-process channel's file descriptor while the channel is being captured."""

    def __init__(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capfd: pytest.CaptureFixture[bytes],
        plant: tuple[str, bytes] | None = None,
        pepper_hex: str | None = None,
    ) -> None:
        self.gateway = fake_ibapi.FakeGateway.capture_day()
        fake_ibapi.install(monkeypatch, self.gateway)
        workdir = tmp_path / "cwd"
        workdir.mkdir()
        _paper_environment(monkeypatch, workdir)
        # throwaway: generated here (or by the positive control), gone with tmp_path
        self.pepper_hex = pepper_hex or secrets.token_hex(32)
        monkeypatch.setenv("CHRONOS_CAPTURE_PEPPER", self.pepper_hex)
        self.root = tmp_path / "out"
        self.out = self.root / "session-1"

        def emit(channel: str) -> None:
            if plant is not None and plant[0] == channel:
                os.write(_FD_CHANNELS[channel], b"leak: " + plant[1] + b"\n")

        argv = ["capture_readonly.py", "--out", str(self.out), "--label", LABEL]
        monkeypatch.setattr(sys, "argv", argv)
        with _all_logs() as logs, _adapter_calls(monkeypatch) as calls:
            capfd.readouterr()  # drop pytest's own bytes, before the capture's invocation starts
            emit("capture_stdout")
            emit("capture_stderr")
            # r2 (Daybreak r1 delta P2-1): the module is loaded INSIDE the retained window, so
            # what its import prints or logs is part of the searched capture output
            capture = _load("capture_readonly")
            self.code = capture.main()
            self.capture_stdout, self.capture_stderr = capfd.readouterr()
            emit("replay_stdout")
            emit("replay_stderr")
            self.replay_failures = _load("replay_check").check_session(self.out, allow_demo=True)
            self.replay_stdout, self.replay_stderr = capfd.readouterr()
        self.printed = self.capture_stdout.decode("utf-8", "replace")
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
            timeout=120,
            check=False,
        )
        self.replay_cli_stdout: bytes = cli.stdout
        self.replay_cli_stderr: bytes = cli.stderr
        self.replay_cli = (cli.stdout + cli.stderr).decode("utf-8", "replace")
        self.replay_code = cli.returncode

    def every_byte(self) -> bytes:
        """The whole output tree (every file, as bytes, the manifest included), the capture's and
        the in-process replay's stdout and stderr, the replay CLI's stdout and stderr, every log
        record and every replay failure string."""

        files = sorted(path for path in self.root.rglob("*") if path.is_file())
        assert files, "the capture wrote nothing"
        assert "manifest.json" in {path.name for path in files}
        chunks = [path.read_bytes() for path in files]
        chunks += [
            self.capture_stdout,
            self.capture_stderr,
            self.replay_stdout,
            self.replay_stderr,
            self.replay_cli_stdout,
            self.replay_cli_stderr,
        ]
        chunks += [text.encode("utf-8") for text in ("\n".join(self.logs), *self.replay_failures)]
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
def session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfdbinary: pytest.CaptureFixture[bytes]
) -> Session:
    s = Session(tmp_path, monkeypatch, capfdbinary)
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


_TARGETS = [
    "raw",
    "raw_repr",
    "hex",
    "HEX",
    "base64",
    "base64_unpadded",
    "urlsafe_base64",
    "urlsafe_base64_unpadded",
    "raw_account",
]


def _needle(target: str, pepper_hex: str) -> bytes:
    if target == "raw_account":
        return fake_ibapi.ACCOUNT.encode()
    raw = bytes.fromhex(pepper_hex)
    return {
        "raw": raw,
        "raw_repr": repr(raw).encode(),
        "hex": pepper_hex.encode(),
        "HEX": pepper_hex.upper().encode(),
        "base64": base64.b64encode(raw),
        "base64_unpadded": base64.b64encode(raw).rstrip(b"="),
        "urlsafe_base64": base64.urlsafe_b64encode(raw),
        "urlsafe_base64_unpadded": base64.urlsafe_b64encode(raw).rstrip(b"="),
    }[target]


@pytest.mark.parametrize("target", _TARGETS)
@pytest.mark.parametrize("channel", sorted(_FD_CHANNELS))
def test_r1_positive_control_a_leak_written_to_each_stream_is_found(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfdbinary: pytest.CaptureFixture[bytes],
    channel: str,
    target: str,
) -> None:
    """Daybreak P2-1: the capture's stderr and the in-process replay's stdout/stderr were not
    searched. Each needle is WRITTEN to the real file descriptor while that channel is captured
    (independently of the others), and the same scanner must find it."""

    pepper_hex = secrets.token_hex(32)  # throwaway, so the needle is known before the run
    needle = _needle(target, pepper_hex)
    s = Session(tmp_path, monkeypatch, capfdbinary, plant=(channel, needle), pepper_hex=pepper_hex)
    assert s.code == 0 and s.pepper_hex == pepper_hex
    assert target in _found(s.every_byte(), s)
    assert target not in _found(s.every_byte().replace(needle, b""), s)  # the hit IS the plant


@pytest.mark.parametrize("target", _TARGETS)
@pytest.mark.parametrize("fd", [1, 2], ids=["stdout", "stderr"])
def test_r2_positive_control_a_leak_written_while_the_capture_module_loads_is_found(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfdbinary: pytest.CaptureFixture[bytes],
    fd: int,
    target: str,
) -> None:
    """Daybreak r1 delta P2-1: bytes emitted while ``capture_readonly`` is IMPORTED were drained
    before the retained window opened. The needle is written to the real fd from inside the
    module load (``importlib.util.module_from_spec``, which U-f2's ``_load`` calls just before
    ``exec_module``), and the same scanner must find it in the capture's own stream."""

    import importlib.util

    pepper_hex = secrets.token_hex(32)  # throwaway, so the needle is known before the run
    needle = _needle(target, pepper_hex)
    real_module_from_spec = importlib.util.module_from_spec
    emitted: list[str] = []

    def loading(spec: Any) -> Any:
        if spec.name == "uf2_capture_readonly":
            os.write(fd, b"import-time leak: " + needle + b"\n")
            emitted.append(spec.name)
        return real_module_from_spec(spec)

    monkeypatch.setattr(importlib.util, "module_from_spec", loading)
    s = Session(tmp_path, monkeypatch, capfdbinary, pepper_hex=pepper_hex)

    assert emitted == ["uf2_capture_readonly"] and s.code == 0
    stream = s.capture_stdout if fd == 1 else s.capture_stderr
    assert needle in stream  # it landed in the capture's own retained stream
    assert target in _found(s.every_byte(), s)


@pytest.mark.parametrize("target", _TARGETS)
@pytest.mark.parametrize("record", ["replay_cli_stdout", "replay_cli_stderr", "replay_failure"])
def test_r1_positive_control_a_leak_in_each_recorded_channel_is_found(
    session: Session, record: str, target: str
) -> None:
    """The replay CLI's two pipes (``subprocess`` ``capture_output``) and the replay's failure
    strings: a needle in each recorded channel is found by the same scanner."""

    needle = _needle(target, session.pepper_hex)
    if record == "replay_failure":
        session.replay_failures.append("leak: " + needle.decode("latin-1"))
        expected = "raw_as_text" if target == "raw" else target  # a str record holds text
    else:
        setattr(session, record, getattr(session, record) + b"leak: " + needle)
        expected = target
    assert expected in _found(session.every_byte(), session)


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


def _assert_adapter_calls_are_reads(calls: list[str]) -> None:
    """The adapter prohibition, shared by the normal test and its positive control."""

    called = set(calls)
    assert {"connect", "positions", "historical_bars"} <= called  # the recorder really ran
    assert not called & _ORDER_METHODS, (
        f"order-plane adapter calls: {sorted(called & _ORDER_METHODS)}"
    )
    assert called <= _READ_ALLOWLIST, f"unlisted adapter calls: {sorted(called - _READ_ALLOWLIST)}"


def _assert_wire_has_no_mutating_request(wire: list[str]) -> None:
    """The wire prohibition, shared by the normal test and its positive control."""

    sent = set(wire)
    assert {"connect", "reqPositions", "reqHistoricalData"} <= sent  # the transcript really ran
    assert not sent & _MUTATING_METHODS, (
        f"mutating wire requests: {sorted(sent & _MUTATING_METHODS)}"
    )


def test_the_capture_calls_only_read_methods_of_the_adapter(session: Session) -> None:
    _assert_adapter_calls_are_reads(session.calls)


def test_the_capture_sends_no_mutating_request_on_the_wire(session: Session) -> None:
    _assert_wire_has_no_mutating_request(session.wire)
    assert {"placeOrder", "cancelOrder"} <= _MUTATING_METHODS  # the inventory still names them


def test_positive_control_a_seeded_order_call_is_caught_by_both_recorders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Daybreak P2-2: the same capture, but the harness ALSO calls the adapter's ``cancel_order``
    (the adapter refuses it before send) and sends a raw ``cancelOrder`` request on the fake wire
    through a client of the same gateway. Each recorder holds the call, and the SAME helper the
    normal test uses raises for it -- after its liveness check passed on the real entries."""

    capture = _load("capture_readonly")
    real_run = capture.run_capture
    gateway = fake_ibapi.FakeGateway.capture_day()

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
        raw = fake_ibapi.FakeEClient.bound_to(gateway)(wrapper=None)
        raw.connect("127.0.0.1", 4002, 77)
        raw.cancelOrder(fake_ibapi.OWNED_ID)
        raw.disconnect()
        return result

    fake_ibapi.install(monkeypatch, gateway)
    workdir = tmp_path / "cwd"
    workdir.mkdir()
    _paper_environment(monkeypatch, workdir)
    monkeypatch.setenv("CHRONOS_CAPTURE_PEPPER", secrets.token_hex(32))
    monkeypatch.setattr(capture, "run_capture", run_and_cancel)
    with _adapter_calls(monkeypatch) as calls:
        code, printed = _run_capture_main(monkeypatch, capture, tmp_path / "out" / "session-1")
    assert code == 0, printed
    wire = [method for _client, method in gateway.transcript]
    assert "cancelOrder" in wire  # the mutating request really reached the recorded wire
    with pytest.raises(AssertionError, match=r"order-plane adapter calls: \['cancel_order'\]"):
        _assert_adapter_calls_are_reads(list(calls))
    with pytest.raises(AssertionError, match=r"mutating wire requests: \['cancelOrder'\]"):
        _assert_wire_has_no_mutating_request(wire)
    # and without the seeded calls, the same helpers pass on the same recordings
    _assert_adapter_calls_are_reads([c for c in calls if c != "cancel_order"])
    _assert_wire_has_no_mutating_request([m for m in wire if m != "cancelOrder"])


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
