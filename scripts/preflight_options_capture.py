"""Check every prerequisite for the nightly option-chain capture, before scheduling it.

Usage: preflight_options_capture.py [--symbols SPY,QQQ] [--connect] [--print-units]

Forward option capture is the one Chronos job whose missed runs are **unrecoverable**:
IBKR keeps no history for expired options, so a session not captured on the day is gone
(``docs/histdata_runbook.md``). That makes a silent misconfiguration expensive in a way a
retry cannot fix, so this script front-loads the failures — each check names the exact
remedy, and a non-zero exit means "do not schedule yet".

It is **read-only and offline by default**: without ``--connect`` it opens no socket to
the gateway and reads no market data. ``--connect`` adds one bounded, read-only chain
fetch to prove the gateway actually answers for the account's data permissions.
"""

from __future__ import annotations

import argparse
import os
import socket
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
#: The exchange calendar the session label belongs to. Option snapshots are labeled by
#: US trading date, never by the capturing machine's local date.
EASTERN = ZoneInfo("America/New_York")


def eastern_session_date(now: datetime) -> date:
    """The US trading date a capture taken at ``now`` belongs to."""

    return now.astimezone(EASTERN).date()


def utc_default_mislabels_session(now: datetime) -> bool:
    """Whether ``chronos.histdata``'s UTC-date default would misname this session.

    ``python -m chronos.histdata options`` defaults ``--session`` to the **UTC** date. A
    run scheduled in a local timezone west of UTC can therefore cross UTC midnight and
    file the session under tomorrow — Friday's chain stored as Saturday, a date on which
    no session exists. Pinning the schedule to UTC (or passing ``--session`` explicitly)
    is what prevents it; this returns True exactly when the default would be wrong.
    """

    return now.astimezone(UTC).date() != eastern_session_date(now)


class _Checks:
    """Accumulates results so every prerequisite is reported, not just the first failure."""

    def __init__(self) -> None:
        self.failed = 0

    def ok(self, name: str, detail: str) -> None:
        print(f"  PASS  {name}: {detail}")

    def fail(self, name: str, detail: str, remedy: str) -> None:
        self.failed += 1
        print(f"  FAIL  {name}: {detail}")
        print(f"        fix: {remedy}")

    def warn(self, name: str, detail: str, remedy: str) -> None:
        print(f"  WARN  {name}: {detail}")
        print(f"        fix: {remedy}")


def _check_ibapi(checks: _Checks) -> None:
    try:
        import ibapi  # noqa: F401
    except ImportError:
        checks.fail(
            "ibapi",
            "the official TWS API is not importable",
            "install it per docs/ibkr_setup.md — it is not on PyPI and not a dependency",
        )
        return
    checks.ok("ibapi", "importable")


def _check_settings(checks: _Checks) -> object | None:
    try:
        from chronos.config.settings import get_settings

        settings = get_settings()
    except Exception as error:  # any config error is a hard stop here
        checks.fail("settings", f"could not load: {error}", "correct .env / environment")
        return None
    checks.ok("settings", "loaded")

    data_id = settings.ib_data_client_id
    if data_id == settings.ib_client_id:
        checks.fail(
            "IB_DATA_CLIENT_ID",
            f"equals IB_CLIENT_ID ({data_id}) — the capture would fight the trading backend",
            "set IB_DATA_CLIENT_ID to an id no other client uses (default 18)",
        )
    else:
        checks.ok("IB_DATA_CLIENT_ID", f"{data_id}, distinct from IB_CLIENT_ID")
    return settings


def _check_gateway(checks: _Checks, host: str, port: int) -> None:
    try:
        with socket.create_connection((host, port), timeout=5):
            pass
    except OSError as error:
        checks.fail(
            "gateway",
            f"nothing accepting connections at {host}:{port} ({error.__class__.__name__})",
            "start TWS or IB Gateway (paper or live — capture is read-only) and enable its API",
        )
        return
    checks.ok("gateway", f"{host}:{port} accepting connections")


def _check_history_root(checks: _Checks, root: Path) -> None:
    try:
        root.mkdir(parents=True, exist_ok=True)
        probe = root / ".preflight_write_probe"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
    except OSError as error:
        checks.fail(
            "history root", f"{root} is not writable ({error})", "fix ownership/permissions"
        )
        return
    existing = len(list((root / "options").glob("*"))) if (root / "options").is_dir() else 0
    checks.ok("history root", f"{root} writable; {existing} underlying dir(s) captured so far")


