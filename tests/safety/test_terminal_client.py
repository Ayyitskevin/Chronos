"""The browser client's honesty rules, executed rather than read (M8a, ADR-0018).

``chronos.terminal.views`` decides what the terminal *may say*; these tests
cover the other half — what it actually draws once it has been told. The client
is the surface an operator reads before deciding whether to intervene, so its
failures are the same two the read-models guard against, arriving one layer
later:

- **Fabrication.** A cached kill switch outliving a backend outage, a frozen
  status bar that still says BACKEND OK, a null critical count rendered as a
  measured zero, "quantity: not sized" printed under a record whose payload
  could not be read at all. Every one of these is a positive claim the client is
  not in a position to make, and every one of them reads as *calmer* than the
  truth.
- **Offering an action the backend will refuse.** An enabled REVOKE MANDATE
  before the client knows whether this backend holds the writer lease, a note
  field labelled optional that the domain requires, a revocation that names one
  grant and could withdraw another.

## Why these run the file instead of reading it

``terminal.js`` has no bundler, no framework and no exports (ADR-0018 §6). The
cheap alternative is to assert on its source text, which pins spelling rather
than behaviour: a ``noteRequired: true`` nothing reads would pass. So
``tests/support/terminal_harness.js`` loads the real file into a ``node:vm``
context with a small fake DOM and a ``fetch`` the scenario drives, runs one named
scenario, and prints JSON. The assertions stay here, in Python, where a failure
reads as a failed claim about a value.

## Honest residual

The harness is not a browser. It has no layout, no CSS and no real event
dispatch, and its ``window.setInterval`` deliberately never fires — so these
tests pin logic and rendered text, never appearance. They also need ``node`` on
PATH. Rather than skip silently where it matters most, a missing interpreter is
a *failure* under CI and a skip on a developer machine: a pin that quietly stops
running is the same class of problem as a panel that quietly stops refreshing.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

_HARNESS = Path(__file__).resolve().parents[1] / "support" / "terminal_harness.js"
_CLIENT = (
    Path(__file__).resolve().parents[2] / "src" / "chronos" / "terminal" / "static" / "terminal.js"
)


def _node() -> str:
    """The interpreter, or a decision about what its absence means."""

    found = shutil.which("node")
    if found:
        return found
    if os.environ.get("CI"):
        raise AssertionError(
            "node is not on PATH, so the terminal client's behaviour was not checked at all; "
            "under CI that is a failure rather than a skip"
        )
    return pytest.skip("node is not installed, so the browser client cannot be executed")


def _scenario(name: str) -> Any:
    """Run one harness scenario and return what it observed."""

    completed = subprocess.run(
        [_node(), str(_HARNESS), str(_CLIENT), name],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, f"scenario {name} failed:\n{completed.stderr}"
    return json.loads(completed.stdout)


def test_the_client_is_syntactically_valid() -> None:
    """``node --check`` on the real file, so a broken client fails here first.

    Every test below loads the same file, so a syntax error would surface as
    fifteen confusing failures instead of one plain one.
    """

    completed = subprocess.run(
        [_node(), "--check", str(_CLIENT)], capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, completed.stderr


# -------------------------------------------------------------- the status bar


def test_authority_badges_do_not_survive_a_failed_poll() -> None:
    """A cached ``kill_switch_engaged: false`` may not keep the badge hidden.

    The disengaged and disarmed renderings are *absent* badges, and absence is
    how this page says "clear". So a status object left over from before an
    outage would go on asserting the safest possible reading of the two facts
    that matter most, for as long as the outage lasted. Unobserved must render
    as the loud dashed unknown instead — never hidden.
    """

    seen = _scenario("cached_badges_do_not_outlive_an_outage")

    live = seen["live"]
    assert live["kill"]["hidden"] is True
    assert live["armed"]["hidden"] is True
    assert live["role"]["text"] == "WRITER"
    assert live["autonomy"]["text"] == "AUTONOMY RUNNING"

    for state in ("afterFailure", "afterUnavailable"):
        bar = seen[state]
        assert bar["kill"]["hidden"] is False, state
        assert bar["kill"]["text"] == "KILL SWITCH UNKNOWN"
        assert "badge-unknown" in bar["kill"]["className"]
        assert bar["armed"]["hidden"] is False
        assert bar["armed"]["text"] == "ARM STATE UNKNOWN"
        assert bar["role"]["text"] == "ROLE UNKNOWN"
        assert bar["autonomy"]["text"] == "AUTONOMY UNKNOWN"
        assert bar["alerts"]["text"] == "ALERTS UNKNOWN"


def test_a_failing_poll_is_what_demotes_the_bar() -> None:
    """The two tests around this one hand ``drawStatus`` a status and check what
    it draws. That leaves the interesting half unpinned: nothing yet proves the
    *poll* is what sets that status. A ``pollSystem`` that quietly stopped
    demoting on failure would keep every one of those assertions green while the
    page went back to reporting a cached kill switch as clear — the exact defect
    the review found, passing its own regression test.

    So this drives the real thing: a real answer, then a real rejection, and no
    status is named anywhere in the scenario. What is under test is the path from
    "a read failed" to "the page stops claiming to know", which is the only part
    an operator's safety actually rests on.
    """

    seen = _scenario("a_failing_poll_demotes_the_bar_without_being_told")

    live = seen["live"]
    assert live["status"] == "ok"
    assert live["bar"]["kill"]["hidden"] is True
    assert live["bar"]["armed"]["hidden"] is True

    after = seen["afterFailure"]
    assert after["status"] == "stale"
    assert after["bar"]["kill"]["hidden"] is False
    assert after["bar"]["kill"]["text"] == "KILL SWITCH UNKNOWN"
    assert after["bar"]["armed"]["hidden"] is False
    assert after["bar"]["armed"]["text"] == "ARM STATE UNKNOWN"
    assert after["bar"]["role"]["text"] == "ROLE UNKNOWN"

    # The cached reading is still in memory, and that is deliberate — it is what
    # lets a recovered backend redraw at once. The fix is not that the cache was
    # thrown away; it is that holding it no longer lets a stale ``false`` be
    # rendered as an observation.
    assert seen["cacheRetained"] is True


def test_the_status_bar_ages_out_like_a_panel() -> None:
    """A poll that is never answered must not leave the bar looking live.

    ``fetch`` has no timeout of its own, so a request the backend accepts and
    drops suspends ``pollSystem`` forever with no failure to record. The clock
    keeps ticking beside a role, an autonomy state and two authority badges from
    whenever the last answer arrived. The panels already demote on age; the bar
    now reuses the same rule.
    """

    seen = _scenario("the_status_bar_ages_out")

    assert seen["fresh"]["status"] == "ok"
    assert seen["fresh"]["bar"]["kill"]["hidden"] is True

    aged = seen["aged"]
    assert aged["status"] == "stale"
    assert "no successful status read in" in aged["error"]
    assert aged["bar"]["conn"]["text"] == "● BACKEND STALE"
    assert aged["bar"]["kill"]["hidden"] is False
    assert aged["bar"]["role"]["text"] == "ROLE UNKNOWN"


def test_reads_are_bounded_and_owner_actions_are_not() -> None:
    """The asymmetry is deliberate, and it is the honest way round.

    A read that hangs is a panel frozen while it still looks live, so reads
    carry an ``AbortSignal``. Aborting a POST abandons only the browser's half of
    it: the backend may well have applied the revocation, so reporting it as
    refused would be a claim about durable state this client cannot make.
    """

    seen = _scenario("reads_are_bounded_by_a_timeout")

    assert seen["timeoutMs"] > 0
    assert seen["readHasSignal"] is True
    assert seen["readSignalAborts"] is True
    assert seen["writeHasSignal"] is False


# ------------------------------------------------------------------ ordering


def test_a_slow_status_answer_never_overwrites_a_newer_one() -> None:
    """Requests are not serialized, so the answers must be sequenced.

    The interval fires without waiting, ``refreshAll`` fires on every return to
    the tab and after every accepted owner action, and a read may take the whole
    timeout. Without a token the slower of two in-flight polls wins and restamps
    ``systemAt``, which is old authority wearing a fresh timestamp.
    """

    seen = _scenario("a_slow_status_answer_is_discarded")

    assert seen["requests"] == 2
    assert seen["afterNewer"] == "NEWER"
    assert seen["afterOlder"] == "NEWER"


def test_a_slow_panel_answer_never_overwrites_a_newer_one() -> None:
    """The same rule per panel, including the freshness stamp it would restamp."""

    seen = _scenario("a_slow_panel_answer_is_discarded")

    assert seen["requests"] == 2
    assert seen["afterNewer"] == "NEWER"
    assert seen["afterOlder"] == "NEWER"
    assert seen["stampMoved"] is False


# -------------------------------------------------------------- owner actions


def test_no_owner_action_is_offered_before_authority_is_observed() -> None:
    """Loading and stale are both "unknown", and unknown does not enable a button.

    ``boot`` fires the status poll and the registry load concurrently, and
    ``/terminal/commands`` touches no database while ``/terminal/system`` opens a
    session — so the registry routinely wins and the restored mandate panel
    mounts before this client knows whether the backend is read-only. The button
    still renders, disabled, with the reason on it: a button that vanishes
    teaches nothing.
    """

    seen = _scenario("actions_wait_for_observed_authority")

    assert seen["booting"] == seen["copy"]["statusUnknown"]
    assert seen["stale"] == seen["copy"]["statusUnknown"]
    assert seen["readOnly"] == seen["copy"]["readOnly"]
    assert seen["writer"] == ""

    assert seen["button"]["text"] == "REVOKE MANDATE"
    assert seen["button"]["disabled"] is True
    assert seen["button"]["title"] == seen["copy"]["statusUnknown"]
    assert seen["copy"]["statusUnknown"] in seen["hostText"]


def test_the_acknowledge_form_requires_the_note_the_domain_requires() -> None:
    """One rule, one place. The client used to declare it a second time, wrongly.

    ``chronos.supervisor.alerts.acknowledge`` raises on an empty note and the
    route turns that into a 422. A field labelled optional invites the operator
    into exactly that refusal, for doing what the label told them to.
    """

    seen = _scenario("acknowledging_requires_a_note")

    assert seen["labels"][0] == "note (required, recorded with the acknowledgement)"
    assert "optional" not in " ".join(seen["labels"])
    assert seen["withoutNote"] is True, "the confirm button was enabled with an empty note"
    assert seen["withNote"] is False


def test_nothing_was_in_force_is_not_reported_as_a_revocation() -> None:
    """``revoked: false`` is a 200 by design, and it is not a success.

    The route calls it "an answer, not an error" — but the owner asked for a
    grant to be withdrawn and nothing was withdrawn. Keying the outcome on HTTP
    status alone made the one case the route designed a distinct answer for the
    one case the client reported as if it had worked.

    The same scenario pins the mandate id travelling with the request: the typed
    confirmation names a grant, and the request must bind to the grant it named.
    """

    seen = _scenario("nothing_in_force_is_not_a_success")

    assert seen["body"] == {"reason": "standing down", "mandate_id": "M-1"}
    assert "M-1" in seen["why"]
    assert seen["resultClass"] == "confirm-result bad"
    assert "nothing changed" in seen["resultText"]
    assert "accepted" not in seen["resultText"]


def test_a_conflicting_grant_reaches_the_operator_in_the_backends_words() -> None:
    """A backend can restart under an edited grant while the owner types a reason.

    ADR-0017 auto-activation makes that a real sequence, not a hypothetical: the
    form says M-1, the grant in force is M-2, and the chain would have recorded
    the revocation of a mandate the owner never named. The id is sent so the
    backend can refuse, and the refusal is shown rather than swallowed.

    The surfacing half of this already worked — ``errorText`` carries a JSON
    ``detail`` behind the status — so this test would have passed before the
    change. It is kept because what made the 409 *reachable* is new: without the
    id in the body there is nothing for the route to compare, and a path that
    can now be taken deserves an assertion that it lands somewhere legible.
    """

    seen = _scenario("a_conflicting_grant_is_reported")

    assert seen["className"] == "confirm-result bad"
    assert "409" in seen["text"]
    assert "the grant in force is M-2" in seen["text"]


def test_an_accepted_action_repeats_the_routes_own_detail() -> None:
    """What is still allowed to finish is the route's statement, not this file's.

    ``RevokeResult.detail`` says a cycle already in flight completes;
    ``AcknowledgeResult.detail`` says the condition is unchanged and may recur.
    Discarding either and printing "accepted by the backend" leaves the operator
    to assume the more complete outcome.
    """

    seen = _scenario("an_accepted_action_carries_the_route_detail")

    assert seen["revoke"]["className"] == "confirm-result ok"
    assert "a cycle already in flight finishes" in seen["revoke"]["text"]
    assert seen["acknowledge"]["className"] == "confirm-result ok"
    assert "may raise it again" in seen["acknowledge"]["text"]


# ----------------------------------------------------------------- rendering


def test_an_undecodable_record_asserts_nothing_about_what_the_cycle_did() -> None:
    """ "not sized" is a claim about the cycle; a damaged payload cannot support it.

    ``_journal_entry`` returns ``quantity: None`` for a decode failure *because
    nothing could be read*, and the client cannot tell that from a cycle that
    stopped before sizing. Printing the confident sentence under a record it has
    just chipped PAYLOAD UNDECODABLE is the panel contradicting itself.
    """

    seen = _scenario("an_undecodable_entry_claims_nothing")

    damaged = seen["damaged"]
    assert "PAYLOAD UNDECODABLE" in damaged["text"]
    assert "not sized" not in damaged["text"]
    assert "not compiled" not in damaged["text"]
    assert damaged["unknowns"] == ["quantity and limit: unknown — the payload would not decode"]

    # A decoded cycle that genuinely stopped before sizing still says so.
    unsized = seen["unsized"]
    assert "quantity: not sized · limit: not compiled" in unsized["text"]
    assert unsized["unknowns"] == []


def test_an_unscoped_journal_does_not_claim_the_account_has_no_cycles() -> None:
    """``stream_present: false`` carries two different facts; only one is "none".

    ``journal_view`` answers false both for an account whose stream is empty and
    for a backend bound to no account, which could not look. The queue and
    alerts panels already branch on ``account_fingerprint`` for this reason; the
    journal now does too, and states its scope like its siblings.
    """

    seen = _scenario("an_unscoped_journal_says_scope_unknown")

    assert seen["unscoped"]["titles"] == ["SCOPE UNKNOWN"]
    assert "not bound to an account" in seen["unscoped"]["text"]
    assert "NO CYCLES RECORDED" not in seen["unscoped"]["text"]
    assert seen["scoped"]["titles"] == ["NO CYCLES RECORDED"]


def test_the_alert_badge_separates_an_unknown_count_from_a_measured_zero() -> None:
    """A null critical count is what an unscoped backend answers, not a quiet one.

    ``alertCountNode`` says "critical count unknown" in the panel. A status bar
    that silently dropped the suffix left the two surfaces disagreeing about the
    same field, with the quieter one wrong.
    """

    seen = _scenario("the_alert_badge_separates_unknown_from_zero")

    assert seen["quiet"] == {"text": "0 ALERTS", "className": "st st-dim"}
    assert seen["pendingOnly"]["text"] == "3 ALERTS"
    assert seen["critical"] == {"text": "2 ALERTS · 1 CRITICAL", "className": "st st-bad"}

    for case in ("criticalUnknown", "quietButUnknown"):
        assert "CRITICAL UNKNOWN" in seen[case]["text"], case
        assert seen[case]["className"] == "st st-warn", case
    assert seen["allUnknown"]["text"] == "ALERTS UNKNOWN"


def test_equity_that_was_never_observed_renders_as_unknown_not_as_no_drawdown() -> None:
    """A zero drawdown is the most reassuring number on the panel. It must be earned.

    ``CountersView.equity_observed`` is false when a counter row exists — orders
    counted, turnover accumulated — but no equity snapshot was ever taken, in
    which case peak, trough and both drawdowns are null. Rendering them as the
    bare word "unknown" beside eight measured figures under-states it; the label
    names the reason.
    """

    seen = _scenario("equity_that_was_never_observed_is_unknown")

    unobserved = seen["unobserved"]
    assert len(unobserved["unknowns"]) == 4
    assert set(unobserved["unknowns"]) == {"unknown — no equity was observed this session"}
    assert "No equity snapshot was taken this session" in unobserved["text"]
    assert "unknown, not zero" in unobserved["text"]

    observed = seen["observed"]
    assert observed["unknowns"] == []
    assert "no equity" not in observed["text"]


# -------------------------------------------------------- the registry's reach


def test_a_server_supplied_panel_name_cannot_reach_object_prototype() -> None:
    """The panel id is registry text, and registry text may be wrong.

    ``PANELS[name]`` answers ``constructor``, ``toString`` and friends with an
    inherited ``Object.prototype`` member. That passes the ``if (!spec)`` guard
    and mounts a panel whose ``endpoint`` is undefined: a permanent LOADING tile
    throwing an unhandled rejection every five seconds. Own-property lookup
    refuses the name and says so.
    """

    seen = _scenario("an_inherited_panel_name_is_refused")

    for name in ("constructor", "toString", "__proto__", "hasOwnProperty"):
        assert seen[name]["panels"] == 0, f"{name} mounted a panel"
        assert f'panel "{name}", which this client cannot draw' in seen[name]["message"]

    # And a real panel still opens, so the guard is a filter and not a wall.
    assert seen["real"]["panels"] == 1
