"""PM memory: the proposals that never converted, and the reason attached.

Every other per-symbol memory PM reads is keyed on a POSITION, so a symbol
that was proposed repeatedly and never filled is invisible to all of them.
These tests pin the join that makes it visible, the threshold that keeps it
from becoming wallpaper, the verbatim reason rendering, and the empty case.
"""

import json

from src.storage.db import Database
from tests.pipeline_factory import build_pipeline


def _pipeline(tmp_path, name="t.db"):
    db = Database(str(tmp_path / name))
    db.initialize()
    pipeline = build_pipeline(db=db)
    return pipeline, db


def _target(db, run_id, decision_id, symbol, risk=1.0, days_ago=1):
    """A PM target — one proposal."""
    row_id = db.insert_specialist_evidence(
        run_id=run_id,
        decision_id=decision_id,
        agent_name="portfolio_manager",
        kind="target",
        scope="symbol",
        symbol=symbol,
        evidence_json=json.dumps(
            {
                "symbol": symbol,
                "risk_allocation_pct": risk,
                "conviction": "high",
                "thesis": "t",
                "thesis_invalid_if": "x",
            }
        ),
    )
    db.conn.execute(
        "UPDATE specialist_evidence SET timestamp = datetime('now', ?) WHERE id = ?",
        (f"-{days_ago} days", row_id),
    )
    db.conn.commit()
    return row_id


def _proposed_order(db, run_id, decision_id, symbol, days_ago=1):
    row_id = db.insert_specialist_evidence(
        run_id=run_id,
        decision_id=decision_id,
        agent_name="portfolio_manager",
        kind="proposed_order",
        scope="symbol",
        symbol=symbol,
        evidence_json=json.dumps({"action": "BUY", "symbol": symbol, "allocation_pct": 5.0}),
    )
    db.conn.execute(
        "UPDATE specialist_evidence SET timestamp = datetime('now', ?) WHERE id = ?",
        (f"-{days_ago} days", row_id),
    )
    db.conn.commit()


def _skip(db, run_id, decision_id, symbol, reason, days_ago=1):
    row_id = db.insert_specialist_evidence(
        run_id=run_id,
        decision_id=decision_id,
        agent_name="execution",
        kind="execution_skip",
        scope="symbol",
        symbol=symbol,
        evidence_json=json.dumps({"symbol": symbol, "reason": reason, "detail": "d"}),
    )
    db.conn.execute(
        "UPDATE specialist_evidence SET timestamp = datetime('now', ?) WHERE id = ?",
        (f"-{days_ago} days", row_id),
    )
    db.conn.commit()


def _constructor_drop(db, run_id, decision_id, symbol, detail="widened past noise band", days_ago=1):
    """The `pipeline_event` row `DecisionStage` persists when the
    deterministic constructor drops a target before it ever becomes a
    `proposed_order` row — see `PortfolioConstructor.last_drop_reasons`.
    """
    row_id = db.insert_specialist_evidence(
        run_id=run_id,
        decision_id=decision_id,
        agent_name="pipeline",
        kind="pipeline_event",
        scope="symbol",
        symbol=symbol,
        evidence_json=json.dumps(
            {
                "stage": "deterministic_gate",
                "outcome": "blocked",
                "reason": "constructor_dropped",
                "detail": detail,
            }
        ),
    )
    db.conn.execute(
        "UPDATE specialist_evidence SET timestamp = datetime('now', ?) WHERE id = ?",
        (f"-{days_ago} days", row_id),
    )
    db.conn.commit()


def _verdict(db, run_id, decision_id, *, approved, category="rr_fail", modifications=None, days_ago=1):
    row_id = db.insert_specialist_evidence(
        run_id=run_id,
        decision_id=decision_id,
        agent_name="risk_manager",
        kind="verdict",
        scope="run",
        symbol=None,
        evidence_json=json.dumps(
            {
                "approved": approved,
                "reason_category": category,
                "modifications": modifications or [],
                "scale_all_buys": 1.0,
                "reasoning": "r",
            }
        ),
    )
    db.conn.execute(
        "UPDATE specialist_evidence SET timestamp = datetime('now', ?) WHERE id = ?",
        (f"-{days_ago} days", row_id),
    )
    db.conn.commit()


