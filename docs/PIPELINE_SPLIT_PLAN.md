# Splitting src/pipeline.py and src/pipeline_stages.py — design only

## Current reconciliation — 2026-10-06

This plan's line ranges and status row below are historical. In the current tree,
steps 0–8 and 10–12 have landed. Step 9's proposed `pipeline_evening.py` mixin
was superseded: `src/sessions/evening_session.py`,
`evening_stop_proximity_session.py`, `expected_sessions_session.py`, and
`quarterly_meta_session.py` hold those bodies behind thin `TradingPipeline`
entry points. The base is 1,756 lines and `pipeline_stages.py` is 805; the
largest tracked `src/` Python file is 2,166 lines. This is a boundary inventory,
not proof of live behavior or of the original one-step-per-PR sequence.

`scripts.audit_moved_patch_targets` reports 150 LIVE, 2 MIRRORED, 0 REEXPORT,
and 0 MISSING static test targets. The two mirrored seams are exercised by the
late-entry-window and short-add tests. `scripts.pipeline_method_guard` and the
rewritten `scripts.file_size_guard` pass. The focused split/stage/evening tests
passed 151/151, and the extra evening-boundary/mirrored-seam selection passed
19/19. The required PR #1546 CI passed both test shards on this same code tree.
The scanner cannot prove dynamically constructed patch targets or an actual
Paper session; neither is claimed here. No current executable gap was proven,
so further moves require a specific failing boundary or test.

Measured 2026-09-30 against main at 747d159 (`src/pipeline.py` 20,035 lines, `src/pipeline_stages.py` 10,536).
Method of derivation: an AST pass over `TradingPipeline` (227 methods, class spans lines 940–20035) recording every
`self.<method>()` call, every `self.<attr>` read/write, and every lazy import; plus grep of every importer, every
test monkeypatch target, every source-text scanner, and the hunk headers of all 22 open PRs. Nothing below is guessed
from names alone. The AST script is at `scratchpad/ast_map.py`; rerun it before executing any step — line numbers
in this document are dead the moment a PR merges.

**RE-MEASURED 2026-10-01 against current main — every figure in the original 2026-09-30 pass is now stale.**
The original figures are left in place above and in the tables below so the drift is visible; where a number is
restated here, THIS is the one to use. Measured 2026-10-01: `src/pipeline.py` 21,864 lines (was 20,035);
`src/pipeline_stages.py` 10,674 lines (was 10,536); `TradingPipeline` holds 239 methods (was 227) and the class
spans lines 1024–21864 (was 940–20035). 129 commits have merged since the plan was written, 34 of them touching
these two files. Test exposure measured 2026-10-01: 255 monkeypatch/patch sites across 28 test files, with
`compute_indicators` alone at 67 sites (the plan says 42 below — read 67); and 338 `TradingPipeline` construction
sites across 128 importing test files (the plan says 327 call sites below — read 338 across 128 files).
Consequence, stated plainly: **every line number and every cluster range in §1, §2 and §4 below is dead.** They
describe the right clusters in the right order; they do not describe today's offsets. Re-run `scratchpad/ast_map.py`
immediately before each step and take the ranges from it, never from this document.

---

## 1. What is actually in src/pipeline.py

### Module level (lines 1–939, ~940 lines)

| Lines | Content | Cohesion |
|---|---|---|
| 1–87 | imports (incl. `compute_indicators` kept ONLY as a re-export for tests; `_get_sector` from broker) | — |
| 88–150 | logger, `_WAL_SELL_SENTINEL`, `_PM_PROFILE_SYMBOL_CAP`, `SessionTerminated`, `CarryForward` | mixed |
| 152–257 | risk-number coercion helpers (`_optional_risk_number`, `_finite_float_or_none`, `_risk_number`, `_threaded_risk_settings`) | pure, used by `build_risk_config` |
| 258–424 | `_HARD_TRIGGER_KEYWORDS` + `_reason_cites_hard_trigger`, `_valuation_signal_from` | exit-trigger doctrine; pinned by `tests/test_prompts_contract.py:112` by path |
| 425–500 | `_missed_ops_quality_metrics` | evening digest |
| 501–653 | broker-state predicates (`_market_is_open_now`, `_price_is_through_stop`, `_position_notional`, `_classify_coverage_gap`) | protection |
| 654–896 | `build_risk_config`, `build_constructor_config` (deliberately lifted from `__init__`, see banner) | construction |
| 897–939 | `_smart_money_refresh_sources_word`, `_reconciled_exit_action` | misc |
| 968–1023 (measured 2026-10-01; new since the plan) | `_actions_with_scan_fallback`, `_reason_claims_alignment_exit` | exit execution; both are called ONLY from `_midday_execute_llm_actions` (cluster L) and both are pinned by name by `tests/test_alignment_exit_wiring.py` |

### Inside `TradingPipeline` (lines 940–20035), in file order

Clusters below are contiguous ranges whose methods call each other and share the same external imports.
"Roots" = never called by another method of the class (called only from `pipeline_stages`, tests, or the scheduler).

