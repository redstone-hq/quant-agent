# Telegram message schedule

What time each mechanism kicks off, every weekday (Eastern Time), taken directly
from the desk's own configuration — not logs, so this holds regardless of how
busy a given day is. Actual send time trails kickoff by a few minutes depending
on how much the run has to process.

| Time (ET) | What kicks off | Always sends a message? |
|---|---|---|
| 6:15am | Telegram channel self-test | Only if the test fails |
| 6:30am | Price data refresh | Only if it fails |
| 8:00am | Pre-earnings check | Unconfirmed — may stay quiet with no filings |
| 8:45am | Code-matches-server check | Only if it finds drift |
| 8:50am | Scheduled-job integrity check | Only if it finds drift |
| 8:55am | To-do list health check | Only if it finds a problem |
| 8:55am | Overnight desk health report | Yes |
| 9:00am | Daily P&L report | Yes |
| 9:30am | Morning session summary | Yes |
| 9:45am–3:45pm, every 30 min | Intraday opportunity / flash check | Only if it finds something |
| 9:30am–4:00pm, every 30 min | Stop-loss repair sweep | Only if a repair is needed |
| 1:00pm | Midday session summary | Yes |
| 3:30pm | Close session summary | Yes |
| 4:30pm | Post-close desk health report | Yes |
| 6:30pm | Price data refresh #2 | Only if it fails |
| 8:00pm | Evening session summary | Yes |

All day and night, a silence watchdog also checks every 30 minutes, but only
speaks up if the desk goes quiet when it shouldn't.

**Guaranteed sends per trading day: 6.** Everything else is conditional on an
actual finding — none of it is on a bare arbitrary timer with no reason behind
it.

This does not cover alerts triggered by an actual problem (a blocked trade,
a naked position, a jammed decision seat, etc.) — those are event-driven, not
scheduled, and are tracked separately.

*Generated 2026-09-20, from the desk's own timer configuration
(`scripts/systemd/*.timer`, `scripts/run_if_et_window.sh`) and the deployed
log-health timer. Re-verify against those files if the schedule is ever
suspected to have changed.*