def _trade(db, run_id, decision_id, symbol, fill_status, days_ago=1):
    row_id = db.insert_trade(
        symbol=symbol,
        action="BUY",
        qty=10,
        price=100.0,
        reasoning="r",
        run_id=run_id,
        decision_id=decision_id,
        fill_status=fill_status,
        stop_loss=90.0,
    )
    db.conn.execute(
        "UPDATE trades SET timestamp = datetime('now', ?) WHERE id = ?",
        (f"-{days_ago} days", row_id),
    )
    db.conn.commit()


# --- the join -----------------------------------------------------------


def test_join_pairs_proposal_to_fill_via_decision_id(tmp_path):
    """A proposal converts only when a FILLED trade shares its decision_id.

    `decision_id` is the sole key linking an order back to the decision that
    asked for it — every other column (run_id, symbol) is ambiguous across
    the intraday runs of one session.
    """
    pipeline, db = _pipeline(tmp_path)
    # NVDA proposed and filled under d1.
    _target(db, "r1", "d1", "NVDA", days_ago=3)
    _proposed_order(db, "r1", "d1", "NVDA", days_ago=3)
    _trade(db, "r1", "d1", "NVDA", "filled", days_ago=3)
    # AMD proposed under the SAME run but a DIFFERENT decision, never filled.
    _target(db, "r1", "d2", "AMD", days_ago=3)

    out = pipeline._build_blocked_proposals()
    assert "Conversion: 1 of 2 proposals reached a fill (50%)" in out


def test_fill_under_a_different_decision_does_not_convert(tmp_path):
    """Same symbol, same run, wrong decision — the proposal is still blocked.

    Guards the join against degrading to a symbol match, which would let any
    later fill of a name retroactively 'convert' every earlier ask for it.
    """
    pipeline, db = _pipeline(tmp_path)
    _target(db, "r1", "d1", "NVDA", days_ago=3)
    _trade(db, "r1", "d-other", "NVDA", "filled", days_ago=3)

    out = pipeline._build_blocked_proposals()
    assert "Conversion: 0 of 1 proposals reached a fill (0%)" in out


def test_a_later_non_fill_status_does_not_unconvert_an_earlier_fill(tmp_path):
    """A fill locks in conversion; a stray later row on the same key must
    not undo it.

    `trades` can carry more than one row per (decision_id, symbol) — a
    retry or a repeg. One fill converts the proposal, so a filled row wins
    over any other status regardless of arrival order (see the comment
    above the fills loop in `_build_blocked_proposals`) — a broker-side
    cleanup row landing after the fill must not reopen an already-converted
    proposal. Mutation-found: removing the "already filled" guard left every
    existing test green, because none of them logged a second trades row
    after a fill.
    """
    pipeline, db = _pipeline(tmp_path)
    _target(db, "r1", "d1", "NVDA", days_ago=3)
    _trade(db, "r1", "d1", "NVDA", "filled", days_ago=3)
    _trade(db, "r1", "d1", "NVDA", "canceled", days_ago=3)  # arrives after the fill

    out = pipeline._build_blocked_proposals()
    assert "Conversion: 1 of 1 proposals reached a fill (100%)" in out


def test_zero_sized_target_is_an_exit_not_a_proposal(tmp_path):
    """A target sized to zero closes a position; it is not an ask to get in.

    Counting exits here would inflate the blocked count with a different
    defect entirely.
    """
    pipeline, db = _pipeline(tmp_path)
    _target(db, "r1", "d1", "AAPL", risk=0.0, days_ago=3)
    _target(db, "r1", "d1", "NVDA", risk=1.0, days_ago=3)

    out = pipeline._build_blocked_proposals()
    assert "of 1 proposals" in out
    assert "AAPL" not in out


def test_legacy_target_weight_pct_still_sizes_a_proposal(tmp_path):
    """Older rows carry `target_weight_pct` instead of `risk_allocation_pct`.

    Reading only the live field would silently drop every pre-2026-08-28
    proposal out of the window.
    """
    pipeline, db = _pipeline(tmp_path)
    row_id = db.insert_specialist_evidence(
        run_id="r1",
        decision_id="d1",
        agent_name="portfolio_manager",
        kind="target",
        scope="symbol",
        symbol="JPM",
        evidence_json=json.dumps({"symbol": "JPM", "target_weight_pct": 10.0, "conviction": "high"}),
    )
    db.conn.execute(
        "UPDATE specialist_evidence SET timestamp = datetime('now', '-3 days') WHERE id = ?",
        (row_id,),
    )
    db.conn.commit()

    out = pipeline._build_blocked_proposals()
    assert "of 1 proposals" in out


