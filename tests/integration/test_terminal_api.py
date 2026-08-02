"""The operator terminal's data plane over the real app (M8a, ADR-0018).

Runs the whole FastAPI application through its lifespan against the DemoBroker,
with an owner mandate on disk so the authority panels have something true to
say. The DemoBroker cannot transmit and nothing here proposes, previews,
confirms, or submits anything — the terminal has no route through which it
could, which is the property ADR-0018 §4 exists to keep.

What these tests are actually for, in order of how badly the milestone fails
without them:

1. **A demoted backend must still answer.** Every ``GET`` here is deliberately
   not writer-gated, and a later edit adding ``require_writer`` "for
   consistency" would silently take an operator's window away at the moment
   they most need it. ``test_every_read_route_answers_on_a_read_only_backend``
   is what makes that edit fail.
2. **The mutating routes must be gated, and refuse honestly.** Acknowledging and
   revoking write state a read-only backend does not own, and revocation is a
   new authorization surface (ADR-0018 residual 4): it must refuse an
   unexplained request, and it must report "nothing was in force" as the answer
   it is rather than as a failure.
3. **Nothing may carry a broker account id.** ``account_fingerprint`` is a
   pseudonym and is expected in these bodies; the raw id it is derived from
   never is.

The autonomy tick is disabled for every app built here. Letting it run would
drive the demo broker from a worker thread on its own schedule, which turns a
test about routes into a test about timing — and the routes never call it.
"""

from __future__ import annotations

import ast
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from chronos.api.auth import load_or_create_token
from chronos.api.main import create_app
from chronos.api.terminal_session import SESSION_COOKIE
from chronos.autonomy import (
    AutonomyMandate,
    AutonomyMode,
    CapitalLimits,
    ConcentrationLimits,
    FamilyPromotion,
    InstrumentScope,
    MarketDataRequirements,
    OrderForm,
    PromotionLevel,
    StrategyForm,
    TradableAssetClass,
    VersionPins,
)
from chronos.broker.demo import DEMO_ACCOUNT_ID
from chronos.config.settings import get_settings
from chronos.domain.enums import DataQuality
from chronos.orders.service import OrderManagementService
from chronos.persistence.database import Database
from chronos.supervisor import alerts as alert_module
from chronos.supervisor import proposals
from chronos.terminal import commands, views
from chronos.utils.identifiers import account_fingerprint
from chronos.utils.locking import WriterLease

REPO_ROOT = Path(__file__).resolve().parents[2]

_FINGERPRINT = account_fingerprint(DEMO_ACCOUNT_ID)

#: Every read route, which is also every route that must survive demotion.
READ_ROUTES = (
    "/terminal/commands",
    "/terminal/system",
    "/terminal/mandate",
    "/terminal/journal",
    "/terminal/counters",
    "/terminal/queue",
    "/terminal/alerts",
)


def _mandate(now: datetime) -> AutonomyMandate:
    """An owner grant for the demo account, valid around ``now``.

    PAPER_AUTONOMOUS on purpose: the panels and the revocation route are what is
    under test, and a live mode would add gate machinery that has nothing to do
    with whether a window renders.
    """

    return AutonomyMandate(
        mandate_id="m-terminal-test",
        mandate_version=1,
        account_fingerprint=_FINGERPRINT,
        mode=AutonomyMode.PAPER_AUTONOMOUS,
        promotions=(
            FamilyPromotion(
                asset_class=TradableAssetClass.EQUITY, level=PromotionLevel.PAPER_AUTONOMOUS
            ),
        ),
        effective_from=now - timedelta(hours=1),
        expires_at=now + timedelta(days=1),
        versions=VersionPins(
            provider="external-worker",
            model_id="ingress",
            model_version="1",
            prompt_version="1",
            tool_schema_version="1",
            decision_schema_version="1",
            policy_version="1",
        ),
        scope=InstrumentScope(
            asset_classes=(TradableAssetClass.EQUITY,),
            symbols=("SPY",),
            strategies=(StrategyForm.LONG_EQUITY,),
            order_forms=(OrderForm.LIMIT,),
        ),
        capital=CapitalLimits(
            allocated_capital_usd=Decimal(50_000),
            max_order_notional_usd=Decimal(10_000),
            max_gross_exposure_usd=Decimal(500_000),
            max_net_exposure_usd=Decimal(500_000),
            max_position_notional_usd=Decimal(100_000),
            max_shares_per_order=100,
            min_cash_floor_usd=Decimal(1_000),
            min_buying_power_usd=Decimal(500),
        ),
        concentration=ConcentrationLimits(max_symbol_exposure_pct=Decimal("0.50")),
        market_data=MarketDataRequirements(
            max_quote_age_seconds=Decimal(5),
            permitted_data_qualities=(DataQuality.LIVE,),
        ),
        owner_authorization_ref="owner-terminal-test",
        authored_at=now,
    )


