"""Run the Chronos model worker: ``python -m worker`` from the repository root.

A separate process from the backend, deliberately: it can be started, stopped,
and crashed without touching the process that holds the broker connection, and
a worker that is not running simply produces no decisions — the correct
failure mode for a decision source.

The banner names the two facts that matter most about a running worker: which
model is thinking, and whether forwarding is on. It prints no API key and no
token.
"""

from __future__ import annotations

import logging
import os
import sys

from worker.config import WorkerConfigError, load_config
from worker.cycle import run_loop


def main(argv: list[str]) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        config = load_config(dict(os.environ))
    except WorkerConfigError as error:
        print(f"chronos-worker: refusing to start: {error}", file=sys.stderr)
        return 2

    once = "--once" in argv
    posture = (
        "FORWARDING to the proposal ingress"
        if config.forward
        else ("DRY RUN (decisions are logged, nothing is sent)")
    )
    print(
        f"chronos-worker: {'one cycle' if once else f'looping every {config.interval_seconds}s'}"
        f" — {posture}\n"
        f"  model     : {config.model}\n"
        f"  backend   : {config.backend_url}\n"
        f"  watchlist : {', '.join(config.symbols)}\n"
        f"  kinds     : {', '.join(sorted(config.kinds))}",
        file=sys.stderr,
    )
    try:
        run_loop(config, once=once)
    except KeyboardInterrupt:
        print("chronos-worker: stopped", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