# --- the threshold ------------------------------------------------------


def test_threshold_needs_three_proposals_and_zero_fills(tmp_path):
    """Two blocks is noise; three is a pattern. One fill clears the name.

    MUTATION CHECK: changing `min_proposals` to 2, or dropping the
    zero-fills condition, makes this test fail — see the assertions on TWICE
    and MIXED below.
    """
    pipeline, db = _pipeline(tmp_path)
    # THRICE: 3 proposals, 0 fills → surfaces.
    for i, day in enumerate((5, 4, 3)):
        _target(db, f"r{i}", f"d-thrice-{i}", "THRICE", days_ago=day)
    # TWICE: only 2 proposals → below the bar.
    for i, day in enumerate((5, 4)):
        _target(db, f"rt{i}", f"d-twice-{i}", "TWICE", days_ago=day)
    # MIXED: 3 proposals but one filled → not a repeat block.
    for i, day in enumerate((5, 4, 3)):
        _target(db, f"rm{i}", f"d-mixed-{i}", "MIXED", days_ago=day)
    _trade(db, "rm0", "d-mixed-0", "MIXED", "filled", days_ago=5)

    out = pipeline._build_blocked_proposals()
    assert "- THRICE: proposed 3×" in out
    assert "- TWICE:" not in out
    assert "- MIXED:" not in out


def test_window_excludes_proposals_older_than_lookback(tmp_path):
    """A repeat pattern that ended a month ago is history, not a live block."""
    pipeline, db = _pipeline(tmp_path)
    for i, day in enumerate((40, 39, 38)):
        _target(db, f"r{i}", f"d-old-{i}", "STALE", days_ago=day)

    out = pipeline._build_blocked_proposals(lookback_days=21)
    assert out == ""  # nothing in the window at all
    wide = pipeline._build_blocked_proposals(lookback_days=60)
    assert "- STALE: proposed 3×" in wide


# --- the reasons --------------------------------------------------------


def test_execution_skip_reason_is_rendered_verbatim(tmp_path):
    """`qty_zero` / `geometry_rr` / `insufficient_cash` come from the data.

    The taxonomy already exists in `execution_skip.reason`; inventing a
    parallel vocabulary would make this section and the executor's own logs
    disagree about the same event.
    """
    pipeline, db = _pipeline(tmp_path)
    for i, (day, reason) in enumerate(((5, "qty_zero"), (4, "geometry_rr"), (3, "insufficient_cash"))):
        _target(db, f"r{i}", f"d{i}", "PATH", days_ago=day)
        _proposed_order(db, f"r{i}", f"d{i}", "PATH", days_ago=day)
        _skip(db, f"r{i}", f"d{i}", "PATH", reason, days_ago=day)

    out = pipeline._build_blocked_proposals()
    assert (
        "- PATH: proposed 3× across 3 sessions, filled 0 — most recent first: insufficient_cash, geometry_rr, qty_zero"
    ) in out


def test_execution_skip_takes_priority_over_a_rejecting_verdict(tmp_path):
    """Pins the current precedence when a proposal carries BOTH a stored
    execution_skip reason and a rejecting verdict for the same decision:
    the execution_skip reason is what gets reported, not the verdict
    rejection.

    Why skips are checked first is not written down anywhere in the source
    — this test exists so a future reordering is a deliberate choice, not
    an accidental one. Mutation-found: swapping the check order left every
    existing test green, because none of them logged both an execution_skip
    and a rejecting verdict for the same key.
    """
    pipeline, db = _pipeline(tmp_path)
    _target(db, "r1", "d1", "NVDA", days_ago=3)
    _proposed_order(db, "r1", "d1", "NVDA", days_ago=3)
    _verdict(db, "r1", "d1", approved=False, category="rr_fail", days_ago=3)
    _skip(db, "r1", "d1", "NVDA", "qty_zero", days_ago=3)

    out = pipeline._build_blocked_proposals()
    assert "qty_zero" in out
    assert "rm_rejected" not in out