async def _no_ticks(autonomy: object) -> None:
    """Stand in for the tick task: these tests exercise routes, not the schedule."""

    return None


@pytest.fixture()
def demo_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    monkeypatch.chdir(REPO_ROOT)
    mandate_file = tmp_path / "mandate.json"
    mandate_file.write_text(_mandate(datetime.now(UTC)).model_dump_json(), encoding="utf-8")
    monkeypatch.setenv("BROKER_MODE", "demo")
    monkeypatch.setenv("DEMO_PROFILE", "empty_account")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'chronos.db'}")
    monkeypatch.setenv("LOG_FILE", str(tmp_path / "chronos.log"))
    monkeypatch.setenv("BACKEND_TOKEN_FILE", str(tmp_path / "backend_api_token"))
    monkeypatch.setenv("LIVE_KILL_SWITCH_FILE", str(tmp_path / "kill.json"))
    monkeypatch.setenv("SESSION_BASELINE_FILE", str(tmp_path / "baseline.json"))
    monkeypatch.setenv("AUTONOMY_MANDATE_FILE", str(mandate_file))
    monkeypatch.setenv("AUTONOMY_ALERT_FILE", str(tmp_path / "owner_alerts.jsonl"))
    monkeypatch.setattr("chronos.api.main.autonomy_tick_task", _no_ticks)
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


@pytest.fixture()
def client(demo_env: Path) -> Iterator[TestClient]:
    with TestClient(create_app()) as test_client:
        yield test_client


@pytest.fixture()
def headers(demo_env: Path) -> dict[str, str]:
    """The local API token, created here rather than read after a backend made it.

    ``load_or_create_token`` is the same call the lifespan makes and is
    idempotent, so this fixture does not depend on an app having started first —
    which matters for the restart test, where the token is needed before the
    first backend exists.
    """

    return {"X-Chronos-Token": load_or_create_token(demo_env / "backend_api_token")}


@pytest.fixture()
def read_only_client(demo_env: Path) -> Iterator[TestClient]:
    """A backend that lost the race for the single-writer lease.

    The lease is taken externally first, exactly as a first backend would take
    it, so this app boots demoted with no autonomy runtime — which is the state
    every read route below has to keep answering in.
    """

    database = Database(f"sqlite:///{demo_env / 'chronos.db'}")
    database.initialize()
    lease = WriterLease(database.sessions, holder="test-first-writer")
    assert lease.acquire() is True
    try:
        with TestClient(create_app()) as test_client:
            assert test_client.get("/health").json()["read_only"] is True
            yield test_client
    finally:
        lease.release()
        database.dispose()


def _raise_alert(client: TestClient, *, kind: str = "terminal.test") -> int:
    """Put one unacknowledged alert in the backend's own database, and return its id."""

    sessions = client.app.state.backend.runtime.database.sessions
    with sessions.begin() as session:
        alert_module.raise_alert(
            session,
            account_fingerprint=_FINGERPRINT,
            severity=alert_module.AlertSeverity.WARNING,
            kind=kind,
            summary="a condition raised by the test, awaiting the owner",
            now=datetime.now(UTC),
        )
    with sessions.begin() as session:
        pending = alert_module.unacknowledged(session, account_fingerprint=_FINGERPRINT)
    assert pending, "the fixture failed to raise an alert"
    return int(pending[0].id)


# ------------------------------------------------------------------- the token


@pytest.mark.parametrize("path", READ_ROUTES)
def test_every_data_route_requires_the_token(client: TestClient, path: str) -> None:
    assert client.get(path).status_code == 401


def test_mutating_routes_require_the_token(client: TestClient) -> None:
    assert client.post("/terminal/mandate/revoke", json={"reason": "x"}).status_code == 401
    assert client.post("/terminal/alerts/1/acknowledge", json={"note": "x"}).status_code == 401


# ------------------------------------------------------------------- the reads


@pytest.mark.parametrize("path", READ_ROUTES)
def test_every_read_route_answers_on_a_writer(
    client: TestClient, headers: dict[str, str], path: str
) -> None:
    assert client.get(path, headers=headers).status_code == 200


