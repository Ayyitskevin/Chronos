"""B1 U-d: ``python -m chronos.cli b1-preflight``, the OFFLINE preflight (DESIGN r2 §U-d).

What the command is, said plainly: an owner-local step that READS SECRETS. It opens the private
``.env`` (once, through one descriptor: ``read_authority_file(..., AuthorityMode.SECRET)``, the
TOCTOU-free admission Daybreak's r1 delta asked for), so it reads the capture pepper (P10 checks
its shape) and any credential mistakenly present (P9). "It never prints a value" is the
disclosure control this suite proves, not "it never reads one".

Proved here, each with a positive control where the assertion is a negative:
* every config check fails closed with an operator-readable message, and the exit codes are
  0 CLEAR / 78 REFUSE / 1 UNDECIDED;
* no network: no socket is created, bound, connected or resolved (an audit hook), no broker
  module or broker library is imported (a fresh process), and the command's transitive chronos
  import closure names no broker module and imports no network module (AST);
* the effects are exactly the declared reads: the admitted ``.env``, the configured database
  read-only, the kill-switch and halt state, ``/proc/net/tcp*`` and the installed ``ibapi``'s
  files; nothing is written;
* a canary pepper, a canary credential and the account id appear in no output byte, log record,
  exception text or result object.
The Read-Only API checkbox is reported UNVERIFIABLE, never CLEAR. Nothing here connects to
anything; ALLOW_* stay false.
"""

from __future__ import annotations

import ast
import hashlib
import json
import logging
import os
import subprocess
import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

import pytest

from chronos.cli import b1_preflight
from chronos.cli.b1_preflight import (
    EXIT_CLEAR,
    EXIT_REFUSED,
    EXIT_UNDECIDED,
    B1Report,
    run_b1_preflight,
)

_REPO = Path(__file__).resolve().parents[2]
PEPPER_CANARY = "c0ffee" * 10 + "abcd"  # 64 lowercase hex: a valid shape, a unique byte string
CREDENTIAL_CANARY = "canary-credential-7f3e1b"
ACCOUNT = "DU7654321"
_PAPER = 7497
_LIVE = 7496


# --- building a host ------------------------------------------------------------------------


def _good_text(database: Path) -> str:
    return "\n".join(
        [
            "BROKER_MODE=ibkr",
            "BROKER_ADAPTER=official_ibkr",
            "IB_ENVIRONMENT=paper",
            "IB_HOST=127.0.0.1",
            f"IB_PORT={_PAPER}",
            "IB_CLIENT_ID=17",
            f"IB_ACCOUNT_ID={ACCOUNT}",
            "ALLOW_ORDER_TRANSMIT=false",
            "ALLOW_LIVE_TRADING=false",
            "SYMBOL_ALLOWLIST=AAPL,MSFT,SPY",
            f"DATABASE_URL=sqlite:///{database}",
            f"CHRONOS_CAPTURE_PEPPER={PEPPER_CANARY}",
            "",
        ]
    )


def _tcp_line(port: int, state: str = "0A") -> str:
    return f"   0: 0100007F:{port:04X} 00000000:0000 {state} 00000000:00000000 00:00000000 0 1000\n"


@pytest.fixture
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Path]]:
    """A clean B1 host in tmp: the good .env (0600), a listening paper port, a 4-arg ibapi."""

    work = tmp_path / "repo"
    work.mkdir()
    monkeypatch.chdir(work)  # relative state paths (kill switch, halt) land here
    for name in list(os.environ):  # the runner's own exports would (rightly) trip P12
        if name.startswith(("BROKER_", "IB_", "ALLOW_", "AUTONOMY_", "CHRONOS_")):
            monkeypatch.delenv(name)
    env_file = work / ".env"
    database = tmp_path / "b1" / "chronos-b1.db"
    env_file.write_text(_good_text(database), encoding="utf-8")
    env_file.chmod(0o600)
    proc_net = tmp_path / "proc-net"
    proc_net.mkdir()
    (proc_net / "tcp").write_text("  sl  local_address ...\n" + _tcp_line(_PAPER))
    (proc_net / "tcp6").write_text("  sl  local_address ...\n")
    site = tmp_path / "site"
    _install_fake_ibapi(site, "self, reqId, errorCode, errorString, advancedOrderRejectJson=''")
    monkeypatch.syspath_prepend(str(site))
    yield {
        "work": work,
        "env": env_file,
        "database": database,
        "proc_net": proc_net,
        "site": site,
        "halt": work / "data" / "platform_halt.json",
    }


