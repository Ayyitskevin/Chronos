"""KEYSRC: the persistence key must come from exactly one source (assessment (b) + (d) naming).

The loader (pydantic-settings) reads ``CHRONOS_PERSISTENCE_KEY`` case-insensitively from the
process environment, then from the dotenv file ``Settings.model_config["env_file"]`` names: the
environment wins and, inside the file, the last line wins. A second source is how a store gets
bound under a key nobody escrowed (out/KEYSRC-ASSESS.md §2), so the two composition roots
(``build_runtime`` and the mandate CLI) count the names and lines first and refuse more than one,
naming each source and its count, never the value, a digest, a length or a prefix. B1 stays
file-only: it counts the file's lines and its P12 folds case. No file named ``.env`` is created
here: every dotenv is ``dotenv.synthetic``.
"""

from __future__ import annotations

import io
import logging
import os
import re
from hashlib import sha256
from pathlib import Path

import pytest
from tests.support.persistence_keys import derived_key_hex

from chronos import runtime as runtime_module
from chronos.cli import b1_preflight
from chronos.cli.main import main
from chronos.config import persistence_key as pk
from chronos.config.persistence_key import PersistenceKeyError
from chronos.config.settings import Settings, get_settings
from chronos.persistence.database import Database
from chronos.utils.identifiers import PersistenceKey

_NAME = "CHRONOS_PERSISTENCE_KEY"
_KELVIN = (
    "CHRONOS_PERSISTENCE_\N{KELVIN SIGN}EY"  # KELVIN SIGN: the loader's str.lower() folds it to "k"
)
_ACCOUNT = "DU7654321"


def _key(label: str) -> str:
    """A synthetic key, derived at run time (never a literal, never a real key)."""

    return derived_key_hex(f"keysrc:{label}")


def _leaks(text: str, *keys: str, path: Path | None = None) -> list[str]:
    """What key material ``text`` carries, by kind only (an assertion never prints a value)."""

    scrubbed = text.replace(str(path.absolute()), "<path>") if path is not None else text
    found: list[str] = []
    for key in keys:
        if key in scrubbed:
            found.append("value")
        if key[:8] in scrubbed:
            found.append("prefix")
        if sha256(key.encode()).hexdigest() in scrubbed:
            found.append("digest of the text")
        if sha256(bytes.fromhex(key)).hexdigest() in scrubbed:
            found.append("digest of the bytes")
    if re.search(r"\b64\b", scrubbed):
        found.append("length")
    return found


def _dotenv(tmp_path: Path, *lines: str) -> Path:
    path = tmp_path / "dotenv.synthetic"
    path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
    path.chmod(0o600)
    return path


def _refusal(environ: dict[str, str], files: list[Path]) -> str:
    with pytest.raises(PersistenceKeyError) as caught:
        pk.persistence_key_source(environ=environ, env_files=files)
    return str(caught.value)


# --- 1. the count: what the loader reads, one per name or line -----------------------------------


@pytest.mark.parametrize(
    ("label", "lines", "expected"),
    [
        ("exact", ["CHRONOS_PERSISTENCE_KEY=v"], 1),
        ("export", ["export CHRONOS_PERSISTENCE_KEY=v"], 1),
        ("lower case", ["chronos_persistence_key=v"], 1),
        ("quoted with spaces", ['  CHRONOS_PERSISTENCE_KEY = "v"  # note'], 1),
        ("empty value", ["CHRONOS_PERSISTENCE_KEY="], 1),
        ("bare name", ["CHRONOS_PERSISTENCE_KEY"], 1),
        ("kelvin sign", [f"{_KELVIN}=v"], 1),
        ("exact twice", ["CHRONOS_PERSISTENCE_KEY=v", "CHRONOS_PERSISTENCE_KEY=w"], 2),
        ("exact then lower", ["CHRONOS_PERSISTENCE_KEY=v", "chronos_persistence_key=w"], 2),
        ("exact then export", ["CHRONOS_PERSISTENCE_KEY=v", "export CHRONOS_PERSISTENCE_KEY=w"], 2),
        ("comment", ["# CHRONOS_PERSISTENCE_KEY=v"], 0),
        ("name inside a value", ["OTHER=CHRONOS_PERSISTENCE_KEY"], 0),
        ("inside a quoted multi-line value", ['X="a', "CHRONOS_PERSISTENCE_KEY=v", '"'], 0),
        ("another key", ["CHRONOS_CAPTURE_PEPPER=v"], 0),
    ],
)
def test_the_count_is_the_lines_the_loader_parses(
    label: str, lines: list[str], expected: int
) -> None:
    assert pk.count_key_lines("".join(f"{line}\n" for line in lines)) == expected, label


