"""Committed-parent rotation, provenance, and fail-closed input boundaries."""

from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from detect_secrets.settings import transient_settings
from scripts import rotate_baseline_fingerprints as rotation
from scripts.verify_release_security import SecurityGateError, verify_git_history_secrets

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/rotate_baseline_fingerprints.py"
MATRIX = "docs/generated/capability-matrix.json"
REASON = "SHA-256 source-inventory identity generated from repository bytes"


def _dump(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=2) + "\n"


def _digest(value: str) -> str:
    return hashlib.sha1(
        value.encode(), usedforsecurity=False
    ).hexdigest()  # pragma: allowlist secret


@dataclass
class Repo:
    root: Path

    def git(self, *args: str) -> str:
        return (
            subprocess.check_output(["git", "-C", str(self.root), *args], stderr=subprocess.PIPE)
            .decode()
            .strip()
        )

    def commit(self, message: str) -> str:
        self.git("add", ".")
        self.git("commit", "-qm", message)
        return self.git("rev-parse", "HEAD")

    def matrix(self, label: str) -> str:
        (self.root / "source.txt").write_text(label)
        value = hashlib.sha256(label.encode()).hexdigest()  # pragma: allowlist secret
        (self.root / MATRIX).write_text(
            _dump({"source_fingerprints": [{"path": "source.txt", "sha256": value}]})
        )
        return value

    def payload(self) -> dict[str, Any]:
        return json.loads((self.root / ".secrets.baseline").read_text())  # type: ignore[no-any-return]

    def baseline(self, value: str, history: list[dict[str, Any]] | None = None) -> None:
        payload = self.payload()
        payload["results"] = {
            MATRIX: [
                {
                    "type": "Hex High Entropy String",
                    "filename": MATRIX,
                    "hashed_secret": _digest(value),
                    "is_verified": False,
                    "line_number": 5,
                    "is_secret": False,
                }
            ]
        }
        payload["history_results"] = {MATRIX: history} if history else {}
        (self.root / ".secrets.baseline").write_text(_dump(payload))

    def run(
        self, *parents: str, check: bool = False, reviewed: str | None = "reviewed"
    ) -> subprocess.CompletedProcess[str]:
        args = [sys.executable, str(SCRIPT)]
        if reviewed is not None:
            args.extend(["--reviewed", reviewed])
        for parent in parents:
            args.extend(["--parent", parent])
        if check:
            args.append("--check")
        return subprocess.run(args, cwd=self.root, capture_output=True, text=True, timeout=30)


def _history(value: str, commit: str) -> dict[str, Any]:
    return {
        "type": "Hex High Entropy String",
        "hashed_secret": _digest(value),
        "is_secret": False,
        "observed_commit": commit,
        "reason": REASON,
    }


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    root = tmp_path / "repository"
    root.mkdir()
    r = Repo(root)
    r.git("init", "-qb", "main")
    r.git("config", "user.name", "Rotation test")
    r.git("config", "user.email", "rotation@example.invalid")
    (root / "docs/generated").mkdir(parents=True)
    (root / "scripts").mkdir()
    (root / "scripts/build_current_state.py").write_text(
        "import hashlib,json\nfrom pathlib import Path\n"
        "m=json.loads(Path('docs/generated/capability-matrix.json').read_text())\n"
        "expected=hashlib.sha256(Path('source.txt').read_bytes()).hexdigest()\n"
        "assert m['source_fingerprints'][0]['sha256']==expected\n"
        "assert Path('state.txt').read_text()=='current'\n"
    )
    (root / "state.txt").write_text("current")
    payload = json.loads((ROOT / ".secrets.baseline").read_text())
    payload["results"] = {}
    payload["history_results"] = {}
    (root / ".secrets.baseline").write_text(_dump(payload))
    value = r.matrix("initial source")
    r.baseline(value)
    r.commit("initial")
    r.git("branch", "reviewed")
    return r


