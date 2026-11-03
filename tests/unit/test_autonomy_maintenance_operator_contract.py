"""Execute the maintenance runbook's pre-start proof without a real unit or private env.

The final UnsetEnvironment property must prove ON before a backend can start;
the ordinary lift must name one discovered source and refuse conflicts.
"""

from __future__ import annotations

import configparser
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SWITCH = "CHRONOS_AUTONOMY_MAINTENANCE"
RUNBOOK = ROOT / "docs/ops/N3-CUTOVER.md"


def _block(language: str, marker: str) -> str:
    text = RUNBOOK.read_text(encoding="utf-8")
    found = re.findall(rf"```{language}\n(\# {marker}\n.*?)\n```", text, re.DOTALL)
    assert len(found) == 1, f"missing or ambiguous {marker}"
    return found[0]


def test_cutover_drop_in_removes_the_switch_at_the_final_merge_step() -> None:
    parser = configparser.ConfigParser(interpolation=None)
    parser.read_string(_block("ini", "MAINT-R1 cutover drop-in"))
    assert SWITCH in parser["Service"].get("UnsetEnvironment", "").split()


def _run_proof(
    tmp_path: Path,
    *,
    expect: str = "on",
    changed: dict[str, str] | None = None,
    contents: str = "CHRONOS_AUTONOMY_MAINTENANCE=off\n",
    failure: str = "",
) -> subprocess.CompletedProcess[str]:
    env_file = tmp_path / "chronos-backend.env"
    env_file.write_text(contents, encoding="utf-8")
    properties = {
        "LoadState": "loaded",
        "NeedDaemonReload": "no",
        "EnvironmentFiles": f"{env_file} (ignore_errors=no)",
        "UnsetEnvironment": SWITCH if expect == "on" else "",
        "WorkingDirectory": str(tmp_path / "checkout"),
        "ExecStart": _reviewed_exec_start(tmp_path),
    }
    properties.update(changed or {})
    fixture = tmp_path / "properties.json"
    fixture.write_text(json.dumps({"properties": properties, "failure": failure}))
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    fake = fakebin / "systemctl"
    fake.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        f"data = json.load(open({str(fixture)!r}))\n"
        "assert '--user' in sys.argv and 'show' in sys.argv\n"
        "assert 'chronos-backend.service' in sys.argv\n"
        "key = next(a.split('=', 1)[1] for a in sys.argv if a.startswith('--property='))\n"
        "print(data['properties'][key])\n"
        "sys.exit(1 if data['failure'] == key else 0)\n",
        encoding="utf-8",
    )
    fake.chmod(0o700)
    script = _block("bash", "MAINT-R1 pre-start proof")
    script = script.replace("<PRIVATE_ENV_DIR>", str(tmp_path))
    script = script.replace("<ABSOLUTE_CHRONOS_CHECKOUT>", str(tmp_path / "checkout"))
    script = script.replace("maintenance_expect=on", f"maintenance_expect={expect}")
    return subprocess.run(
        ["bash", "-c", script],
        env={**os.environ, "PATH": f"{fakebin}:{os.environ['PATH']}"},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def _reviewed_exec_start(tmp_path: Path) -> str:
    python = tmp_path / "checkout/.venv/bin/python"
    return (
        f"{{ path={python} ; argv[]={python} scripts/run_backend.py ; ignore_errors=no ; "
        "start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; code=(null) ; status=0/0 }"
    )


@pytest.mark.parametrize("expect", ["on", "off"])
@pytest.mark.parametrize(
    "case",
    [
        "empty",
        "env-prefix",
        "wrapper",
        "different-python",
        "extra-argv",
        "switch-argv",
        "ignored-errors",
        "multiple-commands",
        "metadata-spoof-in-argv",
        "missing-metadata",
        "different-working-directory",
    ],
)
def test_proof_requires_the_reviewed_command_and_working_directory(
    tmp_path: Path, expect: str, case: str
) -> None:
    reviewed = _reviewed_exec_start(tmp_path)
    python = tmp_path / "checkout/.venv/bin/python"
    variants = {
        "empty": "",
        "env-prefix": reviewed.replace(
            f"path={python} ; argv[]={python}",
            f"path=/usr/bin/env ; argv[]=/usr/bin/env {SWITCH}=off {python}",
        ),
        "wrapper": reviewed.replace(str(python), "/fixture/wrapper.sh"),
        "different-python": reviewed.replace(str(python), "/usr/bin/python3"),
        "extra-argv": reviewed.replace(
            "scripts/run_backend.py ;", "scripts/run_backend.py extra ;"
        ),
        "switch-argv": reviewed.replace(
            "scripts/run_backend.py ;", f"scripts/run_backend.py {SWITCH}=off ;"
        ),
        "ignored-errors": reviewed.replace("ignore_errors=no", "ignore_errors=yes"),
        "multiple-commands": reviewed + " " + reviewed,
        "metadata-spoof-in-argv": reviewed.replace(
            "scripts/run_backend.py ;", "scripts/run_backend.py ; ignore_errors=no ; start_time= ;"
        ),
        "missing-metadata": reviewed.split(" ; start_time=", 1)[0],
        "different-working-directory": reviewed,
    }
    changed = {"ExecStart": variants[case]}
    if case == "different-working-directory":
        changed["WorkingDirectory"] = "/another/checkout"
    result = _run_proof(tmp_path, expect=expect, changed=changed)
    assert result.returncode != 0, result.stdout


@pytest.mark.parametrize("failure", ["ExecStart", "WorkingDirectory"])
def test_execution_identity_discovery_errors_stop_the_proof(tmp_path: Path, failure: str) -> None:
    result = _run_proof(tmp_path, failure=failure)
    assert result.returncode != 0, result.stdout


def test_execution_identity_matches_the_reviewed_unit_template() -> None:
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(ROOT / "docs/ops/chronos-backend.service")
    assert parser["Service"]["WorkingDirectory"] == "%h/<CHRONOS_CHECKOUT>"
    assert parser["Service"]["ExecStart"] == (
        "%h/<CHRONOS_CHECKOUT>/.venv/bin/python scripts/run_backend.py"
    )


def test_reload_readback_claims_effective_state_not_command_history() -> None:
    text = " ".join(RUNBOOK.read_text().split())
    assert "every systemd environment source" in text
    assert "not that a `daemon-reload` command was executed" in text
    assert "Reload with `systemctl --user daemon-reload`" in text


@pytest.mark.parametrize("expect", ["on", "off"])
def test_verified_final_removal_or_explicit_source_allows_the_requested_proof(
    tmp_path: Path, expect: str
) -> None:
    result = _run_proof(tmp_path, expect=expect)
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"maintenance pre-start proof: {expect.upper()}" in result.stdout


@pytest.mark.parametrize(
    "changed",
    [
        {"LoadState": "not-found"},
        {"NeedDaemonReload": "yes"},
        {"EnvironmentFiles": ""},
        {"EnvironmentFiles": "/another/file (ignore_errors=no)"},
        {"EnvironmentFiles": "/another/file (ignore_errors=yes)"},
        {"UnsetEnvironment": ""},
        {"UnsetEnvironment": f"{SWITCH}=off"},
    ],
)
def test_on_proof_refuses_missing_reload_source_or_final_name_removal(
    tmp_path: Path, changed: dict[str, str]
) -> None:
    result = _run_proof(tmp_path, changed=changed)
    assert result.returncode != 0, result.stdout


@pytest.mark.parametrize(
    "failure", ["LoadState", "NeedDaemonReload", "EnvironmentFiles", "UnsetEnvironment"]
)
def test_systemctl_failure_is_not_a_success_even_with_expected_stdout(
    tmp_path: Path, failure: str
) -> None:
    result = _run_proof(tmp_path, failure=failure)
    assert result.returncode != 0, result.stdout


@pytest.mark.parametrize(
    "contents",
    [
        "",
        "CHRONOS_AUTONOMY_MAINTENANCE=on\n",
        "CHRONOS_AUTONOMY_MAINTENANCE=off\nCHRONOS_AUTONOMY_MAINTENANCE=off\n",
        "CHRONOS_AUTONOMY_MAINTENANCE=off\nexport CHRONOS_AUTONOMY_MAINTENANCE=on\n",
    ],
)
def test_lift_refuses_absent_or_conflicting_explicit_source(tmp_path: Path, contents: str) -> None:
    result = _run_proof(tmp_path, expect="off", contents=contents)
    assert result.returncode != 0, result.stdout


@pytest.mark.parametrize("removal", [SWITCH, f"{SWITCH}=off"])
def test_lift_refuses_a_remaining_final_removal(tmp_path: Path, removal: str) -> None:
    result = _run_proof(tmp_path, expect="off", changed={"UnsetEnvironment": removal})
    assert result.returncode != 0, result.stdout


@pytest.mark.parametrize(
    "path",
    [
        "README.md",
        "docs/DEPLOYMENT.md",
        "docs/OPERATIONS.md",
        "docs/SHADOW_CAMPAIGN.md",
        "docs/ops/README.md",
        "docs/ops/chronos-backend.service",
        ".claude/skills/chronos-run-and-operate/SKILL.md",
    ],
)
def test_normal_start_entry_points_disclose_default_on_and_the_explicit_lift(path: str) -> None:
    text = " ".join((ROOT / path).read_text(encoding="utf-8").split())
    assert "maintenance is on by default" in text.lower()
    assert "CHRONOS_AUTONOMY_MAINTENANCE=off" in text