@pytest.mark.parametrize("path", READ_ROUTES)
def test_every_read_route_answers_on_a_read_only_backend(
    read_only_client: TestClient, headers: dict[str, str], path: str
) -> None:
    """Doctrine, not preference: a demoted terminal degrades, it does not go dark."""

    assert read_only_client.get(path, headers=headers).status_code == 200


def test_the_read_only_badge_matches_what_the_writer_gate_will_decide(
    read_only_client: TestClient, headers: dict[str, str]
) -> None:
    body = read_only_client.get("/terminal/system", headers=headers).json()
    assert body["read_only"] is True
    # And the mutating route the badge is describing does in fact refuse.
    refused = read_only_client.post(
        "/terminal/mandate/revoke", json={"reason": "checking the badge"}, headers=headers
    )
    assert refused.status_code == 409


def test_terminal_alerts_answer_where_autonomy_alerts_refuse(
    read_only_client: TestClient, headers: dict[str, str]
) -> None:
    """The divergence from ``GET /autonomy/alerts`` is deliberate (ADR-0018 residual 3).

    If this ever fails because ``/terminal/alerts`` started refusing, the
    terminal has lost the alert feed on exactly the backend whose operator needs
    it; if it fails because ``/autonomy/alerts`` started answering, the older
    route was fixed on purpose and this test should be retired, not patched.
    """

    assert read_only_client.get("/terminal/alerts", headers=headers).status_code == 200
    assert read_only_client.get("/autonomy/alerts", headers=headers).status_code == 409


def test_the_registry_route_serves_the_python_registry(
    client: TestClient, headers: dict[str, str]
) -> None:
    served = client.get("/terminal/commands", headers=headers).json()["commands"]
    assert [entry["code"] for entry in served] == [entry.code for entry in commands.COMMANDS]
    assert served == commands.registry_payload()
    # The client renders these directly, so the payload must survive JSON whole.
    assert json.loads(json.dumps(served)) == served


def test_the_journal_limit_is_clamped_not_rejected(
    client: TestClient, headers: dict[str, str]
) -> None:
    absurd = client.get("/terminal/journal?limit=100000", headers=headers)
    assert absurd.status_code == 200
    assert absurd.json()["limit"] == views.MAX_JOURNAL_LIMIT

    nothing = client.get("/terminal/journal?limit=0", headers=headers)
    assert nothing.status_code == 200
    assert nothing.json()["limit"] == 1

    assert client.get("/terminal/journal", headers=headers).json()["limit"] == (
        views.DEFAULT_JOURNAL_LIMIT
    )


def test_an_absent_counter_row_says_so_rather_than_reporting_zero(
    client: TestClient, headers: dict[str, str]
) -> None:
    """No counters recorded is *no authority established*, never *no losses yet*."""

    body = client.get("/terminal/counters", headers=headers).json()
    assert body["counters_recorded"] is False
    assert body["realized_loss_usd"] is None
    assert body["orders_submitted"] is None


def test_the_system_panel_reports_the_mandate_the_runtime_is_under(
    client: TestClient, headers: dict[str, str]
) -> None:
    body = client.get("/terminal/system", headers=headers).json()
    assert body["autonomy_configured"] is True
    assert body["mandate_id"] == "m-terminal-test"
    assert body["mandate_active"] is True
    assert body["kill_switch_engaged"] is False
    assert body["live_armed"] is False
    assert body["account_fingerprint"] == _FINGERPRINT


def test_a_demoted_backend_still_shows_the_grant_on_disk(
    read_only_client: TestClient, headers: dict[str, str]
) -> None:
    """No runtime here, so the owner's file is the honest fallback.

    It is shown *with* its activation state, which comes from the shared
    database — so a grant this backend never activated reads as one that was
    never activated, rather than as one in force.
    """

    body = read_only_client.get("/terminal/mandate", headers=headers).json()
    assert body["mandate_known"] is True
    assert body["mandate_id"] == "m-terminal-test"
    assert body["activated_at"] is None
    assert body["active"] is False


# --------------------------------------------------------------- acknowledging


def test_acknowledge_records_the_owner_saw_it(client: TestClient, headers: dict[str, str]) -> None:
    alert_id = _raise_alert(client)
    listed = client.get("/terminal/alerts", headers=headers).json()["alerts"]
    assert [entry["id"] for entry in listed] == [alert_id]

    response = client.post(
        f"/terminal/alerts/{alert_id}/acknowledge",
        json={"note": "seen; watching the queue"},
        headers=headers,
    )
    assert response.status_code == 200
    assert response.json()["acknowledged"] is True
    # Acknowledged is not deleted: it leaves the pending set and nothing else.
    assert client.get("/terminal/alerts", headers=headers).json()["alerts"] == []


