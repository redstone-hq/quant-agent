## item 190 — RETIRED 2026-10-01, the sweep is retired to its live release-and-view surface and the last remaining criterion (the `min_order_usd` ledger-row rewrite) is merged

**Filed 2026-09-30, out of the order-placement gates review.** The desk built a feature to automatically sweep idle cash into a short-term Treasury-bill fund, then turned it off. Turning it off did not remove it: the code that runs it is still wired into every trading session, just switched to do nothing. A one-line flag flip would turn it back on with no further review.

That earlier review found one small piece of this dormant feature — a reserve band that decides how much cash to hold back — could not simply be deleted, because deleting it while the rest of the sweep stays wired in would leave the feature reachable with no record of what it does. Properly retiring the whole feature is a big, mechanical job: an estimated 187 places in the code and its tests still refer to it.

Right now nobody owns that job. It is only described inside another item's writeup, where it risks being forgotten once that item's narrower question is answered. This item exists so the cleanup has its own visible line on the board until it is actually done.

**No owner decision needed here** — this is an engineering bookkeeping fix (remove dead, switched-off code) rather than a money or risk-appetite question.



**Completed-step detail moved off the board 2026-10-01**, to keep the
item inside its per-item byte budget. Nothing is lost; each step keeps a
one-line statement on the board and its full account here.

- STEP 2 DONE 2026-09-30: portfolio-manager and rotation funding prose n ... STEP 3 PARTIAL 2026-09-30: `CashSweeper.fund_buys`, `park_excess`, their pipeline/stage call sites and their tests are deleted; STILL TO DO: the `sweep-vehicle liquidation before a BUY` registry entry must move to `retired:` and the three seat prompts that say the vehicle is auto-liquidated must be reworded; the enabled/view hooks (`_sweeper()` consumers, `split_positions`, `reserve_usd`) remain; `release_retired_vehicle` and `_retired_cash_park_symbol` are LIVE (run every session to sell a leftover vehicle and exempt it from the stop audit) and must stay until the vehicle is confirmed not held.

- STEP 4 PART DONE 2026-10-01: the `/account` liquidity view no longer c ... MEASURED against the live checkout: the engine's `TradingPipeline._compute_deployable_cash` adds 0.0 when `_sweeper()` returns None (which it does on `enabled: false`), and `CashSweeper.fund_buys` returns 0.0 on its first line, so nothing converts the vehicle back to cash for the BUY phase; `src.api.routes_live._compute_liquidity` added it unconditionally and so would have read ABOVE the figure the PM sizes against. LATENT, never an incident: the production DB (`/home/qamc/quant-agent/data/quant_agent.db`, read-only, 2026-10-01) holds 0 SGOV position rows (14 historical SGOV trades). Guarded by `tests/test_single_definition_quantities.py::test_disabled_sweep_does_not_inflate_the_dashboard_deployable`

- STEP 5 DONE 2026-10-01: the three constants that became unreachable wh ... the reserve band is RELOCATED, value unchanged, from `CashSweepConfig.reserve_pct` to `CashReserveConfig.pct` (yaml `cash_reserve.pct`) so a later cleanup cannot delete a band `/account` still reports; `min_order_usd`, `_SELL_LIMIT_PAD`, the three `_FUND_*` timeouts and `release_retired_vehicle` are all MEASURED live and stay. Item STAYS OPEN: `min_order_usd` still awaits its per-name read

- WHAT REMAINS, 2026-10-01: not a deletion. The ledger row for `CashSwee ... ALGEBRA CHECKED and stated in the row: the dollar floor does NOT cancel to a constant fraction of equity. The sweeper's view/release surface and `reserve_pct` stay, so their ledger rows stay too — orphaning live constants would be worse than leaving them described.

**RETIREMENT CHECK 2026-10-01.** Every remaining criterion was re-verified
against this checkout before the item was closed, because the board's own
text had already been wrong once on this item:

- the `min_order_usd` ledger-row rewrite is MERGED, not pending: the row at
  `config/number_ledger.yaml` id `src.config.CashSweepConfig.min_order_usd`
  opens with the 2026-10-01 correction, names both trade-governing consumers,
  carries the built `below_min_notional` execution-skip settlement route and
  both failed derivation attempts, and its `-1` ratchet delta is already in
  `config/number_ledger_route_history.yaml`. The corrected text is the first
  thing the row says, so the superseded 2026-09-24 reading cannot be met first.
- both named consumers are live as described: `_min_order_usd` at
  `src/pipeline_stages.py:3558`, read at :6701 and at :9949 where the short
  scale-in add is refused as `below_min_notional` (:9960); and the `funding`
  binding constraint in `src.rotation.rotation_binding_constraints`
  (`src/rotation.py:583-588`), rendered by `rotation_constraint_clause`.
- `release_retired_vehicle` is still live and still KEPT by the 2026-10-01
  ruling (`src/pipeline.py:1602`), the reserve band lives outside the retired
  namespace as `CashReserveConfig.pct`, and the three dead pad/buffer
  constants are gone with their rows. Nothing in the retired feature is
  reachable except the deliberately kept release-and-view surface.

Value unchanged throughout. What `min_order_usd` SHOULD be is not this item's
question: it is carried by its own ledger settlement route, which stays open.

- 2026-10-14 SUPERSEDES the KEPT ruling above: no code path buys the vehicle (the sweep buy was deleted and `enabled: true` adds none) and the desk held none (critic pass, live positions). `release_retired_vehicle`, its session wiring, `_SELL_LIMIT_PAD`, the three `_FUND_*` timeouts and the dead `will_fund` wait they fed are deleted. The stop-audit exemption for a held vehicle (`retired_cash_park_symbol`) stays.
