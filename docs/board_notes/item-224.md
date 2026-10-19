## item 224

Moved from `docs/WORK.md` on 2026-10-01 to satisfy the per-item note-pointer
check, which landed after this item was filed. VERBATIM -- not a word of the
filing author's prose is changed, and nothing is added to it here.

**224. The desk records no realised sector weights, so concentration can only be guessed before the fact and never read after it -- filed 2026-10-01 from item 221.** Item 221 established that the pre-decision preview cannot project a sector mix at all, because sizing depends on a PM target that does not exist when the preview is built; what the desk could record instead, and does not, is the sector weights of the orders the constructor ACTUALLY built, once per run. Without that row nobody can say afterwards whether a session concentrated the book or not. Detail in `item 221, retired 2026-10-01, note deleted 2026-10-19, history in git`.

DONE WHEN:
- [ ] one durable row per run carries the realised `(sector, side)` weights of the orders the constructor built that session, written from executable product code with its call site named, and classified POPULATING rather than UNPROVEN against a real session

---

## STATUS 2026-10-02 -- built, proven in test, live row count UNPROVEN

The recording shipped in #958: `PortfolioConstructor` keeps `last_order_sectors`, `_record_realised_sector_weights` (src/pipeline_entry_orders.py) is called once from `DecisionStage` (src/stage_decision.py) right after `construct_orders`, and writes the `realised_sector_weights` table through `Database.record_realised_sector_weights` (one row per run, unique on run id).

- UNPROVEN (not POPULATING): an orchestrator reported the live store holds 3 rows [measured 2026-10-02 by the orchestrator against the production database at the desk user's data directory; not independently re-read by the author of this note].
- Verified in code and test by the author: one row per run, `(sector, side)` weights, the named call site, and a real `construct_orders` run landing a populated row in a real store (`test_a_real_construct_orders_run_lands_a_populated_row_in_the_store`).
- An earlier version of this note called the item UNPROVEN because the author read a different database file (the checkout's copy, not the live one); that was wrong and is corrected here.

## UPDATE 2026-10-04 -- two gaps closed, box still NOT ticked

- Reduce-only sessions now record their `(sector, side)` weights under kind "reduce" (the earlier test that pinned an empty list for them is replaced). The production row with NULL weights and 6 reducing orders predates the NOT NULL schema and the backfill; it was a legacy row, not a current-code result.
- The recorder reads `last_order_sectors` with no default, outside the try, so a renamed source field raises instead of silently recording nothing.
- Classification: UNPROVEN until a post-fix production row with reducing weights is observed.
