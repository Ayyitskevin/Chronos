"""docs/limitations.md tells the truth about proposer identity and ingress authentication.

At 4e7068e three bullets described the pre-registry design as current: that
``POST /autonomy/proposals`` reuses "the same local API token every mutating endpoint
requires", that the static ingress provenance "cannot distinguish" a TradingView-sourced
author from a model worker, and that the transport "does not distinguish one local worker
from another". Since ADR-0023 the route authenticates through its own dependency
(``require_proposer``), persists the verified registration binding on the queue row, and
the drain stamps identity from that registration; the indistinguishability is only the
residual of the optional static SHADOW posture (no ``AUTONOMY_PROPOSERS_FILE``).

Pins in both directions, numbered to the DOC-6 contract: (1)-(3) the three passages carry
the accepted phrases (anchored allowlists — the old absolutes are forbidden); (4) the source
facts the prose rests on — the FastAPI route's *dependant* names ``require_proposer`` (not a
substring of the file), ``build_identity_resolver`` on a real registration yields the
registration's identity, and ``require_proposer`` with no registry configured is exactly
the token check plus ``None`` (the only path where identity is not credential-derived).

(5) The r2 qualifier (Daybreak P1): the registry authenticates a CREDENTIAL, not the
process presenting it. Distinct registrations are distinct authors; one credential
configured in two processes is one author and the registry cannot tell them apart. The
doc must say exactly that, the bare "distinct authors in provenance" claim must be gone
from the whole document, and the source pin reproduces the reviewer's probe.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from fastapi import HTTPException, Request
from fastapi.routing import APIRoute

from chronos.api import auth as api_auth
from chronos.api.autonomy_wiring import build_identity_resolver
from chronos.api.dependencies import require_writer
from chronos.api.routes import autonomy as autonomy_routes
from chronos.persistence.database import Database
from chronos.supervisor.proposers import (
    ProposerRegistration,
    ProposerRegistry,
    credential_hash,
    registration_binding,
)

ROOT = Path(__file__).resolve().parents[2]
LIMITATIONS = ROOT / "docs" / "limitations.md"

INGRESS_ANCHOR = "- **The ingress transport** (`POST /autonomy/proposals`)"
AUTHOR_ANCHOR = "- **Since 2026-08-12 the caller need not be a model at all"
RESIDUAL_ANCHOR = "- **Who is calling, beyond the token.**"


def _bullet(anchor: str) -> str:
    """One markdown bullet, whitespace-collapsed: its anchor up to the next bullet, heading
    or blank line (the file writes each bullet as one paragraph)."""

    text = LIMITATIONS.read_text(encoding="utf-8")
    start = text.index("\n" + anchor) + 1
    stop = re.search(r"^(?:- |#{1,6} |$)", text[start + 1 :], re.M)
    assert stop is not None, anchor
    return " ".join(text[start : start + 1 + stop.start()].split())


# ----------------------------------------------------------- (1) the ingress bullet


def test_1_ingress_bullet_names_the_proposer_dependency_and_its_exact_fallback() -> None:
    bullet = _bullet(INGRESS_ANCHOR)
    for phrase in (
        "`require_proposer`",
        "`src/chronos/api/routes/autonomy.py`",
        "`src/chronos/api/auth.py`",
        "registered proposer credential",
        "pre-registry posture",
        "`require_token`",
    ):
        assert phrase in bullet, phrase
    assert "the same local API token every mutating endpoint requires" not in bullet


# ----------------------------------------------------------- (2) the author-identity bullet


def test_2_author_bullet_says_identity_comes_from_the_registration() -> None:
    bullet = _bullet(AUTHOR_ANCHOR)
    for phrase in (
        "`src/chronos/supervisor/proposers.py`",
        "`src/chronos/api/autonomy_wiring.py`",
        "credential hash",
        "proposer_id",
        "registry-entry digest",
        "static SHADOW posture",
    ):
        assert phrase in bullet, phrase
    # the indistinguishability survives only as the static-posture residual, never as
    # the current design
    assert (
        "now covers two genuinely different kinds of author and cannot distinguish them"
        not in bullet
    )
    assert "The gap below is therefore wider than it was, not narrower" not in bullet


# ----------------------------------------------------------- (3) the caller residual


def test_3_residual_bullet_is_the_unconfigured_static_posture() -> None:
    bullet = _bullet(RESIDUAL_ANCHOR)
    for phrase in (
        "`AUTONOMY_PROPOSERS_FILE`",
        "`INGRESS_IDENTITY`",
        "owner-authored",
        "ADR-0051",
    ):
        assert phrase in bullet, phrase
    assert "it does not distinguish one local worker from another" not in bullet


# ----------------------------------------------------------- (4) the source facts


def _proposals_route() -> APIRoute:
    routes = [
        route
        for route in autonomy_routes.router.routes
        if isinstance(route, APIRoute) and route.path == "/autonomy/proposals"
    ]
    assert len(routes) == 1, [getattr(r, "path", "?") for r in autonomy_routes.router.routes]
    return routes[0]


def test_4a_the_proposals_route_depends_on_require_proposer_by_object() -> None:
    route = _proposals_route()
    assert route.methods is not None and "POST" in route.methods
    calls = [dependency.call for dependency in route.dependant.dependencies]
    assert api_auth.require_proposer in calls, calls
    # judged before the writer lease: the proposer dependency is declared first
    assert calls.index(api_auth.require_proposer) < calls.index(require_writer)


def _registration(proposer_id: str, credential: str) -> dict[str, object]:
    return {
        "proposer_id": proposer_id,
        "secret_sha256": credential_hash(credential),
        "provider": "anthropic",
        "model_id": "model-x",
        "model_version": "mv-7",
        "prompt_version": "pv-3",
        "tool_schema_version": "ts-2",
        "decision_schema_version": "ds-4",
        "policy_version": "pol-5",
        "expires_at": "2030-01-01T00:00:00+00:00",
        "enabled": True,
    }


def test_4b_a_registered_credential_yields_the_identity_the_drain_stamps(tmp_path: Path) -> None:
    entry = _registration("doc6-worker", "doc6-credential-not-a-real-secret")
    registry_path = tmp_path / "proposers.json"
    registry_path.write_text(
        json.dumps({"schema_version": 1, "proposers": [entry]}), encoding="utf-8"
    )
    registry_path.chmod(0o600)
    resolve = build_identity_resolver(registry_path)
    assert resolve is not None, "a configured registry must produce a resolver"
    binding = registration_binding(ProposerRegistration.model_validate(entry))
    # the resolver consults the revocation table on a real session (ADR-0048);
    # an empty temporary store is the "nothing revoked" case
    database = Database("sqlite+pysqlite:///:memory:")
    database.initialize()
    try:
        with database.sessions.begin() as session:
            resolved = resolve(
                session,
                "doc6-worker",
                binding.credential_epoch,
                binding.registry_entry_digest,
                datetime(2026, 9, 13, 20, 0, tzinfo=UTC),
            )
    finally:
        database.dispose()
    identity = resolved.identity
    assert identity is not None, resolved
    assert identity.proposer_id == "doc6-worker"
    assert (identity.provider, identity.model_id, identity.model_version) == (
        "anthropic",
        "model-x",
        "mv-7",
    )
    assert (
        identity.prompt_version,
        identity.tool_schema_version,
        identity.decision_schema_version,
        identity.policy_version,
    ) == ("pv-3", "ts-2", "ds-4", "pol-5")


def test_4c_no_registry_means_the_token_check_and_no_proposer_and_no_resolver() -> None:
    # The pre-registry (static) posture: no ``proposer_auth`` on the app, so
    # require_proposer is exactly require_token followed by ``None``.
    state = SimpleNamespace(api_token="doc6-local-token-not-a-real-secret")
    without_token = SimpleNamespace(app=SimpleNamespace(state=state), headers={})
    with pytest.raises(HTTPException) as refused:
        api_auth.require_proposer(cast(Request, without_token))
    assert refused.value.status_code == 401
    assert "X-Chronos-Token" in str(refused.value.detail)
    with_token = SimpleNamespace(
        app=SimpleNamespace(state=state),
        headers={"X-Chronos-Token": "doc6-local-token-not-a-real-secret"},
    )
    assert api_auth.require_proposer(cast(Request, with_token)) is None
    # and with no registry path there is no resolver at all: the drain uses the static identity
    assert build_identity_resolver(None) is None


# ----------------------------------------------------------- (5) credential is not process


def test_5a_author_bullet_qualifies_distinct_authors_by_registration_not_process() -> None:
    bullet = _bullet(AUTHOR_ANCHOR)
    for phrase in (
        "Distinct registrations are therefore distinct authors",
        "authenticates is the credential, not the process",
        "one credential configured in two processes is one author",
        "`docs/model_worker.md`",
        "`docs/tradingview_bridge.md`",
    ):
        assert phrase in bullet, phrase
    # the unqualified claim is gone from the whole document, not just this bullet
    whole = " ".join(LIMITATIONS.read_text(encoding="utf-8").split())
    assert "registered worker are therefore distinct authors" not in whole
    assert "distinct authors in `provenance`" not in whole


def test_5b_one_credential_is_one_author_whoever_presents_it() -> None:
    now = datetime(2026, 9, 13, 20, 0, tzinfo=UTC)
    shared = _registration("shared-client", "doc6-shared-credential-not-a-real-secret")
    registry = ProposerRegistry.model_validate({"schema_version": 1, "proposers": [shared]})
    # Daybreak's probe: the same credential presented "as the bridge" and "as the worker"
    # verifies to the same registration -- the presenter label is not an input.
    presented = {
        label: registry.verify("doc6-shared-credential-not-a-real-secret", now=now)
        for label in ("bridge", "worker")
    }
    assert all(match is not None for match in presented.values()), presented
    assert {match.proposer_id for match in presented.values() if match} == {"shared-client"}
    distinct = presented["bridge"] is not presented["worker"]
    assert distinct is False
    # positive control: two registrations with their own credentials ARE distinct authors
    bridge = _registration("tradingview-bridge", "doc6-bridge-credential-not-a-real-secret")
    worker = _registration("claude-worker", "doc6-worker-credential-not-a-real-secret")
    two = ProposerRegistry.model_validate({"schema_version": 1, "proposers": [bridge, worker]})
    as_bridge = two.verify("doc6-bridge-credential-not-a-real-secret", now=now)
    as_worker = two.verify("doc6-worker-credential-not-a-real-secret", now=now)
    assert as_bridge is not None and as_worker is not None
    assert (as_bridge.proposer_id, as_worker.proposer_id) == ("tradingview-bridge", "claude-worker")
    # and "distinct registrations" means distinct credentials by construction: the registry
    # refuses two entries that share a credential hash
    twin = dict(bridge, proposer_id="claude-worker")
    with pytest.raises(ValueError, match="share a credential hash"):
        ProposerRegistry.model_validate({"schema_version": 1, "proposers": [bridge, twin]})