def test_rm_plan_rejection_carries_its_reason_category(tmp_path):
    """A rejected plan blocks every symbol in it, tagged with the category."""
    pipeline, db = _pipeline(tmp_path)
    for i, day in enumerate((5, 4, 3)):
        _target(db, f"r{i}", f"d{i}", "NVDA", days_ago=day)
        _proposed_order(db, f"r{i}", f"d{i}", "NVDA", days_ago=day)
        _verdict(db, f"r{i}", f"d{i}", approved=False, category="rr_fail", days_ago=day)

    out = pipeline._build_blocked_proposals()
    assert "rm_rejected:rr_fail, rm_rejected:rr_fail, rm_rejected:rr_fail" in out
    assert "Top blocks: rm_rejected:rr_fail × 3." in out


def test_rm_zeroing_one_symbol_blocks_only_that_symbol(tmp_path):
    """An approved plan can still kill one name by modifying it to zero."""
    pipeline, db = _pipeline(tmp_path)
    for i, day in enumerate((5, 4, 3)):
        _target(db, f"r{i}", f"d{i}", "NVDA", days_ago=day)
        _target(db, f"r{i}", f"d{i}", "XOM", days_ago=day)
        _proposed_order(db, f"r{i}", f"d{i}", "NVDA", days_ago=day)
        _proposed_order(db, f"r{i}", f"d{i}", "XOM", days_ago=day)
        _trade(db, f"r{i}", f"d{i}", "XOM", "filled", days_ago=day)
        _verdict(
            db,
            f"r{i}",
            f"d{i}",
            approved=True,
            category="clean",
            modifications=[
                {
                    "symbol": "NVDA",
                    "field": "allocation_pct",
                    "original_value": 8.0,
                    "new_value": 0.0,
                    "reason": "no catalyst",
                }
            ],
            days_ago=day,
        )

    out = pipeline._build_blocked_proposals()
    assert "- NVDA: proposed 3×" in out
    assert "rm_zeroed, rm_zeroed, rm_zeroed" in out
    assert "- XOM:" not in out


def test_constructor_dropped_symbol_is_not_blamed_for_a_rm_veto(tmp_path):
    """A Risk Manager veto only blames symbols that actually reached it.

    PM can propose more symbols than the deterministic constructor ends up
    building `proposed_order` rows for — the constructor drops some before
    the Risk Manager ever sees the plan. When the Risk Manager then vetoes
    what's left, only the symbols in its ORDERED plan were actually vetoed
    by it; blaming the constructor-dropped symbols too (as the pre-fix
    `_outcome` did, by checking `verdict.approved` before checking
    membership in `ordered`) overcounts Risk Manager vetoes with names it
    never reviewed. Mirrors `scripts/blocked_proposals_census.py::classify`,
    which already applies this `ordered` gate.
    """
    pipeline, db = _pipeline(tmp_path)
    # DROPPED1/DROPPED2: PM proposed them, but the constructor never built
    # an order for them — they never reached the Risk Manager.
    _target(db, "r1", "d1", "DROPPED1", days_ago=3)
    _target(db, "r1", "d1", "DROPPED2", days_ago=3)
    # SURVIVOR: the constructor kept it in the plan, and the Risk Manager
    # then vetoed the whole plan.
    _target(db, "r1", "d1", "SURVIVOR", days_ago=3)
    _proposed_order(db, "r1", "d1", "SURVIVOR", days_ago=3)
    _verdict(db, "r1", "d1", approved=False, category="rr_fail", days_ago=3)

    out = pipeline._build_blocked_proposals(min_proposals=1)
    assert "- SURVIVOR: proposed 1× across 1 sessions, filled 0 — most recent first: rm_rejected:rr_fail" in out
    assert "DROPPED1: proposed 1× across 1 sessions, filled 0 — most recent first: no_order_built" in out
    assert "DROPPED2: proposed 1× across 1 sessions, filled 0 — most recent first: no_order_built" in out
    # Neither dropped symbol carries the Risk Manager's reason.
    assert "DROPPED1: proposed 1× across 1 sessions, filled 0 — most recent first: rm_rejected" not in out
    assert "DROPPED2: proposed 1× across 1 sessions, filled 0 — most recent first: rm_rejected" not in out


