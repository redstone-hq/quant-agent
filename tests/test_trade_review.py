"""Trade Review aggregation on hand-built `trades` rows (display only)."""

from datetime import UTC, datetime

import pytest

from src.storage.analytics.calibration import conviction_positions
from src.storage.analytics.trade_review import (
    CAVEAT,
    build_trade_review,
    open_position_r,
    profit_factor,
    t_test_p_one_tailed,
)

NOW = datetime(2026, 10, 14, 18, 0, tzinfo=UTC)
_ID = [0]


def _row(pid, sym, action, qty, price, ts, stop=None, pnl=None):
    _ID[0] += 1
    return {
        "id": _ID[0],
        "position_id": pid,
        "symbol": sym,
        "action": action,
        "qty": qty,
        "price": price,
        "fill_qty": qty,
        "fill_price": price,
        "fill_status": "filled",
        "stop_loss": stop,
        "decision_id": None,
        "timestamp": ts,
        "realized_pnl": pnl,
        "conviction": "medium",
    }


def _trip(pid, sym, open_action, close_action, qty, entry, exit_, stop, pnl, t_open, t_close):
    return [
        _row(pid, sym, open_action, qty, entry, t_open, stop=stop),
        _row(pid, sym, close_action, qty, exit_, t_close, pnl=pnl),
    ]


def _rows():
    return (
        _trip("A", "AAA", "BUY", "SELL", 100, 50, 52, 49, 200.0, "2026-10-13 14:00:00", "2026-10-13 16:00:00")  # +2R
        + _trip("B", "BBB", "BUY", "SELL", 100, 20, 19.5, 19, -50.0, "2026-10-14 14:00:00", "2026-10-14 15:00:00")
        + _trip("C", "CCC", "SHORT", "COVER", 10, 100, 98, 102, 20.0, "2026-10-14 15:00:00", "2026-10-14 16:00:00")
        + _trip("D", "DDD", "BUY", "SELL", 10, 10, 11, None, 10.0, "2026-10-14 16:00:00", "2026-10-14 17:00:00")
        + _trip("E", "EEE", "BUY", "SELL", 10, 10, 20, 9, 100.0, "2026-10-09 14:00:00", "2026-10-09 16:00:00")
    )


def test_trades_r_and_exclusion():
    review = build_trade_review(_rows(), now=NOW)
    assert review["excluded_pre_clean_record_n"] == 1
    assert review["n_closed"] == 4
    by_sym = {t["symbol"]: t for t in review["last_trades"]}
    assert by_sym["AAA"]["r"] == 2.0 and by_sym["AAA"]["pnl"] == 200.0
    assert by_sym["BBB"]["r"] == -0.5
    assert by_sym["CCC"]["r"] == 1.0 and by_sym["CCC"]["side"] == "short"
    assert by_sym["DDD"]["r"] is None
    assert "EEE" not in by_sym


def test_r_unknown_is_excluded_from_r_stats_never_zero():
    review = build_trade_review(_rows(), now=NOW)
    allw = review["windows"]["all"]
    assert allw["n"] == 4 and allw["r_unknown"] == 1
    assert allw["expectancy_r"] == round((2.0 - 0.5 + 1.0) / 3, 2)
    assert sum(b["count"] for b in review["r_histogram"]) == 3
    assert review["quality"]["n"] == 3 and review["quality"]["caveat"] == CAVEAT


def test_today_and_week_windows():
    review = build_trade_review(_rows(), now=NOW)
    assert review["today"]["n"] == 3  # B, C, D closed on 2026-10-14
    assert review["today"]["pnl_usd"] == -50.0 + 20.0 + 10.0
    assert review["today"]["wins"] == 2 and review["today"]["losses"] == 1
    assert review["windows"]["week"]["n"] == 4  # Mon 2026-10-12 onward


def test_win_rate_matches_calibration():
    rows = _rows()
    clean = [p for p in conviction_positions(rows, 36500) if not p["open_ts"] < "2026-10-12"]
    calibration_win_rate = round(sum(1 for p in clean if p["pnl"] > 0) / len(clean) * 100, 1)
    assert build_trade_review(rows, now=NOW)["windows"]["all"]["win_rate_pct"] == calibration_win_rate == 100.0 * 3 / 4


def test_slices_carry_n():
    review = build_trade_review(_rows(), now=NOW)
    sides = {s["label"]: s for s in review["slices"]["by_side"]}
    assert sides["long"]["n"] == 3 and sides["short"]["n"] == 1
    assert all("n" in s for g in review["slices"].values() for s in g)
    assert sum(s["n"] for s in review["slices"]["by_entry_time_et"]) == 4


def test_zero_trades_empty_states():
    review = build_trade_review([], now=NOW)
    assert review["n_closed"] == 0 and review["last_trades"] == []
    assert review["windows"]["all"]["profit_factor"] == "n/a"
    assert review["windows"]["all"]["expectancy_r"] is None
    assert review["quality"]["score"] is None and review["quality"]["p_value"] is None
    assert review["drawdown_r"] == 0 and review["longest_losing_streak"] == 0