def test_acknowledging_an_unknown_alert_is_404(client: TestClient, headers: dict[str, str]) -> None:
    response = client.post(
        "/terminal/alerts/9999/acknowledge", json={"note": "no such alert"}, headers=headers
    )
    assert response.status_code == 404


def test_acknowledging_without_a_note_is_refused(
    client: TestClient, headers: dict[str, str]
) -> None:
    """The domain owns this rule; the route only reports it."""

    alert_id = _raise_alert(client)
    response = client.post(
        f"/terminal/alerts/{alert_id}/acknowledge", json={"note": "   "}, headers=headers
    )
    assert response.status_code == 422
    assert "note" in response.json()["detail"]
    # And the alert is still pending, because nothing was written.
    listed = client.get("/terminal/alerts", headers=headers).json()["alerts"]
    assert [entry["id"] for entry in listed] == [alert_id]


def test_acknowledge_is_refused_on_a_read_only_backend(
    read_only_client: TestClient, headers: dict[str, str]
) -> None:
    response = read_only_client.post(
        "/terminal/alerts/1/acknowledge", json={"note": "seen"}, headers=headers
    )
    assert response.status_code == 409


# ------------------------------------------------------------------- revoking


def test_revoking_without_a_reason_is_refused_and_changes_nothing(
    client: TestClient, headers: dict[str, str]
) -> None:
    """An unexplained revocation is not auditable, so it is not accepted."""

    response = client.post("/terminal/mandate/revoke", json={"reason": "  "}, headers=headers)
    assert response.status_code == 422
    assert "reason" in response.json()["detail"]
    assert client.get("/terminal/mandate", headers=headers).json()["revoked"] is False