def test_a_data_fault_reaches_the_pm_digest_as_a_fault_not_a_drop(tmp_path):
    """2026-09-12. The PM-facing digest mirrors the census: a symbol the
    constructor could not measure is `data_fault:<code>`, never
    `constructor_dropped` and never the unexplained bucket."""
    pipeline, db = _pipeline(tmp_path)
    _target(db, "r1", "d1", "A", days_ago=2)
    row_id = db.insert_specialist_evidence(
        run_id="r1",
        decision_id="d1",
        agent_name="pipeline",
        kind="pipeline_event",
        scope="symbol",
        symbol="A",
        evidence_json=json.dumps(
            {
                "stage": "deterministic_gate",
                "outcome": "unmeasurable",
                "reason": "data_fault",
                "fault": "price_history_unusable",
                "detail": "DATA FAULT: coverage=no_bars",
                "targeted": True,
            }
        ),
    )
    db.conn.execute(
        "UPDATE specialist_evidence SET timestamp = datetime('now', '-2 days') WHERE id = ?",
        (row_id,),
    )
    db.conn.commit()

    out = pipeline._build_blocked_proposals(min_proposals=1)

    assert "A: proposed 1× across 1 sessions, filled 0 — most recent first: data_fault:price_history_unusable" in out
    assert "constructor_dropped" not in out
    assert "no_order_built" not in out


def test_constructor_dropped_symbol_gets_its_own_reason_not_no_order_built(tmp_path):
    """A constructor drop carries its OWN reason, not the generic bucket.

    `pipeline_stages.DecisionStage` now persists a `pipeline_event` row
    (stage='deterministic_gate', outcome='blocked', reason=
    'constructor_dropped') for every target the constructor drops before
    it ever becomes a `proposed_order` row, recovered from
    `PortfolioConstructor.last_drop_reasons` (2026-09-03, funnel-queue item
    2). Before this test, `_outcome` had no way to see that row at all —
    `get_proposal_funnel_rows` never fetched it — so a constructor-dropped
    symbol fell into the generic `no_order_built` bucket even though the
    constructor's own reason was sitting right there in the database.

    Scenario: PM proposes A, B and C. The constructor drops A before
    building any order for it. B and C survive to `proposed_order`, and
    the Risk Manager then vetoes the smaller remaining plan. Only B and C
    were ever reviewed by the Risk Manager — A must be attributed to the
    constructor's drop, not to the Risk Manager's veto, and not to the
    unexplained `no_order_built` bucket either.
    """
    pipeline, db = _pipeline(tmp_path)
    _target(db, "r1", "d1", "A", days_ago=2)
    _target(db, "r1", "d1", "B", days_ago=2)
    _target(db, "r1", "d1", "C", days_ago=2)
    _constructor_drop(db, "r1", "d1", "A", detail="stop widened past noise band")
    _proposed_order(db, "r1", "d1", "B", days_ago=2)
    _proposed_order(db, "r1", "d1", "C", days_ago=2)
    _verdict(db, "r1", "d1", approved=False, category="rr_fail", days_ago=2)

    out = pipeline._build_blocked_proposals(min_proposals=1)

    assert "A: proposed 1× across 1 sessions, filled 0 — most recent first: constructor_dropped" in out
    assert "B: proposed 1× across 1 sessions, filled 0 — most recent first: rm_rejected:rr_fail" in out
    assert "C: proposed 1× across 1 sessions, filled 0 — most recent first: rm_rejected:rr_fail" in out
    # A must not be blamed on the Risk Manager, and must not fall into the
    # unexplained generic bucket either.
    assert "A: proposed 1× across 1 sessions, filled 0 — most recent first: rm_rejected" not in out
    assert "A: proposed 1× across 1 sessions, filled 0 — most recent first: no_order_built" not in out


def test_unfilled_order_status_is_rendered_verbatim(tmp_path):
    """`trades.fill_status` is copied through, so 'canceled' stays 'canceled'."""
    pipeline, db = _pipeline(tmp_path)
    for i, day in enumerate((5, 4, 3)):
        _target(db, f"r{i}", f"d{i}", "VLO", days_ago=day)
        _proposed_order(db, f"r{i}", f"d{i}", "VLO", days_ago=day)
        _trade(db, f"r{i}", f"d{i}", "VLO", "canceled", days_ago=day)

    out = pipeline._build_blocked_proposals()
    assert "order_canceled, order_canceled, order_canceled" in out


