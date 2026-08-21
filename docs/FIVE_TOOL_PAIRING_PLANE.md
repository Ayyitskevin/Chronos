# Five-Tool pairing feature plane

Status: **research-only**. Pine parity `UNVERIFIED`. No promotion, paper, or live authority.

This sidecar attaches closed-bar pairing features and typed entry vetoes to an
immutable Five-Tool v3.6 opportunity stream. It does not edit
`research/pine/00_five_tool_confluence_aio.pine`, the 219-input contract, or
campaign `five-tool-v3.6-preregistered-002`.

## Sidecar rule

`FiveToolEngine` still emits the only host intents. Pairings may replace
`ENTER_LONG` / `ENTER_SHORT` with `NONE`. Exit intents always pass. ALIGN,
VIX state, RVOL, and tail state are never written into
`FiveToolBarInput.external_regime`.

The pairing opportunity stream is the control replay's traces. Treatment
reuses the Five-Tool fill path with those ENTER intents masked. Treatment
engine traces may differ because Five-Tool sizing and halts read equity;
pairing identity does not use them.

## Families

| Family | Pine source | Daily behavior | Veto |
|---|---|---|---|
| Tail-risk | 32 | Same-symbol moments | `FAT_TAILED` blocks ENTER |
| RVOL | 04 daily / 26 TOD | Daily In-Play is the veto; TOD is inert on `DAY_1` | not In-Play or warmup blocks ENTER |
| IV regime | 31 | Prior-completed VIX; VIX3M optional | `STRESS`, or `ELEVATED` + backwardation; missing VIX fails closed |
| Breadth | 09 | ETF-ratio ALIGN; TICK/ADD/VOLD optional | `ALIGN == -1`; missing RSP/SPY/QQQ fails closed |

`ELEVATED` tail state is recorded only. It does not change size.

## Companions

Unit tests use synthetic fixtures under `tests/fixtures/features/`. There is no
certified VIX/VIX3M/RSP corpus in this repository. The companion catalog and
the certified-intake object in
`research/five_tool_pairing_v1_campaign_manifest.json` stay
`pending_certified_dataset`. The checked intake document is
`research/five_tool_certified_intake_v1.json`. It names the required
overlapping series, freezes the consumed QQQ 2022-01 through 2024-01-10
window, and keeps dataset identities unset. `open_certified_intake` and
`require_certified_companion_dataset` refuse every call, including a forged
digest. This slice does not download market data, open
`CertifiedDatasetCatalog`, or write `HOLDOUTS.json`.

The owner book is locked: **GLD**, **IWM**, and **QQQ**. SPY stays a
companion. QQQM is not tradable. The pairing campaign pins the book digest;
a different set is a new identity.

`usd_regime` is a gold-only treatment: UUP close slope over a frozen
30-session lookback. Rising dollar vetoes GLD ENTER. It is off on the
default policy. `require_certified_uup` refuses owner-byte opens.

On primary **GLD**, pairing families `iv_regime` and `breadth` are inert.
Equity VIX and RSP/SPY–QQQ/SPY ALIGN do not veto gold. Same-symbol tail and
daily RVOL still do. That rule is locked on `FeaturePolicy` before certified
GLD bars are readable. See
[FIVE_TOOL_GOLD_STRATEGY.md](FIVE_TOOL_GOLD_STRATEGY.md). Gold cells live on
blocked campaign `five-tool-pairing-gld-v1-preregistered-001`.

## Campaign

`five-tool-pairing-v1-preregistered-001` is blocked before the first data read
and authorizes **zero** executable trials. Hypotheses: `H-PAIR-TAIL`,
`H-PAIR-RVOL`, `H-PAIR-VIX`, `H-PAIR-BREADTH`. See
[FIVE_TOOL_PAIRING_HYPOTHESES.md](FIVE_TOOL_PAIRING_HYPOTHESES.md).

## Shadow learning loop

Closed-bar Five-Tool traces, pairing snapshots, and vetoes can be projected
into an `EvidenceBundle` as **advisory** facts (`bundle_version` `1.1`). A
deterministic reference worker outside the order plane emits `HOLD` unless
exactly one bar is Five-Tool `ENTER_*` and pairing `ALLOW`. The same ingress
parses that JSON; the shadow journal records HOLD and refused or accepted OPEN
as **not sent**. Admission is not attempted. This does not loosen `min_score`
or pairing vetoes.

More symbols, more sessions, or a Neutral-only Kalman sleeve are later
widening choices. Fewer gates are not. Promotion still binds the Five-Tool
contract digest, pairing policy digest, companion catalog identity, worker
pins, and bundle version. See
[FIVE_TOOL_SHADOW_LEARNING.md](FIVE_TOOL_SHADOW_LEARNING.md).
