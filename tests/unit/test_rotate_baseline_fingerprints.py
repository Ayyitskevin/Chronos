"""Committed-parent rotation, provenance, and fail-closed input boundaries."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/rotate_baseline_fingerprints.py"
MATRIX = "docs/generated/capability-matrix.json"
REASON = "SHA-256 source-inventory identity generated from repository bytes"


def _dump(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=2) + "\n"


def _digest(value: str) -> str:
    return hashlib.sha1(value.encode(), usedforsecurity=False).hexdigest()  # pragma: allowlist secret


@dataclass
class Repo:
    root: Path

    def git(self, *args: str) -> str:
        return subprocess.check_output(["git", "-C", str(self.root), *args], stderr=subprocess.PIPE).decode().strip()

    def commit(self, message: str) -> str:
        self.git("add", ".")
        self.git("commit", "-qm", message)
        return self.git("rev-parse", "HEAD")

    def matrix(self, label: str) -> str:
        (self.root / "source.txt").write_text(label)
        value = hashlib.sha256(label.encode()).hexdigest()  # pragma: allowlist secret
        (self.root / MATRIX).write_text(_dump({"source_fingerprints": [{"path": "source.txt", "sha256": value}]}))
        return value

    def payload(self) -> dict[str, Any]:
        return json.loads((self.root / ".secrets.baseline").read_text())  # type: ignore[no-any-return]

    def baseline(self, value: str, history: list[dict[str, Any]] | None = None) -> None:
        payload = self.payload()
        payload["results"] = {MATRIX: [{"type": "Hex High Entropy String", "filename": MATRIX,
            "hashed_secret": _digest(value), "is_verified": False, "line_number": 6, "is_secret": False}]}
        payload["history_results"] = {MATRIX: history} if history else {}
        (self.root / ".secrets.baseline").write_text(_dump(payload))

    def run(self, *parents: str, check: bool = False) -> subprocess.CompletedProcess[str]:
        args = [sys.executable, str(SCRIPT)]
        for parent in parents:
            args.extend(["--parent", parent])
        if check:
            args.append("--check")
        return subprocess.run(args, cwd=self.root, capture_output=True, text=True, timeout=30)


def _history(value: str, commit: str) -> dict[str, Any]:
    return {"type": "Hex High Entropy String", "hashed_secret": _digest(value), "is_secret": False,
            "observed_commit": commit, "reason": REASON}


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
        "assert m['source_fingerprints'][0]['sha256']==hashlib.sha256(Path('source.txt').read_bytes()).hexdigest()\n"
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
    assert payload["results"][MATRIX][0]["line_number"] == 6
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
    assert {e["hashed_secret"] for e in entries} == {_digest(v) for v in [initial, common_value, main_value, side_value]}
    assert repo.payload()["results"][MATRIX][0]["hashed_secret"] == _digest(merged)


@pytest.mark.parametrize("plant", ["outside", "non-fingerprint"])
def test_new_values_are_not_absorbed_and_never_printed(repo: Repo, plant: str) -> None:
    parent = repo.git("rev-parse", "HEAD")
    value = hashlib.sha256(b"planted unreviewed value").hexdigest()  # pragma: allowlist secret
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


@pytest.mark.parametrize("problem", ["unavailable commit", "unfindable value", "duplicate identity", "round-trip"])
def test_invalid_parent_provenance_or_writer_refuses_without_writing(repo: Repo, problem: str) -> None:
    parent = repo.git("rev-parse", "HEAD")
    value = json.loads(repo.git("show", parent + ":" + MATRIX))["source_fingerprints"][0]["sha256"]
    entry = _history(value, parent)
    if problem == "unavailable commit":
        entry["observed_commit"] = "f" * 40  # pragma: allowlist secret
    elif problem == "unfindable value":
        entry["hashed_secret"] = "0" * 40  # pragma: allowlist secret
    payload = repo.payload()
    payload["history_results"] = {MATRIX: [entry, dict(entry)] if problem == "duplicate identity" else [entry]}
    (repo.root / ".secrets.baseline").write_text(json.dumps(payload) if problem == "round-trip" else _dump(payload))
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


def test_generator_must_accept_the_working_artifacts(repo: Repo) -> None:
    parent = repo.git("rev-parse", "HEAD")
    (repo.root / "state.txt").write_text("stale")
    before = (repo.root / ".secrets.baseline").read_bytes()
    result = repo.run(parent)
    assert result.returncode == 2 and "regeneration check" in result.stderr
    assert (repo.root / ".secrets.baseline").read_bytes() == before


def test_inventory_value_must_be_explained_by_source_bytes_even_with_bad_generator(repo: Repo) -> None:
    parent = repo.git("rev-parse", "HEAD")
    path = repo.root / MATRIX
    payload = json.loads(path.read_text())
    payload["source_fingerprints"][0]["sha256"] = hashlib.sha256(b"unexplained").hexdigest()  # pragma: allowlist secret
    path.write_text(_dump(payload))
    (repo.root / "scripts/build_current_state.py").write_text("raise SystemExit(0)\n")
    before = (repo.root / ".secrets.baseline").read_bytes()
    result = repo.run(parent)
    assert result.returncode == 2 and "not explained by source" in result.stderr
    assert (repo.root / ".secrets.baseline").read_bytes() == before
