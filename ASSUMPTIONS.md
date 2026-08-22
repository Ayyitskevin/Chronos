# ASSUMPTIONS

Conservative assumptions made while building the Chronos deterministic trading platform.
Each assumption is recorded here because the owner was not available to answer, and the
decision was safe to make conservatively. Anything that changes financial risk defaults to
zero / empty / disabled / deny.

## Corpus

- **A-01 — Corpus size.** The build brief describes "approximately 77 Pine Scripts." The
  authoritative source (Notion → Trading Library → Pine Quant Library — Master Index) catalogs
  **42 script artifacts** (catalog numbers 00–40 plus archived 0A), some published in multiple
  Notion "part" pages. No other Pine sources were found in the Trading Library. We inventory
  what actually exists and record the discrepancy rather than inventing scripts. If the owner
  has ~35 additional scripts elsewhere, they can be added to `research/pine/` and re-audited.
- **A-02 — Notion is the source of truth for Pine sources.** The Master Index version log says
  code pages are "byte-exact with fetch-back verification." We treat the fetched text as the
  canonical source and record SHA-256 hashes at ingestion time in `research/strategy_registry.yaml`.
  TradingView compilation cannot be validated from this environment (no TradingView access);
  compile status is recorded as `UNVERIFIED` unless the Notion page itself documents a compile.
- **A-03 — No TradingView reference exports exist.** The owner has not provided TradingView
  strategy-tester exports (trade lists, indicator series). All parity work is therefore labeled
  "translation verified against specification," never "verified against TradingView." Providing
  exports is an owner action item.

## Account and instruments

- **A-10 — Account type.** *(Amended 2026-08-02.)* As originally written: assumed a small
  IBKR **cash account** (~USD 3,000), no margin, no short selling, long-only equity/ETF
  positions. **The ~USD 3,000 premise is superseded as a statement of fact:** the last
  documented account snapshot is approximately **USD 110**
  (`docs/VISION_COMPLETION_PLAN.md` §2). Whether the account is funded toward the original
  premise, or the scope is cut to match ~USD 110, is a **live, unresolved owner decision**
  (plan §11 owner gates). **Owner direction, 2026-08-02: fund toward the original premise
  rather than descope** — the ≈USD 110 snapshot is a temporary state, not a design
  constraint, so sizing defaults (`MIN_CASH_BUFFER_USD`, the `--equity`/`--cash` CLI
  defaults) stay as they are pending the funded balance. **The exact capital envelope is
  still unfrozen** and remains a Phase 0 deliverable (plan §5: the owner-approved capital
  envelope, loss/drawdown/CVaR and concentration limits must be frozen *before* the
  evidence they judge). Until that number exists, no work may quietly assume any
  particular balance, and any run against the live account must pass its own `--equity`. At ~USD 110, cash-secured options and
  most futures are economically unavailable, and the default `MIN_CASH_BUFFER_USD` of 5000
  alone makes every cash-secured put unaffordable. The rest of the assumption is unchanged
  and still binding: pattern-day-trading rules make intraday strategies impractical below
  USD 25,000 in a margin account; cash-account settlement further constrains turnover.
  Consequence: only **daily-bar, long-only, non-leveraged** strategies are candidates for
  eventual execution; intraday scripts are classified research-only.
- **A-11 — Instruments.** Candidate universe restricted to highly liquid US-listed ETFs
  (SPY, QQQ, IWM, DIA, GLD, TLT) unless the owner approves otherwise. Single-stock candidates
  are research-only until approved.
- **A-12 — Options.** *(Amended 2026-07-25.)* As originally written: the wheel dashboard
  remains decision-support only and the deterministic platform adds no options execution
  path. The second clause still holds — `chronos.execution`/`chronos.risk` have no options
  path and remain live-incapable. The first is superseded: the `chronos.orders` plane gained
  a gated options execution path in Milestones 5-7, and ADR-0016 scopes autonomous options
  trading (long calls/puts, cash-secured puts, covered calls, defined-risk verticals; never
  uncovered short options) to Milestone 8. Options-related corpus scripts (31, 39, 40) are
  still classified as studies/readouts, not executable strategies.

## Costs

- **A-20 — Commission model.** IBKR Lite is not available via API; assumed IBKR Pro tiered/fixed
  US equity commission: USD 0.005/share, minimum USD 1.00 per order, plus regulatory fees.
  Research uses a conservative round-trip cost floor of USD 2.10 per trade plus slippage.
