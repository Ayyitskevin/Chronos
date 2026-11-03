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
    script = script.replace("maintenance_expect=on", f"maintenance_expect={expect}")
    return subprocess.run(
        ["bash", "-c", script],
        env={**os.environ, "PATH": f"{fakebin}:{os.environ['PATH']}"},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


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
