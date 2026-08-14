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
    #: The backend-issued bundle id this snapshot came from (ADR-0028), or empty
    #: under the pre-ADR-0028 posture where the worker composes its own view. It
    #: is never worker-chosen: a bundle id the worker invented would name a
    #: record the backend never wrote, and the drain would refuse it.
    bundle_id: str = ""

    def citation(self) -> dict[str, Any]:
        """The evidence citation the worker attaches to every proposal.

        ``evidence_id`` is the **issued bundle id** whenever the backend issued
        one, because that is the name the drain resolves against its durable
        record. Without one it stays the local snapshot label, which is the
        pre-ADR-0028 behavior unchanged — honest either way about what the id
        refers to.

        ``kind`` is always ``worker_evidence_snapshot``, and that matters more
        than it looks: ADR-0028 binds each bundle kind to the citation kinds it
        admits, so this worker's citation can only ever back a
        ``backend_served`` record. It cannot be used to satisfy the bridge's
        attested kind, and the bridge's cannot satisfy this one.
        """

        return {
            "evidence_id": self.bundle_id or f"worker-snapshot:{self.as_of}",
            "kind": "worker_evidence_snapshot",
            "as_of": self.as_of,
            "digest": self.digest,
            "excerpt": "Backend evidence snapshot: account, positions, orders, daily bars.",
        }


#: The proposer-credential header (ADR-0023). Restated, not imported, for the
#: same reason as ``TOKEN_HEADER``; pinned equal to ``chronos.api.auth`` by
#: ``tests/safety/test_model_worker_isolation.py``.
PROPOSER_HEADER = "X-Chronos-Proposer-Token"

#: Issuance may take this long. Composing a bundle reaches the broker for bars,
#: so it is allowed the same budget as any other evidence read.
ISSUE_TIMEOUT_SECONDS = 30.0


def request_issued_bundle(
    config: WorkerConfig, client: httpx.Client
) -> EvidenceSnapshot | None | _BindingDisabled:
    """Ask the backend to issue a bundle, and render exactly what it serves.

    This is the worker's half of ADR-0028's agreement rule, and it is deliberately
    the *dumbest possible* implementation: take the ``document`` string the
    backend returns and use it verbatim as the canonical text. The worker does
    not parse it, reformat it, re-serialize it, or add to it. That is what makes
    "the digest it cites equals the digest the backend recorded" true by
    construction rather than by care — and every way of being clever here
    (pretty-printing for the prompt, merging in a locally-fetched extra fact,
    round-tripping through ``json.loads``) is precisely the rendering drift the
    equality check exists to catch.

    Three answers, and the distinction is load-bearing:

    - a snapshot — the bundle was issued and its bytes are in hand;
    - :data:`BINDING_DISABLED` — the backend said, with a 404, that evidence
      binding is not configured. That is the backend stating its posture, not a
      failure, and the caller falls back to composing locally, which is the
      pre-ADR-0028 behavior verbatim;
    - ``None`` — anything else. A 503, a 429 at the issuance cap, a malformed
      body, an unreachable backend: the cycle does not think. Evidence is never
      invented to keep a cycle alive, and a worker that quietly reverted to local
      composition when issuance *failed* would be proposing under a posture the
      owner turned on and the backend could not honor.
    """

    headers = {TOKEN_HEADER: config.api_token}
    if config.proposer_token:
        headers[PROPOSER_HEADER] = config.proposer_token
    try:
        response = client.post(
            f"{config.backend_url}/autonomy/evidence",
            headers=headers,
            json={
                "kind": "backend_served",
                "symbols": list(config.symbols),
                "lookback_days": config.lookback_days,
            },
            timeout=ISSUE_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as error:
        _logger.warning("Evidence issuance is unreachable: %s", type(error).__name__)
        return None
    if response.status_code == 404:
        return BINDING_DISABLED
    if response.status_code != 201:
        _logger.warning(
            "Evidence issuance refused with HTTP %s; the cycle will not think",
            response.status_code,
        )
        return None
    try:
        body = response.json()
    except ValueError:
        _logger.warning("Evidence issuance returned a body that is not JSON")
        return None
    if not isinstance(body, dict):
        return None
    document = body.get("document")
    digest = body.get("digest")
    bundle_id = body.get("bundle_id")
    if not isinstance(document, str) or not isinstance(digest, str):
        return None
    if not isinstance(bundle_id, str) or not bundle_id:
        return None
    local = hashlib.sha256(document.encode("utf-8")).hexdigest()
    if local != digest:
        # The backend's own two answers disagree, so one of them is wrong and
        # there is no way to tell which. Refusing beats citing either.
        _logger.error(
            "The issued bundle's digest does not match the document served with it; "
            "the cycle will not think"
        )
        return None
    return EvidenceSnapshot(
        canonical=document,
        digest=digest,
        as_of=str(body.get("issued_at") or datetime.now(tz=UTC).isoformat()),
        bundle_id=bundle_id,
    )


class _BindingDisabled:
    """The backend answered that evidence binding is off. Not a failure."""

    __slots__ = ()


#: Singleton sentinel; identity-compared, never truthiness-compared, so it can
#: never be confused with ``None`` (a real failure) at a call site.
BINDING_DISABLED = _BindingDisabled()


def gather(config: WorkerConfig, client: httpx.Client) -> EvidenceSnapshot | None:
    """Fetch the snapshot, or return None — the cycle then refuses to think.

    ``client`` is caller-supplied so tests inject a mock transport; production
    passes a plain ``httpx.Client``.

    Under ADR-0028 the backend composes and digests this document instead
    (:func:`request_issued_bundle`); this local composition remains the
    pre-ADR-0028 path and is what runs when the backend says binding is off.
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