| # | Lines | Lines moved | Cluster | Evidence it is one thing |
|---|---|---|---|---|
| A | 946–1619 | ~675 | `__init__` (516 lines) + `_sweeper`/cash-park + `_compute_deployable_cash` + the three tiny qty formatters | construction; the qty helpers are called from 5 clusters and from `pipeline_stages` |
| B | 1633–1731 | ~100 | `_total_pnl_since_reset`, `_forced_close_side_and_qty`, `_trade_executed_or_pending` | small shared helpers |
| C | 1733–2242 | ~510 | universe admission: screen, Form-4 currency, nominated externals, transient smart-money symbols | 4 roots + 7 private helpers, all only reachable from `MorningResearchStage` |
| D | 2244–3085 | ~840 | risk-verdict application: `_filter_hard_risk_decisions` (252), `_persist_hard_risk_block`, `_apply_risk_modifications` (330), floor breach, `_reconcile_size_to_risk_budget`, `_has_actionable_signal_fn` | all roots called by `RiskStage`; share lazy imports from `src.risk.rules` |
| E | 3088–3228 | ~140 | `_resolve_live_context`, `_live_session_context` | live-price context |
| F | 3238–6006 | ~2,770 | **protection**: `_reconcile_stop_coverage` (603), elected-unfilled, 8 owner alerts, `_repair_stop_coverage`, `_submit_protected_sell` (207), exit settlement, `_finalize_pending_protections` + core (301), stray-stop cancel, write-ahead restore/cancel, `_drain_pending_repegs`, `_drain_pending_protection_restores` (206), `_reprotect_residual_after_partial_sell`, `_order_accepted` | every method touches broker orders + `src.execution.stop_records`/`stop_repair`/`scale_in`; reachable from ALL six session roots; 3 of the 5 open PRs on this file land here |
| G | 6009–6092 | ~85 | `_refuse_queued_earnings_buys`, `_is_trading_day` | misc |
| H | 6094–6758 | ~665 | broker reconciliation: `_reconcile_fills`, `_reconcile_orphan_pending_submits`, `_reconcile_stop_out_fills` (253), `_surface_reconcile_outcomes` | called as a block by 5 session roots |
| I | 6760–9819 | ~3,060 | **prompt-facts builders**: 40 `_build_*`/`_missed_ops_*`/`_thesis_*` methods, `_build_pm_facts` (220), `_build_blocked_proposals` (288), `_ensure_correlation_matrix`, `_compute_recent_performance` | read DB/broker → return text/dicts for LLM prompts; place no orders **with one measured exception, see below**; 11 are roots called only from stages |

**Correction, measured 2026-10-01 — cluster I is NOT order-free.** `_handle_ex_dividends` sits inside cluster I's
range and MODIFIES LIVE STOPS: it shifts each affected position's stop down by the dividend, files the stop-shift
legs, refuses to record an unconfirmed move, and writes the new stop back to the desk's own records. It is called
once per session from the intra-check path. The old "places NO orders" description of cluster I was therefore
untrue of the file as it stands. **Reassignment: `_handle_ex_dividends` moves to `src/pipeline_protection.py`
(`ProtectionMixin`), not to `PromptFactsMixin`** — the protection module's stated scope is "everything that places,
cancels, restores or reconciles a protective stop", which is exactly what this method does, and keeping a
stop-moving method in a module advertised as read-only is the boundary lying about touching money. It moves in
step 2 with the rest of protection, not in step 1. Its tests (`tests/test_pm_memory.py`,
`tests/test_audit_fixes_2026_07_16.py`) must be run by step 2 as well as by step 1.

| J | 9822–10076 | ~255 | `_refresh_account_state`, `_sync_positions_from_broker`, `_run_news_update`, `_load_earnings_analyses`, `_earnings_preprocess_symbols` | account/news refresh |
| K | 10078–11909 | ~1,830 | **held-position exit engine**: target-revision adjudication (286), structural protection (185+77), `_substantiate_exit_triggers` (222), `_holding_discipline_check_for_exit` (250), trails (170), event-risk block, `_risk_review_exits` (352), approvals | own banner at 10078; only reachable from `run_midday`/`run_close`; lazy-imports `src.risk.exit_guard/exit_refusal/exit_trigger/target_revision` |
| L | 11911–12763 | 853 | `_midday_execute_llm_actions` (one method) | executes K's decisions; calls F (`_submit_protected_sell`, `_finalize_pending_protections`) and K |
| M | 12765–13998 | ~1,235 | **gross-exposure ceiling / de-lever ladder**: `_force_delever` (315), `_resolve/_enforce_gross_ceiling`, conviction cut, SIGTERM unwind, trims, shortfall alerts | own banner "Spec §11.2" at 13175; pinned by `tests/test_gross_exposure_ladder.py`; calls F to sell |
| N | 14000–14397 | ~400 | stage accessors, cost circuit, kill switch, `_evidence_gate_skip` (161) | session plumbing |
| O | 14399–14973 | ~575 | `run_morning` + `_run_morning_body` (425) + snapshot/pnl/report persistence | morning orchestration |
| P | 14975–16142 | ~1,170 | `_build_position_facts` (356), review metrics, `run_position_review` + `_run_position_review_body` (664, calls 42 methods) | midday/close orchestration |
| Q | 16144–16415 | ~270 | earnings preprocess session | orchestration |
| R | 16417–16988 | ~570 | intra-check session, scan locks, paid-scan slot | orchestration |
| S | 16990–17210 | ~220 | change detectors (macro regime/prints, news peek) | research freshness |
| T | 17212–17503 | ~290 | Form-4 backlog alert, congressional refresh, specialist evidence | research freshness |
| U | 17505–17832 | ~330 | `_carry_forward_{macro,news,earnings,insider}` | research freshness |
| V | 17834–18297 | ~460 | heal lost research seats (`_try_one_paid_research_retry` 214) | research freshness |
| W | 18299–18922 | ~625 | intraday opportunity scan body + helpers | orchestration; `tests/test_invariants.py:561–590` reads its SOURCE via `inspect.getsource` |
| X | 18924–19781 | ~860 | evening session (`_run_evening_body` 612) + proximity + expected-sessions + quarterly trigger | orchestration |
| Y | 19783–20035 | ~250 | quarterly meta reflection, `run_daily` | orchestration |

Shared mutable state is small: only 5 `self.` attributes are written by more than one method
(`cost_circuit`, `_last_account_snapshot`, `_last_evidence_freshness`, `_unsettled_exit_orders`,
`_intra_preamble_deferred`); everything else is set once in `__init__`. That is what makes a partition possible.

Cross-cluster reach: 122 methods (10,562 lines) are reachable from two or more session roots; `run_midday`/`run_close`
each reach 110 methods / ~9,750 lines. The sessions are the tangle; the clusters underneath them are not.