def test_every_counted_line_form_is_one_the_loader_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The count is of the loader's own reading: each single-line form above loads as the key."""

    monkeypatch.delenv(_NAME)  # the conftest key would outrank the file
    key = _key("form")
    for line in (
        f"CHRONOS_PERSISTENCE_KEY={key}",
        f"export CHRONOS_PERSISTENCE_KEY={key}",
        f"chronos_persistence_key={key}",
        f"  CHRONOS_PERSISTENCE_KEY = '{key}'  # note",
        f"{_KELVIN}={key}",
    ):
        path = _dotenv(tmp_path, line)
        loaded = Settings(_env_file=path).chronos_persistence_key  # type: ignore[call-arg]
        matches = loaded is not None and loaded.get_secret_value() == key
        assert matches, "a counted form did not load as the key"
        assert pk.count_key_lines(line + "\n") == 1


# --- 2. the source check: more than one refuses, naming each source -------------------------------


def test_environment_and_file_with_different_values_refuse(tmp_path: Path) -> None:
    path = _dotenv(tmp_path, f"CHRONOS_PERSISTENCE_KEY={_key('file')}")
    message = _refusal({_NAME: _key("env")}, [path])

    assert "named 2 times" in message
    assert "the process environment (1 name)" in message
    assert f"the dotenv file {path.absolute()} (1 line)" in message
    assert _leaks(message, _key("env"), _key("file"), path=path) == []


def test_environment_and_file_with_the_same_value_refuse(tmp_path: Path) -> None:
    """(D) in the assessment: two copies agree today and drift apart on the next edit."""

    same = _key("same")
    path = _dotenv(tmp_path, f"CHRONOS_PERSISTENCE_KEY={same}")
    message = _refusal({_NAME: same}, [path])

    assert "named 2 times" in message
    assert _leaks(message, same, path=path) == []


@pytest.mark.parametrize(
    "lines",
    [
        ["CHRONOS_PERSISTENCE_KEY={a}", "chronos_persistence_key={b}"],
        ["CHRONOS_PERSISTENCE_KEY={a}", "export CHRONOS_PERSISTENCE_KEY={b}"],
        ["CHRONOS_PERSISTENCE_KEY={a}", "CHRONOS_PERSISTENCE_KEY={b}"],
        ["CHRONOS_PERSISTENCE_KEY=", "CHRONOS_PERSISTENCE_KEY={b}"],
    ],
    ids=["exact+lower", "exact+export", "exact twice", "empty template line + key"],
)
def test_two_lines_in_one_file_refuse(tmp_path: Path, lines: list[str]) -> None:
    a, b = _key("a"), _key("b")
    path = _dotenv(tmp_path, *(line.format(a=a, b=b) for line in lines))
    message = _refusal({}, [path])

    assert "named 2 times" in message
    assert f"the dotenv file {path.absolute()} (2 lines)" in message
    assert "process environment" not in message
    assert _leaks(message, a, b, path=path) == []


@pytest.mark.parametrize("name", ["chronos_persistence_key", _KELVIN], ids=["lower", "kelvin"])
def test_an_environment_name_in_any_case_the_loader_folds_counts(tmp_path: Path, name: str) -> None:
    """Scenario (d): a lower-case export outranks the file: a source like the exact name."""

    path = _dotenv(tmp_path, f"CHRONOS_PERSISTENCE_KEY={_key('file')}")
    message = _refusal({name: _key("env")}, [path])

    assert "the process environment (1 name)" in message
    assert _leaks(message, _key("env"), _key("file"), path=path) == []


