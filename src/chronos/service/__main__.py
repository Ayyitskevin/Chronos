"""``python -m chronos.service`` — the supervised shadow/paper service loop.

Runs the startup gate then one or more decision cycles. Resilience:
- an unexpected exception persists a STRATEGY_EXCEPTION halt and exits nonzero;
- SIGINT/SIGTERM request a clean stop after the current cycle;
- a restart re-enters startup (halt read, hydrate, reconcile) and never
  resumes trading automatically.

Default mode is SHADOW (capability NO_ORDERS): the loop reports would-be
intents and cannot submit. There is no flag that enables live trading.
"""

from __future__ import annotations

import argparse
import signal
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType

from chronos.control.halt import HaltReason, HaltStore
from chronos.control.modes import TradingMode
from chronos.research.runner import STRATEGY_FACTORIES
from chronos.service.cycle import ServiceCycle
from chronos.service.runtime import ServiceComponents, ServiceConfig, build_shadow_components
from chronos.service.startup import run_startup

_STOP = False


def _handle_stop(signum: int, frame: FrameType | None) -> None:  # pragma: no cover - signal path
    del frame
    global _STOP
    _STOP = True
    print(f"\nsignal {signum} received; stopping after current cycle")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="chronos-service",
        description="Chronos shadow/paper service loop (SHADOW by default; never live)",
    )
    parser.add_argument(
        "--mode",
        choices=["shadow", "paper"],
        default="shadow",
        help="operating mode; live/canary are refused in code and not selectable here",
    )
    parser.add_argument("--symbols", default="SPY,QQQ")
    parser.add_argument("--strategies", default="regime_trend_v1,mean_reversion_v1")
    parser.add_argument("--equity", type=float, default=3000.0)
    parser.add_argument("--data-dir", type=Path, default=Path("research/data/raw"))
    parser.add_argument("--policy", type=Path, default=Path("config/risk.example.yaml"))
    parser.add_argument("--halt-file", type=Path, default=Path("data/platform_halt.json"))
    parser.add_argument("--audit-file", type=Path, default=Path("data/platform_audit.jsonl"))
    parser.add_argument("--watch", action="store_true", help="loop instead of running once")
    parser.add_argument("--interval", type=float, default=3600.0, help="seconds between cycles")
    return parser


def _config_from_args(args: argparse.Namespace) -> ServiceConfig:
    return ServiceConfig(
        mode=TradingMode(args.mode),
        symbols=tuple(s.strip().upper() for s in args.symbols.split(",") if s.strip()),
        equity_usd=args.equity,
        strategy_ids=tuple(s.strip() for s in args.strategies.split(",") if s.strip()),
        halt_file=args.halt_file,
        audit_file=args.audit_file,
        data_dir=args.data_dir,
        risk_policy_path=args.policy,
    )


def _banner(components: ServiceComponents) -> None:
    lock = components.mode_lock
    print("=" * 72)
    print(f"CHRONOS SERVICE  |  MODE: {lock.mode.value.upper()}")
    print(f"CAPABILITY       |  {lock.capability.value}")
    print("LIVE TRADING     |  hard-disabled in this build (no override exists)")
    if lock.denial_reasons:
        print(f"MODE DENIALS     |  {'; '.join(lock.denial_reasons)}")
    print("=" * 72)


def _run_cycle(components: ServiceComponents) -> None:
    report = ServiceCycle(components).run_once(now_utc=datetime.now(tz=UTC))
    print(f"cycle: {report.detail}")
    for decision in report.decisions:
        print(
            f"  {decision.symbol:5s} {decision.strategy_id:18s} "
            f"proposal={decision.proposal} intent={decision.intent} "
            f"risk_approved={decision.risk_approved} submission={decision.submission}"
        )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = _config_from_args(args)
    try:
        components = build_shadow_components(config, strategy_factories=STRATEGY_FACTORIES)
    except Exception as error:
        HaltStore(config.halt_file).halt(
            HaltReason.INVALID_CONFIGURATION, f"service failed to start: {error!r}"
        )
        print(f"CONFIG ERROR — halted: {error}")
        return 2

    _banner(components)
    try:
        startup = run_startup(components)
        print(f"startup: {startup.detail}")
        if startup.halted:
            print("service halted; not running decision cycles. Resolve, rearm, restart.")
            return 1

        if not args.watch:
            _run_cycle(components)
            return 0

        signal.signal(signal.SIGINT, _handle_stop)
        signal.signal(signal.SIGTERM, _handle_stop)
        while not _STOP:
            _run_cycle(components)
            slept = 0.0
            while slept < args.interval and not _STOP:
                time.sleep(min(1.0, args.interval - slept))
                slept += 1.0
        print("clean shutdown")
        return 0
    except Exception as error:
        components.halt_store.halt(
            HaltReason.STRATEGY_EXCEPTION, f"service loop exception: {error!r}"
        )
        print(f"SERVICE EXCEPTION — halted: {error}")
        return 3


if __name__ == "__main__":
    sys.exit(main())