### What is in src/pipeline_stages.py (10,536 lines)

| Lines | Content | ~Lines |
|---|---|---|
| 143–290 | book-risk inputs, gross ceiling for session, rotation flags | 150 |
| 290–1710 | rotation execution: precheck, sell gate, buy-leg projection, ranked-margin, alerts | 1,420 |
| 1709–2530 | entry-order path: trade-updates stream, submit window, sizing price, `_repeg_entry_order` (287), owner alerts | 820 |
| 2531–2930 | constructor drops/side flips, `_apply_repeg`, repoint, WAL delete | 400 |
| 2930–3370 | `_persist_evidence`, `_check_levels_coverage`, earnings-figure sanity + XBRL cross-check, `_classify_earnings_status` | 440 |
| 3370–3760 | sizing: fractional, `_size_shares`, risk budget, `_min_order_usd`, payoff skip, deployment budget, single-name cap | 390 |
| 3763–4820 | record/alert helpers, PM candidate accounting, soft-exit heals, seat stances/nominations, risk events, scale advisory | 1,060 |
| 4823–6340 | `class MorningResearchStage` | 1,518 |
| 6341–7044 | `class DecisionStage` | 704 |
| 7045–7346 | risk-stage helpers (earnings cap, sector alert, parse loss, dropped reasons) | 300 |
| 7347–8412 | `class RiskStage` | 1,066 |
| 8413–10536 | `class ExecutionStage` | 2,124 |

Every free function here already takes `pipeline` as its first argument and reads only `pipeline.db/broker/config`
and 30 private `TradingPipeline` methods (list in §5). It never imports `src.pipeline` at runtime (only under
`TYPE_CHECKING`, line 96).

---

## 2. Proposed module boundaries

### The mechanism, decided first because it decides what is provable

Move class methods as **mixin classes**, not as free functions:

```python
# src/pipeline.py (after)
class TradingPipeline(ProtectionMixin, ExitEngineMixin, DeleverMixin, PromptFactsMixin, ...):
    def __init__(...): ...   # unchanged
```

Why this and not the `pipeline_stages` free-function style:
- Method bodies are copied byte-for-byte; the only edit is the enclosing `class` line and the import block. A move
  whose diff is "same text, different file" is reviewable as behaviour-preserving without reading the logic.
- 327 test sites construct `TradingPipeline.__new__(TradingPipeline)` and call private methods on it (re-measured
  2026-10-01: **338 construction sites across 128 importing test files**); 60 distinct
  private methods are pinned by name on the class (§5). Mixins keep every `TradingPipeline._x` attribute intact.
  Free functions would break all of them and change every `self._x(` call site inside 20k lines.
- `inspect.getsource(TradingPipeline._method)` (used by `tests/test_invariants.py`) follows the function object, so
  it keeps working across files.

Cost, stated honestly: mixins **partition**, they do not **decouple**. `self._submit_protected_sell()` from the exit
engine still reaches into the protection module. That is the correct first step for live-money code; real
decoupling (passing a protection object instead of `self`) is a later, behaviour-affecting refactor and is out of
scope for this plan.

A mechanical guard ships with step 1 and stays forever: a test asserting that
`{name for name, _ in inspect.getmembers(TradingPipeline, inspect.isfunction)}` equals a frozen list of the
method names (227 when the plan was written; **239 measured 2026-10-01** — freeze the list the mapping run
produces on the day step 0 lands, not either of these numbers), and that each method's `__qualname__` module is the one this plan assigns. That makes "a method
silently vanished or was duplicated" a red build, not a review-time hope.

### Files from src/pipeline.py

| New file | Clusters | ~Lines | Why it is one unit | What stays adjacent-by-accident and is NOT included |
|---|---|---|---|---|
| `src/pipeline_protection.py` — `ProtectionMixin` | F + H + `_handle_ex_dividends` (reassigned out of I, 2026-10-01) + module helpers 501–653 + `_WAL_SELL_SENTINEL` + `_reconciled_exit_action` | ~3,600 | Everything that places, cancels, restores or reconciles a protective stop or a sell against the broker. The live-money code the owner's stop-fix queue lives in. Changes together: 3 of 5 open PRs on this file are inside it. | The tiny qty formatters in A (used by 5 clusters) stay in the base class. `_refuse_queued_earnings_buys` (G) is next to it in the file but belongs with risk application (D). |
| `src/pipeline_exits.py` — `ExitEngineMixin` | K + L + module helpers 258–424 (`_HARD_TRIGGER_KEYWORDS`, `_reason_cites_hard_trigger`) + `_actions_with_scan_fallback` + `_reason_claims_alignment_exit` (assigned 2026-10-01) | ~2,850 | The "sell only on alignment" doctrine: target revision, structural protection, trigger substantiation, holding discipline, trails, AI risk review, and the one method that executes the resulting actions. Only reachable from `run_midday`/`run_close`. 2 of 5 open PRs land here. | `_build_position_facts` (P) feeds it but is a prompt builder → PromptFacts. `_actions_with_scan_fallback`: its only caller is `_midday_execute_llm_actions` (cluster L), which moves here. `_reason_claims_alignment_exit`: same single caller, and it encodes the "exit on alignment" doctrine this module owns. Both are imported by name from `src.pipeline` by `tests/test_alignment_exit_wiring.py`, so both must stay re-exported from `src.pipeline` (§5). |
| `src/pipeline_delever.py` — `DeleverMixin` | M | ~1,235 | The Spec §11.2 gross-exposure ladder; has its own banner, its own test file, and an owner ruling. Sells through ProtectionMixin. | `_sweep_symbol` (3 callers) is listed inside M but is cash-sweep plumbing; keep in base. |
| `src/pipeline_prompt_facts.py` — `PromptFactsMixin` | I + `_build_position_facts`/`_build_review_metric_deltas` from P (`_build_own_recent_decisions` removed 2026-10-18) + `_missed_ops_quality_metrics` + `_PM_PROFILE_SYMBOL_CAP` | ~3,550 | Read-only DB/broker → prompt context. Places no orders. Largest and safest move. | `_ensure_correlation_matrix` writes nothing and is a root; include. `_handle_ex_dividends` is inside cluster I's range but modifies live stops → ProtectionMixin (see §1 correction, 2026-10-01). |
| `src/pipeline_risk_gate.py` — `RiskGateMixin` | D + G's `_refuse_queued_earnings_buys` | ~920 | The deterministic application of risk verdicts to sizes (`RiskStage`'s backend). Money-governing; 4 of the 36 ledger sites in this file are here. | — |
| `src/pipeline_admission.py` — `AdmissionService` (DONE 2026-10-05, shell deleted) | C | ~510 | Who gets into the research universe. Only `MorningResearchStage` calls it. | — |
| `src/pipeline_research_continuity.py` — `ResearchContinuityMixin` | S + T + U + V | ~1,300 | One question: "may yesterday's paid research be reused, and if a seat is lost can it be healed?" Change detectors, carry-forward, heal, Form-4 backlog all answer it. | — |
| `src/pipeline_intraday.py` — `IntradayMixin` | R + W | ~1,200 | The intra-check session and the opportunity scan; `tests/test_invariants.py` already treats these four methods as one unit. | — |
| `src/pipeline_evening.py` — `EveningMixin` | X + Y | ~1,100 | Evening report, proximity checks, quarterly meta reflection. | — |
| **stays in `src/pipeline.py`** | A, B, E, J, N, O, P (orchestration part), Q, `build_*_config`, `SessionTerminated`, `CarryForward` | ~3,700 | The orchestrator: construction, session entry points, morning and position-review bodies, cost circuit / kill switch. This IS the desk's top-level control flow and splitting it further would be arbitrary. | — |

