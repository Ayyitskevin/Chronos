"""Read-only IBKR gateway evidence capture for the real-gateway campaign.

Captures the VISION_COMPLETION_PLAN.md section 7 read-only evidence list from the
configured broker into one session directory of sanitized, canonical JSON files:

    capture.json              every observation (or the error each step produced)
    derived_liquid_hours.json session evidence derived from captured raw strings
    manifest.json             sha256 of every file + session metadata

It NEVER calls preview_order, submit_order, modify_order, or cancel_order, and it
refuses to start unless ALLOW_ORDER_TRANSMIT and ALLOW_LIVE_TRADING are false and
AUTONOMY_MANDATE_FILE is unset. Steps that fail are recorded as errors and the
capture continues; absence of evidence is itself evidence.

Usage (from the repo root, .env configured per the campaign skill):

    .venv/bin/python .claude/skills/chronos-real-gateway-campaign/scripts/\
capture_readonly.py --out /path/to/session-dir --label session-1

Rehearsal against the demo broker (no gateway, output is NOT gateway evidence):

    BROKER_MODE=demo ... capture_readonly.py --out /tmp/rehearsal --allow-demo
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import sys
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from hashlib import sha256
from pathlib import Path
from typing import Any

STEP_TIMEOUT_S = 30.0


def to_jsonable(value: Any) -> Any:
    """Best-effort conversion of Chronos domain objects to JSON-serializable data."""

    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if hasattr(value, "model_dump"):
        return to_jsonable(value.model_dump(mode="json"))
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: to_jsonable(getattr(value, field.name))
            for field in dataclasses.fields(value)
        }
    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        items = [to_jsonable(item) for item in value]
        if isinstance(value, (set, frozenset)):
            items = sorted(items, key=repr)
        return items
    return repr(value)


def canonical_json(payload: Any) -> str:
    return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True, default=str) + "\n"


def derive_liquid_hours(capture: dict[str, Any]) -> dict[str, Any]:
    """Recompute session evidence from the RAW captured strings, deterministically.

    Pure function of capture.json content: the offline replay check re-runs this
    exact function and byte-compares the result (VCP section 7 EXIT: fixtures
    replay offline exactly). The probe instant is the captured session timestamp,
    not the wall clock, so replay is reproducible forever.
    """

    from chronos.services.liquid_hours import parse_liquid_hours

    probe_text = capture["meta"]["captured_at_utc"]
    probe = datetime.fromisoformat(probe_text)
    derived: dict[str, Any] = {"probe_instant_utc": probe_text, "contracts": {}}

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            raw = node.get("liquid_hours")
            zone = node.get("time_zone_id")
            if isinstance(raw, str) and raw and isinstance(zone, str) and zone:
                key = f"{node.get('symbol', '?')}:{node.get('local_symbol') or node.get('con_id')}"
                parsed = parse_liquid_hours(raw, time_zone_id=zone)
                if parsed is None:
                    derived["contracts"][key] = {"parsed": None, "confirms_open": None}
                else:
                    derived["contracts"][key] = {
                        "parsed": {
                            "timezone": parsed.timezone,
                            "windows": [
                                {
                                    "opens_at": window.opens_at.isoformat(),
                                    "closes_at": window.closes_at.isoformat(),
                                }
                                for window in parsed.windows
                            ],
                            "closed_dates": sorted(parsed.closed_dates),
                            "covered_dates": sorted(parsed.covered_dates),
                        },
                        "confirms_open": parsed.confirms_open(probe),
                    }
            for item in node.values():
                walk(item)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(capture)
    return derived


async def run_capture(settings: Any, args: argparse.Namespace) -> dict[str, Any]:
    from chronos.broker.market_data import MarketDataManager
    from chronos.domain.enums import BrokerAdapter, BrokerMode, OptionRight
    from chronos.marketdata.bars import BarInterval

    capture: dict[str, Any] = {
        "meta": {
            "captured_at_utc": datetime.now(UTC).isoformat(),
            "label": args.label,
            "broker_mode": settings.broker_mode.value,
            "broker_adapter": settings.broker_adapter.value,
            "ib_environment": settings.ib_environment.value,
            "gateway_evidence": settings.broker_mode is BrokerMode.IBKR,
            "allow_order_transmit": settings.allow_order_transmit,
            "allow_live_trading": settings.allow_live_trading,
        },
        "steps": {},
    }
    steps: dict[str, Any] = capture["steps"]

    if settings.broker_mode is BrokerMode.DEMO:
        from chronos.broker.demo import DemoBroker, demo_now

        broker: Any = DemoBroker(profile=settings.demo_profile)
        clock = demo_now
    elif settings.broker_adapter is BrokerAdapter.IB_ASYNC:
        from chronos.broker.ibkr import IBKRBroker

        broker = IBKRBroker(settings)
        clock = None
    else:
        from chronos.broker.official_ibkr import OfficialIBKRBroker

        broker = OfficialIBKRBroker(settings)
        clock = None
    manager = MarketDataManager(broker, clock=clock)

    async def step(name: str, coroutine: Any) -> Any:
        try:
            result = await asyncio.wait_for(coroutine, timeout=STEP_TIMEOUT_S)
        except Exception as error:
            steps[name] = {"error": f"{type(error).__name__}: {error}"}
            return None
        steps[name] = to_jsonable(result)
        return result

    local_before = datetime.now(UTC)
    connected = False
    try:
        try:
            await asyncio.wait_for(broker.connect(), timeout=STEP_TIMEOUT_S)
            connected = True
            steps["connect"] = {"ok": True}
        except Exception as error:
            steps["connect"] = {"error": f"{type(error).__name__}: {error}"}
            return capture

        await step("connection_status", broker.connection_status())
        server_time = await step("server_time", broker.server_time())
        steps["server_time_offset"] = {
            "local_utc_before_request": local_before.isoformat(),
            "local_utc_after_response": datetime.now(UTC).isoformat(),
            "note": "record the observed offset; no expected value exists for a first contact",
        }
        account = await step("account_summary", broker.account_summary())
        if account is not None and getattr(account, "account_id", ""):
            args.account_ids.add(account.account_id)
        await step("positions", broker.positions())
        await step("executions", broker.executions())
        await step("open_orders", broker.open_orders())
        steps["completed_orders"] = {
            "not_captured": (
                "the Broker protocol exposes open_orders and executions only; "
                "reqCompletedOrders is not implemented (record as a known gap)"
            )
        }

        symbols = list(args.symbols or settings.symbol_allowlist)[: args.max_symbols]
        for symbol in symbols:
            prefix = f"symbol:{symbol}"
            underlying = await step(
                f"{prefix}:qualify_underlying", broker.qualify_underlying(symbol)
            )
            if underlying is None:
                continue
            managed = await step(
                f"{prefix}:underlying_quote",
                manager.underlying_quote(underlying, force_refresh=True),
            )
            if args.skip_options:
                continue
            chain_snapshot = await step(
                f"{prefix}:option_chain_parameters",
                manager.option_chain_parameters(underlying, force_refresh=True),
            )
            if chain_snapshot is None:
                continue
            populated = next(
                (
                    chain
                    for chain in chain_snapshot.parameters
                    if chain.expirations
                    and chain.strikes
                    and chain.underlying_con_id == underlying.con_id
                ),
                None,
            )
            if populated is None:
                steps[f"{prefix}:option_specs"] = {"error": "no populated chain for underlying"}
                continue
            spot = None
            if managed is not None:
                quote = managed.quote
                spot = next(
                    (value for value in (quote.last, quote.close, quote.bid, quote.ask) if value),
                    None,
                )
            if spot is None:
                steps[f"{prefix}:option_specs"] = {"error": "no usable spot price for narrowing"}
                continue
            as_of = (server_time or datetime.now(UTC)).date()
            try:
                specs = MarketDataManager.narrow_option_specs(
                    populated,
                    symbol=symbol,
                    spot=Decimal(str(spot)),
                    right=OptionRight.PUT,
                    as_of=as_of,
                    min_dte=settings.min_dte,
                    target_dte=settings.target_dte,
                    max_dte=settings.max_dte,
                    max_expirations=1,
                    strikes_per_expiration=2,
                )
            except ValueError as error:
                steps[f"{prefix}:option_specs"] = {"error": f"ValueError: {error}"}
                continue
            steps[f"{prefix}:option_specs"] = to_jsonable(specs)
            if specs:
                batch = await step(
                    f"{prefix}:qualify_option_contracts",
                    manager.qualify_option_contracts(specs, force_refresh=True),
                )
                qualified_options = tuple(batch or ())
                if batch is None:
                    # Isolate which spec failed: qualify one at a time and record each
                    # outcome (the manager refuses a batch on any incomplete return),
                    # and retain the successful contracts for the next read-only step.
                    recovered = []
                    for spec in specs:
                        qualified = await step(
                            f"{prefix}:qualify_option:{spec.expiration}:{spec.strike}:{spec.right.value}",
                            manager.qualify_option_contracts([spec], force_refresh=True),
                        )
                        if qualified is not None:
                            recovered.extend(qualified)
                    qualified_options = tuple(recovered)
                if qualified_options:
                    await step(
                        f"{prefix}:option_market_rules",
                        manager.option_market_rules(qualified_options),
                    )
                else:
                    steps[f"{prefix}:option_market_rules"] = {
                        "not_captured": "no option contract qualified for a market-rule read"
                    }

        if not args.skip_bars and symbols:
            first = steps.get(f"symbol:{symbols[0]}:qualify_underlying")
            if isinstance(first, dict) and "error" not in first:
                underlying_again = await broker.qualify_underlying(symbols[0])
                await step(
                    f"symbol:{symbols[0]}:historical_bars_1d_30d",
                    broker.historical_bars(
                        underlying_again, interval=BarInterval.DAY_1, lookback_days=30
                    ),
                )

        steps["active_subscription_count_before_disconnect"] = manager.active_subscription_count
        bridge = getattr(broker, "bridge", None)
        if bridge is not None:
            steps["callback_notices"] = to_jsonable(list(getattr(bridge, "notices", [])))
    finally:
        if connected:
            try:
                await asyncio.wait_for(broker.disconnect(), timeout=STEP_TIMEOUT_S)
                steps["disconnect"] = {"ok": True}
            except Exception as error:
                steps["disconnect"] = {"error": f"{type(error).__name__}: {error}"}

    await step("final_connection_status", broker.connection_status())
    return capture


def sanitize(text: str, account_ids: set[str]) -> str:
    from chronos.utils.identifiers import account_fingerprint

    for account_id in sorted(account_ids, key=len, reverse=True):
        cleaned = account_id.strip()
        if len(cleaned) >= 4:
            text = text.replace(cleaned, f"ACCT-{account_fingerprint(cleaned)[:16]}")
    return text


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, help="session output directory (created)")
    parser.add_argument("--label", default="", help="session label, e.g. session-1")
    parser.add_argument("--symbols", nargs="*", default=None)
    parser.add_argument("--max-symbols", type=int, default=2)
    parser.add_argument("--skip-options", action="store_true")
    parser.add_argument("--skip-bars", action="store_true")
    parser.add_argument(
        "--allow-demo",
        action="store_true",
        help="permit BROKER_MODE=demo rehearsal (output is NOT gateway evidence)",
    )
    args = parser.parse_args()

    from chronos.config.settings import Settings
    from chronos.domain.enums import BrokerMode

    settings = Settings()
    if settings.allow_order_transmit or settings.allow_live_trading:
        print("REFUSED: ALLOW_ORDER_TRANSMIT and ALLOW_LIVE_TRADING must both be false.")
        return 2
    if settings.autonomy_mandate_file is not None:
        print("REFUSED: AUTONOMY_MANDATE_FILE must be unset during the read-only campaign.")
        return 2
    if settings.broker_mode is BrokerMode.DEMO and not args.allow_demo:
        print("REFUSED: BROKER_MODE=demo needs --allow-demo; demo output is not gateway evidence.")
        return 2

    args.account_ids = {settings.ib_account_id} if settings.ib_account_id.strip() else set()

    from chronos.broker.base import BrokerError

    try:
        capture = asyncio.run(run_capture(settings, args))
    except BrokerError as error:
        # Adapter construction failed before any capture step ran (e.g. BROKER_MODE=ibkr
        # with the official adapter and no ibapi installed). Refuse with the guidance
        # instead of a raw traceback; nothing has been written.
        print(f"REFUSED: cannot construct the configured broker adapter: {error}")
        print("(nothing was written; see chronos-real-gateway-campaign Phase 0.1)")
        return 2
    derived = derive_liquid_hours(capture)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    files = {
        "capture.json": sanitize(canonical_json(capture), args.account_ids),
        "derived_liquid_hours.json": sanitize(canonical_json(derived), args.account_ids),
    }
    manifest = {
        "campaign": "chronos-real-gateway-campaign",
        "created_utc": capture["meta"]["captured_at_utc"],
        "label": args.label,
        "gateway_evidence": capture["meta"]["gateway_evidence"],
        "files": {},
    }
    for name, text in files.items():
        (out_dir / name).write_text(text, encoding="utf-8")
        manifest["files"][name] = sha256(text.encode("utf-8")).hexdigest()
    (out_dir / "manifest.json").write_text(canonical_json(manifest), encoding="utf-8")

    errors = [
        name
        for name, value in capture["steps"].items()
        if isinstance(value, dict) and "error" in value
    ]
    print(
        f"capture written to {out_dir} ({len(capture['steps'])} steps, {len(errors)} with errors)"
    )
    for name in errors:
        print(f"  step with error: {name}: {capture['steps'][name]['error']}")
    print("errors are observations, not failures: record them in the session evidence doc")
    return 0


if __name__ == "__main__":
    sys.exit(main())
