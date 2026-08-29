"""Fail-closed tests for the exact pip-bootstrap identity."""

from __future__ import annotations

import re
from importlib import metadata
from pathlib import Path

import pytest
from scripts.verify_pip_bootstrap import (
    PipBootstrapError,
    locked_pip_version,
    verify_pip_bootstrap,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PIP_VERSION = "26.2.1"
_PIP_HASHES = frozenset(
    {
        "71138adf1f4ca900cdb7d289c21b7494329f2332b6d85f0e1c42108c0384ed3e",
        "f6ad667e89a1fe78046c8f13232b247200f5258d7828f3f7883d660878e0813f",
    }
)


def _locked_entry(lock: Path, package: str) -> tuple[str, frozenset[str]]:
    lines = lock.read_text(encoding="utf-8").splitlines()
    prefix = f"{package}=="
    for index, raw_line in enumerate(lines):
        line = raw_line.strip()
        if not line.startswith(prefix):
            continue
        version = line.removeprefix(prefix).removesuffix("\\").strip()
        hashes: list[str] = []
        for continuation in lines[index + 1 :]:
            match = re.fullmatch(r"\s*--hash=sha256:([0-9a-f]{64})\s*\\?", continuation)
            if match is None:
                break
            hashes.append(match.group(1))
        return version, frozenset(hashes)
    raise AssertionError(f"{package!r} is absent from {lock}")


def test_bootstrap_input_and_lock_pin_the_exact_published_pip_artifacts() -> None:
    input_requirements = tuple(
        line
        for raw_line in (_REPO_ROOT / "requirements-bootstrap.in")
        .read_text(encoding="utf-8")
        .splitlines()
        if (line := raw_line.strip()) and not line.startswith("#")
    )
    lock = _REPO_ROOT / "requirements-bootstrap.lock"
    lock_text = lock.read_text(encoding="utf-8")

    assert input_requirements == (f"pip=={_PIP_VERSION}",)
    assert locked_pip_version(lock) == _PIP_VERSION
    locked_hashes = re.findall(r"--hash=sha256:([0-9a-f]{64})", lock_text)
    assert len(locked_hashes) == len(_PIP_HASHES)
    assert frozenset(locked_hashes) == _PIP_HASHES


def test_dev_lock_cannot_replace_pip_with_a_different_same_version_artifact() -> None:
    bootstrap_entry = _locked_entry(_REPO_ROOT / "requirements-bootstrap.lock", "pip")
    dev_entry = _locked_entry(_REPO_ROOT / "requirements-dev.lock", "pip")

    assert bootstrap_entry == (_PIP_VERSION, _PIP_HASHES)
    assert dev_entry == bootstrap_entry


def test_bootstrap_verifier_accepts_only_the_locked_installed_version(tmp_path: Path) -> None:
    lock = tmp_path / "requirements-bootstrap.lock"
    lock.write_text("pip==26.2.1 \\\n    --hash=sha256:" + "a" * 64 + "\n", encoding="utf-8")

    assert verify_pip_bootstrap(lock, version_getter=lambda _name: "26.2.1") == "26.2.1"

    with pytest.raises(PipBootstrapError, match="pip bootstrap drift"):
        verify_pip_bootstrap(lock, version_getter=lambda _name: "26.0.1")


@pytest.mark.parametrize(
    "content",
    (
        "",
        "setuptools==84.0.0\n",
        "pip>=26.2.1\n",
        "pip==26.2.1\nsetuptools==84.0.0\n",
    ),
)
def test_bootstrap_lock_refuses_missing_inexact_or_additional_requirements(
    tmp_path: Path,
    content: str,
) -> None:
    lock = tmp_path / "requirements-bootstrap.lock"
    lock.write_text(content, encoding="utf-8")

    with pytest.raises(PipBootstrapError):
        locked_pip_version(lock)


def test_bootstrap_verifier_refuses_missing_pip(tmp_path: Path) -> None:
    lock = tmp_path / "requirements-bootstrap.lock"
    lock.write_text("pip==26.2.1\n", encoding="utf-8")

    def missing(_name: str) -> str:
        raise metadata.PackageNotFoundError("pip")

    with pytest.raises(PipBootstrapError, match="pip is not installed"):
        verify_pip_bootstrap(lock, version_getter=missing)
