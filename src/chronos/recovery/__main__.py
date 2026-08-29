"""Packaged recovery-measurement command.

This command never deletes or overwrites a path.  It emits one local recovery
observation, not an operational RPO/RTO guarantee.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from chronos.recovery.measurement import (
    RecoveryMeasurementError,
    capture_snapshot,
    restore_snapshot,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m chronos.recovery",
        description="Capture or restore a bounded Chronos recovery snapshot.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    capture = commands.add_parser("capture", help="capture a new recovery snapshot")
    capture.add_argument("--source-data", type=Path, required=True)
    capture.add_argument("--snapshot-root", type=Path, required=True)
    capture.add_argument("--source-id", required=True)

    restore = commands.add_parser("restore", help="restore and measure a snapshot")
    restore.add_argument("--snapshot-root", type=Path, required=True)
    restore.add_argument("--restore-root", type=Path, required=True)

    args = parser.parse_args(argv)
    try:
        if args.command == "capture":
            payload = capture_snapshot(
                source_data=args.source_data,
                snapshot_root=args.snapshot_root,
                source_id=args.source_id,
            ).to_dict()
        else:
            payload = restore_snapshot(
                snapshot_root=args.snapshot_root,
                restore_root=args.restore_root,
            ).to_dict()
    except RecoveryMeasurementError as error:
        print(f"recovery measurement refused: {error}", file=sys.stderr)
        return 2
    print(json.dumps(payload, indent=2, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
