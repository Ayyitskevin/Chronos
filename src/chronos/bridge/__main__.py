"""Run the TradingView signal bridge: ``python -m chronos.bridge``.

A separate process from the backend, deliberately. It is started, stopped, and
crashed independently of the process that holds the broker connection, so a
bridge that dies produces no proposals — which is the correct failure mode for
a signal source — and a bridge that is compromised has still gained nothing but
the ability to ask.

The banner names the posture it is starting in, because the single most
consequential fact about a running bridge is whether it forwards. It prints no
secret and no token.
"""

from __future__ import annotations

import logging
import os
import sys

import uvicorn

from chronos.bridge.app import create_app
from chronos.bridge.config import BridgeConfigError, load_config


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        config = load_config(dict(os.environ))
    except BridgeConfigError as error:
        print(f"chronos-bridge: refusing to start: {error}", file=sys.stderr)
        return 2

    posture = "FORWARDING" if config.forward else "DRY RUN (nothing will be sent)"
    print(
        f"chronos-bridge: listening on {config.host}:{config.port} — {posture}\n"
        f"  proposals go to : {config.ingress_url}\n"
        f"  symbols allowed : {', '.join(sorted(config.symbols))}\n"
        f"  kinds allowed   : {', '.join(sorted(config.kinds))}\n"
        f"  rate limit      : {config.max_alerts_per_minute}/min, "
        f"alerts older than {config.max_alert_age_seconds}s refused",
        file=sys.stderr,
    )
    uvicorn.run(create_app(config), host=config.host, port=config.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