def test_one_parent_rotation_and_check_never_write_until_apply(repo: Repo) -> None:
    parent = repo.git("rev-parse", "HEAD")
    before = (repo.root / ".secrets.baseline").read_bytes()
    current = repo.matrix("changed source")
    result = repo.run(parent, check=True)
    assert result.returncode == 1 and "history count: 1" in result.stdout
    assert "#" in result.stdout and (repo.root / ".secrets.baseline").read_bytes() == before
    assert repo.run(parent).returncode == 0
    payload = repo.payload()
    assert payload["results"][MATRIX][0]["hashed_secret"] == _digest(current)
    assert payload["results"][MATRIX][0]["line_number"] == 5
    assert payload["history_results"][MATRIX][0]["observed_commit"] == parent
    assert repo.run(parent, check=True).returncode == 0


def test_two_parent_merge_unions_shared_history_once_and_retires_both_values(repo: Repo) -> None:
    first = repo.git("rev-parse", "HEAD")
    initial = json.loads(repo.git("show", first + ":" + MATRIX))["source_fingerprints"][0]["sha256"]
    common_value = repo.matrix("common change")
    common_history = [_history(initial, first)]
    repo.baseline(common_value, common_history)
    common = repo.commit("common")
    shared = [*common_history, _history(common_value, common)]
    repo.git("switch", "-qc", "side")
    side_value = repo.matrix("side change")
    repo.baseline(side_value, shared)
    side = repo.commit("side")
    repo.git("switch", "-q", "main")
    main_value = repo.matrix("main change")
    repo.baseline(main_value, shared)
    main = repo.commit("main")
    merged = repo.matrix("merged source")
    (repo.root / ".secrets.baseline").write_text("<<<<<<< left\n=======\n>>>>>>> right\n")
    result = repo.run(main, side)
    assert result.returncode == 0, result.stderr
    entries = repo.payload()["history_results"][MATRIX]
    assert len(entries) == len({entry["hashed_secret"] for entry in entries}) == 4
    assert {e["hashed_secret"] for e in entries} == {
        _digest(v) for v in [initial, common_value, main_value, side_value]
    }
    assert repo.payload()["results"][MATRIX][0]["hashed_secret"] == _digest(merged)


@pytest.mark.parametrize("plant", ["outside", "non-fingerprint", "low-entropy"])
def test_new_values_are_not_absorbed_and_never_printed(repo: Repo, plant: str) -> None:
    parent = repo.git("rev-parse", "HEAD")
    value = hashlib.sha256(b"planted unreviewed value").hexdigest()  # pragma: allowlist secret
    if plant == "low-entropy":
        value = "0" * 40  # pragma: allowlist secret
    repo.matrix("changed source")
    if plant == "outside":
        (repo.root / "outside.txt").write_text(f'value = "{value}"\n')
    else:
        path = repo.root / MATRIX
        payload = json.loads(path.read_text())
        payload["unrelated"] = value
        path.write_text(_dump(payload))
    before = (repo.root / ".secrets.baseline").read_bytes()
    result = repo.run(parent)
    assert result.returncode == 2 and "outside matrix fingerprints" in result.stderr
    assert value not in result.stdout + result.stderr
    assert (repo.root / ".secrets.baseline").read_bytes() == before


@pytest.mark.parametrize(
    "problem", ["unavailable commit", "unfindable value", "duplicate identity", "round-trip"]
)
def test_invalid_parent_provenance_or_writer_refuses_without_writing(
    repo: Repo, problem: str
) -> None:
    parent = repo.git("rev-parse", "HEAD")
    value = json.loads(repo.git("show", parent + ":" + MATRIX))["source_fingerprints"][0]["sha256"]
    entry = _history(value, parent)
    if problem == "unavailable commit":
        entry["observed_commit"] = "f" * 40  # pragma: allowlist secret
    elif problem == "unfindable value":
        entry["hashed_secret"] = "0" * 40  # pragma: allowlist secret
    payload = repo.payload()
    payload["history_results"] = {
        MATRIX: [entry, dict(entry)] if problem == "duplicate identity" else [entry]
    }
    (repo.root / ".secrets.baseline").write_text(
        json.dumps(payload) if problem == "round-trip" else _dump(payload)
    )
    bad_parent = repo.commit("bad parent")
    before = (repo.root / ".secrets.baseline").read_bytes()
    result = repo.run(bad_parent)
    assert result.returncode == 2 and problem in result.stderr
    assert (repo.root / ".secrets.baseline").read_bytes() == before


