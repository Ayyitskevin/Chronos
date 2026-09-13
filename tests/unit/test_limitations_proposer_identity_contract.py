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
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute

from chronos.api import auth as api_auth
from chronos.api.autonomy_wiring import build_identity_resolver
from chronos.api.dependencies import require_writer
from chronos.api.routes import autonomy as autonomy_routes
from chronos.persistence.database import Database
from chronos.supervisor.proposers import (
    ProposerRegistration,
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
    assert len(routes) == 1, [r.path for r in autonomy_routes.router.routes]
    return routes[0]


def test_4a_the_proposals_route_depends_on_require_proposer_by_object() -> None:
    route = _proposals_route()
    assert "POST" in route.methods
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
        api_auth.require_proposer(without_token)
    assert refused.value.status_code == 401
    assert "X-Chronos-Token" in str(refused.value.detail)
    with_token = SimpleNamespace(
        app=SimpleNamespace(state=state),
        headers={"X-Chronos-Token": "doc6-local-token-not-a-real-secret"},
    )
    assert api_auth.require_proposer(with_token) is None
    # and with no registry path there is no resolver at all: the drain uses the static identity
    assert build_identity_resolver(None) is None
