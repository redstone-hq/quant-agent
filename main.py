import argparse
import logging
import os
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from src.config import load_config
from src.cost_table import refresh_pricing
from src.credentials import report_startup_credentials
from src.notifier import TelegramNotifier
from src.notifier.owner_alert_funnel import send_owner_alert_with_outcome
from src.pipeline import TradingPipeline
from src.scheduler import TradingScheduler
from src.trader_feed import format_session_result

PROJECT_ROOT = Path(__file__).resolve().parent

# Exit status of a one-shot entry that did nothing because the owner's Stop is
# in force (EX_TEMPFAIL). scripts/run_if_et_window.sh reads it: no last-run
# marker (a Start later the same day may still run the session), no ping.
OWNER_STOP_EXIT = os.EX_TEMPFAIL  # 75 on Linux; run_if_et_window.sh matches it
# `--mode honour_stop` only: Stop is in force but cancelling resting unfilled
# entries faulted (or the config/broker could not be reached). The owner switch
# alerts on it. 0 from that mode means Stop is not in force (e.g. Start won).
OWNER_STOP_CANCEL_FAULT_EXIT = os.EX_UNAVAILABLE  # 69 on Linux

# Result statuses that mean "this session did NOT do its job for a
# transient/recoverable reason" — a broker-API blip at snapshot time,
# or an earnings fetch failure. Deterministic model/validation failures are
# terminal for the slot: repeating the whole paid stack was the cost-amplifier
# behind the 2026-08-25 incident. Provider calls already have one bounded
# transient retry inside BaseAgent and the mandatory cost circuit owns the
# aggregate attempt/spend ceilings.
# returns these as a result dict WITHOUT raising, so without this the
# process exits 0, the OS-timer wrapper (scripts/run_if_et_window.sh)
# writes its last-run marker, and the slot is treated as done for the
# whole day — a 09:30 broker hiccup silently kills the entire morning
# session. Exiting non-zero makes the wrapper skip the last-run write
# so the next 30-min tick retries. Terminal "nothing to do" outcomes
# (no_trades / market_holiday / executed / reviewed / ...) are NOT
# here — those are successful completions and must exit 0. (audit F2)
_RETRYABLE_RESULT_STATUSES = frozenset(
    {
        "broker_error",
        "fetch_error",
    }
)
# `buys_unfunded` is deliberately terminal for the slot.  Re-running it used
# to repeat the full paid research -> PM -> RM chain merely because a funding
# sell settled late.  A future execution-only checkpoint may retry that
# deterministic funding path; until then, repeating paid analysis is less safe
# than leaving the approved BUY unsubmitted and reporting it truthfully.

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        RotatingFileHandler(
            PROJECT_ROOT / "quant_agent.log",
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
        ),
    ],
)
logger = logging.getLogger(__name__)


def _owner_stop_in_force(config_arg) -> bool:
    """Owner Stop at a one-shot entry, BEFORE the notifier, pricing or pipeline exist.

    Stop (owner ruling 2026-10-09) = the desk is off: pick up owner intents,
    and if Stop is in force cancel resting UNFILLED entries through a bare
    broker (protective stops kept) and exit with no session, no AI, no push.
    Every systemd timer reaches the desk through here (run_if_et_window.sh,
    run_daily_export.sh). A config that cannot be found returns False so the
    normal path reports it loudly; `--mode live` checks per job instead
    (TradingScheduler._run_safe).
    """
    from src.owner_intents import NOT_STOPPED

    return _owner_stop_outcome(config_arg, alert=True) not in (NOT_STOPPED, None)