def test_absent_downstream_rows_are_named_as_absences(tmp_path):
    """No order built vs built-but-never-placed are different failures.

    Neither is recorded anywhere, so neither can be quoted verbatim — but
    conflating them would hide which half of the machinery dropped the ask.
    """
    pipeline, db = _pipeline(tmp_path)
    # NOORD: target only — the target never became an order.
    for i, day in enumerate((5, 4, 3)):
        _target(db, f"ra{i}", f"da{i}", "NOORD", days_ago=day)
    # NOPLACE: order built, approved, but no trade row and no skip.
    for i, day in enumerate((5, 4, 3)):
        _target(db, f"rb{i}", f"db{i}", "NOPLACE", days_ago=day)
        _proposed_order(db, f"rb{i}", f"db{i}", "NOPLACE", days_ago=day)
        _verdict(db, f"rb{i}", f"db{i}", approved=True, category="clean", days_ago=day)

    out = pipeline._build_blocked_proposals()
    assert "- NOORD: proposed 3×" in out
    assert "no_order_built, no_order_built, no_order_built" in out
    assert "- NOPLACE: proposed 3×" in out
    assert "order_not_placed, order_not_placed, order_not_placed" in out


def test_reasons_are_ordered_most_recent_first(tmp_path):
    """The line says 'most recent first'; it must actually be."""
    pipeline, db = _pipeline(tmp_path)
    _target(db, "r1", "d1", "NVDA", days_ago=6)
    _proposed_order(db, "r1", "d1", "NVDA", days_ago=6)
    _skip(db, "r1", "d1", "NVDA", "qty_zero", days_ago=6)
    _target(db, "r2", "d2", "NVDA", days_ago=4)
    _proposed_order(db, "r2", "d2", "NVDA", days_ago=4)
    _skip(db, "r2", "d2", "NVDA", "geometry_rr", days_ago=4)
    _target(db, "r3", "d3", "NVDA", days_ago=2)
    _proposed_order(db, "r3", "d3", "NVDA", days_ago=2)
    _skip(db, "r3", "d3", "NVDA", "insufficient_cash", days_ago=2)

    out = pipeline._build_blocked_proposals()
    assert "most recent first: insufficient_cash, geometry_rr, qty_zero" in out


# --- the aggregate ------------------------------------------------------


def test_aggregate_reports_conversion_rate_and_top_blocks(tmp_path):
    """The desk has no other view of its own conversion rate."""
    pipeline, db = _pipeline(tmp_path)
    _target(db, "r1", "d1", "AAA", days_ago=3)
    _trade(db, "r1", "d1", "AAA", "filled", days_ago=3)
    for i, sym in enumerate(("BBB", "CCC", "DDD")):
        _target(db, f"r{i}x", f"d{i}x", sym, days_ago=3)
        _proposed_order(db, f"r{i}x", f"d{i}x", sym, days_ago=3)
        _skip(db, f"r{i}x", f"d{i}x", sym, "qty_zero", days_ago=3)

    out = pipeline._build_blocked_proposals()
    assert "Conversion: 1 of 4 proposals reached a fill (25%)" in out
    assert "Top blocks: qty_zero × 3." in out


def test_aggregate_lists_at_most_three_blocking_reasons(tmp_path):
    pipeline, db = _pipeline(tmp_path)
    reasons = ("qty_zero", "geometry_rr", "insufficient_cash")
    # 3 of the first, 2 of the second, 1 of the third, plus 4 no_order_built.
    for n, reason in zip((3, 2, 1), reasons):
        for i in range(n):
            did = f"d-{reason}-{i}"
            _target(db, did, did, f"S{reason[:3].upper()}{i}", days_ago=3)
            _proposed_order(db, did, did, f"S{reason[:3].upper()}{i}", days_ago=3)
            _skip(db, did, did, f"S{reason[:3].upper()}{i}", reason, days_ago=3)
    out = pipeline._build_blocked_proposals()
    top_line = [ln for ln in out.split("\n") if ln.startswith("Top blocks:")]
    assert len(top_line) == 1
    assert top_line[0].count("×") == 3


# --- the empty cases ----------------------------------------------------