def _install_fake_ibapi(site: Path, error_params: str, *, version: str = "10.19.4") -> None:
    package = site / "ibapi"
    package.mkdir(parents=True, exist_ok=True)
    (package / "__init__.py").write_text(
        "raise ImportError('the preflight must never import ibapi')\n"
    )
    (package / "wrapper.py").write_text(
        f"class EWrapper:\n    def error(self{', ' if error_params.startswith('self, ') else ''}"
        f"{error_params.removeprefix('self, ')}):\n        pass\n"
    )
    dist = site / f"ibapi-{version}.dist-info"
    dist.mkdir(exist_ok=True)
    (dist / "METADATA").write_text(f"Metadata-Version: 2.1\nName: ibapi\nVersion: {version}\n")


def _run(host: dict[str, Path], environ: Mapping[str, str] | None = None) -> B1Report:
    return run_b1_preflight(
        env_file=host["env"],
        environ={} if environ is None else environ,
        halt_file=host["halt"],
        proc_net=host["proc_net"],
    )


def _bounded_run(host: dict[str, Path], seconds: float = 10.0) -> B1Report:
    """``_run`` on a daemon thread: a regression that blocks (a path read of a planted FIFO)
    fails the test instead of hanging the suite (the reader's build-ua P3-2 lesson)."""

    import threading

    box: list[B1Report] = []
    worker = threading.Thread(target=lambda: box.append(_run(host)), daemon=True)
    worker.start()
    worker.join(seconds)
    assert box, f"the preflight did not return within {seconds:.0f}s (did a read block?)"
    return box[0]


def _rewrite(host: dict[str, Path], key: str, value: str | None) -> None:
    lines = [ln for ln in host["env"].read_text().splitlines() if not ln.startswith(f"{key}=")]
    if value is not None:
        lines.append(f"{key}={value}")
    host["env"].write_text("\n".join(lines) + "\n")
    host["env"].chmod(0o600)


def _levels(report: B1Report, check: str) -> list[str]:
    return [finding.level for finding in report.findings if finding.check == check]


def _text(report: B1Report, check: str) -> str:
    return " | ".join(finding.text for finding in report.findings if finding.check == check)


# --- 1. the clean host, and the verdict -----------------------------------------------------


def test_a_good_b1_file_on_a_clean_host_is_clear(host: dict[str, Path]) -> None:
    report = _run(host)
    verdict, code = report.verdict()
    assert code == EXIT_CLEAR, [str(f) for f in report.findings]
    assert verdict.startswith("B1-PREFLIGHT CLEAR")
    assert "NOT checked" in verdict and "Read-Only API" in verdict
    assert _levels(report, "env") == ["CLEAR"]
    assert _levels(report, "ibapi") == ["CLEAR"]
    assert "10.19.4" in _text(report, "ibapi")


def test_the_read_only_api_checkbox_is_unverifiable_never_clear(host: dict[str, Path]) -> None:
    report = _run(host)
    assert _levels(report, "read-only-api") == ["UNVERIFIABLE"]
    assert "owner visual check" in _text(report, "read-only-api")


@pytest.mark.parametrize(
    ("levels", "code"),
    [
        (["CLEAR", "INFO", "ADVISORY", "UNVERIFIABLE"], EXIT_CLEAR),
        (["CLEAR", "UNDECIDED"], EXIT_UNDECIDED),
        (["UNDECIDED", "REFUSE"], EXIT_REFUSED),
        (["REFUSE"], EXIT_REFUSED),
    ],
)
def test_the_exit_code_is_refuse_over_undecided_over_clear(levels: list[str], code: int) -> None:
    report = B1Report()
    for number, level in enumerate(levels):
        report.add(level, f"check-{number}", "text")
    assert report.verdict()[1] == code
    assert (EXIT_CLEAR, EXIT_UNDECIDED, EXIT_REFUSED) == (0, 1, 78)


