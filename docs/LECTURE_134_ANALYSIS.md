# Lecture 134: what Chronos is modeled after, and what it deliberately is not

Source analyzed: Quant Guild, *"How to Build an AI Stock Trading Bot with Interactive
Brokers"* (Lecture 134), from the owner-provided repository
`romanmichaelpaolucci/Quant-Guild-Library`. Video: `youtube.com/watch?v=ogZmSXD_56U`.

The owner's direction: **Chronos is to be modeled after this.** This document records
what that means precisely, because the answer has two halves that must not be blurred:
Chronos adopts Lecture 134's *experience* and rejects its *mechanics*. The original
ADR-0016 directive was written with exactly this project as its named reference — its
"do not reproduce the reference project's unsafe details" list corresponds line by line
to what this codebase actually does.

One note on method: the reference repository contains a committed `.env` file which
appears to hold a live API credential. Per the directive it was **not opened, read, or
copied** during this analysis, and nothing from it appears here. If you clone that repo
locally: do not use or reuse anything in that file.

---

## 1. What Lecture 134 is

A Flask application (port 5050) serving a terminal-style UI, with an OpenAI-driven
trading agent operating an Interactive Brokers account through TWS.

**The experience** (this is the part worth wanting):

- A **terminal dashboard**: holdings, live metrics, account status, per-holding detail.
- A **chat surface** where the operator talks to the agent about the portfolio, and the
  agent answers from live account and market data.
- **Per-holding theses**: narrative, target, cost basis, conviction — saved and shown
  alongside the position.
- **Portfolio objectives** the operator edits: max drawdown, max single-name weight,
  cash floor, sector restrictions.
- An **auto-review sweep**: on a schedule (~5 minutes), the agent reviews the whole
  portfolio against the objectives and theses, and acts.
- **Compressed chat memory / a "brain"**: dated JSON state carrying conversation
  summaries and decisions across sessions.
- **The model is the decision-maker.** Its system prompt: *"You are the decision-maker.
  The UX chat user is an observer — do NOT ask them to approve target changes, trims, or
  exits. Decide and use tools."* And: *"Prefer executing trades over merely recommending
  them."*
- **A standing autonomy policy** (`brain/policy.py`) — a priority-ordered decision tree
  the agent applies to every holding on every sweep:
  - **Path R** (risk/mandate): trim overweight names to `max_single_name`, restore the
    cash floor, exit avoid-list names — risk overrides thesis.
  - **Path B** (target hit): thesis played out → full liquidation.
  - **Path C** (thesis invalidated): full exit.
  - **Path A** (thesis valid, upside remains): hold, re-target above spot, optionally add.
  - **Path N** (deployable cash): *invent one or two new tickers* fitting the
    objectives, write a thesis, size, and execute.
  - **Path D**: nothing material — do nothing.

  This policy tree is the best thing in the reference, and it maps almost exactly onto
  Chronos's `DecisionKind` vocabulary: R → REDUCE, B/C → CLOSE, A → HOLD/INCREASE,
  N → OPEN, D → HOLD. It is the *worker-side portfolio policy* Chronos's external model
  worker should implement — expressed there as prompt prose, expressible here as typed
  proposals judged by the gateway.

This is the target experience, and it is genuinely good product design: a portfolio
with a resident analyst who watches it, explains itself, and acts under stated
objectives.

## 2. How Lecture 134 works, mechanically

- **One process, one registry.** The Flask process holds the IB connection. The agent's
  17 tools live in a single registry mixing reads (`get_market_snapshot`,
  `get_positions`, `get_historical_news`), memory writes (`save_thesis`,
  `record_trade`) and **direct broker writes** (`place_equity_order`, `cancel_order`).
  The dispatch that answers "what is SPY trading at" is the dispatch that places an
  order.
- **The model authorizes itself.** `place_equity_order(confirm: bool = False)` looks
  like a safety gate, but the *model* supplies `confirm`, and its prompt instructs:
  *"always pass confirm=true when you intend to trade — do not wait for the user."* The
  gate's key is taped to the gate.