def _owner_stop_outcome(config_arg, *, alert):
    """owner_intents.stop_outcome with the desk's credentialed broker; None when config cannot load."""
    config_path = Path(config_arg)
    if not config_path.is_absolute():
        config_path = PROJECT_ROOT / config_path
    if not config_path.exists():
        return None
    try:
        config = load_config(config_path)
    except Exception as exc:  # noqa: BLE001 - recorded; the normal path below reloads and pushes the failure
        from src.sentinel.guarded import NO_LEDGER, record_guarded_pass

        record_guarded_pass(NO_LEDGER, "main.owner_stop_config", exc, log=logger)
        return None
    db_path = getattr(getattr(config, "storage", None), "db_path", None)

    def _broker():
        from src.execution.broker import AlpacaBroker

        return AlpacaBroker(
            api_key=config.api_keys.alpaca_key, secret_key=config.api_keys.alpaca_secret, paper=config.alpaca.paper
        )

    from src.owner_intents import NOT_STOPPED, stop_outcome

    outcome = stop_outcome(db_path, _broker, alert=alert)
    if outcome != NOT_STOPPED:
        logger.warning("owner Stop in force: the desk is off; nothing run")
    return outcome


def _honour_stop_only(config_arg) -> int:
    """`--mode honour_stop`: the owner switch's Stop path and nothing else.

    Picks up owner intents and, when Stop is in force, cancels resting
    unfilled entries. Builds no notifier, pricing, AI or session and sends
    nothing: the owner switch alerts on the exit code. 75 = swept clean,
    OWNER_STOP_CANCEL_FAULT_EXIT = Stop in force but the cancel faulted or the
    config could not load, 0 = Stop not in force.
    """
    from src.owner_intents import NOT_STOPPED, SWEPT

    outcome = _owner_stop_outcome(config_arg, alert=False)
    if outcome == SWEPT:
        return OWNER_STOP_EXIT
    if outcome == NOT_STOPPED:
        return 0
    return OWNER_STOP_CANCEL_FAULT_EXIT


def _exit_if_owner_stopped(args) -> None:
    """One-shot modes exit with OWNER_STOP_EXIT while the owner's Stop is in force."""
    if args.mode == "honour_stop":
        sys.exit(_honour_stop_only(args.config))
    if args.mode != "live" and _owner_stop_in_force(args.config):
        sys.exit(OWNER_STOP_EXIT)


