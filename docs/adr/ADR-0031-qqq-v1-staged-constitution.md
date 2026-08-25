# ADR-0031 — QQQ v1 staged research and risk constitution

Status: **accepted — owner directive, 2026-08-25.** Index entries: DECISIONS.md D-35,
D-36, D-37, and D-38.

## Context

Phase 0 requires the owner to freeze scope, economics, risk, data budget, and the
evidence sequence before a clean holdout is opened. The owner selected QQQ shares as
the first narrow wedge, including long and short research, while keeping current live
risk at zero. The existing D-12/ADR-0008 executable boundary is daily-bar, long-only,
non-leveraged ETFs. Chronos also currently refuses `SHORT_EQUITY` in the compiler
because it has no deterministic borrow/shortability evidence.

The research rationale is intentionally weaker than a performance claim. Time-series
momentum literature motivates a falsifiable trend hypothesis; it does not validate a
QQQ rule or parameter. Short legs are asymmetric and can crash during sharp rebounds,
so the short side cannot be treated as a mirrored long signal. Volatility management
is likewise a candidate control, not an established benefit for this implementation.
Primary references: Moskowitz, Ooi, and Pedersen, “Time Series Momentum,” *JFE* 104
(2012); Daniel and Moskowitz, “Momentum Crashes,” *JFE* 122 (2016),
<https://doi.org/10.1016/j.jfineco.2015.12.002>; Moreira and Muir, “Volatility
Managed Portfolios,” NBER 22208, <https://doi.org/10.3386/w22208>.

## Decision

The exact machine-readable constitution is
`research/qqq_v1_constitution.json`. Its SHA-256 is `4c99ce9d09f43a418c7342b0e40a0795b253bf3f1cd0e37d29419498b3008d56` and any material
edit creates a new identity and resets the affected strategy to research.

### Scope and sequence

- QQQ is the only execution target. SPY, IWM, DIA, GLD, and TLT are validation-only;
  their inclusion grants no execution authority.
- Decisions use confirmed daily closes and are first eligible for execution in the
  next session. Intraday execution remains out of scope.
- The first campaign is simple long/short trend following. Later, separate campaigns
  may test long/short mean reversion, a regime-combined strategy, and model-selected
  signals, in that order. Each pays its own multiplicity and earns its own evidence;
  implementation or success in one never promotes another.
- The existing burned QQQ window, 2022-01-01 through 2024-01-10, is never clean again.
  No replacement owner holdout is selected or opened by this decision.

### Economics and evidence gate

- Research normalizes to USD 3,000, but current live allocation and live risk remain
  USD 0. Funding toward approximately USD 3,000 is a later owner action.
- Funding review requires an unchanged holdout pass, at least 90 calendar days of
  shadow evidence, supervised-paper evidence, and fresh owner approval. Funding is
  not a mandate, promotion, or authorization to trade.
- The primary benchmark is a volatility-matched QQQ/cash blend; raw QQQ buy-and-hold
  total return is also reported. The exact cash-leg source is still blocking.
- A candidate needs at least four percentage points of annualized post-cost alpha by
  point estimate, while the 95% alpha lower bound must remain above zero. Every
  commission, spread, slippage, funding, borrow, model, and data cost is included.
- Incremental recurring data/software budget is USD 0 per month. If trustworthy data
  cannot certify under that constraint, the campaign remains blocked.

### Frozen risk envelope

At the USD 3,000 research reference capital:

- gross exposure at most 100%; leverage at most 1.0x; QQQ concentration at most 100%;
- peak-to-trough drawdown at most 10%;
- observed daily and session loss at most 2%, or USD 60;
- 95% daily CVaR loss at most 1.5%, or USD 45.

The daily/session control halts new exposure after loss is observed. It cannot promise
that a gap, short squeeze, or execution failure will not exceed USD 60.

### Short side remains unavailable

This decision chooses a target and a research question; it does not supersede the
current executable long-only boundary. Before any executable short can exist, a later
reviewed change must supply compiler support, strategy-level short promotion binding,
point-in-time borrow and shortability evidence, a content-addressed borrow-cost
schedule, verified account eligibility, independent short-side evidence, and owner
approval. Until then `SHORT_EQUITY` remains omitted from every submitting mandate and
its correct runtime outcome is refusal.