def _check_session_label(checks: _Checks, now: datetime) -> None:
    session = eastern_session_date(now)
    if utc_default_mislabels_session(now):
        checks.warn(
            "session label",
            f"running now would default --session to {now.astimezone(UTC).date()} (UTC) "
            f"but this is the {session} US session",
            "pin the schedule to UTC (OnCalendar=... UTC) or pass --session explicitly; "
            "see docs/histdata_runbook.md",
        )
        return
    checks.ok("session label", f"UTC date matches the {session} US session")


def _check_live_chain(checks: _Checks, symbol: str) -> None:
    from chronos.histdata.options_client import OfficialIBKROptionClient

    client = OfficialIBKROptionClient()
    try:
        client.connect()
        chain = client.fetch_chain(symbol)
    except Exception as error:  # surface the adapter's own words verbatim
        checks.fail(
            "live chain read",
            f"{symbol}: {error}",
            "check the gateway's API settings and this account's option data permissions",
        )
        return
    finally:
        client.disconnect()
    checks.ok(
        "live chain read",
        f"{symbol}: {len(chain.expirations)} expirations, {len(chain.strikes)} strikes, "
        f"spot={chain.spot}",
    )


def _units(symbols: str) -> str:
    return f"""# ~/.config/systemd/user/chronos-options-capture.service
[Unit]
Description=Chronos EOD option-chain capture (ADR-0012)
After=network-online.target

[Service]
Type=oneshot
WorkingDirectory=%h/Chronos
ExecStart=%h/Chronos/.venv/bin/python -m chronos.histdata options --symbols {symbols}
# One JSON line per underlying lands in the journal: journalctl --user -u chronos-options-capture

# ~/.config/systemd/user/chronos-options-capture.timer
[Unit]
Description=Nightly Chronos option-chain capture

[Timer]
# The UTC pin is load-bearing, not style. `21:15` unpinned means 21:15 LOCAL; east of
# UTC-0 that crosses UTC midnight, and --session defaults to the UTC date, so Friday's
# chain would be filed as Saturday. Verify before trusting either form:
#   systemd-analyze calendar "Mon..Fri 21:15 UTC"
OnCalendar=Mon..Fri 21:15 UTC
# Deliberately NOT Persistent=true. A missed session cannot be recovered — IBKR keeps no
# history for expired options — so a catch-up run would capture *today's* chain and file
# it under the missed date. A visible gap beats a plausible wrong row.
Persistent=false
Unit=chronos-options-capture.service

[Install]
WantedBy=timers.target

# Install, then verify the schedule resolves as intended:
#   systemctl --user daemon-reload
#   systemctl --user enable --now chronos-options-capture.timer
#   systemctl --user list-timers chronos-options-capture.timer
# A user timer only fires while the user has a session; for an unattended host enable
# lingering once:  sudo loginctl enable-linger $USER
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", default="SPY,QQQ,IWM", help="comma-separated underlyings")
    parser.add_argument(
        "--connect",
        action="store_true",
        help="additionally perform one bounded, read-only chain fetch against the gateway",
    )
    parser.add_argument(
        "--print-units", action="store_true", help="print systemd user unit templates and exit"
    )
    args = parser.parse_args()

    if args.print_units:
        print(_units(args.symbols))
        return

    now = datetime.now(UTC)
    checks = _Checks()
    print(f"Chronos option-capture preflight — {now.isoformat(timespec='seconds')}")
    print(f"Repository: {REPOSITORY_ROOT}")

    _check_ibapi(checks)
    settings = _check_settings(checks)
    if settings is not None:
        _check_gateway(checks, settings.ib_host, settings.ib_port)  # type: ignore[attr-defined]
    _check_history_root(
        checks,
        Path(os.environ.get("CHRONOS_HISTORY_ROOT", REPOSITORY_ROOT / "research/data/history")),
    )
    _check_session_label(checks, now)

    if args.connect:
        if checks.failed:
            print("\n  SKIP  live chain read: earlier checks failed")
        else:
            _check_live_chain(checks, args.symbols.split(",")[0].strip().upper())

    print()
    if checks.failed:
        raise SystemExit(
            f"{checks.failed} prerequisite(s) unmet — do NOT schedule capture yet.\n"
            "Every day this stays unscheduled is a trading day of option chains that "
            "cannot be recovered later."
        )
    print("All prerequisites met. Install the schedule with --print-units.")


if __name__ == "__main__":
    main()