def test_revoking_withdraws_authority_and_says_what_happened(
    client: TestClient, headers: dict[str, str]
) -> None:
    before = client.get("/terminal/mandate", headers=headers).json()
    assert before["active"] is True and before["revoked"] is False

    response = client.post(
        "/terminal/mandate/revoke",
        json={"reason": "standing the supervisor down for a review"},
        headers=headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["revoked"] is True
    assert body["mandate_id"] == "m-terminal-test"

    after = client.get("/terminal/mandate", headers=headers).json()
    assert after["revoked"] is True
    assert after["active"] is False


def test_revoking_binds_to_the_grant_the_owner_was_shown(
    client: TestClient, headers: dict[str, str]
) -> None:
    """The typed confirmation names a grant, and the recorded act must be that grant.

    ADR-0017 auto-activation makes the divergence real rather than theoretical:
    this backend can restart under an edited mandate while the owner is still
    typing a reason about the one the panel showed them. Revoking the wrong
    mandate would still only remove authority — but the confirmation gate exists
    so that what the owner stated and what the chain records are one event, and a
    409 is how that stays true.
    """

    shown = client.get("/terminal/mandate", headers=headers).json()["mandate_id"]
    assert shown == "m-terminal-test"

    conflicted = client.post(
        "/terminal/mandate/revoke",
        json={"reason": "standing down", "mandate_id": "m-something-else"},
        headers=headers,
    )
    assert conflicted.status_code == 409
    detail = conflicted.json()["detail"]
    assert "m-terminal-test" in detail and "m-something-else" in detail
    # Refused before anything was written: the grant is untouched.
    assert client.get("/terminal/mandate", headers=headers).json()["revoked"] is False

    accepted = client.post(
        "/terminal/mandate/revoke",
        json={"reason": "standing down", "mandate_id": shown},
        headers=headers,
    )
    assert accepted.status_code == 200
    assert accepted.json()["revoked"] is True


def test_revoking_without_naming_a_grant_keeps_working(
    client: TestClient, headers: dict[str, str]
) -> None:
    """The id is a check, not an argument. A caller with no panel still revokes.

    curl, a script, a future CLI — none of them were shown a mandate id, so none
    of them can bind to one, and requiring it would make the only route that can
    stand the supervisor down refuse the callers most likely to be reaching for
    it in a hurry.
    """

    response = client.post(
        "/terminal/mandate/revoke", json={"reason": "no panel here"}, headers=headers
    )
    assert response.status_code == 200
    assert response.json()["revoked"] is True


def test_naming_a_grant_when_none_is_in_force_is_still_not_a_failure(
    client: TestClient, headers: dict[str, str]
) -> None:
    """Nothing is recorded, so there is no act for the name to disagree with.

    The owner asked for authority to be gone and it is gone; answering 409 here
    would report a conflict about a revocation that never happened.
    """

    assert (
        client.post(
            "/terminal/mandate/revoke",
            json={"reason": "first", "mandate_id": "m-terminal-test"},
            headers=headers,
        ).json()["revoked"]
        is True
    )
    again = client.post(
        "/terminal/mandate/revoke",
        json={"reason": "again", "mandate_id": "m-terminal-test"},
        headers=headers,
    )
    assert again.status_code == 200
    assert again.json()["revoked"] is False


def test_revoking_twice_reports_nothing_in_force_rather_than_an_error(
    client: TestClient, headers: dict[str, str]
) -> None:
    """``revoked: false`` is an answer. The owner's intent already held."""

    first = client.post("/terminal/mandate/revoke", json={"reason": "first"}, headers=headers)
    assert first.json()["revoked"] is True
    second = client.post("/terminal/mandate/revoke", json={"reason": "again"}, headers=headers)
    assert second.status_code == 200
    assert second.json()["revoked"] is False


def test_revocation_survives_a_restart(demo_env: Path, headers: dict[str, str]) -> None:
    """ADR-0017: a restart is not permission to undo the owner standing the system down."""

    with TestClient(create_app()) as first:
        assert (
            first.post(
                "/terminal/mandate/revoke", json={"reason": "stand down"}, headers=headers
            ).json()["revoked"]
            is True
        )

    with TestClient(create_app()) as second:
        body = second.get("/terminal/mandate", headers=headers).json()
        assert body["revoked"] is True
        assert body["active"] is False
        # The revoked grant did not bring an autonomy runtime back with it.
        assert second.get("/terminal/system", headers=headers).json()["autonomy_configured"] is (
            False
        )


def test_revoke_is_refused_on_a_read_only_backend(
    read_only_client: TestClient, headers: dict[str, str]
) -> None:
    response = read_only_client.post(
        "/terminal/mandate/revoke", json={"reason": "not mine to revoke"}, headers=headers
    )
    assert response.status_code == 409


# ------------------------------------------------------------------ disclosure


def test_no_response_body_carries_a_broker_account_id(
    client: TestClient, headers: dict[str, str]
) -> None:
    """The pseudonym travels; the identity it stands for never does."""

    _raise_alert(client)
    bodies = [client.get(path, headers=headers).text for path in READ_ROUTES]
    bodies.append(
        client.post(
            "/terminal/mandate/revoke", json={"reason": "disclosure check"}, headers=headers
        ).text
    )
    for body in bodies:
        assert DEMO_ACCOUNT_ID not in body

    system = client.get("/terminal/system", headers=headers).json()
    assert system["account_fingerprint"] == _FINGERPRINT
    assert len(_FINGERPRINT) == 64


def test_the_queue_panel_never_reproduces_a_stored_payload(
    client: TestClient, headers: dict[str, str]
) -> None:
    """Untrusted worker input is described, never echoed into the owner's browser."""

    marker = "PAYLOAD-THAT-MUST-NOT-TRAVEL"
    sessions = client.app.state.backend.runtime.database.sessions
    with sessions.begin() as session:
        proposals.enqueue(
            session,
            account_fingerprint=_FINGERPRINT,
            payload=json.dumps({"note": marker}),
            now=datetime.now(UTC),
        )
    response = client.get("/terminal/queue", headers=headers)
    assert response.status_code == 200
    assert response.json()["pending_depth"] == 1
    assert marker not in response.text


# ----------------------------------------------------------------- the client


def test_the_shell_is_served_same_origin_without_a_token(client: TestClient) -> None:
    """A browser cannot put a header on a document load, so the shell carries none.

    Everything behind it still does — which the token tests above pin, and which
    the module docstring in ``chronos.api.main`` discloses as the reason the
    bundled client currently sees 401 on its panels.
    """

    for path in ("/terminal/app", "/terminal/app/"):
        shell = client.get(path)
        assert shell.status_code == 200
        assert "text/html" in shell.headers["content-type"]
        assert "CHRONOS" in shell.text

    for asset in ("/terminal/static/terminal.css", "/terminal/static/terminal.js"):
        served = client.get(asset)
        assert served.status_code == 200
        assert served.text


def test_no_terminal_path_reflects_the_host_header_into_a_redirect(
    client: TestClient, headers: dict[str, str]
) -> None:
    """The shell is a named file, not a directory that has to be resolved.

    ``StaticFiles(html=True)`` answered ``GET /terminal/app`` with a 307 whose
    ``Location`` was built from the request's own ``Host`` — the path preserved
    and a slash appended, so never an attacker-chosen destination, but ADR-0018
    claims the mount cannot be redirected by user input and that was not true as
    written. Both spellings of the entry point are now routes, and
    ``redirect_slashes`` is off, so nothing under ``/terminal`` answers 3xx at
    all. A URL that does not exist gets a 404, which is what it is.
    """

    hostile = {"Host": "evil.example.com"}
    for path in ("/terminal/app", "/terminal/app/", "/terminal/static", "/terminal/static/"):
        response = client.get(path, headers=hostile, follow_redirects=False)
        assert response.status_code in (200, 404), path
        assert "location" not in response.headers, path

    for path in READ_ROUTES:
        response = client.get(path, headers={**headers, **hostile}, follow_redirects=False)
        assert response.status_code == 200
        assert "location" not in response.headers


def test_every_terminal_response_carries_the_content_security_policy(
    client: TestClient, headers: dict[str, str]
) -> None:
    """The second layer under "injected text may be seen, never act".

    The first layer is the client never assigning markup, which
    ``tests/safety/test_terminal_client_has_no_html_sinks.py`` makes structural.
    This is what makes the *next* ``innerHTML`` inert instead of exploitable —
    which stops being academic the day a browser session credential exists, since
    same-origin script in this page would then be order submission from the
    process holding the broker session.

    Refusals are checked alongside the successes on purpose: a 401 body is still
    a response a browser can be made to render.
    """

    expected = (
        "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
        "img-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    )
    covered = (
        "/terminal/app",
        "/terminal/static/terminal.js",
        "/terminal/static/terminal.css",
        "/terminal/static/nothing-here.js",
        "/terminal/system",
    )
    for path in covered:
        response = client.get(path)
        assert response.headers["content-security-policy"] == expected, path
        assert response.headers["x-content-type-options"] == "nosniff", path

    authorized = client.get("/terminal/system", headers=headers)
    assert authorized.status_code == 200
    assert authorized.headers["content-security-policy"] == expected

    # Scoped to the terminal: the policy describes a document, and /health is not
    # one. If this ever fails because the headers went app-wide, that is a choice
    # to make deliberately rather than a side effect of touching this middleware.
    assert "content-security-policy" not in client.get("/health").headers


def test_the_static_mount_cannot_escape_its_directory(client: TestClient) -> None:
    """Rooted by construction. The encoded form is used because a client would
    otherwise normalize the dot segments away before the server ever saw them."""

    for path in (
        "/terminal/static/%2e%2e/%2e%2e/config/settings.py",
        "/terminal/app/%2e%2e%2f%2e%2e%2fapi/main.py",
    ):
        assert client.get(path).status_code in (400, 404)


def test_the_terminal_routes_do_not_import_the_order_plane(client: TestClient) -> None:
    """The route module names no broker, execution, or order-construction module.

    ADR-0018 §4 expressed as an import graph. ``chronos.runtime`` is imported for
    the ``AppRuntime`` type, and through it the route reads an account id and two
    safety-layer states — but there is no direct import here through which an
    order could be built or sent.
    """

    from chronos.api.routes import terminal as terminal_routes

    source = Path(terminal_routes.__file__).read_text(encoding="utf-8")
    imported: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    forbidden = ("chronos.broker", "chronos.orders", "chronos.execution")
    assert not [name for name in imported if name.startswith(forbidden)]


def test_no_terminal_route_calls_the_order_plane(
    client: TestClient, headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every stage of the order lifecycle is booby-trapped, then the terminal is driven.

    The import test above is structural and this one is behavioural, and both
    are worth keeping: an import can be added indirectly (a helper module, a
    late import inside a function) without the AST scan noticing, whereas
    anything that actually proposed, priced, confirmed, or submitted would come
    through one of these four methods and fail here.

    The account id the routes *do* read is a property, not one of these, so the
    trap does not fire on the one order-plane read the terminal is allowed.
    """

    def _forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a terminal route reached the order plane")

    for name in ("propose", "preview", "confirm", "submit"):
        monkeypatch.setattr(OrderManagementService, name, _forbidden)

    for path in READ_ROUTES:
        assert client.get(path, headers=headers).status_code == 200
    alert_id = _raise_alert(client)
    assert (
        client.post(
            f"/terminal/alerts/{alert_id}/acknowledge", json={"note": "seen"}, headers=headers
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/terminal/mandate/revoke", json={"reason": "no order here"}, headers=headers
        ).status_code
        == 200
    )


# ---------------------------------------------------------------- the session


def test_a_valid_token_buys_a_session_cookie(client: TestClient, headers: dict[str, str]) -> None:
    """The exchange M8b exists to make: a token the operator holds, for a cookie
    a browser can actually send on a document-initiated request."""

    opened = client.post("/terminal/session", json={"token": headers["X-Chronos-Token"]})
    assert opened.status_code == 201
    assert opened.json()["authenticated"] is True
    assert SESSION_COOKIE in opened.cookies

    # And the cookie alone now answers a data route, with no header anywhere.
    assert client.get("/terminal/system").status_code == 200


def test_the_session_cookie_is_scoped_to_the_terminal(
    client: TestClient, headers: dict[str, str]
) -> None:
    """The security property the whole design rests on.

    An ambient credential in this page is only acceptable because the browser
    will not attach it to the order plane. If this scope ever widened to ``/``,
    script injected into the terminal could reach ``/orders/*`` with the browser
    authenticating for it — the M8a injection review's named worst case, and the
    reason ADR-0018's terminal is a window rather than a gateway.
    """

    opened = client.post("/terminal/session", json={"token": headers["X-Chronos-Token"]})
    assert opened.status_code == 201

    cookie = next(c for c in client.cookies.jar if c.name == SESSION_COOKIE)
    assert cookie.path == "/terminal"

    # Set-Cookie carries the flags that make it non-readable and non-cross-site.
    raw = opened.headers["set-cookie"].lower()
    assert "httponly" in raw
    assert "samesite=strict" in raw


def test_the_session_does_not_authenticate_the_order_plane(
    client: TestClient, headers: dict[str, str]
) -> None:
    """Holding a terminal session must not be holding an order-plane credential.

    Asserted at the *server*, not merely by trusting the browser to honour the
    cookie's path: an order route asked with the session and no header still
    refuses, so even a client that sent the cookie everywhere would gain nothing.
    """

    assert (
        client.post("/terminal/session", json={"token": headers["X-Chronos-Token"]}).status_code
        == 201
    )
    assert client.get("/terminal/system").status_code == 200

    for path in ("/orders/intents", "/account/summary", "/live/status"):
        answered = client.get(path)
        assert answered.status_code == 401, f"{path} accepted the terminal session"


def test_a_wrong_token_buys_nothing_and_explains_nothing(client: TestClient) -> None:
    """One way to be wrong, and no detail a prober could use to get warmer."""

    refused = client.post("/terminal/session", json={"token": "not-the-token"})
    assert refused.status_code == 401
    assert SESSION_COOKIE not in refused.cookies
    assert refused.json()["detail"] == "invalid token"
    assert client.get("/terminal/system").status_code == 401


def test_closing_a_session_stops_it_working(client: TestClient, headers: dict[str, str]) -> None:
    """Logging out has to forget the session server-side.

    Clearing the cookie alone would leave a credential this process still
    honoured, which is the difference between signing out and hiding the badge.
    """

    assert (
        client.post("/terminal/session", json={"token": headers["X-Chronos-Token"]}).status_code
        == 201
    )
    assert client.get("/terminal/system").status_code == 200

    closed = client.delete("/terminal/session")
    assert closed.status_code == 200
    assert client.get("/terminal/system").status_code == 401


def test_an_expired_session_is_refused(client: TestClient, headers: dict[str, str]) -> None:
    """The TTL binds even if nothing swept recently."""

    from chronos.api.terminal_session import TerminalSessions

    store = TerminalSessions(ttl=timedelta(seconds=30))
    minted = store.create(now=datetime(2026, 7, 26, 12, 0, tzinfo=UTC))
    assert store.validate(minted, now=datetime(2026, 7, 26, 12, 0, 29, tzinfo=UTC)) is True
    assert store.validate(minted, now=datetime(2026, 7, 26, 12, 0, 31, tzinfo=UTC)) is False


def test_live_sessions_are_bounded(client: TestClient) -> None:
    """A caller who knows the token still cannot grow this process without bound.

    Refusing the new login rather than evicting an old one is deliberate:
    evicting would let anyone holding the token sign the operator out.
    """

    from chronos.api.terminal_session import TerminalSessions

    now = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
    store = TerminalSessions(max_sessions=2)
    first = store.create(now=now)
    store.create(now=now)
    with pytest.raises(HTTPException) as refused:
        store.create(now=now)
    assert refused.value.status_code == 429
    assert store.validate(first, now=now) is True


def test_the_header_still_works_exactly_as_before(
    client: TestClient, headers: dict[str, str]
) -> None:
    """Every non-browser caller — curl, a script, a future CLI — is untouched."""

    assert client.get("/terminal/system", headers=headers).status_code == 200
    assert SESSION_COOKIE not in client.cookies


def test_the_session_grants_no_writer_authority(
    read_only_client: TestClient, demo_env: Path
) -> None:
    """Signing in is not arming. A demoted backend still refuses the mutations."""

    token = load_or_create_token(demo_env / "backend_api_token")
    assert read_only_client.post("/terminal/session", json={"token": token}).status_code == 201
    assert read_only_client.get("/terminal/system").status_code == 200
    refused = read_only_client.post("/terminal/mandate/revoke", json={"reason": "test"})
    assert refused.status_code == 409


# --------------------------------------------------------------- the stop buttons
#
# ADR-0018 §4 grants the terminal "engage and disengage the durable kill switch"
# and "arm and disarm the live session". Until these routes existed the browser
# could do none of it: the M8b session cookie is scoped ``path=/terminal``, so
# the client cannot reach ``POST /live/kill`` — asserted by
# ``test_the_session_does_not_authenticate_the_order_plane`` — and the operator's
# only emergency stop was curl with the raw API token.
#
# Only the authority-REMOVING half is exposed here. That asymmetry is the whole
# design of ``chronos.api.routes.live`` and these tests pin it: engaging the stop
# and dropping the arm must work on a demoted backend, and neither granting
# counterpart may appear under ``/terminal``.


def test_the_terminal_can_engage_the_kill_switch(
    client: TestClient, headers: dict[str, str]
) -> None:
    """The emergency stop is reachable from the surface the operator is looking at."""

    response = client.post(
        "/terminal/live/kill", json={"reason": "smoke: operator stopped it"}, headers=headers
    )
    assert response.status_code == 200
    assert response.json()["engaged"] is True
    assert client.get("/live/status", headers=headers).json()["kill_switch"]["engaged"] is True


def test_engaging_the_kill_switch_requires_a_reason(
    client: TestClient, headers: dict[str, str]
) -> None:
    """An unexplained stop is indistinguishable from a stray request in the audit trail."""

    for body in ({"reason": ""}, {"reason": "   "}):
        refused = client.post("/terminal/live/kill", json=body, headers=headers)
        assert refused.status_code == 422
    assert client.get("/live/status", headers=headers).json()["kill_switch"]["engaged"] is False


def test_the_stop_buttons_work_on_a_demoted_backend(
    read_only_client: TestClient, headers: dict[str, str]
) -> None:
    """The point of the whole asymmetry, and the reason these are not writer-gated.

    A backend that lost the single-writer lease is exactly the backend whose
    operator most needs the stop. ``/terminal/mandate/revoke`` answers 409 here
    (see ``test_the_session_grants_no_writer_authority``); these two must not.
    """

    engaged = read_only_client.post(
        "/terminal/live/kill", json={"reason": "demoted: stop anyway"}, headers=headers
    )
    assert engaged.status_code == 200
    assert engaged.json()["engaged"] is True

    disarmed = read_only_client.post("/terminal/live/disarm", headers=headers)
    assert disarmed.status_code == 200
    assert disarmed.json()["armed"] is False


def test_disarming_an_unarmed_session_is_not_an_error(
    client: TestClient, headers: dict[str, str]
) -> None:
    """The owner asked for the authority to be gone; it already was."""

    response = client.post("/terminal/live/disarm", headers=headers)
    assert response.status_code == 200
    assert response.json()["armed"] is False


def test_the_stop_buttons_still_require_a_credential(client: TestClient) -> None:
    """Not writer-gated is not ungated."""

    assert client.post("/terminal/live/kill", json={"reason": "no token"}).status_code == 401
    assert client.post("/terminal/live/disarm").status_code == 401


def test_the_terminal_offers_no_route_that_grants_live_authority() -> None:
    """Arming and disengaging are absent from ``/terminal`` **on purpose**.

    ADR-0018 §4 permits them, so this is a scoping decision rather than a
    prohibition, and it is recorded as one: putting an authority-GRANTING action
    behind a browser session cookie is a different posture question than putting
    an emergency stop there, and it is the owner's to answer. If that answer ever
    arrives, this test is the thing to delete — deliberately, not by accident.
    """

    from chronos.api.routes.terminal import router

    paths = {route.path for route in router.routes}  # type: ignore[attr-defined]
    assert "/terminal/live/kill" in paths
    assert "/terminal/live/disarm" in paths
    assert "/terminal/live/arm" not in paths
    assert "/terminal/live/kill/disengage" not in paths