def test_no_repeat_offenders_renders_an_explicit_none(tmp_path):
    """A quiet section must not be mistakable for a missing one.

    With proposals on record but no repeat block, the aggregate still
    renders and the repeat list says 'none' in words — a blank here would
    read as 'the desk has no conversion data', which is a different and
    much worse claim.
    """
    pipeline, db = _pipeline(tmp_path)
    _target(db, "r1", "d1", "NVDA", days_ago=3)
    _trade(db, "r1", "d1", "NVDA", "filled", days_ago=3)
    _target(db, "r2", "d2", "XOM", days_ago=2)
    _trade(db, "r2", "d2", "XOM", "filled", days_ago=2)

    out = pipeline._build_blocked_proposals()
    assert out  # not blank
    assert "Conversion: 2 of 2 proposals reached a fill (100%)" in out
    assert "Repeat blocked names: none" in out
    assert "no symbol was proposed 3+ times without a fill" in out
    assert "\n- " not in out  # no per-symbol lines


def test_no_proposals_at_all_returns_empty_for_the_section_default(tmp_path):
    """A fresh desk yields "", so PM renders its own 'no proposals' default.

    Returning a 0-of-0 conversion line instead would put a fabricated 0%
    conversion rate in front of the model.
    """
    pipeline, db = _pipeline(tmp_path)
    out = pipeline._build_blocked_proposals()
    assert out == ""


def test_db_failure_degrades_to_empty_not_a_crash(tmp_path):
    """A memory section must never take the session down."""
    pipeline, db = _pipeline(tmp_path)

    class Boom:
        def get_proposal_funnel_rows(self, since_ts):
            raise RuntimeError("db gone")

    pipeline.db = Boom()
    assert pipeline._build_blocked_proposals() == ""


def test_corrupt_evidence_json_is_skipped_not_fatal(tmp_path, caplog):
    """One unparseable row must not blank the section — but it is logged."""
    pipeline, db = _pipeline(tmp_path)
    for i, day in enumerate((5, 4, 3)):
        _target(db, f"r{i}", f"d{i}", "NVDA", days_ago=day)
    row_id = db.insert_specialist_evidence(
        run_id="rbad",
        decision_id="dbad",
        agent_name="portfolio_manager",
        kind="target",
        scope="symbol",
        symbol="JUNK",
        evidence_json="{not json",
    )
    db.conn.execute(
        "UPDATE specialist_evidence SET timestamp = datetime('now', '-2 days') WHERE id = ?",
        (row_id,),
    )
    db.conn.commit()

    with caplog.at_level("WARNING"):
        out = pipeline._build_blocked_proposals()
    assert "- NVDA: proposed 3×" in out
    assert "JUNK" not in out
    assert any("blocked_proposals" in r.message for r in caplog.records)


# --- the section is diagnostic only -------------------------------------


def test_section_states_it_bars_nothing(tmp_path):
    """Someone else owns enforcement. This is memory, not a gate."""
    from unittest.mock import patch
    from src.agents.portfolio_manager import PortfolioManagerAgent

    with patch("anthropic.Anthropic"):
        agent = PortfolioManagerAgent(api_key="test", model="claude-opus-4-6")
        msg = agent.build_user_message(
            analyses=[],
            positions=[],
            macro_analysis=None,
            cash_balance=5000.0,
            total_value=10000.0,
            blocked_proposals=(
                "Conversion: 1 of 9 proposals reached a fill (11%) in the "
                "last 21 days.\n"
                "Top blocks: rm_rejected:rr_fail × 5.\n"
                "Repeat blocked names (3+ proposals, 0 fills):\n"
                "- NVDA: proposed 7× across 7 sessions, filled 0 — most "
                "recent first: rm_rejected:rr_fail, no_order_built, qty_zero"
            ),
        )
        assert "## Proposal Conversion" in msg
        assert "NVDA: proposed 7×" in msg
        assert "none of these symbols is barred" in msg


def test_section_default_when_no_conversion_data(tmp_path):
    from unittest.mock import patch
    from src.agents.portfolio_manager import PortfolioManagerAgent

    with patch("anthropic.Anthropic"):
        agent = PortfolioManagerAgent(api_key="test", model="claude-opus-4-6")
        msg = agent.build_user_message(
            analyses=[],
            positions=[],
            macro_analysis=None,
            cash_balance=5000.0,
            total_value=10000.0,
        )
        assert "## Proposal Conversion" in msg
        assert "no proposals on record in the last 21 days" in msg
