"""Fail-closed contract tests for the release security gate."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest
from detect_secrets.pre_commit_hook import main as detect_secrets_hook
from scripts import run_research as research_runner
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

    assert candidates
    assert {item["type"] for item in candidates} == {
        "Hex High Entropy String",
        "Secret Keyword",
    }
    assert all(item.get("is_secret") is False for item in candidates)
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
    )

    assert called[-1].name == "tracked-file secret scan"
    assert called[-1].argv[-2:] == ("--", "src/chronos/app.py")


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