- **Market orders by default.** `order_type` defaults to `MKT`; the prompt's lifecycle
  paths route through it. No price protection unless the model chooses `LMT`.
- **Advisory sizing.** `propose_position_size` computes a limit-respecting size
  (max_single_name, cash floor, stop-based risk budget) — but nothing *binds* it.
  `place_equity_order` never calls it; the model is told to consult it and may pass any
  quantity it likes.
- **JSON files as authoritative storage.** `holdings.json`, `theses.json`, `goals.json`,
  chat memory — unlocked, unversioned, unhashed; any process (or the agent, or a crash
  mid-write) can corrupt the record.
- **The scheduler runs the same authority.** The auto-sweep invokes the same agent with
  the same tools and the same self-supplied `confirm=true` — unattended, every few
  minutes, with the env flags (`IB_ALLOW_ORDERS`, `IB_READONLY`) as the only gate
  between a prompt and `placeOrder`.
- **Real, but thin, guards exist**: env-flag order blocking, same-symbol/side working
  order dedup, a buy-side notional cap, 2-second order pacing, an 8-second
  acknowledgment wait. These are the guards of a demo, not of an unattended system —
  none of them survives a wrong model output that stays under the notional cap.
- **The sweep scheduler** is a ~1-second daemon thread; when the UI's AUTO toggle is on
  and the chosen interval (minutes/hours/days) has elapsed, it runs a full agent sweep
  against the live book. The committed demo history shows the payoff: a scheduled sweep
  autonomously executing a full take-profit liquidation at a thesis target, unprompted.
- **Fair credit where due:** out of the box the env defaults are research-only
  (`IB_READONLY=true`, `IB_ALLOW_ORDERS=false`), order placement has a soft notional
  cap, duplicate same-side working orders are refused, and orders are paced ~2s apart.
  The instinct toward gating is present; the gates are just thin, self-confirmed, and
  all standing in one process with the model.
- **A `.env` is committed to the public repository**, apparently containing a live API
  credential.

None of this is a criticism of the lecture — it is teaching material, built to show the
loop working end to end in one sitting, and it does that well. But the directive's
prohibition list is this codebase, item for item:

| Directive prohibition | Lecture 134 reality |
|---|---|
| no raw broker object in the model process | the agent's tools run in the Flask process holding `ib` |
| no mixed registry of read tools and IBKR write functions | one `_HANDLERS` dict, reads and `place_equity_order` together |
| no model-supplied `confirm=true` | prompt: *"always pass confirm=true"* |
| no unrestricted market-order preference | `order_type` defaults to `MKT` |
| no unlocked JSON files as authoritative storage | `holdings.json` / `theses.json` / `goals.json` |
| no scheduler bypass of risk/mandate checks | auto-sweep = same agent, same tools, no extra gate |
| no committing reference secrets / `.env` | a `.env` sits in the public repo |

## 3. The mapping: every Lecture 134 capability, the Chronos way

Chronos has been building the same experience on a different spine. The
correspondence, piece by piece:

| Lecture 134 | Chronos equivalent | Difference that matters |
|---|---|---|
| "You are the decision-maker" prompt authority | `AutonomyMandate` + `ModelDecisionGateway` | authority is an owner-signed, expiring, revocable document — not a sentence in a prompt |
| `confirm=true` supplied by the model | admission → sizing → compilation → the order plane's ten-gate stack | the model cannot supply any part of its own authorization; there is no field for it |
| autonomy policy paths R/B/C/A/N/D (prompt prose) | `DecisionKind` vocabulary + worker-side policy | the policy tree becomes typed proposals; each path's action is judged by the gateway instead of self-executed |
| `goals.json` objectives | `AutonomyMandate` limits (capital, loss, activity, concentration, market-data floors) | every limit is enforced or explicitly classified inert, pinned by tests; a breach stops new exposure while keeping positions closable |
| `propose_position_size` (advisory) | `size_order` (binding) | the kernel's number is the only number; a model request is an upper bound |
| `order_type="MKT"` default | `OrderForm` has no `MARKET` member | market orders are unrepresentable, not discouraged |
| mixed tool registry | `ToolKind = {READ, DECISION}` | a write tool cannot be expressed; the registry freezes at startup |
| agent in the broker process | external worker → hardened ingress → durable queue → tick | the model process holds no broker handle, no key, no submission path |
| 5-minute auto-sweep scheduler | the M7 tick (time-driven, events coalesce) | the sweep cannot bypass any gate, because it *is* the gated path; cadence is config, not caller-driven |
| JSON brain | schema-versioned SQLite, WAL + `synchronous=FULL`, hash-chained append-only streams | the record survives crashes and detects tampering |
| chat memory / compressed summaries | decision journal + theses on the decision contract (`thesis`, `rationale`, `invalidation_conditions`, `confidence`) | narrative is recorded and displayed but structurally cannot become an order parameter |
| env flags as the live gate | mode ladder + per-family promotion + activation + arming + kill switch + drawdown breaker + writer lease | an environment variable alone can never activate live autonomy |
| holdings/metrics/chat UI | **not yet built for autonomy** | this is the genuine gap — see §4 |

The one-sentence version: **Lecture 134 trusts the model and decorates it with checks;
Chronos distrusts the model and routes it through a kernel.** Both produce the same
owner experience when everything goes right. They differ in what happens the first time
the model is wrong, prompted maliciously, or simply down.

## 4. What Chronos still owes the Lecture 134 experience

Honest gap list, since "modeled after" cuts both ways. The safety spine is ahead of the
reference; the *experience layer* is behind it:

1. **The terminal.** Lecture 134's dashboard shows holdings, metrics, theses, agent
   status, and chat in one place. Chronos has a Streamlit operational dashboard for the
   deterministic platform, but no autonomy surface: nothing yet renders the decision
   journal, cycle outcomes, mandate state, session counters, or alerts as a product.
2. **The chat.** There is no conversational surface. In Chronos's architecture this
   belongs in the *worker* (the owner's AI workspace — Athena/Icarus/Minerva — is
   exactly this), talking to Chronos through the evidence/proposal contracts. The
   experience is achievable without moving the model into the broker process — the
   worker chats, and Chronos remains the thing that judges.
3. **Theses as a first-class display.** The decision contract carries thesis, rationale,
   uncertainties, and invalidation conditions, and the journal records them — but no
   view presents "here is what the system believes about each holding and why."
4. **The auto-review sweep as a *product* behavior.** The tick exists; what does not
   exist is the packaged loop where each tick issues an evidence bundle to the worker,
   the worker reviews the portfolio against the mandate, and the outcome lands in the
   journal and on the dashboard. The plumbing is all present; the loop that walks it
   end to end with a real worker is not.
5. **Objectives editing.** Lecture 134 lets the operator edit goals in the UI. The
   Chronos equivalent — authoring and activating a mandate — currently requires code.
   An owner surface for author → review → activate (with the activation event recorded)
   is missing.

These, together, are the natural next milestone: the experience layer, built on the
existing spine, with the model worker remaining external.

## 5. What will not be carried over, restated

For the record, because "modeled after" must not erode these:

- No prompt-granted authority. The mandate is the only authority, and it expires.
- No model-supplied confirmation of anything, ever.
- No market orders. No naked shorts. No spreads-by-legs. The capability matrix is a
  whitelist.
- No broker handle, credential, or submission path in any process a model runs in.
- No JSON file as an authoritative record. The journal is hash-chained SQLite.
- No scheduler that bypasses a gate. The tick *is* the gate walk.
- No secrets in the repository. The reference's committed `.env` was not read, and
  Chronos's own `.env.example` carries no live values.

*Analyzed 2026-07-25 from the owner-linked source (repository read in full; the
committed demo chat history is the video's actual session, dated 2026-07-23). The video
itself could not be played from this environment, so the t≈50:54 deep-link is inference:
given the demo arc, it most plausibly lands in the finished-system payoff — the
chat-driven full-book rebalance and/or the scheduled sweep executing autonomously.
Everything architectural above is confirmed from code, not inferred.*