**Boundaries I judge arbitrary and therefore do not propose:** splitting `_run_position_review_body` (664 lines, 42
callees) or `_run_evening_body` (612) into pieces; splitting `__init__` (516); separating O/P/Q sessions into one
file each. Those are long because they are linear scripts, and cutting a linear script at an arbitrary line is
the "same tangle in more files" outcome the brief warns about. They get shorter only when a real seam appears.

### Files from src/pipeline_stages.py

Same rule: these are already free functions, so each move is a file change plus an import line.

| New file | Lines (today) | ~Lines | Why |
|---|---|---|---|
| `src/stage_morning_research.py` | 4823–6340 | 1,520 | one class |
| `src/stage_decision.py` | 6341–7044 | 700 | one class |
| `src/stage_risk.py` | 7045–8412 | 1,370 | class + its 5 private helpers |
| `src/stage_execution.py` | 8413–10536 | 2,120 | one class |
| `src/pipeline_rotation_exec.py` | 290–1710 | 1,420 | rotation execution; NOTE `src/rotation.py` (1,594 lines) already exists — whether these belong inside it I could not determine without reading it (§6) |
| `src/pipeline_entry_orders.py` | 1709–2930 | 1,220 | entry submission, stream warm-up, repeg, repoint, WAL |
| `src/pipeline_sizing.py` | 3370–3760 | 390 | share sizing, risk budget, deployment budget, single-name cap — money-governing, 22 ledger sites |
| `src/pipeline_earnings_quality.py` | 3094–3370 | 280 | earnings-figure sanity + XBRL cross-check |
| **stays in `src/pipeline_stages.py`** | 143–290, 2930–3094, 3763–4820 | ~1,500 | shared record/alert/nomination helpers, `_persist_evidence`, `_record_pipeline_event` |
| `src/ports/event_journal.py` + `src/storage/event_journal.py` | — | ~120 | **LANDED (conversion step 6, 2026-10-01).** `_persist_evidence` / `_record_pipeline_event` are now shims over the L2 `EventJournal` port; the bodies live in the L3 `DatabaseEventJournal`. In-memory fake for injection: `tests/fake_event_journal.py`. Rows byte-identical before/after (captured on both sides, diffed); the only observable delta is the storage-failure WARNING's logger name. |

---

## 3. Dependency graph and cycles

Mixin modules import only `src.*` leaf modules they already lazily import today (`src.execution.*`, `src.risk.*`,
`src.models`, `src.pipeline_context`). They must **not** import `src.pipeline` (they are its bases).

```
src/pipeline.py  ──imports──▶  every *Mixin module
                 ──imports──▶  src/pipeline_stages.py  (top-level, unchanged)
src/pipeline_stages.py ──imports──▶ src/pipeline  ONLY under TYPE_CHECKING (line 96)   ← keep it that way
Mixin modules    ──▶ src.execution.*, src.risk.*, src.data.*, src.models, src.pipeline_context
ProtectionMixin  ◀── ExitEngineMixin, DeleverMixin, session code   (via self.*, not import)
```

Cycles the split WOULD create, and the break for each:

1. **`pipeline.py` ↔ any mixin module that imports `TradingPipeline` for a type hint.** Break: type-only import
   under `if TYPE_CHECKING:` (the pattern `pipeline_stages` already uses) or `Self`-typed `self`.
2. **Mixin ↔ `pipeline_stages`.** `_run_universe_screen`, `_drain_pending_repegs`, `_holding_discipline_check_for_exit`,
   `_persist_heal_call`, `_run_position_review_body` lazily import from `src.pipeline_stages` inside the method body.
   Those lazy imports move with the method text and stay lazy; `pipeline_stages` never imports the mixin modules at
   runtime, so there is no cycle. Do NOT hoist them to module level "for tidiness" — that is exactly the edit that
   would create the cycle.
