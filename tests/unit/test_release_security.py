"""Fail-closed contract tests for the release security gate."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest
from detect_secrets.pre_commit_hook import main as detect_secrets_hook
from scripts import run_research as research_runner
from scripts import verify_release_security as security_module
from scripts.verify_pip_bootstrap import locked_pip_version
from scripts.verify_release_security import (
    EXPECTED_TOOL_VERSIONS,
    ScanCommand,
    SecurityGateError,
    build_scan_commands,
    verify_release_security,
)

from chronos.backtest.engine import BacktestResult

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _git(repository: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ("git", "-C", str(repository), *args),
        check=check,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _commit(repository: Path, message: str) -> str:
    _git(repository, "add", "-A")
    _git(repository, "commit", "-m", message)
    return _git(repository, "rev-parse", "HEAD")


def _initialize_repository(repository: Path) -> None:
    repository.mkdir()
    _git(repository, "init", "-b", "main")
    _git(repository, "config", "user.name", "Chronos Test")
    _git(repository, "config", "user.email", "chronos-test@example.invalid")
    (repository / "README.md").write_text("clean\n", encoding="utf-8")
    _commit(repository, "initial")


def _write_test_baseline(
    path: Path,
    *,
    history_results: dict[str, list[dict[str, object]]] | None = None,
) -> None:
    payload = json.loads((_REPO_ROOT / ".secrets.baseline").read_text(encoding="utf-8"))
    payload["results"] = {}
    payload["history_results"] = history_results or {}
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def test_security_gate_pip_identity_matches_the_bootstrap_lock() -> None:
    assert EXPECTED_TOOL_VERSIONS["pip"] == locked_pip_version(
        _REPO_ROOT / "requirements-bootstrap.lock"
    )


class _EmptySeries:
    symbol = "SPY"

    def __len__(self) -> int:
        return 0


def test_reviewed_baseline_contains_only_explicit_false_positive_fingerprints() -> None:
    payload = json.loads((_REPO_ROOT / ".secrets.baseline").read_text(encoding="utf-8"))
    candidates = [item for items in payload["results"].values() for item in items]
    historical_candidates = [
        item for items in payload["history_results"].values() for item in items
    ]

    assert candidates
    # Grows by one per change that regenerates
    # docs/generated/capability-matrix.json: its source-inventory line takes a
    # new fingerprint, and the superseded one stays reachable in git history, so
    # it joins the reviewed set rather than being dropped. 11 at ADR-0053, 12 at
    # ADR-0056.
    assert len(historical_candidates) == 12
    assert {item["type"] for item in candidates} == {
        "Hex High Entropy String",
        "Secret Keyword",
    }
    assert all(item.get("is_secret") is False for item in candidates)
    assert {item["type"] for item in historical_candidates} == {"Hex High Entropy String"}
    assert all(item.get("is_secret") is False for item in historical_candidates)
    assert all(item.get("reason") for item in historical_candidates)
    assert "secret_value" not in json.dumps(payload)


def test_scan_commands_bind_exact_release_inputs_and_thresholds(tmp_path: Path) -> None:
    baseline_copy = tmp_path / "baseline-copy.json"
    commands = build_scan_commands(
        python=Path("/venv/bin/python"),
        baseline=baseline_copy,
        tracked_files=("src/chronos/app.py", "docs/a file.md"),
    )

    assert commands == (
        ScanCommand(
            name="runtime dependency audit",
            argv=(
                "/venv/bin/python",
                "-m",
                "pip_audit",
                "--require-hashes",
                "--disable-pip",
                "--progress-spinner",
                "off",
                "-r",
                "requirements-runtime.lock",
            ),
        ),
        ScanCommand(
            name="Python static analysis",
            argv=(
                "/venv/bin/python",
                "-m",
                "bandit",
                "-r",
                "src/chronos",
                "worker",
                "scripts",
                "--severity-level",
                "medium",
                "--confidence-level",
                "medium",
                "--quiet",
            ),
        ),
        ScanCommand(
            name="tracked-file secret scan",
            argv=(
                "/venv/bin/python",
                "-m",
                "detect_secrets.pre_commit_hook",
                "--baseline",
                str(baseline_copy),
                "--json",
                "--",
                "src/chronos/app.py",
                "docs/a file.md",
            ),
        ),
    )


def test_hosted_ci_injects_its_python_into_the_make_target() -> None:
    workflow = (_REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert "fetch-depth: 0" in workflow
    assert "run: make security-gate PY=python" in workflow


def test_gate_refuses_tool_version_drift_before_running_scans(tmp_path: Path) -> None:
    (tmp_path / ".secrets.baseline").write_text("{}\n", encoding="utf-8")
    called: list[ScanCommand] = []

    def drifted_version(distribution: str) -> str:
        expected = EXPECTED_TOOL_VERSIONS[distribution]
        return "0.0.0" if distribution == "bandit" else expected

    with pytest.raises(SecurityGateError, match="bandit version drift"):
        verify_release_security(
            root=tmp_path,
            python=Path("/venv/bin/python"),
            version_getter=drifted_version,
            tracked_files_loader=lambda _root: ("tracked.py",),
            command_runner=lambda command, _root: called.append(command),
            history_scanner=lambda _root, _baseline: None,
        )

    assert called == []


def test_gate_refuses_an_empty_tracked_file_set(tmp_path: Path) -> None:
    (tmp_path / ".secrets.baseline").write_text("{}\n", encoding="utf-8")

    with pytest.raises(SecurityGateError, match="no tracked files"):
        verify_release_security(
            root=tmp_path,
            python=Path("/venv/bin/python"),
            version_getter=lambda name: EXPECTED_TOOL_VERSIONS[name],
            tracked_files_loader=lambda _root: (),
            command_runner=lambda _command, _root: None,
            history_scanner=lambda _root, _baseline: None,
        )


def test_gate_excludes_only_the_fingerprint_baseline_from_its_own_scan(
    tmp_path: Path,
) -> None:
    (tmp_path / ".secrets.baseline").write_text("{}\n", encoding="utf-8")
    called: list[ScanCommand] = []

    verify_release_security(
        root=tmp_path,
        python=Path("/venv/bin/python"),
        version_getter=lambda name: EXPECTED_TOOL_VERSIONS[name],
        tracked_files_loader=lambda _root: (
            ".secrets.baseline",
            "src/chronos/app.py",
        ),
        command_runner=lambda command, _root: called.append(command),
        history_scanner=lambda _root, _baseline: None,
    )

    assert called[-1].name == "tracked-file secret scan"
    assert called[-1].argv[-2:] == ("--", "src/chronos/app.py")


def test_gate_runs_history_scan_against_the_private_baseline_copy(tmp_path: Path) -> None:
    baseline = tmp_path / ".secrets.baseline"
    baseline.write_text("{}\n", encoding="utf-8")
    history_calls: list[tuple[Path, Path, bytes]] = []

    verify_release_security(
        root=tmp_path,
        python=Path("/venv/bin/python"),
        version_getter=lambda name: EXPECTED_TOOL_VERSIONS[name],
        tracked_files_loader=lambda _root: ("tracked.py",),
        command_runner=lambda _command, _root: None,
        history_scanner=lambda root, copied_baseline: history_calls.append(
            (root, copied_baseline, copied_baseline.read_bytes())
        ),
    )

    assert len(history_calls) == 1
    root, copied_baseline, copied_bytes = history_calls[0]
    assert root == tmp_path
    assert copied_baseline != baseline
    assert copied_baseline.name == baseline.name
    assert copied_bytes == baseline.read_bytes()


def test_gate_refuses_a_stale_baseline_instead_of_mutating_the_checkout(
    tmp_path: Path,
) -> None:
    baseline = tmp_path / ".secrets.baseline"
    baseline.write_text('{"version": "1.5.0"}\n', encoding="utf-8")

    def mutate_copy(command: ScanCommand, _root: Path) -> None:
        if command.name == "tracked-file secret scan":
            copied_baseline = Path(command.argv[4])
            copied_baseline.write_text('{"version": "changed"}\n', encoding="utf-8")

    with pytest.raises(SecurityGateError, match="baseline is stale"):
        verify_release_security(
            root=tmp_path,
            python=Path("/venv/bin/python"),
            version_getter=lambda name: EXPECTED_TOOL_VERSIONS[name],
            tracked_files_loader=lambda _root: ("tracked.py",),
            command_runner=mutate_copy,
            history_scanner=lambda _root, _baseline: None,
        )

    assert baseline.read_text(encoding="utf-8") == '{"version": "1.5.0"}\n'


@pytest.mark.parametrize("failing_scan", range(3))
def test_every_scanner_failure_blocks_the_gate(tmp_path: Path, failing_scan: int) -> None:
    (tmp_path / ".secrets.baseline").write_text("{}\n", encoding="utf-8")
    calls = 0

    def fail_selected(command: ScanCommand, _root: Path) -> None:
        nonlocal calls
        current = calls
        calls += 1
        if current == failing_scan:
            raise subprocess.CalledProcessError(1, command.argv)

    with pytest.raises(subprocess.CalledProcessError):
        verify_release_security(
            root=tmp_path,
            python=Path("/venv/bin/python"),
            version_getter=lambda name: EXPECTED_TOOL_VERSIONS[name],
            tracked_files_loader=lambda _root: ("tracked.py",),
            command_runner=fail_selected,
            history_scanner=lambda _root, _baseline: None,
        )

    assert calls == failing_scan + 1


def test_secret_scanner_rejects_a_new_credential_shape(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    baseline = tmp_path / ".secrets.baseline"
    shutil.copyfile(_REPO_ROOT / ".secrets.baseline", baseline)
    candidate = tmp_path / "new_config.py"
    fake_access_key = "AKIA" + ("A" * 16)
    candidate.write_text(f"api_key = {fake_access_key!r}\n", encoding="utf-8")

    assert detect_secrets_hook(["--baseline", str(baseline), "--json", "--", str(candidate)]) == 1
    output = capsys.readouterr().out
    assert "AWS Access Key" in output
    assert fake_access_key not in output


def test_history_scanner_rejects_a_secret_added_then_deleted_without_echoing_it(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    _initialize_repository(repository)
    fake_access_key = "AKIA" + ("A" * 16)
    (repository / "config.py").write_text(f"value = {fake_access_key!r}\n", encoding="utf-8")
    _commit(repository, "add credential")
    (repository / "config.py").unlink()
    _commit(repository, "remove credential")
    baseline = repository / ".secrets.baseline"
    _write_test_baseline(baseline)

    with pytest.raises(SecurityGateError) as raised:
        security_module.verify_git_history_secrets(root=repository, baseline=baseline)

    message = str(raised.value)
    assert "AWS Access Key" in message
    assert "config.py" in message
    assert fake_access_key not in message


def test_history_scanner_rejects_a_nul_bearing_secret_added_then_deleted(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    _initialize_repository(repository)
    fake_access_key = "AKIA" + ("N" * 16)
    (repository / "nul_config.py").write_bytes(f"value = {fake_access_key!r}\n".encode() + b"\0")
    _commit(repository, "add NUL-bearing credential")
    (repository / "nul_config.py").unlink()
    _commit(repository, "remove NUL-bearing credential")
    baseline = repository / ".secrets.baseline"
    _write_test_baseline(baseline)

    with pytest.raises(SecurityGateError) as raised:
        security_module.verify_git_history_secrets(root=repository, baseline=baseline)

    message = str(raised.value)
    assert "AWS Access Key" in message
    assert "nul_config.py" in message
    assert fake_access_key not in message


def test_history_scanner_rejects_a_merge_resolution_only_secret(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    _initialize_repository(repository)
    _git(repository, "switch", "-c", "side")
    (repository / "settings.py").write_text("setting = 'side'\n", encoding="utf-8")
    _commit(repository, "side setting")
    _git(repository, "switch", "main")
    (repository / "settings.py").write_text("setting = 'main'\n", encoding="utf-8")
    _commit(repository, "main setting")
    _git(repository, "merge", "side", check=False)
    fake_access_key = "AKIA" + ("B" * 16)
    (repository / "settings.py").write_text(
        f"setting = 'resolved'\nvalue = {fake_access_key!r}\n",
        encoding="utf-8",
    )
    _commit(repository, "resolve with credential")
    baseline = repository / ".secrets.baseline"
    _write_test_baseline(baseline)
    object_directory = Path(
        _git(repository, "rev-parse", "--path-format=absolute", "--git-path", "objects")
    )
    objects_before = {
        path.relative_to(object_directory) for path in object_directory.rglob("*") if path.is_file()
    }

    with pytest.raises(SecurityGateError) as raised:
        security_module.verify_git_history_secrets(root=repository, baseline=baseline)

    message = str(raised.value)
    assert "AWS Access Key" in message
    assert "settings.py" in message
    assert fake_access_key not in message
    assert {
        path.relative_to(object_directory) for path in object_directory.rglob("*") if path.is_file()
    } == objects_before


def test_history_scanner_accepts_an_exact_reviewed_fingerprint_and_refuses_stale_review(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    _initialize_repository(repository)
    fake_access_key = "AKIA" + ("C" * 16)
    (repository / "retired.py").write_text(f"value = {fake_access_key!r}\n", encoding="utf-8")
    observed_commit = _commit(repository, "add retired credential shape")
    (repository / "retired.py").unlink()
    _commit(repository, "remove retired credential shape")
    baseline = repository / ".secrets.baseline"
    reviewed = {
        "retired.py": [
            {
                "type": "AWS Access Key",
                "hashed_secret": hashlib.sha1(fake_access_key.encode("utf-8")).hexdigest(),
                "is_secret": False,
                "observed_commit": observed_commit,
                "reason": "synthetic regression credential",
            }
        ]
    }
    _write_test_baseline(baseline, history_results=reviewed)

    security_module.verify_git_history_secrets(root=repository, baseline=baseline)

    reviewed["retired.py"][0]["observed_commit"] = "f" * 40
    _write_test_baseline(baseline, history_results=reviewed)
    with pytest.raises(SecurityGateError, match="not reachable"):
        security_module.verify_git_history_secrets(root=repository, baseline=baseline)

    reviewed["retired.py"][0]["observed_commit"] = observed_commit
    reviewed["retired.py"][0]["hashed_secret"] = "0" * 40
    _write_test_baseline(baseline, history_results=reviewed)
    with pytest.raises(SecurityGateError, match="stale historical secret review"):
        security_module.verify_git_history_secrets(root=repository, baseline=baseline)


@pytest.mark.parametrize(
    ("case", "message"),
    (
        ("plaintext", "never store candidate plaintext"),
        ("not-reviewed", "contains an unreviewed result"),
        ("missing-field", "entry schema is invalid"),
        ("duplicate", "duplicate identity"),
    ),
)
def test_history_scanner_refuses_invalid_review_records(
    tmp_path: Path,
    case: str,
    message: str,
) -> None:
    repository = tmp_path / "repository"
    _initialize_repository(repository)
    entry: dict[str, object] = {
        "type": "Hex High Entropy String",
        "hashed_secret": "0" * 40,
        "is_secret": False,
        "observed_commit": _git(repository, "rev-parse", "HEAD"),
        "reason": "synthetic invalid-record test",
    }
    entries = [entry]
    if case == "plaintext":
        forbidden_plaintext_key = "secret" + "_value"
        entry[forbidden_plaintext_key] = "not-a-real-" + "secret"
    elif case == "not-reviewed":
        entry["is_secret"] = True
    elif case == "missing-field":
        del entry["reason"]
    else:
        entries.append(dict(entry))
    baseline = repository / ".secrets.baseline"
    _write_test_baseline(baseline, history_results={"retired.py": entries})

    with pytest.raises(SecurityGateError, match=message):
        security_module.verify_git_history_secrets(root=repository, baseline=baseline)


def test_history_scanner_refuses_an_unparseable_patch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    _initialize_repository(repository)
    baseline = repository / ".secrets.baseline"
    _write_test_baseline(baseline)

    def fail_parse(_collection: object, _patch: str) -> None:
        raise security_module.UnidiffParseError("synthetic parse failure")

    monkeypatch.setattr(security_module.SecretsCollection, "scan_diff", fail_parse)

    with pytest.raises(SecurityGateError, match="could not be parsed safely"):
        security_module.verify_git_history_secrets(root=repository, baseline=baseline)


def test_history_scanner_refuses_a_non_repository(tmp_path: Path) -> None:
    baseline = tmp_path / ".secrets.baseline"
    _write_test_baseline(baseline)

    with pytest.raises(SecurityGateError, match="preflight"):
        security_module.verify_git_history_secrets(root=tmp_path, baseline=baseline)


def test_history_scanner_refuses_shallow_history(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _initialize_repository(source)
    (source / "second.txt").write_text("second\n", encoding="utf-8")
    _commit(source, "second")
    repository = tmp_path / "shallow"
    subprocess.run(
        ("git", "clone", "--depth", "1", source.as_uri(), str(repository)),
        check=True,
        capture_output=True,
        text=True,
    )
    baseline = repository / ".secrets.baseline"
    _write_test_baseline(baseline)

    with pytest.raises(SecurityGateError, match="shallow"):
        security_module.verify_git_history_secrets(root=repository, baseline=baseline)


def test_history_scanner_refuses_octopus_merges(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    _initialize_repository(repository)
    _git(repository, "switch", "-c", "side-a")
    (repository / "a.txt").write_text("a\n", encoding="utf-8")
    _commit(repository, "side a")
    _git(repository, "switch", "main")
    _git(repository, "switch", "-c", "side-b")
    (repository / "b.txt").write_text("b\n", encoding="utf-8")
    _commit(repository, "side b")
    _git(repository, "switch", "main")
    (repository / "main.txt").write_text("main\n", encoding="utf-8")
    _commit(repository, "main")
    _git(repository, "merge", "side-a", "side-b", "-m", "octopus")
    baseline = repository / ".secrets.baseline"
    _write_test_baseline(baseline)

    with pytest.raises(SecurityGateError, match="octopus"):
        security_module.verify_git_history_secrets(root=repository, baseline=baseline)


def test_history_scanner_refuses_an_oversized_patch_stream(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    _initialize_repository(repository)
    baseline = repository / ".secrets.baseline"
    _write_test_baseline(baseline)

    with pytest.raises(SecurityGateError, match="exceeded"):
        security_module.verify_git_history_secrets(
            root=repository,
            baseline=baseline,
            max_diff_bytes=1,
        )


def test_static_scanner_rejects_dynamic_evaluation(tmp_path: Path) -> None:
    candidate = tmp_path / "unsafe.py"
    candidate.write_text("result = eval('1')\n", encoding="utf-8")

    result = subprocess.run(
        (
            sys.executable,
            "-m",
            "bandit",
            str(candidate),
            "--severity-level",
            "medium",
            "--confidence-level",
            "medium",
            "--quiet",
        ),
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "B307" in result.stdout


def test_research_run_uses_private_halt_state_and_cleans_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    halt_paths: list[Path] = []

    def fake_run_backtest(**kwargs: object) -> BacktestResult:
        halt_path = kwargs["halt_store"]._path  # type: ignore[attr-defined]
        halt_paths.append(halt_path)
        assert halt_path.is_file()
        assert halt_path.parent.name.startswith("chronos-research-halt-")
        return BacktestResult(
            strategy_id="baseline_buy_hold",
            strategy_version="test",
            symbol="SPY",
            equity_curve_dates=(date(2026, 1, 2),),
            equity_curve=(3000.0,),
            trades=(),
            risk_rejections=0,
            skipped_conversions=0,
            data_quality_blocking=False,
            final_equity=3000.0,
        )

    monkeypatch.setattr(research_runner, "run_backtest", fake_run_backtest)

    result = research_runner.run_one(
        _EmptySeries(),  # type: ignore[arg-type]
        "baseline_buy_hold",
        research_runner.BASE,
        "test",
    )

    assert result["risk_rejections"] == 0
    assert len(halt_paths) == 1
    assert not halt_paths[0].parent.exists()


def test_the_success_line_names_the_check_this_gate_does_not_make(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """P2-09: a standalone green must not read as manifest/lock coherence.

    This gate never compares ``pyproject.toml`` to the locks; that comparison is
    the release-artifact gate's. A success line silent about it invites exactly
    the over-reading the two-gate split exists to prevent.
    """

    monkeypatch.setattr(security_module, "verify_release_security", lambda: None)
    assert security_module.main() == 0
    line = capsys.readouterr().out.strip()
    assert line.startswith("release security gate passed (")
    assert "manifest/lock coherence" in line
    assert "release-artifact gate" in line
