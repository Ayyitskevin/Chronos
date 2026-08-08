#!/usr/bin/env python3
"""Chronos environment sanity check — READ-ONLY diagnostic.

Verifies the development environment can actually run the project's gates:
interpreter versions (project requires Python >= 3.12), the project venv at
.venv/ (every Makefile target hard-codes .venv/bin/...), tool availability
inside that venv (pytest, ruff, mypy, alembic), the hash-pinned
requirements-dev.lock (existence, parseability, pin/hash counts), node
(the terminal-client safety tests execute real JS and HARD-FAIL in CI when
node is missing), and PYTHONPATH hygiene (src layout).

STRICTLY READ-ONLY: files opened for reading only; subprocesses are
--version/-c probes that write nothing into the repo; no network access.

Exit codes: 0 = clean, 1 = findings (WARN/CRIT), 2 = could not run.

Usage:
    python3 .claude/skills/chronos-diagnostics/scripts/env_check.py
Optional env: CHRONOS_REPO_ROOT (see state_inventory.py).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

_findings = 0


def emit(level: str, text: str) -> None:
    global _findings
    if level in ("WARN", "CRIT"):
        _findings += 1
    print(f"[{level:>4}] {text}")


def detail(text: str) -> None:
    print(f"       {text}")


def find_repo_root() -> Path | None:
    override = os.environ.get("CHRONOS_REPO_ROOT")
    if override:
        root = Path(override)
        return root if (root / "AGENTS.md").is_file() else None
    root = Path(__file__).resolve().parents[4]
    return root if (root / "AGENTS.md").is_file() else None


def probe(cmd: list[str]) -> str | None:
    """Run a version-probe command; first output line or None."""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        out = (proc.stdout or proc.stderr).strip()
        return out.splitlines()[0] if proc.returncode == 0 and out else None
    except (OSError, subprocess.SubprocessError):
        return None


def version_tuple(version_text: str) -> tuple[int, int] | None:
    match = re.search(r"(\d+)\.(\d+)", version_text)
    return (int(match.group(1)), int(match.group(2))) if match else None


def check_interpreters(root: Path) -> None:
    print("\n== Python interpreters ==")
    required = ">=3.12"
    try:
        pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
        match = re.search(r'requires-python\s*=\s*"([^"]+)"', pyproject)
        if match:
            required = match.group(1)
    except OSError:
        emit("WARN", "pyproject.toml unreadable — assuming requires-python >=3.12")
    emit("INFO", f"project requires-python: {required}")

    here = sys.version.split()[0]
    ok = sys.version_info >= (3, 12)
    emit(
        "OK" if ok else "INFO",
        f"this script is running on Python {here}"
        + ("" if ok else " (fine for diagnostics; NOT enough to run the project)"),
    )

    for name in ("python3", "python3.12"):
        found = shutil.which(name)
        if not found:
            emit("INFO", f"{name}: not on PATH")
            continue
        version = probe([found, "--version"]) or "unknown"
        parsed = version_tuple(version)
        if name == "python3" and parsed is not None and parsed < (3, 12):
            emit("WARN", f"{name} -> {version} at {found} — BELOW requires-python")
            detail("Known trap: bare `python3 -m venv .venv` builds a 3.11 venv that")
            detail("cannot run the project. Use `python3.12 -m venv .venv` explicitly.")
        else:
            emit("OK", f"{name} -> {version} at {found}")


def check_venv(root: Path) -> None:
    print("\n== Project venv (.venv/) ==")
    venv_python = root / ".venv" / "bin" / "python"
    if not venv_python.is_file():
        emit("WARN", f".venv NOT PRESENT at {root / '.venv'}")
        detail("Every Makefile target hard-codes .venv/bin/... — `make test`,")
        detail("`make gates`, `make backend` all fail until it exists. Build it")
        detail("per the chronos-build-and-env skill (python3.12 + hash-locked")
        detail("install from requirements-dev.lock).")
        return
    version = probe([str(venv_python), "--version"]) or "unknown"
    parsed = version_tuple(version)
    if parsed is not None and parsed < (3, 12):
        emit("CRIT", f".venv python is {version} — below requires-python >=3.12")
        detail("Rebuild the venv with python3.12 (chronos-build-and-env).")
    else:
        emit("OK", f".venv python: {version}")

    # Tool availability inside the venv (the four gates + migrations).
    for module, why in (
        ("pytest", "make test / CI gate 4"),
        ("ruff", "make lint + format-check / CI gates 1-2"),
        ("mypy", "make type / CI gate 3 (strict, src/chronos only)"),
        ("alembic", "make migrate (v2->head upgrade path)"),
    ):
        out = probe([str(venv_python), "-m", module, "--version"])
        if out:
            emit("OK", f".venv has {module}: {out}")
        else:
            emit("WARN", f".venv missing tool: {module} ({why})")
            detail("Reinstall from the lock: pip install --require-hashes -r requirements-dev.lock")

    # Is the chronos package importable from the venv (editable install)?
    out = probe([str(venv_python), "-c", "import chronos, sys; sys.stdout.write(chronos.__file__)"])
    if out:
        emit("OK", f"chronos importable from .venv ({out})")
    else:
        emit("WARN", "chronos NOT importable from .venv")
        detail("Either `pip install -e . --no-deps` into the venv, or run tools")
        detail("with PYTHONPATH=src from the repo root.")


def check_lockfile(root: Path) -> None:
    print("\n== Dependency lock (requirements-dev.lock) ==")
    lock = root / "requirements-dev.lock"
    if not lock.is_file():
        emit("CRIT", "requirements-dev.lock NOT PRESENT")
        detail("The lock is the dependency authority (CI installs with")
        detail("--require-hashes). Without it there is no reproducible env.")
        return
    try:
        text = lock.read_text(encoding="utf-8", errors="replace")
    except OSError as error:
        emit("CRIT", f"lockfile unreadable: {error}")
        return
    lines = text.splitlines()
    pins = sum(1 for line in lines if re.match(r"^[A-Za-z0-9_.-]+==", line))
    hashes = sum(1 for line in lines if "--hash=sha256:" in line)
    header_ok = any("uv pip compile" in line for line in lines[:5])
    emit("OK", f"lockfile present: {pins} pinned packages, {hashes} sha256 hashes")
    if header_ok:
        emit("OK", "header shows uv pip compile --generate-hashes provenance")
    else:
        emit("WARN", "lock header missing the uv pip compile provenance comment")
        detail("Regenerate per the header convention (chronos-build-and-env);")
        detail("hand-edited locks fail CI's --require-hashes install.")
    if pins == 0 or hashes < pins:
        emit("WARN", f"lock shape suspicious: {pins} pins vs {hashes} hashes")
        detail("Every pin must carry at least one hash for --require-hashes.")
    # Spot integrity check: hash lines must be well-formed 64-hex digests.
    bad = [
        line
        for line in lines
        if "--hash=sha256:" in line and not re.search(r"--hash=sha256:[0-9a-f]{64}", line)
    ]
    if bad:
        emit("WARN", f"{len(bad)} malformed --hash lines (first: {bad[0].strip()[:60]})")
    else:
        emit("OK", "all --hash lines are well-formed sha256 digests")


def check_node(root: Path) -> None:
    print("\n== Node (terminal-client JS tests) ==")
    node = shutil.which("node")
    if not node:
        emit("WARN", "node not on PATH")
        detail("tests/safety/test_terminal_client.py runs the real terminal JS in")
        detail("node:vm — it SKIPS locally without node but HARD-FAILS when the")
        detail("CI env var is set. Install node before trusting a green local run")
        detail("as CI-equivalent.")
        return
    version = probe([node, "--version"]) or "unknown"
    emit("OK", f"node {version} at {node}")


def check_pythonpath(root: Path) -> None:
    print("\n== PYTHONPATH hygiene ==")
    value = os.environ.get("PYTHONPATH")
    if not value:
        emit("OK", "PYTHONPATH unset (fine when chronos is pip-installed in .venv)")
        detail("Running tests WITHOUT an editable install needs PYTHONPATH=src")
        detail("from the repo root (src layout).")
        return
    entries = [entry for entry in value.split(os.pathsep) if entry]
    emit("INFO", f"PYTHONPATH set: {value}")
    src = root / "src"
    resolved = []
    for entry in entries:
        entry_path = Path(entry)
        if not entry_path.is_absolute():
            entry_path = Path.cwd() / entry_path
        resolved.append(entry_path.resolve())
        if not entry_path.exists():
            emit("WARN", f"PYTHONPATH entry does not exist: {entry}")
    foreign = [p for p in resolved if p != src.resolve() and (p / "chronos").is_dir()]
    if foreign:
        emit("WARN", f"PYTHONPATH exposes another 'chronos' package: {foreign[0]}")
        detail("A shadowing package makes you test the wrong code. Point")
        detail("PYTHONPATH at <repo>/src only, or unset it.")
    elif src.resolve() in resolved:
        emit("OK", "PYTHONPATH includes <repo>/src (src-layout convention)")


def check_env_file(root: Path) -> None:
    print("\n== .env presence (config; details in state_inventory.py) ==")
    if (root / ".env").is_file():
        emit("INFO", ".env present — run state_inventory.py for the live-capability scan")
        detail("Reminder: a live-capable .env makes the whole test suite fail by")
        detail("design (tests/conftest.py safety tripwires).")
    else:
        emit("OK", ".env NOT PRESENT — demo-safe defaults apply; suite tripwires quiet")


def main() -> int:
    root = find_repo_root()
    if root is None:
        print("[CRIT] cannot locate Chronos repo root (no AGENTS.md); set CHRONOS_REPO_ROOT")
        return 2
    print("CHRONOS ENV CHECK (read-only) — repo:", root)

    check_interpreters(root)
    check_venv(root)
    check_lockfile(root)
    check_node(root)
    check_pythonpath(root)
    check_env_file(root)

    print(f"\n== SUMMARY: {_findings} finding(s) (WARN/CRIT lines) ==")
    print("Exit 0 = clean, 1 = findings, 2 = could not run.")
    print("Fixes belong to chronos-build-and-env; this script only observes.")
    return 1 if _findings else 0


if __name__ == "__main__":
    sys.exit(main())
