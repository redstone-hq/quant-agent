
# A/B-ing the ledger's `arbitrary` numbers through the deterministic backtester

Measurement only. No config default and no ledger row was changed by this note.

## Which arbitrary numbers the engine can reach at all

`config/number_ledger.yaml` carries 124 rows at `status: arbitrary` (measured
2026-10-04 against `origin/main`). `scripts/backtest.py` loads the real
`AppConfig` but reads only `config.risk` / `config.trading` / `config.execution`,
so a row is A/B-able only if it is one of those *and* is consumed by the
deterministic layer the engine exercises. Grepping `src/backtest/`,
`src/risk/budget.py`, `src/risk/trailing.py`, `src/portfolio_constructor.py`
and `src/data/levels.py` for config reads leaves this set:

| ledger row (`status: arbitrary`) | where the engine consumes it |
| --- | --- |
| `risk.max_position_risk_pct` | the §2.1 sizing formula; also the size of every `RiskRequest` the engine makes |
| `risk.max_portfolio_risk_pct` | `allocate_risk_budget` ceiling |
| `risk.max_cluster_risk_share_pct` | `allocate_risk_budget` cluster share cap |
| `risk.absolute_min_stop_atr_multiple` | `_resolve_stop` level-backed exemption floor |
| `risk.min_level_touches_for_stop_honor` | structural-level stop honouring |
| `risk.max_target_reach_atr_multiple`, `risk.max_target_horizon_sessions` | target sanity checks only — advisory, no fill effect (`risk.target_divergence_warn_pct`, also listed here, was deleted 2026-10-13) |
| `execution.max_entry_slippage_bps` | reused by the CLI as the flat slippage estimate on both fills |

`risk.min_stop_atr_multiple` is deliberately excluded: the owner closed it at
2.5 ATR on 2026-10-04.

Everything else in the 124 is out of reach — it lives in `SmartMoneyConfig`,
`IntradayScanConfig`, `NominationConfig`, broker retry/backoff constants,
exit-guard noise floors or pipeline factors, none of which this engine calls.

## The A/B that was run

Two copies of `config/settings.yaml` differing in exactly one line:

* **A** — `risk.max_portfolio_risk_pct: 25` (current default)
* **B** — `risk.max_portfolio_risk_pct: 15`

Window 2025-01-01 .. 2025-12-31, 20-name universe slice taken from
`trading.universe` (the large-cap and sector-ETF block), yfinance daily bars,
$100,000 starting equity.

Reported as a difference, B minus A:

| Metric | Delta (B − A) |
| --- | --- |
| Trades | −41 |
| Win rate | −1.08 pts |
| Win/loss ratio | +0.141 |
| Expectancy | +0.0215R |
| Max drawdown | −10.83 pts |
| Total return | +15.04 pts |
| **Binding-budget days** | **243 of 245 → 246 of 246** |

## This is a NON-RESULT, and that is the finding

The engine's own caveat says that on a day the risk budget binds, its
equal-size unranked `RiskRequest`s are served by the allocator's **alphabetical
ticker tie-break**, not by production's verdict ranking, and that the binding
share "is not a discount you can apply to the other numbers: who got funded
changes later equity, later size, and later outcomes."

In this run the budget bound on **essentially every entry day in both arms**
(243/245 and 246/246). There is no unrationed sub-period to read a clean signal
from. The entire +15.04-point return difference is produced by a different
alphabetical subset of tickers getting funded, compounding into different
equity and therefore different later sizes. It is not evidence that a 15%
portfolio risk ceiling is better than 25%.