def main():
    parser = argparse.ArgumentParser(description="LLM Agent Quantitative Trading System")
    parser.add_argument("--config", default="config/settings.yaml", help="Path to config file")
    parser.add_argument(
        "--mode",
        choices=[
            "live",
            "once",
            "morning",
            "midday",
            "close",
            "evening",
            "intra_check",
            "intra_safety",
            "earnings_preprocess",
            "meta",
            "daily",
            "honour_stop",
        ],
        default="once",
        help="Run mode",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "For --mode meta: run the quarterly meta-reflection even when "
            "today isn't the last trading day of the quarter. Useful for "
            "manual invocation / dry runs."
        ),
    )
    args = parser.parse_args()

    # Construct the notifier and the finally-block state FIRST — before
    # anything that can crash (config loading, pricing refresh, pipeline
    # construction). A crash in any of those used to be a complete blind
    # spot: no notifier existed yet and the protective try/finally hadn't
    # started, so nothing could tell the operator the session never ran
    # (cf. the missing-Saturday-report incident — a pydantic
    # ValidationError thrown by load_config()).
    #
    # HONEST LIMIT: the notifier reads TELEGRAM_* from os.environ at
    # construction, so when the root cause is ".env was never sourced"
    # (the Saturday incident's actual trigger) the creds are missing too
    # and the FAILED push is silently dropped. This restructure covers
    # every other early crash (bad YAML while creds are exported, broken
    # pricing cache, TradingPipeline/scheduler construction); the
    # no-env case is only catchable by an external dead-man's switch
    # (see CLAUDE.md observability section).
    _exit_if_owner_stopped(args)
    notifier = TelegramNotifier()
    start = time.monotonic()
    result = None
    error: BaseException | None = None
    # Resolved once config loads; stays None on an early crash so the
    # watchdog falls back to its own project-root default rather than
    # guessing from a half-built config.
    watchdog_db_path: str | None = None
    try:
        # Disk floor FIRST, before anything reads or writes: a session that
        # cannot write is worse than one that does not start. On a breach this
        # refuses with what it needs and what it found (src/desk_disk_floor.py).
        from src.desk_disk_floor import require_desk_disk

        require_desk_disk(PROJECT_ROOT / "data", log=logger)

        # Aim the owner pause door FIRST, at nothing: a desk session whose
        # intent database is not yet known (or never known) reads UNKNOWN =
        # paused, so no startup path can place an order past the switch.
        from src.execution import owner_flags_gate

        owner_flags_gate.configure(None)

        config_path = Path(args.config)
        if not config_path.is_absolute():
            config_path = PROJECT_ROOT / config_path
        if not config_path.exists():
            logger.error("Config file not found: %s", config_path)
            # Exit with the message, not a bare code: this SystemExit is
            # caught below and pushed to Telegram, and str(SystemExit(1))
            # is just "1" — useless from a phone. A string arg keeps the
            # non-zero exit code AND gives the push (and stderr) the path.
            sys.exit(f"Config file not found: {config_path}")

        config = load_config(config_path)
        logger.info("Config loaded. Universe: %s, Paper: %s", config.trading.universe, config.alpaca.paper)

        # Say out loud, every start, how the broker credentials arrived and
        # whether either is an obvious stand-in. Never prints a value. See
        # `src/credentials.py` for why this check exists at all.
        try:
            report_startup_credentials(config.api_keys, logger=logger)
        except Exception as exc:  # noqa: BLE001 - reporting must never block a session
            logger.warning("credential delivery report failed: %s", exc)

        db_path_value = getattr(getattr(config, "storage", None), "db_path", None)
        if isinstance(db_path_value, str) and db_path_value.strip():
            watchdog_db_path = db_path_value

        # Point the already-constructed notifier at Mission Control now that
        # config is available (the notifier itself is built BEFORE this line
        # — see the comment at its construction — specifically so an early
        # crash still gets a Telegram push; that push just won't carry a
        # link). Empty string in settings.yaml → send() appends no link,
        # ever — see NotificationsConfig in src/config.py.
        notifier.mission_control_url = config.notifications.mission_control_url

        # Loud startup warning when running against the live Alpaca endpoint.
        # Operators flipping `alpaca.paper: false` in the YAML is the single
        # action that converts every subsequent BUY/SELL into a real-money
        # order — make sure they SEE the change at every startup, not just
        # the first one. Telegram operators who never look at logs still see
        # the order list itself, but a launchd one-off run is the dangerous
        # case (no Telegram, no live tail) where a misconfigured config
        # could silently flip paper → live with no human-visible signal.
        #
        # UNREACHABLE under QAMC's current governance: `AlpacaConfig` now
        # fails closed on a non-paper config, so `load_config` above raises
        # before this point (see src/config.py::_enforce_paper_only). Kept
        # deliberately — if live trading is ever authorized and that guard is
        # lifted, this warning must already be here rather than needing to be
        # remembered in the same change.
        if not config.alpaca.paper:
            logger.warning(
                "LIVE TRADING ENABLED (alpaca.paper=false). Real-money orders "
                "will be submitted via the Alpaca API key from .env. To revert "
                "to paper trading, set `alpaca.paper: true` in your config."
            )

        # Refresh LLM pricing from LiteLLM's public JSON if our cache is
        # stale (>24h). Best-effort: fetch failure or no-network falls back
        # to the in-memory PRICING dict (cache or hardcoded baseline).
        # This refresh covers direct-provider telemetry. TradingPipeline also
        # verifies the accepted OpenRouter seats against a fresh official
        # catalog; that routed-price check is a mandatory breaker input and
        # fails paid analysis closed while leaving broker safety live.
        try:
            refresh_pricing()
        except Exception as exc:
            logger.warning("pricing refresh failed at startup: %s", exc)

        # Owner command panel (instalment 1): point the broker door at the
        # intent record. Intents raised while the desk was down are picked up
        # (and a Freeze in force swept) by `pickup_owner_intents` once the
        # pipeline exists, below, before any session or scheduled job runs.
        owner_flags_gate.configure(watchdog_db_path)

        if args.mode == "live":
            # The blocking scheduler runs forever in the normal case and
            # never reaches the finally block below. Per-session Telegram
            # notifications are therefore emitted by TradingScheduler._run_safe
            # (its own finally hook + format_session_result), which mirrors
            # the one-shot path here. audit F6: this used to be only a
            # comment claiming parity while _run_safe in fact just logged.
            # If the scheduler itself crashes at startup or exits, the
            # finally block below now also catches it (previously silent).
            send_owner_alert_with_outcome(
                "🟢 quant-agent live scheduler starting",
                notifier=notifier,
                kind="scheduler_startup",
                pnl_header=False,
            )
            scheduler = TradingScheduler(config)
            scheduler.pipeline.pickup_owner_intents()
            scheduler.setup()
            scheduler.start()
            # Reached only if the blocking scheduler returns gracefully
            # (no exception). Without a result dict the finally block
            # would push the cryptic "⚪ live returned non-dict result /
            # type: NoneType" — say what actually happened instead.
            result = {"status": "scheduler_exited", "run_id": "live"}
            return

        pipeline = TradingPipeline(config)
        pipeline.pickup_owner_intents()
        if args.mode == "once" or args.mode == "morning":
            result = pipeline.run_morning()
        elif args.mode == "midday":
            result = pipeline.run_midday()
        elif args.mode == "close":
            result = pipeline.run_close()
        elif args.mode == "evening":
            result = pipeline.run_evening()
        elif args.mode == "intra_check":
            result = pipeline.run_intra_check()
        elif args.mode == "intra_safety":
            # Board item 177: the FREE safety preamble on its own schedule,
            # independent of whether the paid intraday tick runs.
            result = pipeline.run_intra_safety()
        elif args.mode == "earnings_preprocess":
            result = pipeline.run_earnings_preprocess()
        elif args.mode == "meta":
            result = pipeline.run_quarterly_meta_reflection(force=args.force)
        elif args.mode == "daily":
            result = pipeline.run_daily()
    except BaseException as exc:
        # Catch broadly (incl. SystemExit / KeyboardInterrupt) so a
        # wrapper-kill, a config-load crash, or ctrl-C still gets a
        # notification — but re-raise so the process exits with the
        # proper status code.
        error = exc
        raise
    finally:
        # The owner-flag pointer belongs to THIS session, not to the process:
        # released here so it can never name a database that is already gone.
        try:
            from src.execution import owner_flags_gate

            owner_flags_gate.release()
        except Exception as exc:  # noqa: BLE001 - never replace a session error
            logger.warning("owner-flag pointer release failed: %s", exc)
        elapsed = time.monotonic() - start
        # format_session_result reads from the DB (cost line + position
        # snapshot). DB lock contention, a corrupted run_id, or any
        # other ad-hoc failure here would raise — and a raise inside
        # `finally` replaces the in-flight pipeline exception, so the
        # operator sees the notifier failure instead of the real one.
        # Wrap so the original `error` always propagates intact.
        try:
            message = format_session_result(args.mode, result, elapsed, error=error)
        except Exception as exc:  # noqa: BLE001
            logger.exception("format_session_result raised in finally: %s", exc)
            message = None
        # THE ALERT CHANNEL'S OWN WATCHDOG. Every session proves the path it
        # is about to shout over, using the same send-and-delete probe the
        # scheduled heartbeat uses, and records the verdict in SQLite where
        # Mission Control can read it. The thing that depends on the alarm
        # is the thing that tests it — no new schedule, no new credential,
        # no external dependency. See src/alert_watchdog.py for why case (B)
        # (the box itself being dead) is explicitly NOT covered here.
        #
        # Wrapped like everything else in this finally: a watchdog that can
        # replace the in-flight session exception is worse than no watchdog.
        try:
            from src import alert_watchdog

            before = alert_watchdog.read_health(watchdog_db_path)
            verdict = alert_watchdog.verify_alert_channel(
                notifier,
                source=args.mode,
                db_path=watchdog_db_path,
            )
            message = alert_watchdog.annotate_session_message(
                message,
                mode=args.mode,
                before=before,
                result=verdict,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("alert watchdog failed in finally: %s", exc)
        # Owner's ratified rule: a bad analyst seat gets its OWN Telegram
        # message, never a line buried inside the routine session summary
        # above. Fires independently of whether `message` exists at all, so
        # it can't be suppressed by a mode's normal noise policy. See
        # notifier.maybe_alert_data_quality's docstring for why.
        #
        # What may raise that separate push is NARROWER than what counts as
        # degraded, and 2026-09-23 is when those two stopped being the same
        # question. A seat the desk still HOLDS an answer for — `expired`,
        # a carried-over news read on an intraday tick — is disclosed in the
        # session report's own freshness line ("carried over from earlier,
        # not re-read") and in its "degraded:" line, and does not also get
        # a red page saying the session "ran on incomplete research". A seat
        # whose answer never arrived still pages, unchanged. The rule lives
        # in `evidence_gate.page_worthy_statuses`, next to the categories it
        # is reasoning about; the notifier keeps its own per-seat exemptions
        # and applies them on top of whatever this leaves.
        try:
            from src import evidence_gate
            from src.notifier import maybe_alert_data_quality

            maybe_alert_data_quality(
                evidence_gate.data_quality_page_input(result),
                mode=args.mode,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("data-quality alert failed in finally: %s", exc)
        # Defect 1 (PR #978): see src/trader_feed.send_naked_position_alert.
        try:
            from src.trader_feed import send_naked_position_alert

            send_naked_position_alert(
                notifier,
                result if isinstance(result, dict) else None,
                mode=args.mode,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("naked-position alert failed in finally: %s", exc)
        if message:
            # Wrapped in its own try/except inside send(), but be doubly
            # defensive: notifier code in finally must NEVER mask the
            # original exception.
            try:
                symbols = None
                try:
                    from src.trader_feed import extract_alert_symbols

                    run_id = result.get("run_id") if isinstance(result, dict) else None
                    symbols = extract_alert_symbols(run_id, result if isinstance(result, dict) else None)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("extract_alert_symbols failed in finally: %s", exc)
                # preserve_structural_markup=True: `message` is
                # `format_session_result`'s output (src/trader_feed.py),
                # which embeds literal <b>/<blockquote expandable> tags on
                # purpose — see TelegramNotifier.send()'s docstring.
                send_owner_alert_with_outcome(
                    message,
                    notifier=notifier,
                    symbols=symbols,
                    preserve_structural_markup=True,
                    kind=args.mode,
                    run_id=run_id,
                    pnl_header=False,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("notifier crashed in finally: %s", exc)
    logger.info("Result: %s", result)

    # audit F2: exit non-zero on a retryable failure so the OS-timer
    # wrapper does NOT write its last-run marker and the next tick
    # retries. Placed AFTER the finally block so the Telegram
    # notification has already been sent — the operator still gets
    # the FAILED push, the wrapper just doesn't mark the slot done.
    status = result.get("status") if isinstance(result, dict) else None
    if status in _RETRYABLE_RESULT_STATUSES:
        logger.warning(
            "Session %s ended with retryable status %r — exiting non-zero "
            "so the OS-timer wrapper retries this slot on the next tick.",
            args.mode,
            status,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
