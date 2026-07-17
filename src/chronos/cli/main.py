"""``python -m chronos.cli`` — safe operator commands.

Every command prints the mode banner first. There is no command that enables
live trading, no ``--force`` flag, and nothing here can bypass the risk
engine, the halt store, or the mode lock. Paper-capable operation requires
running the (separate, deliberately differently named) service entry point
with a verified paper account; this CLI only inspects, researches, halts,
and rearms.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from chronos.auditlog.log import verify_chain
from chronos.control.halt import HaltReason, HaltStore
from chronos.control.modes import TradingMode, resolve_mode_lock
from chronos.risk.policy import load_risk_policy

DEFAULT_HALT_PATH = Path("data/platform_halt.json")
DEFAULT_AUDIT_PATH = Path("data/platform_audit.jsonl")


def _banner(mode: TradingMode, halt_store: HaltStore) -> None:
    lock = resolve_mode_lock(
        requested_mode=mode,
        paper_account_allowlist=(),
        broker_reported_account_id=None,
        broker_reported_environment_is_paper=None,
        order_transmission_enabled=False,
    )
    halt = halt_store.read()
    print("=" * 72)
    print(
        f"CHRONOS PLATFORM  |  MODE: {lock.mode.value.upper():<12}"
        f"|  CAPABILITY: {lock.capability.value}"
    )
    if halt.trading_blocked:
        reason = halt.reason.value if halt.reason else "UNKNOWN"
        print(f"TRADING HALTED    |  reason: {reason}  |  {halt.detail}")
    else:
        print("HALT STATE        |  armed (not halted)")
    print("LIVE TRADING      |  hard-disabled in this build (no override exists)")
    print("=" * 72)


def cmd_status(args: argparse.Namespace) -> int:
    store = HaltStore(args.halt_file)
    _banner(TradingMode.RESEARCH, store)
    ok, detail = verify_chain(args.audit_file)
    print(f"audit log: {'OK' if ok else 'FAILED'} — {detail}")
    return 0


def cmd_halt(args: argparse.Namespace) -> int:
    store = HaltStore(args.halt_file)
    state = store.halt(HaltReason.OPERATOR_REQUEST, args.reason)
    _banner(TradingMode.RESEARCH, store)
    print(f"halted: {state.detail}")
    return 0


def cmd_rearm(args: argparse.Namespace) -> int:
    store = HaltStore(args.halt_file)
    previous = store.read()
    if previous.halted:
        print(
            "clearing halt "
            f"({previous.reason.value if previous.reason else 'unknown'}: {previous.detail})"
        )
    store.rearm(args.note)
    _banner(TradingMode.RESEARCH, store)
    print("rearmed. Order generation still requires mode capability and reconciliation.")
    return 0


def cmd_risk_show(args: argparse.Namespace) -> int:
    store = HaltStore(args.halt_file)
    _banner(TradingMode.RESEARCH, store)
    policy = load_risk_policy(args.policy)
    print(f"policy version: {policy.policy_version}  hash: {policy.config_hash}")
    for key, value in sorted(policy.model_dump().items()):
        print(f"  {key}: {value}")
    return 0


def cmd_verify_corpus(args: argparse.Namespace) -> int:
    store = HaltStore(args.halt_file)
    _banner(TradingMode.RESEARCH, store)
    registry_path = args.registry
    if not registry_path.exists():
        print(f"registry not found: {registry_path}")
        return 1
    import yaml

    registry = yaml.safe_load(registry_path.read_text(encoding="utf-8"))
    failures = 0
    checked = 0
    for entry in registry.get("scripts", []):
        path = Path(entry["path"])
        if not path.exists():
            print(f"MISSING  {entry['catalog_number']}  {path}")
            failures += 1
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        checked += 1
        if digest != entry["sha256"]:
            print(f"HASH MISMATCH  {entry['catalog_number']}  {path}")
            failures += 1
    print(f"verified {checked} scripts, {failures} failures")
    return 1 if failures else 0


def cmd_verify_audit(args: argparse.Namespace) -> int:
    ok, detail = verify_chain(args.audit_file)
    print(f"audit log: {'OK' if ok else 'FAILED'} — {detail}")
    return 0 if ok else 1


def cmd_shadow_scan(args: argparse.Namespace) -> int:
    store = HaltStore(args.halt_file)
    _banner(TradingMode.SHADOW, store)
    from chronos.auditlog.log import AuditLog
    from chronos.research.runner import STRATEGY_FACTORIES
    from chronos.research.shadow import shadow_scan

    strategies = {
        name: STRATEGY_FACTORIES[name]()
        for name in args.strategies.split(",")
        if name in STRATEGY_FACTORIES
    }
    if not strategies:
        print(f"no known strategies in {args.strategies!r}")
        return 1
    reports = shadow_scan(
        strategies=strategies,
        symbols=tuple(s.strip().upper() for s in args.symbols.split(",") if s.strip()),
        data_dir=args.data_dir,
        risk_policy=load_risk_policy(args.policy),
        halt_store=store,
        audit_log=AuditLog(args.audit_file),
        equity_usd=args.equity,
    )
    print(json.dumps(reports, indent=2, default=str))
    print("SHADOW MODE: nothing was or can be submitted (capability NO_ORDERS).")
    return 0


def cmd_backtest(args: argparse.Namespace) -> int:
    store = HaltStore(args.halt_file)
    _banner(TradingMode.BACKTEST, store)
    from chronos.research.runner import run_named_backtest

    summary = run_named_backtest(
        strategy_name=args.strategy,
        symbol=args.symbol,
        data_dir=args.data_dir,
        policy_path=args.policy,
        initial_cash=args.cash,
        slippage_bps=args.slippage_bps,
    )
    print(json.dumps(summary, indent=2, default=str))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="chronos-platform",
        description="Chronos deterministic trading platform — safe operator commands",
    )
    parser.add_argument("--halt-file", type=Path, default=DEFAULT_HALT_PATH)
    parser.add_argument("--audit-file", type=Path, default=DEFAULT_AUDIT_PATH)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="show mode, halt, and audit-chain status").set_defaults(
        func=cmd_status
    )

    halt = sub.add_parser("halt", help="halt all new order generation (persistent)")
    halt.add_argument("--reason", required=True)
    halt.set_defaults(func=cmd_halt)

    rearm = sub.add_parser("rearm", help="clear a halt after review (requires note)")
    rearm.add_argument("--note", required=True)
    rearm.set_defaults(func=cmd_rearm)

    risk = sub.add_parser("risk-show", help="print the validated risk policy")
    risk.add_argument("--policy", type=Path, default=Path("config/risk.example.yaml"))
    risk.set_defaults(func=cmd_risk_show)

    corpus = sub.add_parser("verify-corpus", help="verify Pine corpus hashes vs registry")
    corpus.add_argument("--registry", type=Path, default=Path("research/strategy_registry.yaml"))
    corpus.set_defaults(func=cmd_verify_corpus)

    audit = sub.add_parser("verify-audit-log", help="verify the audit log hash chain")
    audit.set_defaults(func=cmd_verify_audit)

    shadow = sub.add_parser(
        "shadow-scan", help="evaluate latest closed bars; report would-be intents (no orders)"
    )
    shadow.add_argument("--strategies", default="regime_trend_v1,mean_reversion_v1")
    shadow.add_argument("--symbols", default="SPY,QQQ,IWM,DIA,GLD,TLT")
    shadow.add_argument("--data-dir", type=Path, default=Path("research/data/raw"))
    shadow.add_argument("--policy", type=Path, default=Path("config/risk.example.yaml"))
    shadow.add_argument("--equity", type=float, default=3000.0)
    shadow.set_defaults(func=cmd_shadow_scan)

    backtest = sub.add_parser("backtest", help="run a deterministic backtest")
    backtest.add_argument("--strategy", required=True)
    backtest.add_argument("--symbol", required=True)
    backtest.add_argument("--data-dir", type=Path, default=Path("research/data/raw"))
    backtest.add_argument("--policy", type=Path, default=Path("config/risk.example.yaml"))
    backtest.add_argument("--cash", type=float, default=3000.0)
    backtest.add_argument("--slippage-bps", type=float, default=2.0)
    backtest.set_defaults(func=cmd_backtest)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = args.func
    result: int = handler(args)
    return result


if __name__ == "__main__":
    sys.exit(main())