3. **`src/stage_*.py` ↔ `src/pipeline_stages.py` (remaining helpers).** Stage classes call the shared helpers;
   the helpers never call the stage classes. `pipeline_stages` should re-export the four class names from the
   `stage_*` modules so `from src.pipeline_stages import RiskStage` (6 test files) keeps working. Direction:
   `stage_* → pipeline_stages(helpers)`, and `pipeline_stages` re-exports at the bottom of the file after the
   helpers are defined — a top-of-file re-export would be a genuine import cycle.
4. **Module-level names that tests patch.** `compute_indicators`, `_get_sector`, `_market_is_open_now`, `et_today`,
   `et_now` are monkeypatched *on `src.pipeline`* (42, 20, 9, 6, 2 test sites). A method that moves to a mixin
   module and references those names will resolve them in ITS module's globals, so the patch on `src.pipeline`
   silently stops applying — see §5 (this is the top silent-behaviour risk of the whole exercise).

---

## WHEN TO RUN THIS

**Measured 2026-10-01 against current main.** Over the last fourteen days, 125 commits touched `src/pipeline.py`.
Twenty of them overlap step 1's range (prompt facts) and sixty-three overlap step 4's range (exits). A pure-move
PR conflicts with ANY single touch inside its range, because the whole range is rewritten. Dividing those touch
rates into fourteen days: **step 1's mapping survives roughly one day of normal desk activity; step 4's survives a
few hours.** That is the measurement, not an opinion about it.

**Ruling — risk route, 2026-10-01.** The split starts AFTER today's trading sessions have finished, not before,
and the cluster mapping is re-run immediately before the first step is opened. Reason: the line numbers in this
document die on every merge, and a step opened against a mapping taken hours earlier is a pure-move PR that no
longer moves the text it claims to move — which is unreviewable rather than merely conflicted. This is a timing
call made on the risk route; the owner ratified the milestone and its ordering (board item 210), not this timing.

---

## 4. Ordered extraction sequence

Rules for every step: one PR; method bodies copied verbatim (verify with `git diff --color-moved=dimmed-zebra` and a
`diff <(sed -n 'A,Bp' old) <(sed -n 'C,Dp' new)` in the PR description); the method-inventory guard from §2 passes;
the targeted test files named in the step pass; no in-flight PR still touches the moved range. Each step
is independently landable and leaves main deployable.