def test_matrix_conflict_markers_refuse_without_touching_conflicted_baseline(repo: Repo) -> None:
    parent = repo.git("rev-parse", "HEAD")
    (repo.root / ".secrets.baseline").write_text("<<<<<<< baseline conflict\n")
    before = (repo.root / ".secrets.baseline").read_bytes()
    (repo.root / MATRIX).write_text("<<<<<<< matrix conflict\n")
    result = repo.run(parent)
    assert result.returncode == 2 and "matrix conflict markers" in result.stderr
    assert (repo.root / ".secrets.baseline").read_bytes() == before


def test_carried_misbound_history_stays_exact_and_prints_one_note(repo: Repo) -> None:
    initial = repo.git("rev-parse", "HEAD")
    initial_value = json.loads(repo.git("show", initial + ":" + MATRIX))["source_fingerprints"][0][
        "sha256"
    ]
    current = repo.matrix("changed source")
    repo.baseline(current, [_history(initial_value, initial)])
    wrong_observation = repo.commit("current matrix lacks initial value")
    repo.baseline(current, [_history(initial_value, wrong_observation)])
    parent = repo.commit("misbound history")
    carried = repo.payload()["history_results"][MATRIX][0]
    result = repo.run(parent, reviewed=parent)
    assert result.returncode == 0, result.stderr
    assert _dump(repo.payload()["history_results"][MATRIX][0]) == _dump(carried)
    assert result.stdout.count("NOTE") == 1
    assert _digest(initial_value)[:8] in result.stdout and wrong_observation[:8] in result.stdout


def test_generator_must_accept_the_working_artifacts(repo: Repo) -> None:
    parent = repo.git("rev-parse", "HEAD")
    (repo.root / "state.txt").write_text("stale")
    before = (repo.root / ".secrets.baseline").read_bytes()
    result = repo.run(parent)
    assert result.returncode == 2 and "regeneration check" in result.stderr
    assert (repo.root / ".secrets.baseline").read_bytes() == before


def _gate_outcome(repo: Repo, value: str) -> tuple[bool, bool]:
    """Return pass/found-planted-identity without exposing scanner diagnostics."""
    baseline = repo.root / ".secrets.baseline"
    with (
        transient_settings(repo.payload()),
        redirect_stdout(io.StringIO()),
        redirect_stderr(io.StringIO()),
    ):
        try:
            verify_git_history_secrets(repo.root, baseline)
        except SecurityGateError as error:
            return False, _digest(value) in str(error)
    return True, False