## Relationship to existing decisions

- **D-12/ADR-0008 remains the current executable boundary.** This ADR expands the
  research roadmap and names what a future supersession must prove; it does not make
  shorting executable.
- **D-16/ADR-0016 remains unchanged.** The deterministic kernel retains veto authority,
  promotion is independent, and the model cannot authorize itself.
- **D-21(b)/ADR-0025 remains unchanged.** Owner-capped live experimentation still needs
  its full go gate. This decision is stricter in sequence: current live risk is zero and
  the owner chose a holdout/shadow/supervised-paper funding review first.

## Consequences

- The Phase-0 economics and risk ambiguity for the QQQ wedge is narrowed, but Phase 0
  does not exit: exact trend rules, power, cash/borrow cost identities, certified data,
  a clean holdout map, incident availability, and legal/tax/margin review remain open.
- No dataset is read, no trial is registered, no strategy is selected, and no rung is
  advanced by accepting this ADR.
- A correct next result may be `INSUFFICIENT_EVIDENCE`, including indefinitely if daily
  trade counts cannot clear the frozen sample floor.

## Post-acceptance direction-indicator choice (D-36)

On 2026-08-25 the owner selected price relative to a trailing 200-session simple moving
average as the direction indicator for the first QQQ trend preregistration. The observable
will use confirmed daily closes and cannot act before the next session, preserving D-35's
clock boundary.

This records one design input, not a complete or selected strategy. The preregistration
must still freeze the exact price series and window convention, equality behavior, state
transitions, entry and exit rules, persistence or buffer, sizing, short-side asymmetry, and
deterministic parameter neighbors before data access. No market data was read to make this
choice, and no claim is made that SMA-200 improves returns. The choice was based on its
structural compatibility with the Five-Tool Confluence's moving-average direction filters;
that compatibility is a hypothesis to isolate, not evidence.

The content-addressed D-35 constitution is deliberately unchanged: its selected strategy
remains `null`, its trial count remains zero, and all authority remains absent. The complete
preregistration, once owner-approved, will receive its own identity rather than silently
mutating the constitution.

## Post-acceptance transition choice (D-37)

The owner selected an immediate two-state primary cell with no neutral band or confirmation
delay. Once the signal is initialized, the first confirmed daily close strictly across the
SMA-200 changes its direction; any resulting action remains deferred until the next session.
This is a signal transition, not an instruction to take 100% exposure or bypass a gate.

Two alternatives are retained prospectively as robustness variants rather than rescue
choices: a 1% neutral band around SMA-200 and a five-consecutive-close confirmation rule.
Their complete deterministic semantics and identities must be frozen in the same future
preregistration as the primary cell. No result may be used to choose or redefine them.

This decision still leaves the exact adjusted-price/window convention, equality and initial
state, sizing, protective exits, short-side asymmetry, and parameter-neighbor grid open. It
does not amend the D-35 constitution, read data, register a trial, select a strategy, or grant
authority.

## Post-acceptance sizing-method choice (D-38)

The owner selected deterministic volatility scaling whose binding objective is estimated
daily 95% loss-CVaR no greater than 1.5% of the applicable capital base: USD 45 at the
USD 3,000 research reference. Gross exposure remains capped at 100% and leverage at 1x.
Those values are ceilings, not targets, and do not authorize any position.

The exact CVaR estimator, lookback, capital-base convention, rebalance rule, minimum trade
threshold, and short-side treatment remain open. Missing, stale, non-finite, or otherwise
uncertifiable risk evidence cannot produce exposure. Fixed 100%, fixed 50%, and ATR-stop
sizing are not the primary method unless a future, separately identified preregistration
names them as comparisons.

This decision operationalizes D-35's tail-risk objective at the design level; it does not
show that volatility scaling improves performance or prevents gaps from exceeding the cap.
The immutable constitution, trial count, selected-strategy field, and authority remain
unchanged.