def test_profit_factor_never_infinite():
    assert profit_factor([]) == "n/a"
    assert profit_factor([5.0, 1.0]) == "no losses"
    assert profit_factor([6.0, -3.0]) == 2.0


def test_drawdown_and_streak():
    rows = _trip(
        "L1", "AAA", "BUY", "SELL", 10, 10, 9, 9.5, -10.0, "2026-10-13 14:00:00", "2026-10-13 15:00:00"
    ) + _trip("L2", "BBB", "BUY", "SELL", 10, 10, 9, 9.5, -10.0, "2026-10-13 14:00:00", "2026-10-13 16:00:00")
    review = build_trade_review(rows, now=NOW)
    assert review["longest_losing_streak"] == 2
    assert review["drawdown_r"] == 4.0  # two losses of 2R from the 0 peak
    assert [p["cumulative_r"] for p in review["cumulative_r"]] == [-2.0, -4.0]


def test_t_test_matches_known_values():
    assert t_test_p_one_tailed(0.0, 10) == pytest.approx(0.5)
    assert t_test_p_one_tailed(2.228, 10) == pytest.approx(0.025, abs=1e-3)  # textbook critical value
    assert t_test_p_one_tailed(-2.228, 10) == pytest.approx(0.975, abs=1e-3)


def test_open_position_r_uses_entry_stop():
    entry = [_row("P", "ZZZ", "BUY", 10, 50, "2026-10-14 14:00:00", stop=49)]
    out = open_position_r([{"symbol": "ZZZ", "avg_entry": 50, "current_price": 52, "qty": 10}], entry)
    assert out["ZZZ"] == {"initial_stop": 49, "unrealised_r": 2.0}
    no_stop = [_row("Q", "YYY", "BUY", 10, 50, "2026-10-14 14:00:00")]
    out = open_position_r([{"symbol": "YYY", "avg_entry": 50, "current_price": 52, "qty": 10}], no_stop)
    assert out["YYY"]["unrealised_r"] is None


def test_trade_review_endpoint_and_trades_r_column(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from src.api.server import app
    from src.storage.db import Database

    path = tmp_path / "review.db"
    db = Database(str(path))
    db.initialize()
    for r in _trip("A", "AAA", "BUY", "SELL", 100, 50, 52, 49, 200.0, "2026-10-13 14:00:00", "2026-10-13 16:00:00"):
        db.conn.execute(
            "INSERT INTO trades (symbol, action, qty, price, fill_qty, fill_price, fill_status, stop_loss, "
            "initial_stop_loss, timestamp, position_id, realized_pnl) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                r["symbol"],
                r["action"],
                r["qty"],
                r["price"],
                r["fill_qty"],
                r["fill_price"],
                "filled",
                r["stop_loss"],
                r["stop_loss"],
                r["timestamp"],
                r["position_id"],
                r["realized_pnl"],
            ),
        )
    db.conn.commit()
    import src.api.db_reads as db_reads

    monkeypatch.setattr(db_reads, "get_db_path", lambda: str(path))
    client = TestClient(app)
    body = client.get("/trade-review").json()
    assert body["state"] == "ok" and body["n_closed"] == 1
    assert body["last_trades"][0]["r"] == 2.0
    trades = client.get("/trades").json()["trades"]
    assert sorted(t["r_multiple"] is not None for t in trades) == [False, True]


def test_pnl_sums_recorded_realized_pnl_ignoring_resting_stop_moves():
    """ETN shape (prod 2026-10-13): stop moves are not exits, so every real exit
    carries a realized_pnl and the trade's P&L is their sum."""
    from src.storage.analytics.trade_review import closed_trades

    def stop_move(qty, price, ts):
        row = _row("S", "ETN", "TRAIL_STOP", qty, price, ts)
        row.update(fill_qty=None, fill_price=None, fill_status=None)
        return row

    rows = [
        _row("S", "ETN", "BUY", 10, 100.0, "2026-10-13 14:00:00", stop=95.0),
        _row("S", "ETN", "BUY", 5, 102.0, "2026-10-13 14:30:00", stop=97.0),
        _row("S", "ETN", "PARTIAL_SELL(33%)", 5, 99.0, "2026-10-13 15:00:00", pnl=-7.33),
        stop_move(10, 96.0, "2026-10-13 15:05:00"),
        stop_move(10, 97.5, "2026-10-13 15:30:00"),
        _row("S", "ETN", "SELL", 10, 98.0, "2026-10-13 16:00:00", pnl=-24.67),
    ]
    trades, excluded = closed_trades(rows)
    assert excluded == 0
    assert [t["pnl"] for t in trades] == [pytest.approx(-32.0)]
