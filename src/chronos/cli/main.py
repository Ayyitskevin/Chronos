"""``python -m chronos.cli`` — safe operator commands.

Every command prints the mode banner first. There is no command that enables
live trading, no ``--force`` flag, and nothing here can bypass the risk
engine, the halt store, or the mode lock. This CLI only inspects, researches,
runs shadow scans, halts, and rearms. No paper-submitting service entry point
exists in this build; when one is added it must be a deliberately
differently-named command wired through the same mode lock, reconciliation
gate, and risk engine (docs/GO_LIVE_CHECKLIST.md).
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
    from chronos.auditlog.log import AuditLog, AuditLogCorruptionError
    from chronos.control.halt import HaltReason
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
    try:
        audit_log = AuditLog(args.audit_file)
    except AuditLogCorruptionError as error:
        store.halt(HaltReason.AUDIT_LOG_FAILURE, str(error))
        print(f"AUDIT LOG CORRUPT — halted, refusing to run: {error}")
        return 1
    reports = shadow_scan(
        strategies=strategies,
        symbols=tuple(s.strip().upper() for s in args.symbols.split(",") if s.strip()),
        data_dir=args.data_dir,
        risk_policy=load_risk_policy(args.policy),
        halt_store=store,
        audit_log=audit_log,
        equity_usd=args.equity,
    )
    print(json.dumps(reports, indent=2, default=str))
    print("SHADOW MODE: nothing was or can be submitted (capability NO_ORDERS).")
    return 0


def cmd_monitor(args: argparse.Namespace) -> int:
    from chronos.monitoring.snapshot import build_snapshot, render_text

    snapshot = build_snapshot(
        mode=TradingMode(args.mode),
        halt_file=args.halt_file,
        audit_file=args.audit_file,
        risk_policy_path=args.policy,
        data_dir=args.data_dir,
        symbols=tuple(s.strip().upper() for s in args.symbols.split(",") if s.strip()),
        ledger_path=args.ledger,
    )
    print(render_text(snapshot))
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


def cmd_research_walkforward(args: argparse.Namespace) -> int:
    from dataclasses import asdict

    from chronos.backtest.engine import BacktestConfig
    from chronos.config.settings import get_settings
    from chronos.marketdata.csv_provider import load_daily_csv
    from chronos.registry import RegistryLedger
    from chronos.research.runner import STRATEGY_FACTORIES, current_commit
    from chronos.research.walkforward import walk_forward

    store = HaltStore(args.halt_file)
    _banner(TradingMode.BACKTEST, store)
    if args.strategy not in STRATEGY_FACTORIES:
        print(
            f"unknown strategy {args.strategy!r}; known: {sorted(STRATEGY_FACTORIES)}",
            file=sys.stderr,
        )
        return 1
    csv_path = args.data_dir / f"{args.symbol.upper()}.csv"
    if not csv_path.exists():
        print(f"no data file {csv_path}", file=sys.stderr)
        return 1
    loaded = load_daily_csv(csv_path, symbol=args.symbol.upper(), source="research_raw")

    settings = get_settings()
    test_window = args.test_window or settings.walkforward_test_window_bars
    warmup = args.warmup or settings.walkforward_warmup_bars
    min_trades = args.min_trades or settings.walkforward_min_trades
    config = BacktestConfig(initial_cash_usd=args.cash, slippage_bps_per_side=args.slippage_bps)
    config_payload = {
        "strategy": args.strategy,
        "initial_cash_usd": args.cash,
        "slippage_bps_per_side": args.slippage_bps,
        "test_window": test_window,
        "warmup": warmup,
        "min_trades": min_trades,
        "block_size": args.block_size,
        "n_resamples": args.n_resamples,
        "seed": args.seed,
    }
    config_hash = hashlib.sha256(
        json.dumps(config_payload, sort_keys=True).encode("utf-8")
    ).hexdigest()

    # Dedicated, simulated halt store — never the platform halt file (mirrors run_backtest).
    halt_name = f"walkforward_halt_{args.strategy}_{args.symbol.upper()}.json"
    run_halt = HaltStore(Path("data") / halt_name)
    run_halt.rearm("walk-forward run: simulated halt store")
    report = walk_forward(
        loaded.series,
        STRATEGY_FACTORIES[args.strategy],
        load_risk_policy(args.policy),
        config,
        halt_store=run_halt,
        ledger=RegistryLedger(args.ledger),
        strategy_id=args.strategy,
        config_hash=config_hash,
        code_commit=current_commit(),
        data_hashes={args.symbol.upper(): {"csv_sha256": loaded.sha256}},
        criteria_ref=args.criteria_ref,
        test_window=test_window,
        warmup=warmup,
        min_trades=min_trades,
        seed=args.seed,
        block_size=args.block_size,
        n_resamples=args.n_resamples,
    )
    print(json.dumps(asdict(report), indent=2, default=str))
    print(
        "RESEARCH: read-only walk-forward; no order was or can be submitted. "
        "A low-sample INSUFFICIENT_EVIDENCE verdict is the honest, expected output "
        "at daily-bar trade counts (ADR-0014).",
        file=sys.stderr,
    )
    return 0


def cmd_research_campaign(args: argparse.Namespace) -> int:
    from dataclasses import asdict

    from chronos.backtest.engine import BacktestConfig
    from chronos.registry import RegistryLedger
    from chronos.research.campaign import run_campaign

    store = HaltStore(args.halt_file)
    _banner(TradingMode.BACKTEST, store)
    strategies = [s.strip() for s in args.strategies.split(",") if s.strip()]
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    report = run_campaign(
        strategies=strategies,
        symbols=symbols,
        data_dir=args.data_dir,
        policy=load_risk_policy(args.policy),
        ledger=RegistryLedger(args.ledger),
        # Flat data/ so the per-cell halt files match the gitignored data/*.json glob.
        halt_dir=Path("data"),
        config=BacktestConfig(initial_cash_usd=args.cash, slippage_bps_per_side=args.slippage_bps),
        stage_end=args.stage_end,
        warmup=args.warmup,
        test_window=args.test_window,
        min_trades=args.min_trades,
        seed=args.seed,
        block_size=args.block_size,
        n_resamples=args.n_resamples,
    )
    results_dir = Path("research/results")
    results_dir.mkdir(parents=True, exist_ok=True)
    out_path = results_dir / f"campaign_{args.stage_end}.json"
    out_path.write_text(json.dumps(asdict(report), indent=2, default=str), encoding="utf-8")

    # Human-readable verdict table to stdout.
    print(f"\nRE-VALIDATION CAMPAIGN  policy={report.policy_version} ({report.policy_hash})")
    print(f"dev+val span <= {report.stage_end}  seed={report.seed}  (holdout 2022+ sealed)")
    header = (
        f"{'strategy':<20}{'sym':<5}{'win':>4}{'trades':>7}{'DSR':>7}{'N':>4}  verdict / reason"
    )
    print(header)
    for row in report.verdict_table:
        dsr = f"{row.deflated_sharpe:.2f}" if row.deflated_sharpe is not None else "None"
        print(
            f"{row.strategy_id:<20}{row.symbol:<5}{row.windows:>4}{row.pooled_trades:>7}"
            f"{dsr:>7}{row.trial_count:>4}  {row.verdict} / {row.reason}"
        )
    for symbol, reason in report.excluded:
        print(f"{'(excluded)':<20}{symbol:<5}  {reason}")
    print(f"\nwrote {out_path}")
    print(
        "RESEARCH: read-only campaign; no order was or can be submitted. A table dominated "
        "by INSUFFICIENT_EVIDENCE/FAIL is the honest, expected output at daily-bar trade "
        "counts (ADR-0015). The 2022+ holdout was not touched.",
        file=sys.stderr,
    )
    return 0


def cmd_skb_query(args: argparse.Namespace) -> int:
    from chronos.skb import query as skb_query
    from chronos.skb.compiler import REPO_ROOT, STORE_PATH, load_store
    from chronos.skb.schema import (
        Classification,
        Direction,
        Disposition,
        DispositionReason,
        StrategyFamily,
    )

    store = load_store(REPO_ROOT / STORE_PATH)
    results = skb_query.query_scripts(
        store,
        disposition=Disposition(args.disposition) if args.disposition else None,
        reason=DispositionReason(args.reason) if args.reason else None,
        family=StrategyFamily(args.family) if args.family else None,
        direction=Direction(args.direction) if args.direction else None,
        classification=Classification(args.classification) if args.classification else None,
        executable=(True if args.executable else None),
        ported=args.ported,
        tradable_direction=Direction(args.tradable) if args.tradable else None,
    )
    if args.format == "ids":
        print(" ".join(e.catalog_number for e in results))
    elif args.format == "json":
        print(json.dumps([e.model_dump(mode="json") for e in results], indent=2))
    else:
        for e in results:
            print(
                f"{e.catalog_number:>3}  {e.disposition.value:<10} "
                f"{e.disposition_reason.value:<34} "
                f"{e.strategy_family.value:<22} {e.direction.value:<13} {e.title}"
            )
    print(f"# {len(results)} script(s)", file=sys.stderr)
    return 0


def cmd_skb_stats(args: argparse.Namespace) -> int:
    del args
    from chronos.skb import query as skb_query
    from chronos.skb.compiler import REPO_ROOT, STORE_PATH, load_store

    store = load_store(REPO_ROOT / STORE_PATH)
    print(
        json.dumps(
            {
                "pine_scripts": store.pine_script_count,
                "derived_strategies": store.derived_strategy_count,
                "corpus_hash": store.corpus_hash,
                "by_disposition": skb_query.disposition_counts(store),
                "by_family": skb_query.family_counts(store),
            },
            indent=2,
        )
    )
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

    monitor = sub.add_parser(
        "monitor", help="read-only platform monitor (mode, halt, reconciliation, audit, data)"
    )
    monitor.add_argument("--mode", default="shadow")
    monitor.add_argument("--policy", type=Path, default=Path("config/risk.example.yaml"))
    monitor.add_argument("--data-dir", type=Path, default=Path("research/data/raw"))
    monitor.add_argument("--symbols", default="SPY,QQQ")
    monitor.add_argument(
        "--ledger",
        type=Path,
        default=None,
        help="optional path to the SQLite execution ledger (read-only)",
    )
    monitor.set_defaults(func=cmd_monitor)

    backtest = sub.add_parser("backtest", help="run a deterministic backtest")
    backtest.add_argument("--strategy", required=True)
    backtest.add_argument("--symbol", required=True)
    backtest.add_argument("--data-dir", type=Path, default=Path("research/data/raw"))
    backtest.add_argument("--policy", type=Path, default=Path("config/risk.example.yaml"))
    backtest.add_argument("--cash", type=float, default=3000.0)
    backtest.add_argument("--slippage-bps", type=float, default=2.0)
    backtest.set_defaults(func=cmd_backtest)

    _add_skb_commands(sub)
    _add_registry_commands(sub)
    _add_research_commands(sub)

    return parser


def _add_research_commands(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    research = sub.add_parser("research", help="research-plane tools (read-only, no orders)")
    research_sub = research.add_subparsers(dest="research_command", required=True)

    wf = research_sub.add_parser(
        "walk-forward",
        help="rolling out-of-sample walk-forward with a sample-honest verdict (read-only)",
    )
    wf.add_argument("--strategy", required=True)
    wf.add_argument("--symbol", required=True)
    wf.add_argument("--data-dir", type=Path, default=Path("research/data/raw"))
    wf.add_argument("--policy", type=Path, default=Path("config/risk.example.yaml"))
    wf.add_argument("--ledger", type=Path, default=REGISTRY_LEDGER_PATH)
    wf.add_argument("--criteria-ref", default="docs/AI_QUANT_GAME_PLAN.md#C4")
    wf.add_argument("--cash", type=float, default=3000.0)
    wf.add_argument("--slippage-bps", type=float, default=2.0)
    # Window knobs fall back to settings when omitted (0/None -> default).
    wf.add_argument("--test-window", type=int, default=None)
    wf.add_argument("--warmup", type=int, default=None)
    wf.add_argument("--min-trades", type=int, default=None)
    wf.add_argument("--block-size", type=int, default=20)
    wf.add_argument("--n-resamples", type=int, default=1000)
    wf.add_argument("--seed", type=int, default=0)
    wf.set_defaults(func=cmd_research_walkforward)

    camp = research_sub.add_parser(
        "campaign",
        help="re-validation campaign: walk-forward grid + verdict table (read-only)",
    )
    camp.add_argument("--strategies", default="regime_trend_v1,mean_reversion_v1")
    camp.add_argument("--symbols", default="SPY,QQQ,IWM,DIA,GLD,TLT")
    camp.add_argument("--data-dir", type=Path, default=Path("research/data/raw"))
    # Campaign default IS the research policy (it must permit trades to be non-vacuous);
    # the deny-all example would trade nothing. Still read-only — no order path exists here.
    camp.add_argument("--policy", type=Path, default=Path("config/risk.research.yaml"))
    camp.add_argument("--ledger", type=Path, default=REGISTRY_LEDGER_PATH)
    camp.add_argument("--stage-end", default="2021-12-31", help="dev+val wall; holdout is 2022+")
    camp.add_argument("--cash", type=float, default=3000.0)
    camp.add_argument("--slippage-bps", type=float, default=2.0)
    camp.add_argument("--warmup", type=int, default=252)
    camp.add_argument("--test-window", type=int, default=252)
    camp.add_argument("--min-trades", type=int, default=20)
    camp.add_argument("--block-size", type=int, default=20)
    camp.add_argument("--n-resamples", type=int, default=1000)
    camp.add_argument("--seed", type=int, default=0)
    camp.set_defaults(func=cmd_research_campaign)


def _add_skb_commands(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    from chronos.skb.schema import (
        Classification,
        Direction,
        Disposition,
        DispositionReason,
        StrategyFamily,
    )

    skb = sub.add_parser("skb", help="query the Strategy Knowledge Base (read-only)")
    skb_sub = skb.add_subparsers(dest="skb_command", required=True)

    query = skb_sub.add_parser("query", help="filter Pine scripts by structured fields")
    query.add_argument("--disposition", choices=[d.value for d in Disposition])
    query.add_argument("--reason", choices=[r.value for r in DispositionReason])
    query.add_argument("--family", choices=[f.value for f in StrategyFamily])
    query.add_argument("--direction", choices=[d.value for d in Direction])
    query.add_argument("--classification", choices=[c.value for c in Classification])
    query.add_argument(
        "--executable", action="store_true", help="only integrity PASS_WITH_CONSTRAINTS scripts"
    )
    query.add_argument(
        "--tradable",
        choices=[Direction.LONG.value, Direction.SHORT.value],
        help="tradable in this side (matches bidirectional too)",
    )
    ported = query.add_mutually_exclusive_group()
    ported.add_argument("--ported", dest="ported", action="store_true", default=None)
    ported.add_argument("--not-ported", dest="ported", action="store_false", default=None)
    query.add_argument("--format", choices=["table", "ids", "json"], default="table")
    query.set_defaults(func=cmd_skb_query)

    stats = skb_sub.add_parser("stats", help="summary counts by disposition and family")
    stats.set_defaults(func=cmd_skb_stats)


REGISTRY_LEDGER_PATH = Path("research/registry/registry.jsonl")
HISTORY_ROOT = Path("research/data/history")


def cmd_registry_stats(args: argparse.Namespace) -> int:
    from chronos.registry import RegistryLedger, burned_windows, trial_count

    ledger = RegistryLedger(args.ledger)
    ok, detail = ledger.verify()
    print(
        json.dumps(
            {
                "ledger": str(args.ledger),
                "records": len(ledger.records()),
                "trials": trial_count(ledger),
                "burned_windows": sorted(burned_windows(ledger)),
                "chain_ok": ok,
                "chain_detail": detail,
            },
            indent=2,
        )
    )
    return 0 if ok else 1  # a broken/truncated ledger is a non-zero, actionable state


def cmd_registry_verify(args: argparse.Namespace) -> int:
    from chronos.registry import RegistryLedger

    ok, detail = RegistryLedger(args.ledger).verify()
    print("registry ledger OK" if ok else f"registry ledger FAILED: {detail}")
    return 0 if ok else 1


def cmd_holdout_status(args: argparse.Namespace) -> int:
    from datetime import UTC, datetime

    from chronos.config.settings import get_settings
    from chronos.histdata.holdout import load_holdouts
    from chronos.registry import (
        RegistryLedger,
        accrued_capture_sessions,
        available_budget,
        burned_windows,
    )

    settings = get_settings()
    ledger = RegistryLedger(args.ledger)
    ok, detail = ledger.verify()
    accrued = accrued_capture_sessions(args.history_root)
    print(
        json.dumps(
            {
                "declared_windows": [w.name for w in load_holdouts(args.history_root)],
                "burned_windows": sorted(burned_windows(ledger)),
                "accrued_sessions": accrued,
                "available_unlock_budget": available_budget(
                    ledger,
                    now=datetime.now(UTC),
                    accrued_sessions=accrued,
                    sessions_per_unlock=settings.holdout_sessions_per_unlock,
                    max_outstanding_unlocks=settings.holdout_max_outstanding_unlocks,
                ),
                "chain_ok": ok,
                "chain_detail": detail,
            },
            indent=2,
        )
    )
    return 0 if ok else 1


def cmd_holdout_unlock(args: argparse.Namespace) -> int:
    import os
    from datetime import UTC, datetime

    from chronos.config.settings import get_settings
    from chronos.registry import (
        HoldoutGuardianError,
        RegistryLedger,
        accrued_capture_sessions,
        request_unlock,
    )

    # The phrase is read from the environment, never a flag echoed into process listings.
    phrase = os.environ.get("CHRONOS_HOLDOUT_UNLOCK_PHRASE", "")
    if not phrase:
        print(
            "set CHRONOS_HOLDOUT_UNLOCK_PHRASE to the unlock phrase (never a --flag)",
            file=sys.stderr,
        )
        return 2
    settings = get_settings()
    try:
        grant = request_unlock(
            RegistryLedger(args.ledger),
            args.history_root,
            args.window,
            typed_phrase=phrase,
            reason=args.reason,
            now=datetime.now(UTC),
            accrued_sessions=accrued_capture_sessions(args.history_root),
            ttl_minutes=settings.holdout_unlock_ttl_minutes,
            sessions_per_unlock=settings.holdout_sessions_per_unlock,
            max_outstanding_unlocks=settings.holdout_max_outstanding_unlocks,
        )
    except HoldoutGuardianError as error:
        print(json.dumps({"error": str(error)}), file=sys.stderr)
        return 1
    print(
        json.dumps(
            {"unlock_id": grant.unlock_id, "window": grant.window, "expires_at": grant.expires_at},
            indent=2,
        )
    )
    return 0


def _add_registry_commands(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    registry = sub.add_parser("registry", help="experiment registry (read-only reporting)")
    registry_sub = registry.add_subparsers(dest="registry_command", required=True)
    for name, func, helptext in (
        ("stats", cmd_registry_stats, "records, trial count, and burned windows"),
        ("verify", cmd_registry_verify, "verify the ledger hash chain (exit 1 on tamper)"),
    ):
        parser = registry_sub.add_parser(name, help=helptext)
        parser.add_argument("--ledger", type=Path, default=REGISTRY_LEDGER_PATH)
        parser.set_defaults(func=func)

    holdout = sub.add_parser("holdout", help="holdout guardian (status + owner-typed unlock)")
    holdout_sub = holdout.add_subparsers(dest="holdout_command", required=True)

    status = holdout_sub.add_parser("status", help="declared/burned windows + unlock budget")
    status.add_argument("--ledger", type=Path, default=REGISTRY_LEDGER_PATH)
    status.add_argument("--history-root", type=Path, default=HISTORY_ROOT)
    status.set_defaults(func=cmd_holdout_status)

    unlock = holdout_sub.add_parser("unlock", help="owner-typed single-use holdout unlock")
    unlock.add_argument("--window", required=True)
    unlock.add_argument("--reason", required=True)
    unlock.add_argument("--ledger", type=Path, default=REGISTRY_LEDGER_PATH)
    unlock.add_argument("--history-root", type=Path, default=HISTORY_ROOT)
    unlock.set_defaults(func=cmd_holdout_unlock)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = args.func
    result: int = handler(args)
    return result


if __name__ == "__main__":
    sys.exit(main())