@pytest.mark.parametrize("attack", ["forged-parent-matrix", "new-non-matrix-history"])
def test_committed_parent_attacks_refuse_then_history_gate_finds_plant(
    repo: Repo, attack: str
) -> None:
    initial = repo.git("rev-parse", "HEAD")
    initial_value = json.loads(repo.git("show", initial + ":" + MATRIX))["source_fingerprints"][0][
        "sha256"
    ]
    reviewed_baseline = (repo.root / ".secrets.baseline").read_bytes()
    planted = hashlib.sha256(
        b"unreviewed committed candidate"
    ).hexdigest()  # pragma: allowlist secret
    if attack == "forged-parent-matrix":
        repo.matrix("source bytes unrelated to the planted value")
        path = repo.root / MATRIX
        matrix = json.loads(path.read_text())
        matrix["source_fingerprints"][0]["sha256"] = planted
        path.write_text(_dump(matrix))
        repo.baseline(planted, [_history(initial_value, initial)])
        introduced = repo.commit("untrusted matrix value")
        repo.baseline(planted, [_history(initial_value, initial), _history(planted, introduced)])
        expected = "committed source"
    else:
        retired = repo.root / "retired.txt"
        retired.write_text(f'credential = "{planted}"\n')
        introduced = repo.commit("unreviewed non-matrix value")
        retired.unlink()
        current = repo.matrix("parent source")
        repo.baseline(current, [_history(initial_value, initial)])
        payload = repo.payload()
        payload["history_results"]["retired.txt"] = [_history(planted, introduced)]
        (repo.root / ".secrets.baseline").write_text(_dump(payload))
        expected = "new non-matrix identity"
    parent = repo.commit("untrusted parent review")
    repo.matrix("valid current source")
    (repo.root / ".secrets.baseline").write_bytes(reviewed_baseline)
    result = repo.run(parent)
    unchanged = (repo.root / ".secrets.baseline").read_bytes() == reviewed_baseline
    repo.commit("rotation outcome")
    gate_passed, gate_found_plant = _gate_outcome(repo, planted)
    (repo.root.parent / "rotation-history-outcome.json").write_text(
        json.dumps(
            {
                "rotation_exit": result.returncode,
                "unchanged": unchanged,
                "history_gate_passed": gate_passed,
                "history_gate_found_plant": gate_found_plant,
            }
        )
    )
    assert result.returncode == 2 and expected in result.stderr
    assert unchanged and planted not in result.stdout + result.stderr
    assert not gate_passed and gate_found_plant


def test_legitimate_new_parent_matrix_history_passes(repo: Repo) -> None:
    initial = repo.git("rev-parse", "HEAD")
    initial_value = json.loads(repo.git("show", initial + ":" + MATRIX))["source_fingerprints"][0][
        "sha256"
    ]
    value = repo.matrix("legitimate parent source")
    repo.baseline(value, [_history(initial_value, initial)])
    observed = repo.commit("legitimate new matrix")
    current = repo.matrix("next parent source")
    repo.baseline(current, [_history(initial_value, initial), _history(value, observed)])
    parent = repo.commit("legitimate new history")
    repo.matrix("uncommitted current source")
    result = repo.run(parent)
    assert result.returncode == 0, result.stderr
    history = repo.payload()["history_results"][MATRIX]
    assert next(row for row in history if row["hashed_secret"] == _digest(value)) == _history(
        value, observed
    )


def test_reviewed_non_matrix_history_carries_unchanged_on_merge(repo: Repo) -> None:
    initial = repo.git("rev-parse", "HEAD")
    initial_value = json.loads(repo.git("show", initial + ":" + MATRIX))["source_fingerprints"][0][
        "sha256"
    ]
    value = hashlib.sha256(b"reviewed retired fixture").hexdigest()  # pragma: allowlist secret
    retired = repo.root / "retired.txt"
    retired.write_text(value)
    observed = repo.commit("old reviewed fixture")
    retired.unlink()
    carried = _history(value, observed)
    payload = repo.payload()
    payload["history_results"]["retired.txt"] = [carried]
    (repo.root / ".secrets.baseline").write_text(_dump(payload))
    reviewed = repo.commit("reviewed non-matrix history")
    parents = []
    for branch in ["left", "right"]:
        repo.git("switch", "-qc", branch, reviewed)
        current = repo.matrix(branch + " source")
        repo.baseline(current, [_history(initial_value, initial)])
        payload = repo.payload()
        payload["history_results"]["retired.txt"] = [dict(carried)]
        if branch == "left":
            payload["history_results"]["retired.txt"][0]["reason"] = "untrusted metadata rewrite"
        (repo.root / ".secrets.baseline").write_text(_dump(payload))
        parents.append(repo.commit(branch))
    repo.matrix("merged source")
    result = repo.run(*parents, reviewed=reviewed)
    assert result.returncode == 0, result.stderr
    assert _dump(repo.payload()["history_results"]["retired.txt"][0]) == _dump(carried)


