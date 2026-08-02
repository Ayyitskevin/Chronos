#!/usr/bin/env python3
"""Chronos safety-state snapshot — READ-ONLY diagnostic.

Reports presence + parsed state + interpretation for every durable safety
artifact of the Chronos repo: the live kill-switch file (MISSING => DISENGAGED
— the stop is DISARMED), the platform halt file (MISSING => HALTED — safe),
the autonomy mandate (presence auto-activates on boot), the main DB / platform
ledger / audit JSONL / registry ledger + anchor, alembic migration count vs
SCHEMA_VERSION, .env live-capable vars, git state, and (optionally) the live
pytest collection count.

STRICTLY READ-ONLY:
- Files are opened for reading only; SQLite is opened with a `mode=ro` URI.
- git runs with `--no-optional-locks` so it never refreshes the index.
- The optional pytest collection subprocess runs with `-p no:cacheprovider`
  and PYTHONDONTWRITEBYTECODE=1 so it creates no cache dirs in the repo.
- No network connections of any kind. Never connects to a broker.

Path resolution: defaults are parsed TEXTUALLY out of
src/chronos/config/settings.py (regex over the field assignments), then
overridden by the process environment and a textual parse of .env — mirroring
pydantic-settings precedence (env > .env > default) WITHOUT importing chronos
or loading .env into the process. If the source parse fails (settings.py
drifted), hard-coded 2026-08-02 fallbacks are used and a DRIFT note printed.

Exit codes: 0 = clean (no warnings), 1 = findings (WARN/CRIT lines present),
2 = could not run (e.g. repo root not found).

Usage:
    python3 .claude/skills/chronos-diagnostics/scripts/state_inventory.py
Optional env:
    CHRONOS_REPO_ROOT           override repo-root autodetection
    CHRONOS_DIAG_VENV_PYTHON    python to use for pytest collection
                                (default: <repo>/.venv/bin/python if present)
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Optional

# --------------------------------------------------------------------------
# Fallback defaults, valid as of 2026-08-02 (src/chronos/config/settings.py).
# Used only when the textual parse of settings.py fails.
# --------------------------------------------------------------------------
FALLBACK_DEFAULTS: dict[str, str] = {
    "LIVE_KILL_SWITCH_FILE": "data/live_kill_switch.json",
    "SESSION_BASELINE_FILE": "data/session_baseline.json",
    "AUTONOMY_ALERT_FILE": "data/owner_alerts.jsonl",
    "BACKEND_TOKEN_FILE": "data/backend_api_token",
    "DATABASE_URL": "sqlite:///data/chronos.db",
    "LOG_FILE": "logs/chronos.log",
}

# .env / environment variables that move the system toward live capability.
# key -> predicate description; a "set/true/non-demo" value is a WARN.
LIVE_CAPABLE_VARS: tuple[tuple[str, str], ...] = (
    ("BROKER_MODE", "any value other than 'demo' selects a real broker adapter"),
    ("IB_ENVIRONMENT", "'live' selects the live gateway branch"),
    ("ALLOW_ORDER_TRANSMIT", "'true' is the transmission master switch"),
    ("ALLOW_LIVE_TRADING", "'true' requests the live branch (ADR-0009 conjunction)"),
    ("ALLOW_OUTSIDE_RTH", "'true' permits orders outside regular trading hours"),
    ("IB_ACCOUNT_ID", "non-empty names a real account (required for transmit)"),
    ("IB_ACCOUNT_ALLOWLIST", "non-empty is a live-account allowlist"),
    ("AUTONOMY_MANDATE_FILE", "set => the mandate auto-activates on every boot"),
)

_findings = 0  # module-level count of WARN + CRIT lines


def emit(level: str, text: str) -> None:
    """Print one labeled report line; count WARN/CRIT as findings."""
    global _findings
    if level in ("WARN", "CRIT"):
        _findings += 1
    print(f"[{level:>4}] {text}")


def detail(text: str) -> None:
    print(f"       {text}")


def find_repo_root() -> Optional[Path]:
    """Locate the Chronos repo root (env override, then script location)."""
    override = os.environ.get("CHRONOS_REPO_ROOT")
    if override:
        root = Path(override)
        return root if (root / "AGENTS.md").is_file() else None
    # scripts/ -> chronos-diagnostics -> skills -> .claude -> repo root
    root = Path(__file__).resolve().parents[4]
    return root if (root / "AGENTS.md").is_file() else None


def parse_dotenv(path: Path) -> dict[str, str]:
    """Textual KEY=VALUE parse of a .env file. Never executes or loads it."""
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return values
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, raw = line.partition("=")
        value = raw.strip().strip("'\"")
        values[key.strip().upper()] = value
    return values


def parse_settings_defaults(root: Path) -> tuple[dict[str, str], bool]:
    """Parse Path/str field defaults out of settings.py source (no import).

    Returns (defaults, parsed_ok). Matches lines shaped like:
        live_kill_switch_file: Path = Path("data/live_kill_switch.json")
        database_url: str = "sqlite:///data/chronos.db"
    """
    source_file = root / "src" / "chronos" / "config" / "settings.py"
    defaults: dict[str, str] = {}
    try:
        source = source_file.read_text(encoding="utf-8")
    except OSError:
        return dict(FALLBACK_DEFAULTS), False
    path_re = re.compile(r"^\s{4}(\w+):\s*Path\s*=\s*Path\(\"([^\"]+)\"\)", re.M)
    str_re = re.compile(r"^\s{4}(\w+):\s*str\s*=\s*\"([^\"]+)\"", re.M)
    for regex in (path_re, str_re):
        for name, value in regex.findall(source):
            defaults[name.upper()] = value
    wanted = set(FALLBACK_DEFAULTS)
    if not wanted.issubset(defaults.keys()):
        return dict(FALLBACK_DEFAULTS), False
    return defaults, True


def resolve(
    name: str, defaults: dict[str, str], dotenv: dict[str, str]
) -> tuple[Optional[str], str]:
    """Resolve a setting the way pydantic-settings would: env > .env > default."""
    if name in os.environ:
        return os.environ[name], "process env"
    if name in dotenv:
        return dotenv[name], ".env"
    if name in defaults:
        return defaults[name], "settings.py default"
    return None, "unset"


def sqlite_ro_query(db_path: Path, query: str) -> Optional[list[tuple]]:
    """Run one query against a SQLite file opened read-only; None on failure."""
    try:
        uri = f"file:{db_path.as_posix()}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=2.0) as conn:
            return list(conn.execute(query).fetchall())
    except sqlite3.Error:
        return None


def report_kill_switch(root: Path, defaults: dict, dotenv: dict) -> None:
    value, source = resolve("LIVE_KILL_SWITCH_FILE", defaults, dotenv)
    path = root / (value or FALLBACK_DEFAULTS["LIVE_KILL_SWITCH_FILE"])
    print(f"\n== Live kill switch (live-Wheel order plane) [{source}: {value}] ==")
    if not path.is_file():
        emit("WARN", f"live kill-switch file NOT PRESENT: {path}")
        detail(">>> MISSING FILE MEANS DISENGAGED: the live-plane emergency stop is")
        detail(">>> DISARMED right now (orders/kill_switch.py:83-85 returns")
        detail(">>> engaged=False on FileNotFoundError). Other gates still apply,")
        detail(">>> but the kill switch itself contributes NOTHING in this state.")
        detail("To engage: POST /live/kill on a running backend (token-only, works")
        detail("even read-only). See sibling skill chronos-run-and-operate.")
        return
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        engaged = bool(payload["engaged"])
    except (OSError, ValueError, KeyError, TypeError) as error:
        emit("WARN", f"kill-switch file present but UNPARSEABLE: {error}")
        detail("Code fails closed: an unreadable/corrupt file reads as ENGAGED")
        detail("(kill_switch.py:86-92). Safe, but fix the file deliberately.")
        return
    if engaged:
        emit("INFO", "kill switch ENGAGED (emergency stop active — safe state)")
        detail(f"reason={payload.get('reason')!r} engaged_at={payload.get('engaged_at')!r}")
        detail("Disengage requires POST /live/kill/disengage (writer-gated) + note.")
    else:
        emit("INFO", "kill switch file present and explicitly DISENGAGED")
        detail(f"note={payload.get('note')!r}")


def report_platform_halt(root: Path) -> None:
    # Platform halt path is a CLI flag default, not a Settings field.
    path = root / "data" / "platform_halt.json"
    print("\n== Platform halt (deterministic strategy platform) ==")
    if not path.is_file():
        emit("OK", f"platform halt file NOT PRESENT: {path} => HALTED (NEVER_ARMED)")
        detail("Missing file is the SAFE default for this plane (halt.py:102-109).")
        detail("Note the asymmetry with the live kill switch above: TWO stop")
        detail("mechanisms, OPPOSITE missing-file defaults.")
        return
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        halted = bool(payload.get("halted", True))
    except (OSError, ValueError) as error:
        emit("OK", f"halt file unparseable ({error}) => code reads HALTED (fail closed)")
        return
    if halted:
        emit("OK", f"platform HALTED: reason={payload.get('reason')!r}")
    else:
        emit("WARN", "platform halt file shows REARMED (not halted)")
        detail(f"rearm_note={payload.get('rearm_note')!r}")
        detail("The deterministic platform will generate work when driven. If that")
        detail("is unexpected: python -m chronos.cli halt --reason '...'")


def report_mandate(root: Path, defaults: dict, dotenv: dict) -> None:
    value, source = resolve("AUTONOMY_MANDATE_FILE", defaults, dotenv)
    print(f"\n== Autonomy mandate (ADR-0017 standing grant) [{source}] ==")
    if not value:
        emit("OK", "AUTONOMY_MANDATE_FILE unset => autonomy runtime is INERT")
        detail("No mandate file, no model authority. This is the safe default.")
        return
    emit("WARN", f"AUTONOMY_MANDATE_FILE is SET ({source}): {value}")
    detail(">>> A valid mandate file AUTO-ACTIVATES on every backend boot")
    detail(">>> (autonomy_wiring: persistent-mandate). Treat it like a credential.")
    path = Path(value)
    if not path.is_absolute():
        path = root / path
    if not path.is_file():
        emit("WARN", f"mandate path set but file NOT PRESENT: {path}")
        detail("Backend would boot WITHOUT autonomy and raise a CRITICAL owner")
        detail("alert (broken grant never takes down the process).")
        return
    try:
        raw = path.read_bytes()
        digest16 = hashlib.sha256(raw).hexdigest()[:16]
        emit("INFO", f"mandate file readable, {len(raw)} bytes")
        detail(f"Would activate as owner_event_id=persistent-mandate:{digest16}")
    except OSError as error:
        emit("WARN", f"mandate file unreadable: {error}")


def report_databases(root: Path, defaults: dict, dotenv: dict) -> None:
    print("\n== Durable stores ==")
    # Main DB from DATABASE_URL.
    url, source = resolve("DATABASE_URL", defaults, dotenv)
    url = url or FALLBACK_DEFAULTS["DATABASE_URL"]
    if url.startswith("sqlite:///"):
        db_path = root / url[len("sqlite:///") :]
        if not db_path.is_file():
            emit("INFO", f"main DB NOT PRESENT: {db_path}  [{source}]")
            detail("Fresh-checkout state: no wheel ledger, no order pipeline rows,")
            detail("no autonomy durable state, no writer lease. Nothing to trade")
            detail("with — and no history either (deny-by-default protects you).")
        else:
            emit("OK", f"main DB present: {db_path} ({db_path.stat().st_size} bytes)")
            rows = sqlite_ro_query(db_path, "SELECT version FROM schema_version")
            if rows:
                emit("INFO", f"main DB schema_version = {rows[0][0]} (code expects 7)")
                if rows[0][0] != 7:
                    emit("CRIT", "schema_version differs from SCHEMA_VERSION=7 — the")
                    detail("backend will refuse this DB at boot (fail closed).")
                    detail("Back up first, then: alembic upgrade head")
            scope = sqlite_ro_query(
                db_path, "SELECT broker_mode, environment FROM database_scope"
            )
            if scope:
                emit("INFO", f"DB scope-bound: broker_mode={scope[0][0]} env={scope[0][1]}")
            elif rows:
                emit("INFO", "DB not yet scope-bound to an account")
            if rows is None:
                emit("INFO", "read-only peek failed (locked/WAL); presence-only report")
    else:
        emit("INFO", f"DATABASE_URL is not a plain sqlite:/// URL [{source}]: {url}")
        detail("Skipping read-only peek; presence unknown.")

    # Platform execution ledger.
    ledger = root / "data" / "platform_ledger.db"
    if ledger.is_file():
        rows = sqlite_ro_query(ledger, "SELECT version FROM schema_info")
        version = rows[0][0] if rows else "unreadable"
        emit("OK", f"platform ledger present: {ledger} (schema_info={version})")
    else:
        emit("INFO", f"platform ledger NOT PRESENT: {ledger} (no platform runs yet)")

    # Platform audit JSONL.
    audit = root / "data" / "platform_audit.jsonl"
    if audit.is_file():
        try:
            lines = audit.read_text(encoding="utf-8", errors="replace").splitlines()
            last_ok = True
            if lines:
                try:
                    json.loads(lines[-1])
                except ValueError:
                    last_ok = False
            emit("OK", f"platform audit log present: {len(lines)} records")
            if not last_ok:
                emit("WARN", "audit log LAST LINE does not parse as JSON")
                detail("Chain verify: python -m chronos.cli verify-audit-log")
        except OSError as error:
            emit("WARN", f"audit log unreadable: {error}")
    else:
        emit("INFO", f"platform audit log NOT PRESENT: {audit}")

    # Registry ledger + head anchor.
    reg = root / "research" / "registry" / "registry.jsonl"
    anchor = reg.parent / "registry.head.json"
    if reg.is_file():
        emit("OK", f"registry ledger present: {reg}")
        if anchor.is_file():
            emit("OK", f"registry head anchor present: {anchor}")
        else:
            emit("WARN", "registry ledger present but HEAD ANCHOR MISSING")
            detail("Possible tail truncation (un-burning a holdout is exactly what")
            detail("the anchor detects). Run: python -m chronos.cli registry verify")
    else:
        emit("INFO", f"registry ledger NOT PRESENT: {reg}")
        detail("Expected on a fresh checkout — the registry ships EMPTY; it is")
        detail("created by the first registered research run.")

    # Other durable safety-relevant files (presence only).
    for label, rel in (
        ("owner alerts sink", defaults.get("AUTONOMY_ALERT_FILE", "data/owner_alerts.jsonl")),
        ("session drawdown baseline", defaults.get("SESSION_BASELINE_FILE", "data/session_baseline.json")),
        ("backend API token", defaults.get("BACKEND_TOKEN_FILE", "data/backend_api_token")),
    ):
        path = root / rel
        state = "present" if path.is_file() else "NOT PRESENT"
        emit("INFO", f"{label}: {state} ({path})")


def report_migrations(root: Path) -> None:
    print("\n== Alembic migrations vs code SCHEMA_VERSION ==")
    versions_dir = root / "src" / "chronos" / "persistence" / "migrations" / "versions"
    database_py = root / "src" / "chronos" / "persistence" / "database.py"
    if not versions_dir.is_dir():
        emit("CRIT", f"migrations dir NOT PRESENT: {versions_dir}")
        return
    revisions = sorted(p.name for p in versions_dir.glob("[0-9][0-9][0-9][0-9]_*.py"))
    schema_version: Optional[int] = None
    try:
        match = re.search(r"^SCHEMA_VERSION\s*=\s*(\d+)", database_py.read_text(), re.M)
        if match:
            schema_version = int(match.group(1))
    except OSError:
        pass
    emit("INFO", f"{len(revisions)} migration revisions: {revisions[0]}..{revisions[-1]}"
         if revisions else "no migration revisions found")
    emit("INFO", f"code SCHEMA_VERSION = {schema_version}")
    # Mapping (verified 2026-08-02): revision 000N produces schema v(N+1);
    # 0001 is the v2 baseline stamp. So SCHEMA_VERSION == len(revisions) + 1.
    if schema_version is not None and revisions:
        if schema_version == len(revisions) + 1:
            emit("OK", f"consistent: {len(revisions)} revisions + baseline => v{schema_version}")
        else:
            emit("CRIT", f"MISMATCH: {len(revisions)} revisions but SCHEMA_VERSION={schema_version}")
            detail("Expected SCHEMA_VERSION == revision_count + 1 (0001 = v2 baseline).")
            detail("A new migration or schema bump landed without its counterpart.")
    else:
        emit("WARN", "could not evaluate migration/schema consistency")


def report_dotenv(root: Path, dotenv: dict[str, str]) -> None:
    print("\n== .env / environment live-capability check (textual, never loaded) ==")
    env_file = root / ".env"
    if not env_file.is_file():
        emit("OK", ".env NOT PRESENT — settings.py demo-safe defaults apply")
    else:
        emit("INFO", f".env present with {len(dotenv)} assignments")
    for name, why in LIVE_CAPABLE_VARS:
        for origin, mapping in (("process env", os.environ), (".env", dotenv)):
            if name not in mapping:
                continue
            value = str(mapping[name]).strip()
            lowered = value.lower()
            benign = (
                (name == "BROKER_MODE" and lowered == "demo")
                or (name == "IB_ENVIRONMENT" and lowered == "paper")
                or (lowered in ("", "false", "0", "()"))
            )
            if benign:
                emit("INFO", f"{origin}: {name}={value!r} (safe value)")
            else:
                emit("WARN", f"{origin}: {name}={value!r} — live-capable setting")
                detail(why)
    inert = [k for k in dotenv if k.startswith("PLATFORM_") or k == "PAPER_ACCOUNT_ALLOWLIST"]
    if inert:
        emit("INFO", f"inert vars set in .env (no code reads them): {', '.join(sorted(inert))}")
        detail("Settings uses extra='ignore'; these do nothing. See chronos-config-and-flags.")


def report_git(root: Path) -> None:
    print("\n== Git state (read-only) ==")

    def git(*args: str) -> Optional[str]:
        try:
            out = subprocess.run(
                ["git", "--no-optional-locks", *args],
                cwd=root, capture_output=True, text=True, timeout=30,
            )
            return out.stdout.strip() if out.returncode == 0 else None
        except (OSError, subprocess.SubprocessError):
            return None

    branch = git("rev-parse", "--abbrev-ref", "HEAD")
    head = git("rev-parse", "--short", "HEAD")
    emit("INFO", f"branch={branch} HEAD={head}")
    status = git("status", "--porcelain")
    if status is None:
        emit("WARN", "git status failed (not a git checkout?)")
    elif not status:
        emit("OK", "working tree clean")
    else:
        lines = status.splitlines()
        untracked = sum(1 for line in lines if line.startswith("??"))
        emit("INFO", f"working tree: {len(lines) - untracked} modified/staged, "
             f"{untracked} untracked entries")


def report_test_collection(root: Path) -> None:
    print("\n== Test collection (optional; needs a Python 3.12 venv) ==")
    candidate = os.environ.get("CHRONOS_DIAG_VENV_PYTHON") or str(root / ".venv" / "bin" / "python")
    if not Path(candidate).is_file():
        emit("SKIP", f"no venv python at {candidate} — skipping pytest collection")
        detail("Baseline 2026-08-02: 2490 collected / 2489 passed, 1 skipped.")
        detail("Set CHRONOS_DIAG_VENV_PYTHON to a 3.12 venv python to enable.")
        return
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"  # no __pycache__ in the repo
    env["PYTHONPATH"] = "src"
    try:
        proc = subprocess.run(
            [candidate, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider"],
            cwd=root, capture_output=True, text=True, timeout=300, env=env,
        )
    except (OSError, subprocess.SubprocessError) as error:
        emit("WARN", f"pytest collection failed to run: {error}")
        return
    match = re.search(r"(\d+)\s+tests?\s+collected", proc.stdout)
    if match:
        count = int(match.group(1))
        emit("OK", f"pytest collected {count} tests (baseline 2026-08-02: 2490)")
        if count < 2490:
            emit("WARN", f"collection count DROPPED below the 2490 baseline ({count})")
            detail("Tests disappeared. Investigate before trusting any green run.")
    else:
        emit("WARN", f"could not parse collection output (rc={proc.returncode})")
        detail((proc.stdout or proc.stderr).strip().splitlines()[-1] if (proc.stdout or proc.stderr) else "")


def main() -> int:
    root = find_repo_root()
    if root is None:
        print("[CRIT] cannot locate Chronos repo root (no AGENTS.md found); "
              "set CHRONOS_REPO_ROOT")
        return 2
    print("CHRONOS STATE INVENTORY (read-only) — repo:", root)
    print(f"python: {sys.version.split()[0]}")

    defaults, parsed_ok = parse_settings_defaults(root)
    if not parsed_ok:
        emit("WARN", "settings.py textual parse failed — using 2026-08-02 fallback "
             "defaults (settings source has drifted; update this script)")
    dotenv = parse_dotenv(root / ".env")

    report_kill_switch(root, defaults, dotenv)
    report_platform_halt(root)
    report_mandate(root, defaults, dotenv)
    report_databases(root, defaults, dotenv)
    report_migrations(root)
    report_dotenv(root, dotenv)
    report_git(root)
    report_test_collection(root)

    print(f"\n== SUMMARY: {_findings} finding(s) (WARN/CRIT lines) ==")
    print("Exit 0 = clean, 1 = findings, 2 = could not run.")
    print("A clean report is an OBSERVATION, not gateway evidence or a promotion gate.")
    return 1 if _findings else 0


if __name__ == "__main__":
    sys.exit(main())
