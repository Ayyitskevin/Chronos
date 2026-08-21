# Five-Tool gold pairing identity

Status: **preregistered / blocked before data access**. `gate_advanced: none`.
No paper, live, or promotion authority.

This is the quant overlay for running frozen Five-Tool Confluence AIO v3.6 on
**GLD**. It was frozen before certified gold bars are readable. It does not
edit `research/pine/00_five_tool_confluence_aio.pine` or campaign
`five-tool-v3.6-preregistered-002`.

## What a gold quant actually uses

The host is still Five-Tool on a GLD chart. Regime, Mansfield/RS versus the
benchmark, scores, and the 219-input contract stay the opportunity stream.
Pairing does not replace that engine.

Gold is not an equity-beta sleeve. The overlays a specialist would keep or
drop:

| Overlay | Use on GLD? | Why |
|---|---|---|
| Same-symbol `FAT_TAILED` | Yes | GLD's own return moments; crash/gap days are real |
| Same-symbol daily RVOL In-Play | Yes | Do not enter gold on dead volume |
| Equity VIX `STRESS` / backwardation | No | VIX is S&P fear. Gold often rallies in that weather |
| Equity breadth `ALIGN == -1` | No | RSP/SPY and QQQ/SPY are equity internals |
| Rising USD (UUP) | Treatment only | `usd_regime` vetoes GLD ENTER when UUP slope > 0. Off by default. |
| Real rates (TLT / TIP) | Later | Non-yielding asset; not in this identity |
| "Buy gold when VIX is `STRESS`" | Not default | Flight-to-quality is a different hypothesis |

The locked rule: on primary **GLD**, pairing families `iv_regime` and
`breadth` are inert. QQQ and IWM still take the full equity quartet.

## Campaign

`five-tool-pairing-gld-v1-preregistered-001` is blocked before the first data
read and authorizes **zero** executable trials.

- `H-PAIR-GLD-TAIL` — `FAT_TAILED` on versus off, same Five-Tool GLD stream
- `H-PAIR-GLD-RVOL` — daily In-Play on versus off
- `H-PAIR-GLD-USD` — rising-USD veto from UUP (`usd_slope_lookback=30`,
  frozen). The engine exists and is fixture-tested. Default gold policy
  keeps `enable_usd_regime=False`, so SHADOW GLD OPENs do not require UUP.
  The cell stays non-executable until the owner certifies UUP.
  `require_certified_uup` refuses every open. Chronos does not download
  UUP. UUP is not added to the locked companion set.

Flight-to-quality is not a cell. Inverting equity VIX into a gold entry would
be a new identity.

## Shadow, worker, paper

The reference worker may OPEN GLD when Five-Tool ENTER and the gold pairing
rule ALLOW. That path journals as SHADOW (`admission=not_attempted`,
`transmit=false`). Chronos still does not call a model. Paper and live stay
`none`. Certified overlapping bytes and an owner holdout are still required
before any gold cell can read data.