@pytest.mark.parametrize("failure", ["write", "replace"])
def test_atomic_failure_cleans_temporary_candidate(
    repo: Repo, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    before = (repo.root / ".secrets.baseline").read_bytes()
    candidate = repo.payload()
    candidate["generated_at"] = "test candidate"
    monkeypatch.chdir(repo.root)
    monkeypatch.setattr(rotation, "plan", lambda *args: _dump(candidate).encode())
    original = tempfile.NamedTemporaryFile

    def fail_replace(*args: Any) -> None:
        raise OSError("injected replace failure")

    def temporary(*args: Any, **kwargs: Any) -> Any:
        output = original(*args, **kwargs)

        def fail_write(data: bytes) -> int:
            output.file.write(data[:64])
            raise OSError("injected partial write failure")

        monkeypatch.setattr(output, "write", fail_write)
        return output

    if failure == "write":
        monkeypatch.setattr(tempfile, "NamedTemporaryFile", temporary)
    else:
        monkeypatch.setattr(os, "replace", fail_replace)
    assert rotation.main(["--reviewed", "reviewed", "--parent", repo.git("rev-parse", "HEAD")]) == 2
    assert (repo.root / ".secrets.baseline").read_bytes() == before
    assert not list(repo.root.glob(".baseline-rotate-*"))


def test_reviewed_reference_is_required(repo: Repo) -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--parent", repo.git("rev-parse", "HEAD")],
        cwd=repo.root,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 2 and "--reviewed" in result.stderr


def test_reviewed_result_identity_is_not_reproved_when_retired(repo: Repo) -> None:
    value = hashlib.sha256(b"explicitly reviewed fixture").hexdigest()  # pragma: allowlist secret
    path = repo.root / MATRIX
    matrix = json.loads(path.read_text())
    matrix["source_fingerprints"][0]["sha256"] = value
    path.write_text(_dump(matrix))
    repo.baseline(value)
    reviewed = repo.commit("explicitly reviewed matrix result")
    repo.matrix("valid new working source")
    result = repo.run(reviewed, reviewed=reviewed)
    assert result.returncode == 0, result.stderr
    assert repo.payload()["history_results"][MATRIX][0] == _history(value, reviewed)


def test_carried_current_result_is_not_silently_renumbered(repo: Repo) -> None:
    parent = repo.git("rev-parse", "HEAD")
    path = repo.root / MATRIX
    path.write_text(_dump({"note": "line shift", **json.loads(path.read_text())}))
    before = (repo.root / ".secrets.baseline").read_bytes()
    result = repo.run(parent)
    assert result.returncode == 2 and "carried result line changed" in result.stderr
    assert (repo.root / ".secrets.baseline").read_bytes() == before


def test_parent_cannot_change_reviewed_scanner_policy(repo: Repo) -> None:
    payload = repo.payload()
    payload["version"] = "unreviewed policy version"
    (repo.root / ".secrets.baseline").write_text(_dump(payload))
    parent = repo.commit("unreviewed scanner policy")
    before = (repo.root / ".secrets.baseline").read_bytes()
    result = repo.run(parent)
    assert result.returncode == 2 and "reviewed scanner policy differs" in result.stderr
    assert (repo.root / ".secrets.baseline").read_bytes() == before


def test_inventory_value_must_be_explained_by_source_bytes_even_with_bad_generator(
    repo: Repo,
) -> None:
    parent = repo.git("rev-parse", "HEAD")
    path = repo.root / MATRIX
    payload = json.loads(path.read_text())
    payload["source_fingerprints"][0]["sha256"] = hashlib.sha256(
        b"unexplained"
    ).hexdigest()  # pragma: allowlist secret
    path.write_text(_dump(payload))
    (repo.root / "scripts/build_current_state.py").write_text("raise SystemExit(0)\n")
    before = (repo.root / ".secrets.baseline").read_bytes()
    result = repo.run(parent)
    assert result.returncode == 2 and "not explained by source" in result.stderr
    assert (repo.root / ".secrets.baseline").read_bytes() == before