| Step | Move | ~Lines | Why this order | Tests to run (targeted) |
|---|---|---|---|---|
| 0 | Add the method-inventory guard test and the `SCOPED_PATHS`/ledger-id migration helper (§5). No code moves. | 0 | Makes every later step mechanically checkable. | new test only |
| 1 | `pipeline_prompt_facts.py` (I + position-facts trio) | ~3,550 | Largest reduction, lowest risk: places no orders, mostly roots. Touched by 0 open PRs. Proves the mixin mechanism on non-money code. Must update `tests/test_definition_of_done.py:511–512` path strings in the same PR. | test_definition_of_done, test_pm_memory, test_missed_opportunities, test_evening_replay, test_daily_report |
| 2 | `pipeline_protection.py` (F + H + helpers) | ~3,600 | Biggest safety win: the stop code becomes one reviewable file. Wait for PR 803 (touches 5872–6220) to land first. Carries `_WAL_SELL_SENTINEL` → re-export from `src.pipeline` (7 test imports). `_market_is_open_now` moves here → tests patching `pipeline._market_is_open_now` must be repointed. | test_stop_coverage_repair, test_stop_writeback, test_stop_out_reconciliation, test_wal_protection_side, test_stray_stop_cleanup_after_full_exit, test_zero_stop_refused, test_unreadable_stop_alerting, test_shorts_emergency_close, test_fractional_sizing |
| 3 | `pipeline_delever.py` (M) | ~1,235 | Self-contained, own tests, own banner. | test_gross_exposure_ladder, test_margin_policy, test_kill_switch |
| 4 | `pipeline_exits.py` (K + L + trigger keywords) | ~2,850 | Wait for PRs 801, 841, 828 (all inside 10207–12287). `compute_indicators` is used at 2911 and 3067 — those are in D, not K; check again at execution time. `_HARD_TRIGGER_KEYWORDS` path pinned in `tests/test_prompts_contract.py:112` (prose) — update. | test_phase3_exit_rework, test_position_reviewer, test_target_revision, test_exit_quality, test_risk_verdict_per_symbol, test_seat_heal |
| 5 | `pipeline_risk_gate.py` (D + `_refuse_queued_earnings_buys`) | ~920 | `compute_indicators` used at 2911/3067 (offsets dead) → **must** be imported into the new module AND the 67 tests (measured 2026-10-01) patching `pipeline.compute_indicators` re-pointed, or they silently test the wrong thing. | test_risk_mod_size_reconciliation, test_risk_based_sizing, test_sector_cap_unresolved*, test_sector_dial, test_phase2_risk_wiring |
| 6 | `pipeline_admission.py` (C) | ~510 | LANDED 2026-10-01: cluster C measured at 11 methods, 510 lines, and moved verbatim. Measured patch exposure, NOT the 20 sites this row implied: of the 20 `src.pipeline._get_sector` patch sites only 8 (6 in `test_universe_screen`, 2 in `test_nominations`) reach cluster C and were re-pointed; the other 12 target the hard-risk sector cap, which stays in `src/pipeline.py`. Plus 1 source-text scanner in `test_universe_screen` re-pointed, and 1 ledger citation moved into the new module (the other 35 `src/pipeline.py` citations shifted by -507). Cluster C carries no ledger ids of its own. | test_universe_screen, test_form4_edgar_coverage, test_form4_backlog_order |
| 7 | `pipeline_research_continuity.py` (S+T+U+V) | **LANDED.** Measured at execution 2026-10-01: lines 5961–7268 of `src/pipeline.py`, 1,308 lines, 28 methods — the only step whose ~1,300 estimate held. Exposure MEASURED, not estimated: 212 test references to the 28 names and 40 of them patch/set/read a name through the class or an instance (the plan says 3); every one goes through `TradingPipeline`, and NOT ONE patches `src.pipeline.<method>` on the module, so the mixin keeps all of them working — verified by running them, not assumed. `CarryForward` travelled with the code (constructed only by the moved bodies) and is re-exported from `src.pipeline`. Five module-level names the bodies read are imported into the new module rather than copied: `_persist_evidence`, `_json`, `et_today`, `NewsIntelligenceReport`, `PaidAnalysisSuspended`; no test patches any of them on `src.pipeline`. 22 `src/pipeline.py:A-B` ledger citations were re-pointed by content match (10 into the new module, 12 shifted inside `pipeline.py`); `docs/phases.yaml` `symbol_in_file` checks for `_carry_forward_macro`/`_carry_forward_news` re-pointed; `tests/test_seat_heal_wiring.py`'s `HEALABLE_CATEGORIES` src allowlist re-pointed to the new module. | test_seat_heal, test_seat_heal_wiring, test_form4_backlog_order, test_desk_sees_today, test_macro_partial_verdict |
| 8 | `pipeline_intraday.py` (R+W) | **LANDED.** Measured at execution 2026-10-01: 1,261 lines moved (clusters R and W are adjacent on main once step 7 has landed, old lines 5312–6574). | `tests/test_invariants.py` `getsource` on 4 methods — **CONFIRMED working after the move**: `getsource` resolves through the function object's own code object (`__code__.co_filename`), not through the class's defining module, so all four reads return the moved text from `src/pipeline_intraday.py`. Measured patch exposure, against no estimate in this plan: 14 sites — 13 tests in `tests/test_intraday_scan.py` plus one in `tests/test_invariants.py` patched `src.pipeline.compute_indicators` and had to be re-pointed at `src.pipeline_intraday.compute_indicators`; `tests/test_intraday_scan_crash_visibility.py` was re-pointed for the same reason. One ledger id (`_another_session_recently_active`) and eleven line-range citations moved. | test_invariants, test_intraday_scan |
| 9 | `pipeline_evening.py` (X+Y) | ~1,100 | Last of the class moves; `pipeline.py` lands at ~3,700. | test_evening_replay, test_daily_report |
| 10 **LANDED 2026-10-01** | `pipeline_stages.py` → four `stage_*.py` files, re-exported from `pipeline_stages` | 5,790 moved | PR 844 merged before this ran. Pure class moves; ranges re-derived from the AST (the plan offsets were dead). | test_pipeline_stages, test_silent_gates_recorded, test_event_risk_calendar, test_shorts_stage3 |
| 11 | `pipeline_sizing.py`, `pipeline_earnings_quality.py` | ~670 | 22 ledger site ids renamed (§5). `ps._entry_deployment_budget`/`_size_shares`/`_min_order_usd`/`_live_fill_price` patched by tests → re-point. | test_scale_in, test_subfloor_catalyst_gate, test_earnings_analyst, test_risk_based_sizing |
| 12 **LANDED 2026-10-01** | `pipeline_entry_orders.py` (28 names, 1,146 lines), `pipeline_rotation_exec.py` (21 names, 1,637 lines) | 2,783 moved | Last; the rotation question (fold into `src/rotation.py` or not) must be answered first (§6). | test_rotation_execute, test_desk_sees_today |
| 13 **LANDED 2026-10-04** | `pipeline_cost_gate.py` (paid-analysis gate: cost-circuit activation/preflight/status + suspension payloads, 6 names) and `pipeline_halt_gates.py` (kill switch + evidence gate, 2 names) | 362 moved | First move in the `pipeline_sizing` shape rather than a mixin: function-only modules whose first argument is any stub carrying the attributes read, so each gate is buildable without a `TradingPipeline`; `tests/boundary_harness.py` passes both. Measured 2026-10-04: `src/pipeline.py` 2,529 -> 2,167 lines; 8 bodies AST-identical to `origin/main` (docstring indentation aside); 0 test patches on the moved names, so the 8 one-line shims left on the class keep every caller. Over the 400-line new-file floor as one module, hence two by subject. | test_pipeline_run_gates_boundary, test_boundary_harness, `-k pipeline` |

**Status 2026-10-01 (measured, not recalled):** steps 0, 1, 2, 3, 4, 10 and 7 have landed on `main` or are landing; steps 5 and 6 are open pull requests; steps 8, 9, 11 and 12 remain. `src/pipeline.py` is 7,687 lines after step 7 (9,008 before it).

After step 9: `pipeline.py` ≈ 3,700 lines, largest new file ≈ 3,600. After step 12: `pipeline_stages.py` ≈ 1,500.
Net: two 30k-line files become 15 files, none over 3,600, with a class whose public surface is unchanged.

Docs pass per step (the doctrine): `README.md:578–579` module map; `docs/architecture/SAFETY_BOUNDARIES.md:51`
and `docs/STATE.md:15,182` cite `src/pipeline.py::<method>` — retarget the file in the same PR; ledger ids (§5).

---

## 5. Risks specific to this codebase

**Importers outside tests (all safe under mixins, since `TradingPipeline` stays in `src.pipeline`):**
`main.py:12`, `src/scheduler.py:9`, `src/backtest/engine.py:112`, `scripts/watchlist_candidates.py:120`,
`scripts/backfill_stop_out_fills.py:262`. `src/pipeline_stages.py` reads 30 private methods and 6 attributes off
the `pipeline` object it is handed: `_refresh_account_state, _full_sell_qty, _compute_deployable_cash,
_submit_protected_sell, _structural_protection_for_holding, _persist_hard_risk_block, _order_accepted,
_format_qty, _finalize_pending_protections, _filter_hard_risk_decisions, _ensure_correlation_matrix,
_compute_recent_performance, _build_position_history, _build_active_state_changes, _sweep_symbol,
_require_paid_analysis, _record_heal, _record_exit_refusal, _filter_supported_symbols,
_refuse_queued_earnings_buys, _apply_risk_modifications` and 12 `_build_*`. One of them is fetched with
`getattr(pipeline, "_resolve_gross_ceiling", None)` (`pipeline_stages.py:163`) — a rename would fall back
silently, not fail.