- **A-21 — Slippage.** Baseline slippage assumption for liquid ETFs at daily-bar next-open
  execution: 2 basis points per side, stress-tested at 5/10/25 bps. On a USD 3,000 account a
  fixed USD 1.00 minimum commission is ~3.3 bps per side by itself; this dominates and is
  included in all net results.
- **A-22 — Position sizing for validation.** Research assumes whole-share positions sized from
  account equity (no fractional shares via API by default), which creates meaningful rounding
  drag on a USD 3,000 account. This is modeled.

## Data

- **A-30 — Data source.** No brokerage market data is available in this environment (no
  credentials, no TWS/Gateway). Historical research data is limited to what could be acquired
  and integrity-checked from public dataset mirrors (see `research/data/raw/MANIFEST.json`).
  Data source, retrieval method, hashes, and validation results are recorded per file. Research
  conclusions carry an explicit data-provenance caveat: the owner should re-run research from
  IBKR historical data before any promotion beyond paper.
- **A-31 — Intraday data.** Not reliably obtainable in this environment. Intraday strategies
  (ORB, session VWAP, RVOL screeners) therefore cannot be quantitatively validated here and are
  marked `INSUFFICIENT_INFORMATION` / research-only regardless of their code quality.
  *(Narrowed 2026-08-21, ADR-0029/D-32: the data-plane half of this assumption is retired — an
  hourly ingestion + bar-granular certification path now exists, owner-run against a gateway.
  The strategy-validation half STANDS until a certified hourly release exists and an intraday
  validation plan is written; and D-12's daily-bar executable scope is account economics, not
  data availability, so it does not move with this.)*
- **A-32 — Corporate actions.** Research uses split/dividend-adjusted close series where the
  source provides them, with raw OHLC retained. Signals computed on adjusted series, cost/PnL on
  the same series; this is an approximation documented in the research report.

## Execution environment

- **A-40 — TWS/IB Gateway is owner-operated.** Chronos never automates IBKR login or 2FA. The
  owner must run TWS or IB Gateway locally with the API enabled, paper port 7497 (default).
- **A-41 — ib_async.** The `ib_async` library (community-maintained successor of ib_insync,
  already a dependency of this repo) remains the TWS API client. See ADR-0002.
- **A-42 — Single operator.** The control surface assumes one local operator; no multi-user
  auth is built. The control API binds to localhost only.

## Safety defaults chosen without owner input

*(Restated 2026-07-25. The first line described the pre-Milestone-7 build and is corrected;
the deny-by-default posture behind all of them is unchanged.)*

- Live trading is **disabled by default but no longer impossible**: it is a gated capability
  requiring ADR-0009's full configuration conjunction plus the ten-gate stack, and — for
  autonomous operation — an active owner-authored AutonomyMandate (ADR-0016). The
  deterministic platform (`chronos.execution`) remains hard-refused in code.
- Live account allowlist: empty. Live capital authorization: USD 0. Live risk limits: 0.
  AutonomyMandate limits likewise default to zero and its scopes to empty, so a mandate
  authorizes nothing until the owner enumerates what it permits. *(ADR-0017: an owner may
  enumerate `model_discretion`, making unset capital **ceilings** mean "affordability is the
  bound" — that flag is itself the owner enumerating; silence still grants nothing, and the
  floors are still required.)*
- Autonomy activation is a persistent owner-authored mandate file auto-activated on boot
  (ADR-0017, superseding the SHADOW-default/env-var rule); no file → autonomy inert, an
  invalid or wrong-account file boots inert with a CRITICAL alert, and revocation survives
  restart.
- Short selling, margin, averaging down, pyramiding: disabled. Uncovered short options are
  not expressible in the autonomy strategy vocabulary at all. Market orders exist only as
  the autonomy plane's mandate-granted, **protected** collared-limit form (ADR-0017); an
  unbounded venue market order is unexpressible repository-wide. Options execution exists
  in the `chronos.orders` plane, gated.
- Paper-order transmission also defaults OFF and requires explicit multi-condition opt-in.
- All promotion gates require manual owner action; nothing auto-promotes, and promotion is
  per asset family (a stock promotion authorizes neither futures nor options).