def test_two_environment_names_refuse_on_their_own() -> None:
    message = _refusal({_NAME: _key("x"), "chronos_persistence_key": _key("y")}, [])

    assert "named 2 times" in message
    assert "the process environment (2 names)" in message


def test_an_empty_environment_value_is_a_source(tmp_path: Path) -> None:
    """Scenario (e): an empty environment value shadows the file (the resolver then refuses it)."""

    path = _dotenv(tmp_path, f"CHRONOS_PERSISTENCE_KEY={_key('file')}")
    message = _refusal({_NAME: ""}, [path])

    assert "named 2 times" in message
    assert _leaks(message, _key("file"), path=path) == []


def test_exactly_one_source_is_named_and_not_refused(tmp_path: Path) -> None:
    """The controls: the conftest case (environment only, no dotenv), the host's (file only)."""

    absent = tmp_path / "no-such-dotenv.synthetic"
    assert pk.persistence_key_source(environ={_NAME: _key("env")}, env_files=[absent]) == (
        "the process environment"
    )
    path = _dotenv(tmp_path, "OTHER=1", f"CHRONOS_PERSISTENCE_KEY={_key('file')}", "# a comment")
    assert pk.persistence_key_source(environ={"PATH": "/usr/bin"}, env_files=[path]) == (
        f"the dotenv file {path.absolute()}"
    )
    assert pk.persistence_key_source(environ={}, env_files=[]) is None


def test_a_directory_named_as_the_dotenv_is_not_a_source(tmp_path: Path) -> None:
    """The loader reads only a regular file (or a FIFO, below); neither does the count."""

    folder = tmp_path / "dotenv.synthetic"
    folder.mkdir()
    assert pk.persistence_key_source(environ={_NAME: _key("env")}, env_files=[folder]) == (
        "the process environment"
    )


def test_a_fifo_dotenv_refuses_because_it_cannot_be_counted(tmp_path: Path) -> None:
    fifo = tmp_path / "dotenv.fifo"
    os.mkfifo(fifo)  # never opened: opening a FIFO with no writer would block
    message = _refusal({_NAME: _key("env")}, [fifo])

    assert str(fifo.absolute()) in message
    assert "FIFO" in message


def test_an_unreadable_dotenv_refuses_naming_the_path_only(tmp_path: Path) -> None:
    path = _dotenv(tmp_path, f"CHRONOS_PERSISTENCE_KEY={_key('file')}")
    path.chmod(0o000)
    try:
        if os.access(path, os.R_OK):  # pragma: no cover - a privileged runner can read it anyway
            pytest.skip("this runner can read a 0000 file")
        message = _refusal({}, [path])
    finally:
        path.chmod(0o600)

    assert str(path.absolute()) in message
    assert "PermissionError" in message
    assert _leaks(message, _key("file"), path=path) == []