**Silent behaviour changes (would NOT fail loudly):**
1. **Monkeypatch targets on `src.pipeline` module globals** — `compute_indicators` (42 test sites when the plan
   was written; **67 measured 2026-10-01**), `_get_sector` (20), `_market_is_open_now` (9), `et_today` (6),
   `et_now` (2), `_reason_cites_hard_trigger` (1). Total patch exposure measured 2026-10-01: **255 patch sites
   across 28 test files.**
   A moved method resolves these in the new module; the old patch no longer reaches it, so a test that meant
   to stub the network/clock quietly exercises the real thing. Some would then hit Yahoo (CI is already red on
   two such tests). Mitigation per step: grep `monkeypatch.setattr(pipeline` and `patch("src.pipeline.` for every
   name the moved range references, and re-point in the same PR; the guard from step 0 should also assert that
   no test patches a name on `src.pipeline` that no method in `src.pipeline` references.
2. **Number-ledger site ids.** `src/number_sources.py` scopes `src/pipeline.py` and `src/pipeline_stages.py`
   explicitly (`SCOPED_PATHS`) and ids are `module.Qual.func(param)` / `...func:factor[N]`. The ledger carries
   36 ids under `src.pipeline.*` and 22 under `src.pipeline_stages.*`. A moved function changes its id; if the
   new file is not added to `SCOPED_PATHS` its numbers **drop out of the guard silently**; if it is added, the
   build fails loudly until the ids are renamed (the good outcome). Every step must add its new file to
   `SCOPED_PATHS` and rename the ids; `tests/test_number_sources.py:383` pins the old path and keeps passing
   either way — it is not a guard for this.
3. **`getattr(self, "...", None)` defaults** — 69 sites in `pipeline.py`. Under mixins they still resolve;
   under any free-function conversion they would silently take the default.
4. **Mixin MRO** — if two mixins ever define the same method name, the first in the bases list wins silently.
   The step-0 inventory guard must assert the names (239 measured 2026-10-01) are unique across all mixins.

**Loud breakers (good, but plan for them):**
- `tests/test_shorts_emergency_close.py:420` — `inspect.getsource(TradingPipeline)` returns only the class body in
  `pipeline.py`; whatever it searches for will vanish once that text moves. **Re-pointed 2026-10-01: this is a
  step 4 problem, not a step 2 one.** Measured today, the assertion pins the literal source line
  `if not existing or existing[0].qty <= 0:`, which lives inside `_midday_execute_llm_actions` — cluster L, which
  moves to `src/pipeline_exits.py` in step 4. Step 2 (protection) does not move that text. Read and re-point this
  assertion before **step 4**; `getsource(TradingPipeline)` will no longer contain the line once L moves, so the
  test must be re-pointed at `ExitEngineMixin` (or at the method object) in the step-4 PR.
- `tests/test_definition_of_done.py:461,511,512` — expects rival sites at exactly `src/pipeline.py:_build_position_history`
  and `src/pipeline.py:_build_thesis_health_context`; step 1 changes both.
- `tests/test_prompt_drift_item107.py:349` lists `"src/pipeline.py"` as a scanned site; `scripts/definition_of_done.py`
  and `tests/test_definition_of_done.py:400` reason about `src/pipeline.py` "eleven thousand lines" as the
  false-positive control — its sensitivity assumptions change when the file shrinks.
- `from src.pipeline import _WAL_SELL_SENTINEL / HARD_BLOCK_RULES / RunContext / _reason_cites_hard_trigger /
  _smart_money_refresh_sources_word / _reconciled_exit_action / _missed_ops_quality_metrics / _market_is_open_now`
  — keep every one re-exported from `src.pipeline` (HARD_BLOCK_RULES already is, per `src/risk/rules.py:889`).

**In-flight conflicts (measured from open-PR hunks; 5 of the 22 open PRs touch these files):**
PR 803 → F (5872–6220); PR 801 → K (11346); PR 841 → K/L (275, 942, 11525, 12192); PR 828 → K (937, 10207–10384);
PR 844 → `pipeline_stages` 5001–5065 (MorningResearchStage). Steps 1, 3, 5–9 conflict with none of them today.

---

## 6. What I could not determine

**Recorded 2026-10-01, not fixed here (this pass amends the document only):**
- The in-flight-PR table in §5 (PRs 803, 801, 841, 828, 844 with hunk line numbers) is from 2026-09-30 and 129
  commits have merged since; its PR numbers and every hunk offset in it must be re-scanned before any step, and
  the §4 "wait for PR N" instructions are therefore unverified as written.
- The §1/§2 cluster line ranges and the §2 "~Lines" column were not re-derived in this pass; the two file totals
  grew by 1,829 and 138 lines, so the per-module size estimates are low by an unmeasured amount.

- ANSWERED 2026-10-01 at step 12 execution: it does NEITHER. `src/rotation.py` is a pure decision/wording
  module — dataclasses plus functions whose only imports are `dataclasses` and `src.verdicts`; it takes no
  `pipeline`, no `ctx`, no broker and no db, and it is imported BY `pipeline_stages`, by
  `src/agents/portfolio_manager.py` and by the API. The execution block takes `pipeline`/`ctx`, places and
  cancels orders, writes the WAL and alerts the owner. Folding execution into `rotation.py` would create a
  `rotation` → `pipeline_stages` → `rotation` import cycle and pull broker/db state into a module three other
  packages import for pure text. Decision: its own module, `src/pipeline_rotation_exec.py`.
- What `tests/test_shorts_emergency_close.py:420` actually asserts on the class source — I saw the call, not the
  assertion.
- The exact `compute_indicators`/`_get_sector` reference set per cluster at execution time; the counts above are
  today's, and the 22 open PRs will move them.