def test_the_command_is_registered_and_its_exit_code_is_the_verdicts(
    host: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    from chronos.cli.main import main

    _rewrite(host, "IB_PORT", str(_LIVE))
    code = main(
        [
            "b1-preflight",
            "--env-file",
            str(host["env"]),
            "--halt-file",
            str(host["halt"]),
            "--proc-net",
            str(host["proc_net"]),
        ]
    )
    out = capsys.readouterr().out
    assert code == EXIT_REFUSED
    assert "REFUSE [P3]" in out and out.rstrip().splitlines()[-1].startswith("B1-PREFLIGHT REFUSED")


# --- 2. the file admission: one descriptor, fail closed ------------------------------------


def test_a_missing_env_file_refuses_and_says_what_to_do(host: dict[str, Path]) -> None:
    host["env"].unlink()
    report = _run(host)
    assert report.verdict()[1] == EXIT_REFUSED
    assert _levels(report, "env-file") == ["REFUSE"]
    assert "runbook step 6" in _text(report, "env-file")


@pytest.mark.parametrize("shape", ["mode-0644", "symlink", "two-links", "fifo"])
def test_an_unsafe_env_file_is_refused_before_anything_is_read(
    host: dict[str, Path], shape: str
) -> None:
    env = host["env"]
    if shape == "mode-0644":
        env.chmod(0o644)
    elif shape == "symlink":
        real = env.with_name("real.env")
        env.rename(real)
        env.symlink_to(real)
    elif shape == "two-links":
        os.link(env, env.with_name("second.env"))
    else:
        env.unlink()
        os.mkfifo(env)  # must refuse, not hang (O_NONBLOCK)
    report = _bounded_run(host)
    assert report.verdict()[1] == EXIT_REFUSED
    assert _levels(report, "env-file") == ["REFUSE"]
    assert "chronos-preflight env file" in _text(report, "env-file")
    # nothing downstream ran on an unadmitted file
    assert _levels(report, "env") == [] and _levels(report, "database") == []


# --- 3. every config condition fails closed, readably ---------------------------------------


@pytest.mark.parametrize(
    ("key", "value", "condition"),
    [
        ("IB_PORT", str(_LIVE), "P3"),
        ("IB_HOST", "10.0.0.5", "P3"),
        ("IB_ACCOUNT_ID", "U1234567", "P4"),
        ("IB_ACCOUNT_ID", "DU1234567", "P4"),
        ("ALLOW_ORDER_TRANSMIT", "true", "P5"),
        ("AUTONOMY_MANDATE_FILE", "/srv/mandate.json", "P6"),
        ("DATABASE_URL", "sqlite:///data/chronos.db", "P7"),
        ("IB_DATA_CLIENT_ID", "17", "P8"),
        ("IB_PASSWORD", CREDENTIAL_CANARY, "P9"),
        ("CHRONOS_CAPTURE_PEPPER", "short", "P10"),
        ("IB_ACCOUNT_ALLOWLIST", ACCOUNT, "P13"),
        ("IB_ACCOUNT_ALLOWLIST", f'["{ACCOUNT}"]', "P13"),
    ],
)
def test_each_config_condition_refuses_with_a_readable_message(
    host: dict[str, Path], key: str, value: str, condition: str
) -> None:
    _rewrite(host, key, value)
    report = _run(host)
    assert report.verdict()[1] == EXIT_REFUSED
    assert "REFUSE" in _levels(report, condition), [str(f) for f in report.findings]
    message = _text(report, condition)
    assert len(message) > 20  # an operator-readable rule, not a bare code
    if value not in ("true", "false"):  # a boolean word can occur in any sentence
        assert value not in message  # names the rule, never the value


@pytest.mark.parametrize("value", [None, "", "[]", "   "])
def test_an_absent_or_empty_account_allowlist_is_accepted(
    host: dict[str, Path], value: str | None
) -> None:
    _rewrite(host, "IB_ACCOUNT_ALLOWLIST", value)
    assert _levels(_run(host), "P13") == []


def test_an_exported_variable_that_outranks_the_file_refuses(host: dict[str, Path]) -> None:
    report = _run(host, environ={"IB_PORT": str(_PAPER)})
    assert "REFUSE" in _levels(report, "P12")


# --- 4. the installed ibapi, read without importing it --------------------------------------


def test_no_ibapi_installed_refuses_with_install_guidance(
    host: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "path", [p for p in sys.path if p != str(host["site"])])
    report = _run(host)
    assert _levels(report, "ibapi") == ["REFUSE"]
    assert "runbook step 5" in _text(report, "ibapi")


@pytest.mark.parametrize(
    ("params", "level"),
    [
        ("self, reqId, errorCode, errorString", "CLEAR"),
        ("self, reqId, errorCode, errorString, advancedOrderRejectJson=''", "CLEAR"),
        ("self, reqId, errorTime, errorCode, errorString, advancedOrderRejectJson=''", "REFUSE"),
        ("self, *args", "UNDECIDED"),
    ],
)
def test_the_installed_ewrapper_error_signature_settles_r_arity_before_b1(
    host: dict[str, Path], params: str, level: str
) -> None:
    """Reader r1 P3-1: B1R-F2 is real exactly when the installed error() takes errorTime second
    (the adapter would read the time as the code); this reads the shape from source, offline."""

    _install_fake_ibapi(host["site"], params)
    report = _run(host)
    assert _levels(report, "ibapi") == [level]
    if level == "REFUSE":
        assert "B1R-F2" in _text(report, "ibapi")


# --- 5. the database's scope binding, read-only ----------------------------------------------


def _bind(database: Path, *, broker_mode: str, account: str) -> None:
    from chronos.persistence.database import Database

    database.parent.mkdir(parents=True, exist_ok=True)
    store = Database(f"sqlite:///{database}")
    store.initialize()
    store.bind_scope(broker_mode=broker_mode, environment="paper", account_id=account)
    store.dispose()


def test_an_absent_database_is_clear_it_binds_at_the_first_start(host: dict[str, Path]) -> None:
    assert _levels(_run(host), "scope") == ["CLEAR"]


def test_a_database_bound_to_this_paper_account_is_clear(host: dict[str, Path]) -> None:
    _bind(host["database"], broker_mode="ibkr", account=ACCOUNT)
    assert _levels(_run(host), "scope") == ["CLEAR"]


def test_a_database_bound_elsewhere_refuses_and_names_the_fix(host: dict[str, Path]) -> None:
    _bind(host["database"], broker_mode="demo", account="DEMO-ACCOUNT")
    report = _run(host)
    assert _levels(report, "scope") == ["REFUSE"]
    assert "different broker scope" in _text(report, "scope")
    assert "DATABASE_URL" in _text(report, "scope")


# --- 6. ports, kill switch, halt ---------------------------------------------------------------


def test_a_listening_live_port_refuses(host: dict[str, Path]) -> None:
    (host["proc_net"] / "tcp6").write_text("  sl\n" + _tcp_line(_LIVE))
    report = _run(host)
    assert "REFUSE" in _levels(report, "ports")
    assert "live" in _text(report, "ports").lower()


def test_no_paper_port_listening_is_advisory_not_a_refusal(host: dict[str, Path]) -> None:
    (host["proc_net"] / "tcp").write_text("  sl\n" + _tcp_line(_PAPER, state="01"))  # not LISTEN
    report = _run(host)
    assert _levels(report, "ports") == ["ADVISORY"]
    assert report.verdict()[1] == EXIT_CLEAR


def test_the_kill_switch_and_halt_states_are_reported(host: dict[str, Path]) -> None:
    report = _run(host)
    assert _levels(report, "kill-switch") == ["INFO"]
    assert _levels(report, "halt") == ["INFO"]


def test_a_corrupt_kill_switch_file_is_flagged_as_read_engaged(host: dict[str, Path]) -> None:
    """LiveKillSwitch.read() fails closed on an unreadable file (it reads ENGAGED, it never
    raises), so the preflight flags it for inspection instead of calling it undecided."""

    state = host["work"] / "data" / "live_kill_switch.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text("{not json")
    report = _run(host)
    assert _levels(report, "kill-switch") == ["ADVISORY"]
    assert "unreadable" in _text(report, "kill-switch") and "ENGAGED" in _text(
        report, "kill-switch"
    )


# --- 7. the canary: no secret in any output, log, exception text or result ---------------------


def _leaked(haystack: str) -> list[str]:
    return [canary for canary in (PEPPER_CANARY, CREDENTIAL_CANARY, ACCOUNT) if canary in haystack]


class _Records(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage() + repr(record.__dict__))


@contextmanager
def _all_logs() -> Iterator[_Records]:
    """Every record from the root and the (non-propagating) chronos logger trees."""

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


def _everything(
    host: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
    *,
    json_out: bool,
    with_credential: bool = True,
) -> str:
    from chronos.cli.main import main

    if with_credential:
        _rewrite(host, "IB_PASSWORD", CREDENTIAL_CANARY)  # also forces a P9 refusal
    with _all_logs() as logs:
        argv = ["b1-preflight", "--env-file", str(host["env"]), "--halt-file", str(host["halt"])]
        argv += ["--proc-net", str(host["proc_net"])]
        code = main([*argv, "--json"] if json_out else argv)
        report = _run(host)
    captured = capsys.readouterr()
    return "\n".join(
        [
            captured.out,
            captured.err,
            *logs.lines,
            repr(report),
            *(str(finding) for finding in report.findings),
            report.verdict()[0],
            str(code),
        ]
    )


@pytest.mark.parametrize("json_out", [False, True])
@pytest.mark.parametrize("with_credential", [True, False], ids=["refusing", "clear"])
def test_no_secret_or_account_id_appears_anywhere(
    host: dict[str, Path], capsys: pytest.CaptureFixture[str], json_out: bool, with_credential: bool
) -> None:
    """Both paths: the refusing one (a planted credential, P9) and the CLEAR one, whose summary
    line is only produced when every condition is met (my own M3 mutant leaked there unseen
    while this case covered the refusing path alone)."""

    everything = _everything(host, capsys, json_out=json_out, with_credential=with_credential)
    if with_credential:
        assert "REFUSE [P9]" in everything or '"P9"' in everything  # the canary file really ran
    else:
        assert "B1-PREFLIGHT CLEAR" in everything  # the clear path really ran
    assert _leaked(everything) == []


def test_the_canary_search_catches_a_leak(
    host: dict[str, Path], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Positive control: a mutant that echoes the pepper in a finding is caught by the search."""

    real = b1_preflight.preflight_b1_env

    def leaky(text: str, **kwargs: object) -> list[tuple[str, str]]:
        return [*real(text, **kwargs), ("P10", f"pepper is {PEPPER_CANARY}")]  # type: ignore[arg-type]

    monkeypatch.setattr(b1_preflight, "preflight_b1_env", leaky)
    assert PEPPER_CANARY in _leaked(_everything(host, capsys, json_out=False))


# --- 8. effects: exactly the declared reads, no socket, no write --------------------------------

_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC
_EFFECTS: dict[str, object] = {"on": False, "events": []}


def _effect_hook(event: str, args: tuple[object, ...]) -> None:
    if not _EFFECTS["on"]:
        return
    if event.startswith("socket."):
        raise AssertionError(f"network effect during the preflight: {event}")
    if event in {"open", "os.open", "sqlite3.connect"}:
        events = _EFFECTS["events"]
        assert isinstance(events, list)
        events.append((event, args))


if not getattr(sys, "_b1_preflight_effect_hook", False):
    sys.addaudithook(_effect_hook)
    sys._b1_preflight_effect_hook = True  # type: ignore[attr-defined]


@contextmanager
def _effects() -> Iterator[list[tuple[str, tuple[object, ...]]]]:
    events: list[tuple[str, tuple[object, ...]]] = []
    _EFFECTS["on"], _EFFECTS["events"] = True, events
    try:
        yield events
    finally:
        _EFFECTS["on"] = False


def _opened(events: list[tuple[str, tuple[object, ...]]]) -> list[tuple[str, str, bool]]:
    """(event, path, writes) for every path open."""

    out: list[tuple[str, str, bool]] = []
    for event, args in events:
        target = args[0] if args else None
        if not isinstance(target, str | bytes | os.PathLike):
            continue
        path = os.fsdecode(target)
        mode = args[1] if len(args) > 1 else None
        flags = args[2] if len(args) > 2 else 0
        if event == "sqlite3.connect":
            writes = "mode=ro" not in path
        else:
            writes = (isinstance(mode, str) and any(c in mode for c in "wax+")) or bool(
                isinstance(flags, int) and flags & _WRITE_FLAGS
            )
        out.append((event, path, writes))
    return out


def _allowed(path: str, host: dict[str, Path]) -> bool:
    resolved = path.split("?", 1)[0].removeprefix("file:")
    if resolved.startswith("///"):  # file:///abs/path
        resolved = resolved[2:]
    if not os.path.isabs(resolved):  # a relative state path resolves against the cwd
        resolved = str(Path.cwd() / resolved)
    allowed_exact = {
        str(host["env"]),
        str(host["database"]),
        str(host["halt"]),
        str(host["work"] / "data" / "live_kill_switch.json"),
    }
    return (
        resolved in allowed_exact
        or resolved.startswith(str(host["proc_net"]))
        or resolved.startswith(str(host["site"]))
        or resolved.startswith(str(host["work"] / "data"))  # the kill switch's generation marker
        or resolved.startswith(str(host["database"].parent))
    )


def test_the_preflight_opens_only_its_declared_reads_and_writes_nothing(
    host: dict[str, Path],
) -> None:
    """Daybreak r1 P3-1's exact oracle: modules preloaded (one warm-up run), then every open is
    a declared read and none carries a write flag; any socket event fails the run."""

    _bind(host["database"], broker_mode="ibkr", account=ACCOUNT)  # exercise the database read
    _run(host)  # warm-up: load every lazily imported module outside the guarded window
    with _effects() as events:
        report = _run(host)
    opened = _opened(events)
    assert report.verdict()[1] == EXIT_CLEAR
    assert any(path == str(host["env"]) for _, path, _ in opened)  # the oracle saw the read
    assert [path for _, path, writes in opened if writes] == []
    assert [path for _, path, _ in opened if not _allowed(path, host)] == []
    env_opens = [p for e, p, _ in opened if p == str(host["env"])]
    assert len(env_opens) == 1  # one descriptor, one read


def test_the_effect_oracle_sees_a_stray_open_and_a_write(host: dict[str, Path]) -> None:
    """Positive control: the same oracle flags an undeclared read and a write."""

    stray = host["work"] / "stray.txt"
    with _effects() as events:
        stray.write_text("x")
        Path("/etc/hostname").exists() and Path("/etc/hostname").read_text()
    opened = _opened(events)
    assert str(stray) in [path for _, path, writes in opened if writes]
    assert [path for _, path, _ in opened if not _allowed(path, host)]


def test_the_effect_hook_fails_a_socket(host: dict[str, Path]) -> None:
    """Positive control: a socket created inside the guarded window fails."""

    import socket

    with _effects(), pytest.raises(AssertionError, match="network effect"):
        socket.socket(socket.AF_INET, socket.SOCK_STREAM).close()


def test_a_fresh_process_run_imports_no_broker_and_opens_no_socket(host: dict[str, Path]) -> None:
    """The command end to end in a new interpreter, as an operator runs it: an audit hook armed
    BEFORE chronos is imported fails on any socket event; afterwards no broker module or broker
    library may be loaded."""

    script = f"""
import sys
def hook(event, args):
    if event.startswith("socket."):
        raise SystemExit("socket event: " + event)
sys.addaudithook(hook)
from chronos.cli.main import main
code = main(["b1-preflight", "--env-file", {str(host["env"])!r}, "--halt-file",
             {str(host["halt"])!r}, "--proc-net", {str(host["proc_net"])!r}])
bad = sorted(m for m in sys.modules
             if m.startswith("chronos.broker") or m.split(".")[0] in ("ibapi", "ib_async"))
print("BROKER-MODULES", bad)
print("EXIT", code)
"""
    environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONPATH": os.pathsep.join([str(host["site"]), str(_REPO / "src"), str(_REPO)]),
        "HOME": str(host["work"]),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    done = subprocess.run(
        [sys.executable, "-c", script],
        cwd=host["work"],
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert "socket event" not in done.stdout + done.stderr, done.stderr[-2000:]
    assert "BROKER-MODULES []" in done.stdout, done.stdout + done.stderr[-2000:]
    assert "EXIT 0" in done.stdout


# --- 9. the transitive import closure, statically ----------------------------------------------

_NETWORK_MODULES = frozenset(
    {"socket", "ssl", "http", "urllib", "requests", "httpx", "aiohttp", "websockets", "asyncio"}
)
_BROKER_LIBRARIES = frozenset({"ibapi", "ib_async"})


def _module_file(module: str) -> Path | None:
    base = _REPO / "src" / Path(*module.split("."))
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


def _imports_of(path: Path, module: str) -> set[str]:
    package = module if path.name == "__init__.py" else module.rpartition(".")[0]
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parts = package.split(".")
                parts = parts[: len(parts) - (node.level - 1)]
                base = ".".join([*parts, node.module] if node.module else parts)
            else:
                base = node.module or ""
            found.add(base)
            found.update(f"{base}.{alias.name}" for alias in node.names)
    return found


def _closure(start: str) -> tuple[set[str], set[str]]:
    """(chronos modules reachable, every non-chronos top-level name they import), lazily
    imported names included (function bodies are walked too)."""

    seen: set[str] = set()
    external: set[str] = set()
    todo = [start]
    while todo:
        module = todo.pop()
        path = _module_file(module)
        if module in seen or path is None:
            continue
        seen.add(module)
        parts = module.split(".")
        todo.extend(".".join(parts[:i]) for i in range(1, len(parts)))
        for name in _imports_of(path, module):
            if name.split(".")[0] == "chronos":
                todo.append(name)
            elif name:
                external.add(name.split(".")[0])
    return seen, external


def test_the_preflights_import_closure_has_no_broker_and_no_network_module() -> None:
    modules, external = _closure("chronos.cli.b1_preflight")
    assert "chronos.cli.b1_preflight" in modules and len(modules) > 5  # the walk really ran
    assert sorted(m for m in modules if m.startswith("chronos.broker")) == []
    assert sorted(external & (_NETWORK_MODULES | _BROKER_LIBRARIES)) == []


def test_the_closure_scan_finds_a_broker_or_network_import() -> None:
    """Positive control: the same scan, from a module that does reach the broker plane."""

    modules, external = _closure("chronos.broker.official_ibkr")
    assert "chronos.broker.official_ibkr" in modules
    assert {"socket"} & external or {"asyncio"} & external


def test_the_preflight_never_imports_ibapi_even_when_it_is_installed(host: dict[str, Path]) -> None:
    """The fake ibapi package raises on import; a CLEAR proves it was read as files only."""

    assert _run(host).verdict()[1] == EXIT_CLEAR
    assert "ibapi" not in sys.modules


def test_the_json_output_is_one_object_with_the_verdict(
    host: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    from chronos.cli.main import main

    argv = ["b1-preflight", "--env-file", str(host["env"]), "--halt-file", str(host["halt"])]
    code = main([*argv, "--proc-net", str(host["proc_net"]), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["exit"] == code == EXIT_CLEAR
    assert payload["verdict"].startswith("B1-PREFLIGHT CLEAR")
    assert {line["check"] for line in payload["findings"]} >= {"env", "ibapi", "read-only-api"}


# --- U-d r1: pins for the reader's surviving mutants (review P2-1) and the cheap P3 ------------


def test_r1_m6_a_database_bound_to_another_paper_account_refuses(host: dict[str, Path]) -> None:
    """m6 survived: the existing REFUSE case differs in BROKER MODE only. Same mode (ibkr), same
    environment (paper), a different paper account: the backend would refuse at start, or worse
    mix two accounts' history -- the preflight must REFUSE here too."""

    _bind(host["database"], broker_mode="ibkr", account="DU1111111")
    report = _run(host)
    assert _levels(report, "scope") == ["REFUSE"]
    assert "different broker scope" in _text(report, "scope")
    assert "DU1111111" not in _text(report, "scope")  # names the fix, never the other account


@pytest.mark.parametrize("key", ["IB_PORT", "IB_CLIENT_ID", "TARGET_DTE"])
def test_r1_m8_a_canary_inside_an_invalid_typed_field_appears_nowhere(
    host: dict[str, Path], capsys: pytest.CaptureFixture[str], key: str
) -> None:
    """m8 survived: the canary search only covered files that load. A value Settings REFUSES to
    parse is exactly where pydantic's str(ValidationError) would echo ``input_value=...``; the
    report must carry the rule text only, on every surface."""

    # a SHORT canary: pydantic elides a long input in str(ValidationError) ("c0ffeec0ff...abcd"),
    # so the 64-hex pepper canary would hide exactly the leak this pin is for (measured: m8
    # survived it); the 24-character credential canary is printed whole
    _rewrite(host, key, CREDENTIAL_CANARY)
    everything = _everything(host, capsys, json_out=False, with_credential=False)
    assert "Settings refused the file" in everything  # the refusal path really ran
    assert _leaked(everything) == []


def test_r1_m1_the_other_live_port_4001_also_refuses(host: dict[str, Path]) -> None:
    """m1 survived: only 7496 was tested. IB Gateway's live port is 4001."""

    (host["proc_net"] / "tcp").write_text("  sl\n" + _tcp_line(_PAPER) + _tcp_line(4001))
    report = _run(host)
    assert "REFUSE" in _levels(report, "ports")
    assert report.verdict()[1] == EXIT_REFUSED


def test_r1_m4_a_32_hex_pepper_is_too_short_and_refuses(host: dict[str, Path]) -> None:
    """m4 survived: only a non-hex 'short' value was tested. 32 hex digits (16 bytes) is a valid
    hex string but under the 64-hex (32-byte) requirement."""

    _rewrite(host, "CHRONOS_CAPTURE_PEPPER", "ab" * 16)
    report = _run(host)
    assert "REFUSE" in _levels(report, "P10")
    assert "ab" * 16 not in _text(report, "P10")


def test_r1_m9_a_token_key_is_a_credential_and_refuses(host: dict[str, Path]) -> None:
    """m9 survived: only a *_PASSWORD key was tested; *_TOKEN is in the same rule."""

    _rewrite(host, "IB_TOKEN", CREDENTIAL_CANARY)
    report = _run(host)
    assert "REFUSE" in _levels(report, "P9")
    assert CREDENTIAL_CANARY not in _text(report, "P9")


def test_r1_m11_a_five_parameter_ewrapper_error_is_undecided_never_clear(
    host: dict[str, Path],
) -> None:
    """m11 survived: no test said a LONGER shape must not be CLEAR. Five parameters with the
    known first three are not a shape the adapter is known to read (K4 #1)."""

    _install_fake_ibapi(
        host["site"],
        "self, reqId, errorCode, errorString, advancedOrderRejectJson='', extra=None",
    )
    report = _run(host)
    assert _levels(report, "ibapi") == ["UNDECIDED"]
    assert report.verdict()[1] == EXIT_UNDECIDED


def test_r1_p3_1_a_clear_run_on_a_bound_database_changes_none_of_its_bytes(
    host: dict[str, Path],
) -> None:
    """Reader P3-1, recorded (no src change in this round): the run may leave SQLite's own
    -shm/-wal files beside an existing database -- nothing else -- and the database file's bytes
    are unchanged. "No write" in the docstring means the database and secrets, not the directory."""

    import hashlib

    _bind(host["database"], broker_mode="ibkr", account=ACCOUNT)
    directory = host["database"].parent
    for side in directory.glob("chronos-b1.db-*"):
        side.unlink()
    before = hashlib.sha256(host["database"].read_bytes()).hexdigest()
    listing = {p.name for p in directory.iterdir()}
    report = _run(host)
    assert _levels(report, "scope") == ["CLEAR"]
    assert hashlib.sha256(host["database"].read_bytes()).hexdigest() == before
    new = {p.name for p in directory.iterdir()} - listing
    assert new <= {"chronos-b1.db-shm", "chronos-b1.db-wal"}, new


# --- U-d r2: a PARTIAL leak is a leak (reader delta P2-1, P3-1) ---------------------------------

#: A non-repetitive 64-hex pepper canary: every 8-character window of it is distinct, so a head,
#: a tail or a middle fragment is recognisable (PEPPER_CANARY repeats "c0ffee", whose windows
#: recur). Fixed, not random, so a coincidental match would be deterministic, not flaky. Derived
#: at import, never written as a 64-hex literal: the tracked-file secret scan flags such a literal.
WINDOW_PEPPER = hashlib.sha256(b"chronos-b1-ud-r2-window-canary").hexdigest()
_WINDOW = 8


def _windows(secret: str, width: int = _WINDOW) -> set[str]:
    return {secret[i : i + width] for i in range(len(secret) - width + 1)}


def _partial_leaks(haystack: str, secret: str) -> list[str]:
    """Every >= 8-character window of ``secret`` found in ``haystack`` (head, middle, tail)."""

    return sorted(window for window in _windows(secret) if window in haystack)


def _left_behind(host: dict[str, Path]) -> str:
    """Every file the run could have written (the env file itself excluded: it holds the secret)."""

    root = host["work"].parent
    texts = []
    for path in sorted(p for p in root.rglob("*") if p.is_file() and p != host["env"]):
        texts.append(path.read_bytes().decode("latin-1"))
    return "\n".join(texts)


def test_r2_the_window_search_finds_a_head_a_middle_and_a_tail_fragment() -> None:
    """Positive control: the search sees what pydantic's elision leaves (head...tail) and a cut."""

    elided = f"input_value='{WINDOW_PEPPER[:24]}...{WINDOW_PEPPER[-24:]}'"
    assert WINDOW_PEPPER not in elided and _partial_leaks(elided, WINDOW_PEPPER)
    assert _partial_leaks("x" + WINDOW_PEPPER[28:36] + "y", WINDOW_PEPPER) == [WINDOW_PEPPER[28:36]]
    assert len(_windows(WINDOW_PEPPER)) == len(WINDOW_PEPPER) - _WINDOW + 1  # all distinct


@pytest.mark.parametrize("json_out", [False, True])
@pytest.mark.parametrize("with_credential", [True, False], ids=["refusing", "clear"])
def test_r2_no_fragment_of_the_pepper_appears_on_any_surface(
    host: dict[str, Path], capsys: pytest.CaptureFixture[str], json_out: bool, with_credential: bool
) -> None:
    """Reader delta P2-1: the whole-string canary search cannot see a PARTIAL pepper leak (the
    reader's n5 printed the first 20 hex: 136 passed). Every 8-character window is searched on
    stdout, stderr, every log record, the report, JSON and text, and every file left behind."""

    _rewrite(host, "CHRONOS_CAPTURE_PEPPER", WINDOW_PEPPER)
    everything = _everything(host, capsys, json_out=json_out, with_credential=with_credential)
    assert ("B1-PREFLIGHT CLEAR" in everything) is (not with_credential)  # the path really ran
    assert _partial_leaks(everything + _left_behind(host), WINDOW_PEPPER) == []


@pytest.mark.parametrize("key", ["IB_PORT", "IB_CLIENT_ID"])
def test_r2_the_long_canary_in_an_invalid_field_leaks_no_fragment_either(
    host: dict[str, Path], capsys: pytest.CaptureFixture[str], key: str
) -> None:
    """The elision shape itself: a 64-hex value Settings refuses to parse. A regression that
    echoes pydantic's error would show only head...tail -- this search still sees it."""

    _rewrite(host, key, WINDOW_PEPPER)
    everything = _everything(host, capsys, json_out=False, with_credential=False)
    assert "Settings refused the file" in everything
    assert _partial_leaks(everything + _left_behind(host), WINDOW_PEPPER) == []


def test_r2_p3_1_no_fragment_of_the_other_rows_fingerprint_appears(
    host: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """Reader delta P3-1 (mutant n4): the other row's stored account FINGERPRINT is a hash of a
    brute-forceable DU id, nearly the id itself; no window of it may appear."""

    from chronos.utils.identifiers import account_fingerprint

    _bind(host["database"], broker_mode="ibkr", account="DU1111111")
    everything = _everything(host, capsys, json_out=True, with_credential=False)
    everything += _everything(host, capsys, json_out=False, with_credential=False)
    assert '"scope"' in everything or "[scope]" in everything  # the scope check really ran
    stored = account_fingerprint("DU1111111")
    assert _partial_leaks(everything, stored) == []
    assert "DU1111111" not in everything