def test_the_default_files_are_the_loaders_at_call_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``env_files=None`` counts what ``Settings()`` reads: ``model_config["env_file"]``, cwd."""

    _dotenv(tmp_path, f"CHRONOS_PERSISTENCE_KEY={_key('file')}")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setitem(Settings.model_config, "env_file", "dotenv.synthetic")
    monkeypatch.setenv(_NAME, _key("env"))

    with pytest.raises(PersistenceKeyError, match="named 2 times") as caught:
        pk.persistence_key_source()
    assert str(tmp_path / "dotenv.synthetic") in str(caught.value)

    monkeypatch.delenv(_NAME)
    assert pk.persistence_key_source() == f"the dotenv file {tmp_path / 'dotenv.synthetic'}"


# --- 3. the composition roots -------------------------------------------------------------------


class _Stop(Exception):
    """Raised by a stand-in to end build_runtime once the point under test has passed."""


def _point_settings_at(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    monkeypatch.setitem(Settings.model_config, "env_file", str(path))
    monkeypatch.setattr(runtime_module, "get_settings", lambda: Settings())


def test_build_runtime_refuses_two_sources_before_anything_is_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _dotenv(tmp_path, f"CHRONOS_PERSISTENCE_KEY={_key('file')}")
    _point_settings_at(monkeypatch, path)
    monkeypatch.setenv(_NAME, _key("env"))
    created: list[str] = []
    monkeypatch.setattr(runtime_module, "configure_logging", lambda *a, **k: created.append("log"))
    monkeypatch.setattr(runtime_module, "Database", lambda *a, **k: created.append("database"))

    with pytest.raises(PersistenceKeyError, match="named 2 times") as caught:
        runtime_module.build_runtime(register_atexit=False)

    assert created == []
    assert f"the dotenv file {path.absolute()} (1 line)" in str(caught.value)
    assert _leaks(str(caught.value), _key("env"), _key("file"), path=path) == []


def test_build_runtime_reports_the_one_source_it_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _dotenv(tmp_path, "OTHER=1")  # names no key: the environment is the one source
    _point_settings_at(monkeypatch, path)
    monkeypatch.setattr(runtime_module, "configure_logging", lambda *a, **k: None)

    def stop(*args: object, **kwargs: object) -> None:
        raise _Stop

    monkeypatch.setattr(runtime_module, "Database", stop)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logger = logging.getLogger("chronos.runtime")
    previous = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        with pytest.raises(_Stop):
            runtime_module.build_runtime(register_atexit=False)
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)

    logged = stream.getvalue()
    assert "persistence key from: the process environment" in logged
    assert _leaks(logged, os.environ[_NAME]) == []


@pytest.fixture
def _fresh_settings() -> object:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.usefixtures("_fresh_settings")
def test_mandate_fingerprint_refuses_two_sources_on_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _dotenv(tmp_path, f"CHRONOS_PERSISTENCE_KEY={_key('file')}")
    monkeypatch.setitem(Settings.model_config, "env_file", str(path))
    monkeypatch.setenv(_NAME, _key("env"))

    assert main(["mandate", "fingerprint", "--account-id", _ACCOUNT]) == 1

    captured = capsys.readouterr()
    assert captured.out.startswith("no persistence key available")
    assert "persistence key refused:" in captured.err
    assert "the process environment (1 name)" in captured.err
    assert f"the dotenv file {path.absolute()} (1 line)" in captured.err
    for text in (captured.out, captured.err):
        assert _leaks(text, _key("env"), _key("file"), path=path) == []


@pytest.mark.usefixtures("_fresh_settings")
def test_mandate_fingerprint_names_its_one_source_on_stderr_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from chronos.utils.identifiers import account_fingerprint

    key = _key("file-only")
    path = _dotenv(tmp_path, f"CHRONOS_PERSISTENCE_KEY={key}")
    monkeypatch.setitem(Settings.model_config, "env_file", str(path))
    monkeypatch.delenv(_NAME)

    assert main(["mandate", "fingerprint", "--account-id", _ACCOUNT]) == 0

    captured = capsys.readouterr()
    expected = account_fingerprint(_ACCOUNT, key=PersistenceKey.from_hex(key))
    assert captured.out.splitlines()[0] == expected  # stdout is what it was: the fingerprint first
    assert "persistence key" not in captured.out
    assert captured.err.strip() == f"persistence key from: the dotenv file {path.absolute()}"
    assert _leaks(captured.err, key, path=path) == []


# --- 4. B1: file-only, the in-file count and a case-folding P12 ----------------------------------


def _b1(tmp_path: Path, *lines: str) -> b1_preflight.B1Report:
    path = _dotenv(tmp_path, *lines)
    proc_net = tmp_path / "proc-net"
    proc_net.mkdir()
    return b1_preflight.run_b1_preflight(
        env_file=path, environ={}, halt_file=tmp_path / "halt.json", proc_net=proc_net
    )


def test_b1_refuses_a_file_naming_the_key_on_two_lines(tmp_path: Path) -> None:
    a, b = _key("b1-a"), _key("b1-b")
    report = b1_preflight.B1Report()
    b1_preflight._env_lines(
        report, f"CHRONOS_PERSISTENCE_KEY={a}\nexport chronos_persistence_key={b}\n", {}
    )
    refusals = [f for f in report.findings if f.check == "key-source"]

    assert [f.level for f in refusals] == ["REFUSE"]
    assert "named on 2 lines of the file" in refusals[0].text
    assert _leaks(refusals[0].text, a, b) == []
    assert sum(a in f.text or b in f.text for f in report.findings) == 0  # counts only: no text

    full = _b1(tmp_path, f"CHRONOS_PERSISTENCE_KEY={a}", f"export chronos_persistence_key={b}")
    assert "key-source" in full.verdict()[0]


def test_b1_one_line_naming_the_key_adds_no_key_source_line(tmp_path: Path) -> None:
    report = _b1(tmp_path, f"CHRONOS_PERSISTENCE_KEY={_key('b1-one')}")

    assert [f for f in report.findings if f.check == "key-source"] == []


@pytest.mark.parametrize(
    "name",
    ["chronos_persistence_key", "broker_mode", "BRO\N{KELVIN SIGN}ER_MODE"],
    ids=["key", "mode", "kelvin"],
)
def test_b1_p12_flags_an_exported_name_in_any_case_the_loader_folds(name: str) -> None:
    """Scenario (d): ``case_sensitive=False``, so a lower-case export outranks the file too."""

    found = b1_preflight.preflight_b1_env("", environ={name: "x", "PATH": "/usr/bin"})

    assert ("P12", f"exported variables outrank the file: {name}") in found


# --- 5. the two messages: no new DATABASE_URL as the remedy -------------------------------------


def test_the_bind_refusal_names_a_different_key_and_prescribes_no_new_database_url(
    tmp_path: Path,
) -> None:
    store = tmp_path / "store" / "chronos.db"
    store.parent.mkdir(mode=0o700)
    first = Database(f"sqlite:///{store}")
    first.initialize()
    first.bind_scope(
        broker_mode="ibkr",
        environment="paper",
        account_id=_ACCOUNT,
        key=PersistenceKey.from_hex(_key("bound")),
    )
    first.dispose()
    again = Database(f"sqlite:///{store}")
    again.initialize()
    try:
        with pytest.raises(RuntimeError, match="bound to a different broker scope") as caught:
            again.bind_scope(
                broker_mode="ibkr",
                environment="paper",
                account_id=_ACCOUNT,
                key=PersistenceKey.from_hex(_key("other")),
            )
    finally:
        again.dispose()

    message = str(caught.value)
    assert "or a different persistence key" in message
    assert "configure a separate DATABASE_URL" not in message
    assert _leaks(message, _key("bound"), _key("other")) == []


def test_b1_scope_refusal_names_a_different_key_and_prescribes_no_new_database_url(
    tmp_path: Path,
) -> None:
    store = tmp_path / "b1" / "chronos-b1.db"
    store.parent.mkdir(mode=0o700)
    bound = Database(f"sqlite:///{store}")
    bound.initialize()
    bound.bind_scope(
        broker_mode="ibkr",
        environment="paper",
        account_id=_ACCOUNT,
        key=PersistenceKey.from_hex(_key("bound")),
    )
    bound.dispose()
    settings = b1_preflight._load_settings(
        {
            "IB_ACCOUNT_ID": _ACCOUNT,
            "DATABASE_URL": f"sqlite:///{store}",
            "CHRONOS_PERSISTENCE_KEY": _key("other"),
        }
    )
    assert not isinstance(settings, str)
    report = b1_preflight.B1Report()
    b1_preflight._scope_lines(report, store, settings)
    (finding,) = [f for f in report.findings if f.check == "scope"]

    assert finding.level == "REFUSE"
    assert "bound to a different broker scope or a different persistence key" in finding.text
    assert "DATABASE_URL" not in finding.text.split("(")[0]  # no remedy that starts a new store
    assert "choose a new absolute DATABASE_URL" not in finding.text
    assert "runbook step 6" not in finding.text
    assert "would refuse to start" not in finding.text
    assert _leaks(finding.text, _key("bound"), _key("other")) == []
