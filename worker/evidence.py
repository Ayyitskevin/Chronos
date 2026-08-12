"""Gathering the evidence snapshot the model reasons from (ADR-0027).

The worker reads the backend's own token-protected read endpoints — the same
facts an operator sees — and freezes them into one canonical-JSON document:

- ``GET /account/summary``  — cash, buying power, net liquidation
- ``GET /account/positions`` — what is actually held
- ``GET /orders``            — Chronos-owned orders and their CHR- references
- ``GET /terminal/bars``     — recent daily bars per watchlist symbol

Two properties matter more than what is fetched:

1. **Facts are gathered or the cycle refuses.** Any failed read returns
   ``None`` and the cycle does not think — the same doctrine as the supervisor's
   fact gatherers: evidence is never invented to keep a tick alive. The one
   softness: a bars payload may carry an honest in-band refusal (pacing), which
   is included as data — the model is told the chart is unavailable rather than
   shown a fabricated one.
2. **The digest binds what the model actually saw.** The canonical JSON string
   in this snapshot is the exact text rendered into the prompt, and its SHA-256
   becomes the proposal's evidence-citation digest. Provenance is stamped by
   deterministic worker code from bytes it controls — the model never authors
   its own citation, so it cannot claim to have seen evidence it did not.

The snapshot deliberately contains no credential: the API token travels in a
request header, never in a fetched body, so it can never leak into the prompt,
the digest, or the audit chain.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from worker.config import WorkerConfig

_logger = logging.getLogger("chronos.worker.evidence")

#: One evidence read may take this long before the cycle gives up on it.
READ_TIMEOUT_SECONDS = 30.0

#: The backend's local API token header. Restated, not imported (the worker
#: imports nothing from chronos); pinned equal to ``chronos.api.auth`` by
#: ``tests/safety/test_model_worker_isolation.py``.
TOKEN_HEADER = "X-Chronos-Token"


@dataclass(frozen=True, slots=True)
class EvidenceSnapshot:
    """Everything the model is shown, frozen and digested."""

    #: The exact canonical JSON string rendered into the prompt.
    canonical: str
    #: SHA-256 over the canonical string's UTF-8 bytes.
    digest: str
    #: When the snapshot was taken, ISO-8601 UTC.
    as_of: str

    def citation(self) -> dict[str, Any]:
        """The evidence citation the worker attaches to every proposal."""

        return {
            "evidence_id": f"worker-snapshot:{self.as_of}",
            "kind": "worker_evidence_snapshot",
            "as_of": self.as_of,
            "digest": self.digest,
            "excerpt": "Backend evidence snapshot: account, positions, orders, daily bars.",
        }


def gather(config: WorkerConfig, client: httpx.Client) -> EvidenceSnapshot | None:
    """Fetch the snapshot, or return None — the cycle then refuses to think.

    ``client`` is caller-supplied so tests inject a mock transport; production
    passes a plain ``httpx.Client``.
    """

    headers = {TOKEN_HEADER: config.api_token}
    payload: dict[str, Any] = {}
    try:
        payload["account"] = _read(client, config.backend_url, "/account/summary", headers)
        payload["positions"] = _read(client, config.backend_url, "/account/positions", headers)
        payload["open_orders"] = _read(client, config.backend_url, "/orders", headers)
        bars: dict[str, Any] = {}
        for symbol in config.symbols:
            bars[symbol] = _read(
                client,
                config.backend_url,
                "/terminal/bars",
                headers,
                params={"symbol": symbol, "interval": "1d", "lookback": config.lookback_days},
            )
        payload["daily_bars"] = bars
    except _EvidenceUnavailable as error:
        _logger.warning("Evidence gathering failed; the cycle will not think: %s", error)
        return None

    as_of = datetime.now(tz=UTC).isoformat()
    payload["as_of"] = as_of
    payload["watchlist"] = list(config.symbols)
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return EvidenceSnapshot(
        canonical=canonical,
        digest=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        as_of=as_of,
    )


class _EvidenceUnavailable(RuntimeError):
    """A read failed. The message never contains the token."""


def _read(
    client: httpx.Client,
    base_url: str,
    path: str,
    headers: dict[str, str],
    params: dict[str, Any] | None = None,
) -> Any:
    try:
        response = client.get(
            f"{base_url}{path}", headers=headers, params=params, timeout=READ_TIMEOUT_SECONDS
        )
    except httpx.HTTPError as error:
        raise _EvidenceUnavailable(f"{path}: {type(error).__name__}") from error
    if response.status_code != 200:
        raise _EvidenceUnavailable(f"{path}: HTTP {response.status_code}")
    try:
        return response.json()
    except ValueError as error:
        raise _EvidenceUnavailable(f"{path}: body is not JSON") from error