- Whether the remaining 17 open PRs touch these files: `gh pr diff` returned no hunks for them, which could mean
  "no" or a failed fetch — re-run the hunk scan before each step.
- Whether `src/backtest/engine.py` constructs `TradingPipeline` in a way that depends on module-level names
  beyond the class (it imports only the class; not verified further).
- Runtime import cost / order effects of nine new modules: unmeasured; expected nil because every new import is
  already imported by `pipeline.py` today.
- I did not measure how many of the 125 pipeline-importing test files build the object via `__new__` versus the
  real constructor; the 327 count is call sites, not files.

## 7. `src/data/smart_money.py` provider: sized plan (2026-10-05, measured on main after #1493)

After the coverage lift the file is 1,796 lines; `SECForm4Provider` is lines 215-1,679 (about 1,465). No code change is proposed here; this is the dispatchable plan.

### What the class is made of (line counts, `def`-to-`def`, measured)
- Setup (`__init__`, 23 plain attributes): 92.
- Local stores (`_load_json`, history load/record/merge): 22+37+27+7 = 93.
- HTTP (`_get` with the shared rate limiter, `_remaining`, `_listed_map`, `_ciks_for_symbols`, `listed_map`): 41+6+40+17+4 = 108.
- Discovery (`_discover` alone 294, `_submissions_form4` 39, `recent_filings` 34): 367.
- Filing fetch and parse (`_archive_url`, `_submission`, `_roles`, `_parse_submission`): 6+13+18+131 = 168.
- Manifest and cache reads (`known_accessions` 36, `watched_form4_index` 26, `read_through_date` 13, `read_through_by_cik` 11, `form4_coverage` 59, `form4_freshness` 111): 256.
- Refresh orchestration (`refresh`, ONE method): 377.
- Module-level helpers outside the class (`_text`, `_number`, `_bool`, `_symbol`, `_atomic_json`, retention): about 60.

### What couples them (measured by grep of `self.X` after `__init__`)
- NO attribute is rewritten after `__init__`: all 23 are config, paths or the session, set once. There is no shared mutable instance state to untangle.
- The coupling is the FILES, read-modify-written by `refresh` and read by five other methods: `manifest_path` (refresh 1284/1555/1578; known_accessions 945; read_through 1089/1096; form4_coverage 1113), `observations_path` (refresh 1286/1577; known_accessions 950; merged_history 1682), `tickers_path` (listed_map, 5 reads), `history_path` (3), `raw_dir` (parse only, 1).
- One true global: `_LAST_REQUEST_AT` / `_RATE_LOCK` (module-level, written only inside `_get`), shared by every instance by design.
- The other coupling is LOCAL variables inside `_discover` (294) and `refresh` (377): hundreds of lines sharing locals, which is why neither can be lifted whole.

### External surface (measured)
- `SECForm4Provider`: 8 import sites; `SmartMoneySource` protocol: 6; `_RefreshDeadline`: 2; `_sale_census`: 3; the rest is `et_today` re-exported (10, belongs in `src.util.time`).
- Public methods called from outside the module: `refresh` (5), `fetch` (4), `watched_form4_index` (2), `read_through_by_cik` (2), `recent_filings`, `read_through_date`, `listed_map`, `known_accessions` (1 each); the last three of those groups are called from `src/pipeline_admission.py` only.

### Recommendation: do it, in this order, because the surface is small and there is no mutable instance state
Each step takes collaborators BY VALUE, deletes the moved methods outright (no forwarding), moves the real callers, ships a test that builds the part from fakes without importing `smart_money`. The provider only shrinks, never grows. Every new file under 400.
1. **Local stores** (history + manifest/observation read-write, `_atomic_json`, `_load_json`): about 150 lines new, 93 + helpers out. Test: round-trip through a tmp dir; the store is constructed from three paths.
2. **HTTP client** (`_get`, rate limiter, `_remaining`, `listed_map`, `_ciks_for_symbols`): about 170 new, 108 out. Takes session, user agent, interval, timeout, tickers path. Test: fake session, assert the same client object is used and the interval is honoured. RISK within this step: the limiter is module-global and must stay ONE limiter across instances; the test must pin that.
3. **Filing parser** (`_submission`, `_roles`, `_parse_submission`, `_archive_url`, with `_text/_number/_bool/_symbol`): about 230 new, 168 + 45 out. Takes the client and raw dir. Test: canned XML fixture already in the repo's tests, no network.
4. **Cache reads** (`known_accessions`, `watched_form4_index`, `read_through_*`, `form4_coverage`, `form4_freshness`): about 280 new, 256 out; reads through the step-1 store. Callers: `pipeline_admission` plus the provider. Test: store seeded with a manifest, coverage asserted.
5. **Discovery**: FIRST an in-place change inside the provider that turns `_discover`'s 294 lines into named steps with explicit inputs and outputs (adds no lines net; must be statement-neutral), THEN the lift: about 380 new in two files, 367 out.
6. **Refresh orchestrator** (`refresh`, 377): same two-stage shape, about 390 new split in two. What remains of `SECForm4Provider` is a composer of about 120 lines, built by a `build_*` function; `fetch` and `_merged_history` (about 60) move with step 4 or stay.
- RISKIEST: step 6, then 5. Their locals run through hundreds of lines, so a lift changes how data is passed, not just where code sits; the existing 167-test net (`test_form4_edgar_coverage`, `test_smart_money`, `test_form4_backlog_order`) is the only proof of unchanged behaviour. Steps 1-4 are mechanical.
- Wrinkle on the ratchet: step 5 and 6 first stages must not add statements to a file over 400, so they can only be landed as the lift itself plus a before/after equivalence test over the same recorded fixtures; the planner should size them as ONE change each, not two.
- Not measured: how many of the 167 tests would need re-pointing per step (monkeypatches of module names: 0 found by grep for `monkeypatch.*smart_money.`).
