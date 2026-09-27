"""Which economic-looking strategy and baseline parameters anything acts on — as data.

:mod:`chronos.autonomy.enforcement` answers this for the owner mandate. This
module answers it for the research lane: the parameter blocks of the two
candidate strategies (:class:`~chronos.strategies.mean_reversion.MeanReversionParams`,
:class:`~chronos.strategies.regime_trend.RegimeTrendParams`) and the three
baseline constructors in :mod:`chronos.strategies.baselines`.

**This module enforces nothing.** It records, per parameter block, whether the
strategy module that owns the block reads each field today.

``ENFORCED``
    Read by the owning strategy module — the value shapes what the strategy
    proposes (an entry threshold, an exit, a stop multiple, the exposure
    fraction a proposal carries).
``INERT``
    A value there changes no proposal. No field is INERT today; the label
    exists so a future field that nothing reads can be disclosed instead of
    silently riding along, and so the pin has both directions to guard.

Every classification carries its reader as a ``file:line`` string at the head
— the claim is checkable, not narrative.
``tests/safety/test_strategy_params_disclosed.py`` pins the table against the
dataclasses (a field cannot just appear unclassified) and against an AST
attribute scan of ``src/chronos/strategies/*.py`` (an ENFORCED field nothing
reads fails; an INERT field something reads fails).

Two levels of "bind" are on record, because they are easy to conflate:

- The parameters below shape PROPOSALS. A proposal is advice, not an order
  (:mod:`chronos.strategies.base` docstring, base.py:7-9): it has no share
  quantity and no account fields.
- The proposal's own mechanical floor is
  :meth:`~chronos.strategies.base.StrategyProposal.__post_init__`
  (base.py:82-90 at the head; base.py:80-85 at the packet's base tree):
  ``desired_exposure_fraction`` in [0, 1], ``stop_fraction``
  in (0, 1) when present, and ENTER_LONG requires positive exposure. The
  portfolio layer caps again at sizer.py:99
  (``min(proposal.desired_exposure_fraction, policy.max_fraction_per_position)``).

Neither level is a safety claim about what a strategy does with its params;
ENFORCED says the strategy reads the field, not that the resulting behaviour
has an exercised test behind it.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

__all__ = [
    "ADVISORY_DECLARATION",
    "ENFORCED",
    "INERT",
    "PARAM_ENFORCEMENT",
    "POLICY_CAP",
    "PROPOSAL_FLOOR",
]

ENFORCED = "ENFORCED"
INERT = "INERT"

#: The mechanical floor every proposal satisfies, wherever the params set it:
#: StrategyProposal.__post_init__ (base.py:82-90 at the head; the packet's
#: base-tree citation was base.py:80-85 — the docstring pointer above shifted
#: it by two).
PROPOSAL_FLOOR: Mapping[str, str] = MappingProxyType(
    {
        "desired_exposure_fraction": "base.py:83-84 — ValueError outside [0, 1]",
        "stop_fraction": "base.py:85-86 — ValueError outside (0, 1) when present",
        "enter_long_positive": "base.py:89-90 — ENTER_LONG requires positive exposure",
    }
)

#: A proposal is advice, not an order (strategies/base.py docstring).
ADVISORY_DECLARATION = "base.py:7-9 — no share quantity, no account or broker fields"

#: The portfolio layer's second cap on whatever exposure a proposal asks for.
POLICY_CAP = (
    "portfolio/sizer.py:99 — min(proposal.desired_exposure_fraction, "
    "policy.max_fraction_per_position)"
)

#: Every parameter field, classified, keyed by owning class. Value per field:
#: (status, reader) — reader is a file:line string at the head for ENFORCED
#: fields, the empty string for INERT ones.
PARAM_ENFORCEMENT: Mapping[str, Mapping[str, tuple[str, str]]] = MappingProxyType(
    {
        "MeanReversionParams": MappingProxyType(
            {
                "rsi_len": (ENFORCED, "mean_reversion.py:200-206 RSI RMA; :61 warmup"),
                "oversold": (ENFORCED, "mean_reversion.py:110 entry threshold"),
                "rearm_level": (ENFORCED, "mean_reversion.py:129,:136 re-arm after recross"),
                "exit_level": (ENFORCED, "mean_reversion.py:133 reversion-realized exit"),
                "ema_filter_len": (ENFORCED, "mean_reversion.py:183-187 EMA filter; :61 warmup"),
                "atr_len": (ENFORCED, "mean_reversion.py:174-177 ATR RMA; :61 warmup"),
                "fail_atr_mult": (ENFORCED, "mean_reversion.py:116 stop = mult x ATR / close"),
                "time_stop_bars": (ENFORCED, "mean_reversion.py:134 time-stop exit"),
                "exposure_fraction": (ENFORCED, "mean_reversion.py:123 entry proposal exposure"),
            }
        ),
        "RegimeTrendParams": MappingProxyType(
            {
                "lookback": (ENFORCED, "regime_trend.py:217-223 regime_z window; :81 warmup"),
                "enter_z_base": (ENFORCED, "regime_trend.py:240 entry z threshold base"),
                "exit_z_base": (ENFORCED, "regime_trend.py:241 exit z threshold base"),
                "confirm_bars": (ENFORCED, "regime_trend.py:269 publication streak; :83 warmup"),
                "ema_filter_len": (ENFORCED, "regime_trend.py:203-207 trend filter; :80 warmup"),
                "vol_percentile_len": (ENFORCED, "regime_trend.py:231-235 percentrank window"),
                "percentile_sensitivity": (ENFORCED, "regime_trend.py:237 vol_factor adjustment"),
                "markov_min_stay": (ENFORCED, "regime_trend.py:138 bull-row stay-probability gate"),
                "markov_min_row_n": (ENFORCED, "regime_trend.py:136 minimum bull-row samples"),
                "atr_len": (ENFORCED, "regime_trend.py:195-198 ATR Wilder RMA"),
                "atr_stop_mult": (ENFORCED, "regime_trend.py:141 stop = mult x ATR / close"),
                "exposure_fraction": (ENFORCED, "regime_trend.py:148 entry proposal exposure"),
            }
        ),
        # No dataclass fields: exposure=0.95 / stop_fraction=0.49 are inline
        # literals at baselines.py:58-59 (see the handoff's open question on
        # the fixed 0.95 constants). The empty map is the truthful
        # classification — a field added later must be classified.
        "BuyAndHoldStrategy": MappingProxyType({}),
        "SmaTrendBaseline": MappingProxyType(
            {
                "fast": (ENFORCED, "baselines.py:82 fast SMA window"),
                "slow": (ENFORCED, "baselines.py:75 warmup; :83 slow SMA window"),
            }
        ),
        "DeterministicRandomEntries": MappingProxyType(
            {
                "seed": (ENFORCED, "baselines.py:119 LCG initial state"),
                "entry_probability_percent": (ENFORCED, "baselines.py:131 entry draw threshold"),
                "holding_bars": (ENFORCED, "baselines.py:140 exit after holding period"),
            }
        ),
    }
)