Two further caveats the engine prints apply on top and are not relieved by a
longer window: survivorship bias (the universe is a present-day fixed list, no
delisted names), and the deterministic stop substitution (nearest structural
level stands in for the Tech Analyst's chart read).

## What this implies for the other reachable rows

Because the budget binds on ~100% of entry days under the shipped defaults,
the same contamination lands on **every** reachable row in the table above, not
just the two budget parameters: any change that alters how many shares a
candidate asks for, or where its stop sits, changes which alphabetically-ordered
candidates fit under the ceiling. `max_position_risk_pct`,
`absolute_min_stop_atr_multiple` and `min_level_touches_for_stop_honor` were
therefore not run — the result would have been invalidated by the same caveat
before it was read.

**Conclusion: none of the 124 arbitrary rows can currently be settled by this
engine.** The blocker is not the engine's scope, it is that its unranked
equal-ask design makes the budget bind on every entry day. The prerequisite for
any future A/B here is a deterministic, non-alphabetical ordering for the
engine's `RiskRequest`s — or a run configuration in which the budget does not
bind — and neither is invented here.

## 2026-10-04 — the non-binding-budget route works, and the first settleable runs

**READ THIS FIRST: every number in this section was produced with the portfolio
risk budget deliberately made UNCONSTRAINED (`risk.max_portfolio_risk_pct: 100`,
`risk.max_cluster_risk_share_pct: 100`, in throwaway config copies — no repo
default was changed). It is NOT the desk's real-world expectation and must
never be quoted as one. The live desk runs a 25% ceiling with a 40% cluster
share; a result measured without those ceilings says nothing about what the
rationed book would have earned.**

What it does buy: with both arms unconstrained, the engine's own arbitrated-day
counter reported **0 of 66 entry days binding, in BOTH arms** (AAPL/MSFT/NVDA/
AMZN/GOOGL, 2025-01-01..2025-09-30, yfinance). Zero binding days means zero days
decided by the allocator's alphabetical ticker tie-break, so the difference
between two arms is attributable to the one field that differs. The engine
printed no non-result for these runs. This is the condition the previous section
said was the prerequisite, and it is reachable — the ceilings are the only thing
that was in the way, not the engine's design.

Two A/Bs were run under that condition, same universe, same window, 85 trades in
every arm:

| field changed | arm A | arm B | total return A | total return B | delta |
| --- | --- | --- | --- | --- | --- |
| `risk.breakout_projection_atr_multiple` | 1.0 | 2.0 | -4.73% | -4.73% | 0.00pp (all twelve metrics identical) |
| `risk.max_position_pct` | 65 | 33 | -4.73% | -1.33% | +3.40pp |

The second run is the positive control: it proves the harness does register a
difference when one exists, which is what makes the first run's zero credible as
INERT rather than as broken plumbing. `breakout_projection_atr_multiple` joins
`max_target_reach_atr_multiple` as a row this engine cannot settle — the engine
derives its own nearest-level target and never reaches the live projection path.

`max_position_pct` is not an arbitrary row (65 is a dated owner ruling), so its
+3.40pp is reported as a method check, not as a recommendation to move it.

CAVEATS THAT STILL STAND, and they are not small. Survivorship bias: the universe
is a present-day five-name list, so anything delisted in the window is absent and
the result is biased upward. Flat slippage: 40 bps on both fills from
`execution.max_entry_slippage_bps`, with no commission or fee model. Deterministic
stop substitution: the nearest structural level stands in for the Tech Analyst's
stop choice, and `setup_type` is substituted from `is_consolidating`. Unconstrained
budget: as stated above. Taken together these make any single arm's P&L a
NON-RESULT as a forecast; what survives is the DIFFERENCE between two arms that
share all four caveats, and only for an unrationed book.

## 2026-10-04 (later) — horizon is inert; the per-name risk envelope is the live dial

**READ THIS FIRST: an unconstrained-budget backtest figure is NEVER a desk
expectation.** Both A/Bs here opened `risk.max_portfolio_risk_pct` and
`risk.max_cluster_risk_share_pct` to 100 in BOTH arms (throwaway config copies;
no repo default changed) so the allocator never arbitrated. The live desk
rations at 25 and 40. Only the DIFFERENCE between arms is readable.

Condition verified before reading anything: the engine's own counter reported
**0 of 66 entry days arbitrated in all four arms**. Universe
AAPL/MSFT/NVDA/AMZN/GOOGL, 2025-01-01..2025-09-30, 85 trades per arm.

| field changed | arm A | arm B | total return A | total return B | delta |
| --- | --- | --- | --- | --- | --- |
| `risk.max_target_horizon_sessions` | 60 | 20 | -4.73% | -4.73% | 0.00pp (all twelve metrics identical) |
| `risk.max_position_risk_pct` (control) | 5 | 2 | -4.73% | -0.82% | +3.91pp (max drawdown -23.69pp) |

`max_target_horizon_sessions` is the THIRD parameter measured inert in this
engine, after the target reach multiple and the breakout projection. Corrected
on review: this does NOT mean the deterministic path ignores it — three live
modules consult it. It is a CAP that binds only when a caller passes a larger
horizon, and that horizon is an LLM-produced field the engine states it cannot
produce, so the knob was unreachable here rather than idle. The row stays
`arbitrary`, and the null is no grounds for removing the constant.

`max_position_risk_pct` stays `arbitrary` as well. The control proves it is the
most outcome-moving risk dial measured on this path, but a measurement showing
a number matters is not a criterion for picking its value, and an unrationed
book structurally flatters the smaller envelope. Clearing it on this would be
weak evidence.

All caveats from the section above still stand: survivorship bias, the flat
40bps slippage, the substituted deterministic stop, and the opened budget.
