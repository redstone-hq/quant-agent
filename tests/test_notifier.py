"""TelegramNotifier + format_session_result.

These tests cover:
  - disabled mode (missing env vars, kill switch)
  - HTTP success / failure paths (send swallows all errors)
  - truncation for messages over the Telegram 4096-char limit
  - per-mode formatting (morning/midday/close/evening/earnings/intra/meta)
  - per-mode noise policy: which result statuses are silent
  - error path (exception surfaces with type + message)
"""

from unittest.mock import MagicMock, patch

import pytest
import requests

from src.notifier import TelegramNotifier, format_session_result, was_suppressed


# === TelegramNotifier ===


def test_notifier_disabled_when_no_token(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    monkeypatch.delenv("TELEGRAM_DISABLED", raising=False)
    n = TelegramNotifier()
    assert n.enabled is False
    assert n.send("hello") is False


def test_notifier_disabled_when_no_chat_id(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "abc")
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.delenv("TELEGRAM_DISABLED", raising=False)
    n = TelegramNotifier()
    assert n.enabled is False


def test_notifier_kill_switch_disables_even_with_creds(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "abc")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    monkeypatch.setenv("TELEGRAM_DISABLED", "1")
    n = TelegramNotifier()
    assert n.enabled is False


def test_notifier_enabled_with_both_env_vars(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "abc")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    monkeypatch.delenv("TELEGRAM_DISABLED", raising=False)
    n = TelegramNotifier()
    assert n.enabled is True


def test_notifier_send_posts_to_telegram_api(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "BOT_TOK")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "CHAT_ID")
    monkeypatch.delenv("TELEGRAM_DISABLED", raising=False)
    n = TelegramNotifier()

    with patch("src.notifier.requests.post") as mock_post:
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_post.return_value = mock_response

        ok = n.send("hello world")

    assert ok is True
    mock_post.assert_called_once()
    url = mock_post.call_args.args[0]
    payload = mock_post.call_args.kwargs["json"]
    assert "BOT_TOK" in url
    assert payload["chat_id"] == "CHAT_ID"
    assert payload["text"] == "hello world"
    assert mock_post.call_args.kwargs["timeout"] == 5.0


def test_notifier_send_swallows_http_error(monkeypatch):
    """Telegram returning 500 / 429 / timeout must NEVER raise — a
    notifier failure must not cascade into a trading-session failure."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat")
    monkeypatch.delenv("TELEGRAM_DISABLED", raising=False)
    n = TelegramNotifier()

    with patch("src.notifier.requests.post") as mock_post:
        mock_post.side_effect = requests.ConnectionError("DNS hiccup")
        assert n.send("hello") is False  # swallowed, returns False

    with patch("src.notifier.requests.post") as mock_post:
        bad = MagicMock()
        bad.raise_for_status.side_effect = requests.HTTPError("429 Too Many Requests")
        mock_post.return_value = bad
        assert n.send("hello") is False


def test_notifier_failure_log_redacts_the_bot_token(monkeypatch, caplog):
    """`requests` puts the request URL — .../bot<TOKEN>/sendMessage — into
    HTTPError and ConnectionError messages, so logging the raw exception
    wrote the bot token into quant_agent.log and the journal on every
    Telegram failure. A wrong or rotated token is the most likely failure,
    i.e. it leaked exactly when the operator was most likely to share the
    log."""
    token = "9999999:SENTINELTOKEN"
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", token)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat")
    monkeypatch.delenv("TELEGRAM_DISABLED", raising=False)
    n = TelegramNotifier()
    url = f"https://api.telegram.org/bot{token}/sendMessage"

    with caplog.at_level("WARNING"), patch("src.notifier.requests.post") as mock_post:
        bad = MagicMock()
        bad.raise_for_status.side_effect = requests.HTTPError(f"401 Client Error: Unauthorized for url: {url}")
        mock_post.return_value = bad
        assert n.send("hello") is False

    assert token not in caplog.text
    assert "<redacted>" in caplog.text


def test_notifier_send_document_failure_log_redacts_the_bot_token(
    monkeypatch,
    caplog,
):
    """Same leak, same fix, on the daily P&L CSV path."""
    token = "9999999:SENTINELTOKEN"
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", token)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat")
    monkeypatch.delenv("TELEGRAM_DISABLED", raising=False)
    n = TelegramNotifier()

    with caplog.at_level("WARNING"), patch("src.notifier.requests.post") as mock_post:
        mock_post.side_effect = requests.ConnectionError(
            f"failed for url: https://api.telegram.org/bot{token}/sendDocument"
        )
        assert n.send_document(b"a,b\n1,2\n", "pnl.csv") is False

    assert token not in caplog.text
    assert "<redacted>" in caplog.text


def test_notifier_redaction_cannot_itself_escape_the_swallow(monkeypatch):
    """Redaction runs inside the except block, so it must not become a
    new way for a notifier failure to reach trading. An exception whose
    __str__ raises is the edge case logging used to absorb for us."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat")
    monkeypatch.delenv("TELEGRAM_DISABLED", raising=False)
    n = TelegramNotifier()

    class _Unprintable(Exception):
        def __str__(self):
            raise ValueError("__str__ is broken")

    with patch("src.notifier.requests.post") as mock_post:
        mock_post.side_effect = _Unprintable()
        assert n.send("hello") is False  # swallowed, not raised

    with patch("src.notifier.requests.post") as mock_post:
        mock_post.side_effect = _Unprintable()
        assert n.send_document(b"a,b\n", "pnl.csv") is False


def test_notifier_send_truncates_long_messages(monkeypatch):
    """Telegram caps at 4096 chars; we leave a small margin and append
    a truncation marker."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat")
    monkeypatch.delenv("TELEGRAM_DISABLED", raising=False)
    n = TelegramNotifier()

    huge = "x" * 10000
    with patch("src.notifier.requests.post") as mock_post:
        mock_post.return_value = MagicMock(raise_for_status=MagicMock())
        n.send(huge)

    sent_text = mock_post.call_args.kwargs["json"]["text"]
    assert len(sent_text) <= TelegramNotifier.MAX_MESSAGE_CHARS
    assert sent_text.endswith("[...truncated]")


def test_notifier_send_empty_text_returns_false(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat")
    monkeypatch.delenv("TELEGRAM_DISABLED", raising=False)
    n = TelegramNotifier()
    with patch("src.notifier.requests.post") as mock_post:
        assert n.send("") is False
        mock_post.assert_not_called()


# === Readability + links (owner's phone complaint: a BUY CRM alert whose
# rationale read "...strong heavy accumulation volume" just stopped there,
# mid-sentence, with no way to read more or jump into the dashboard) ===


def test_notifier_send_sets_html_parse_mode(monkeypatch):
    """parse_mode must be set so the tap-through link (<a href>) renders
    as a real link rather than literal angle-bracket text."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat")
    monkeypatch.delenv("TELEGRAM_DISABLED", raising=False)
    n = TelegramNotifier()
    with patch("src.notifier.requests.post") as mock_post:
        mock_post.return_value = MagicMock(raise_for_status=MagicMock())
        n.send("hello")
    assert mock_post.call_args.kwargs["json"]["parse_mode"] == "HTML"


def test_notifier_send_escapes_html_special_characters(monkeypatch):
    """HTML parse_mode requires '&','<','>' escaped in free text or
    Telegram rejects the whole message ("can't parse entities") — the
    single most likely way this feature breaks in production, per the
    task. Underscores and asterisks (Markdown's landmines) need NO
    escaping at all in HTML mode, which is exactly why HTML was chosen
    over MarkdownV2 for a message full of tickers and bullet '*'s."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat")
    monkeypatch.delenv("TELEGRAM_DISABLED", raising=False)
    n = TelegramNotifier()
    rationale = "BUY CRM_STOCK — strong <accumulation> & R&D volume > 70% * high conviction"
    with patch("src.notifier.requests.post") as mock_post:
        mock_post.return_value = MagicMock(raise_for_status=MagicMock())
        ok = n.send(rationale)

    assert ok is True
    sent = mock_post.call_args.kwargs["json"]["text"]
    # The raw characters that ARE special in HTML must be escaped...
    assert "<accumulation>" not in sent
    assert "&lt;accumulation&gt;" in sent
    assert "R&amp;D" in sent
    assert "&amp;" in sent
    # ...while underscore and asterisk (meaningless in HTML) pass through
    # untouched, unlike MarkdownV2 where either would corrupt the message.
    assert "CRM_STOCK" in sent
    assert "* high conviction" in sent


def test_notifier_send_no_link_when_mission_control_url_unset(monkeypatch):
    """Empty/unset base URL setting → no link at all, ever. Never a
    broken link."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat")
    monkeypatch.delenv("TELEGRAM_DISABLED", raising=False)
    n = TelegramNotifier()  # mission_control_url defaults to None/""
    assert n.mission_control_url == ""
    with patch("src.notifier.requests.post") as mock_post:
        mock_post.return_value = MagicMock(raise_for_status=MagicMock())
        ok = n.send("plain session result, no link configured")

    assert ok is True
    sent = mock_post.call_args.kwargs["json"]["text"]
    assert "<a href" not in sent
    assert "http" not in sent


def test_notifier_send_appends_configured_mission_control_link(monkeypatch):
    """When the notifier was constructed with a mission_control_url
    (mirrors main.py / TradingScheduler wiring from config), every send()
    call appends it as a real anchor tag without the caller having to
    pass anything extra."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat")
    monkeypatch.delenv("TELEGRAM_DISABLED", raising=False)
    n = TelegramNotifier(mission_control_url="https://ovh-vps.wallaby-bowfin.ts.net/cockpit/")
    with patch("src.notifier.requests.post") as mock_post:
        mock_post.return_value = MagicMock(raise_for_status=MagicMock())
        ok = n.send("session result")

    assert ok is True
    sent = mock_post.call_args.kwargs["json"]["text"]
    assert '<a href="https://ovh-vps.wallaby-bowfin.ts.net/cockpit/">' in sent
    assert "Mission Control" in sent


def test_notifier_send_link_url_param_overrides_and_can_disable(monkeypatch):
    """Per-call link_url wins over the instance default; passing an
    explicit empty string suppresses the link for that one call."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat")
    monkeypatch.delenv("TELEGRAM_DISABLED", raising=False)
    n = TelegramNotifier(mission_control_url="http://default.example/cockpit")

    with patch("src.notifier.requests.post") as mock_post:
        mock_post.return_value = MagicMock(raise_for_status=MagicMock())
        n.send("a", link_url="http://override.example/cockpit")
    assert "http://override.example/cockpit" in mock_post.call_args.kwargs["json"]["text"]
    assert "default.example" not in mock_post.call_args.kwargs["json"]["text"]

    with patch("src.notifier.requests.post") as mock_post:
        mock_post.return_value = MagicMock(raise_for_status=MagicMock())
        n.send("b", link_url="")
    assert "<a href" not in mock_post.call_args.kwargs["json"]["text"]


def test_notifier_send_escapes_url_and_label_in_link(monkeypatch):
    """The URL sits inside href="..." — a stray '"' or '&' in it must not
    break out of the attribute."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat")
    monkeypatch.delenv("TELEGRAM_DISABLED", raising=False)
    n = TelegramNotifier()
    with patch("src.notifier.requests.post") as mock_post:
        mock_post.return_value = MagicMock(raise_for_status=MagicMock())
        n.send(
            "x",
            link_url="http://example/cockpit?run=1&symbol=CRM",
            link_label='Open "Mission Control"',
        )
    sent = mock_post.call_args.kwargs["json"]["text"]
    assert 'href="http://example/cockpit?run=1&amp;symbol=CRM"' in sent
    assert "&quot;Mission Control&quot;" in sent


def test_notifier_send_truncation_reserves_room_for_the_link(monkeypatch):
    """The global safety-net clip must still respect MAX_MESSAGE_CHARS
    even when a link is appended afterward — the link must not push the
    final payload over Telegram's cap."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat")
    monkeypatch.delenv("TELEGRAM_DISABLED", raising=False)
    n = TelegramNotifier(mission_control_url="https://ovh-vps.wallaby-bowfin.ts.net/cockpit/")
    huge = "x" * 10000
    with patch("src.notifier.requests.post") as mock_post:
        mock_post.return_value = MagicMock(raise_for_status=MagicMock())
        n.send(huge)
    sent = mock_post.call_args.kwargs["json"]["text"]
    assert len(sent) <= TelegramNotifier.MAX_MESSAGE_CHARS
    assert "[...truncated]" in sent
    assert '<a href="https://ovh-vps.wallaby-bowfin.ts.net/cockpit/">' in sent


def test_notifier_failure_still_swallowed_with_link_and_escaping_active(monkeypatch):
    """The core trading-safety guarantee must hold even with the new
    escape/link/clip machinery in the send() path: a Telegram-side error
    is logged and swallowed, never raised — a notifier failure must never
    be able to propagate into a trading path."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat")
    monkeypatch.delenv("TELEGRAM_DISABLED", raising=False)
    n = TelegramNotifier(mission_control_url="https://ovh-vps.wallaby-bowfin.ts.net/cockpit/")
    with patch("src.notifier.requests.post") as mock_post:
        mock_post.side_effect = requests.HTTPError("400 Bad Request: can't parse entities")
        try:
            result = n.send("BUY CRM_STOCK <thesis> & 50% conviction * top pick")
        except Exception as exc:  # noqa: BLE001
            raise AssertionError(f"send() must never raise into a trading path; raised {exc!r}")
        assert result is False


# === Per-symbol tap-through links (`symbols=` on send()) ===


def test_notifier_send_linkifies_known_symbols(monkeypatch):
    """Every symbol named in `symbols` that also appears in the text gets
    wrapped in its own tap-through link, alongside the existing Mission
    Control link."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat")
    monkeypatch.delenv("TELEGRAM_DISABLED", raising=False)
    n = TelegramNotifier()
    with patch("src.notifier.requests.post") as mock_post:
        mock_post.return_value = MagicMock(raise_for_status=MagicMock())
        n.send("BUY CCJ qty=40 @$58.10; HOLD MSFT", symbols=["CCJ", "MSFT"])
    sent = mock_post.call_args.kwargs["json"]["text"]
    assert '<a href="https://finance.yahoo.com/quote/CCJ">CCJ</a>' in sent
    assert '<a href="https://finance.yahoo.com/quote/MSFT">MSFT</a>' in sent


def test_notifier_send_symbol_links_only_wrap_known_symbols(monkeypatch):
    """`symbols` is the whitelist — a capitalized prose word that is NOT in
    it (however ticker-shaped) must never be linkified. Free LLM rationale
    is full of words like ALL/GO/PASS that would false-positive."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat")
    monkeypatch.delenv("TELEGRAM_DISABLED", raising=False)
    n = TelegramNotifier()
    with patch("src.notifier.requests.post") as mock_post:
        mock_post.return_value = MagicMock(raise_for_status=MagicMock())
        n.send("PASS on ALL names except CCJ", symbols=["CCJ"])
    sent = mock_post.call_args.kwargs["json"]["text"]
    assert '<a href="https://finance.yahoo.com/quote/CCJ">CCJ</a>' in sent
    assert "<a href" not in sent.replace(
        '<a href="https://finance.yahoo.com/quote/CCJ">CCJ</a>',
        "",
    )
    assert "PASS on ALL names" in sent


def test_notifier_send_symbol_links_respect_word_boundaries(monkeypatch):
    """A known symbol that is also a prefix of a longer word in the text
    (e.g. ticker "CAT" inside the word "CATEGORY") must not be linked —
    only whole-word mentions count."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat")
    monkeypatch.delenv("TELEGRAM_DISABLED", raising=False)
    n = TelegramNotifier()
    with patch("src.notifier.requests.post") as mock_post:
        mock_post.return_value = MagicMock(raise_for_status=MagicMock())
        n.send("new CATEGORY leader, no CAT trade today", symbols=["CAT"])
    sent = mock_post.call_args.kwargs["json"]["text"]
    assert "CATEGORY" in sent
    assert '<a href="https://finance.yahoo.com/quote/CAT">CAT</a> trade' in sent
    assert "CATEGORY</a>" not in sent
    assert '<a href="https://finance.yahoo.com/quote/CAT">CAT</a>EGORY' not in sent


def test_notifier_send_symbol_links_ignore_non_ticker_shaped_values(monkeypatch):
    """`symbols` is caller-controlled data (ultimately DB rows); anything
    that isn't a plain ticker token must never end up inside an href."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat")
    monkeypatch.delenv("TELEGRAM_DISABLED", raising=False)
    n = TelegramNotifier()
    with patch("src.notifier.requests.post") as mock_post:
        mock_post.return_value = MagicMock(raise_for_status=MagicMock())
        n.send(
            "weird token TOOLONGSYM here",
            symbols=["<script>alert(1)</script>", "TOOLONGSYM", ""],
        )
    sent = mock_post.call_args.kwargs["json"]["text"]
    assert "<a href" not in sent
    assert "<script>" not in sent  # still HTML-escaped as ordinary text


def test_notifier_send_no_symbols_leaves_text_plain(monkeypatch):
    """Omitting `symbols` (the default) must reproduce today's behavior
    exactly — tickers stay plain text, no anchors are added."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat")
    monkeypatch.delenv("TELEGRAM_DISABLED", raising=False)
    n = TelegramNotifier()
    with patch("src.notifier.requests.post") as mock_post:
        mock_post.return_value = MagicMock(raise_for_status=MagicMock())
        n.send("BUY CCJ qty=40")
    sent = mock_post.call_args.kwargs["json"]["text"]
    assert "<a href" not in sent
    assert "BUY CCJ qty=40" in sent


def test_notifier_send_symbol_links_degrade_to_plain_text_over_budget(monkeypatch):
    """If wrapping every mention in `<a href>` would push the message over
    the length budget, the send must NOT risk truncating mid-tag (which
    would ship broken HTML and get the whole alert rejected by Telegram).
    It must fall back to the plain, safely-truncatable text instead —
    symbol links disappear, the alert still ships intact."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat")
    monkeypatch.delenv("TELEGRAM_DISABLED", raising=False)
    n = TelegramNotifier()  # no mission_control_url -> link_html is empty
    # Well under MAX_MESSAGE_CHARS (4000) as plain text...
    text = "CCJ " * 995  # 3980 chars
    assert len(text) < TelegramNotifier.MAX_MESSAGE_CHARS
    with patch("src.notifier.requests.post") as mock_post:
        mock_post.return_value = MagicMock(raise_for_status=MagicMock())
        n.send(text, symbols=["CCJ"])
    sent = mock_post.call_args.kwargs["json"]["text"]
    # ...but linkifying ~995 mentions would blow far past budget, so it
    # must degrade to plain text rather than truncate the linked version.
    assert "<a href" not in sent
    assert "[...truncated]" not in sent
    assert sent.strip() == text.strip()
    assert len(sent) <= TelegramNotifier.MAX_MESSAGE_CHARS


# === _clip_text (shared boundary-aware truncation) ===


def test_clip_text_returns_text_unchanged_when_it_fits():
    from src.notifier import _clip_text

    assert _clip_text("short text", 100) == "short text"


def test_clip_text_never_cuts_mid_word():
    """The operator's literal complaint: 'strong heavy accumulation
    volume' should never become 'strong heavy accumulat' with nothing to
    show it was cut. Every clip must land on a real word boundary."""
    from src.notifier import _clip_text

    text = (
        "CRM is showing strong heavy accumulation volume over the past "
        "three sessions, with institutional buyers stepping in on every "
        "dip below the 20-day moving average and options flow confirming "
        "the bullish tilt heading into next week's earnings print."
    )
    for budget in (20, 40, 60, 90, 130, 200, 400):
        clipped = _clip_text(text, budget)
        assert len(clipped) <= budget
        core = clipped[:-2] if clipped.endswith(" …") else clipped
        # core is always an exact prefix of the source text (never
        # rewritten), and the character right after it is either the
        # end of the string or whitespace — proof the cut landed between
        # words, not inside one.
        assert text.startswith(core)
        tail = text[len(core) : len(core) + 1]
        assert tail in ("", " ")


def test_clip_text_says_so_with_an_ellipsis_when_it_clips():
    from src.notifier import _clip_text

    text = "word " * 100
    clipped = _clip_text(text, 50)
    assert len(clipped) < len(text)
    assert "…" in clipped


def test_clip_text_falls_back_to_hard_cut_only_when_no_boundary_exists():
    """A single unbroken token (no spaces at all) has no boundary to cut
    on — this is the one case a mid-token cut is unavoidable, and the
    function must still degrade gracefully (bounded length, marker
    present) rather than raise or return something unbounded."""
    from src.notifier import _clip_text

    huge_token = "x" * 500
    clipped = _clip_text(huge_token, 50)
    assert len(clipped) <= 50
    assert "…" in clipped


# === format_session_result — raised per-field clips ===


def test_format_evening_suggested_action_survives_past_old_200_char_clip():
    """This IS the reported defect: a per-symbol suggested action used to
    be cut at 200 chars mid-sentence. A 350-char rationale (well past the
    old limit, comfortably under the new one) must now come through
    whole."""
    long_action = (
        "CRM is showing strong heavy accumulation volume across the past "
        "three trading sessions, with block prints clustering just above "
        "the 20-day moving average and options open interest skewing "
        "meaningfully toward calls — treat pullbacks toward $260 as "
        "adds rather than exits while that volume signature holds."
    )
    assert len(long_action) > 200
    result = {
        "status": "reflected",
        "run_id": "run-crm",
        "analysis": {
            "risk_rating": "elevated",
            "suggested_actions": [long_action],
        },
    }
    msg = format_session_result("evening", result, 10.0)
    assert long_action in msg  # intact, not cut mid-word at 200 chars


def test_format_evening_outlook_survives_past_old_280_char_clip():
    long_outlook = (
        "Tomorrow opens with the CPI print at 8:30 and a heavy earnings "
        "slate after the close, so expect elevated realized vol through "
        "the morning session; the desk's base case stays constructive on "
        "quality growth names but the plan is to trim into any print-day "
        "gap rather than chase strength, keeping enough dry powder to add "
        "if the tape gives back the open drawdown by midday."
    )
    assert len(long_outlook) > 280
    result = {
        "status": "reflected",
        "run_id": "run-outlook",
        "analysis": {"tomorrow_outlook": long_outlook},
    }
    msg = format_session_result("evening", result, 10.0)
    assert long_outlook in msg


# === format_session_result === (existing suite continues below)


def test_format_morning_executed_shows_orders_and_status():
    result = {
        "status": "executed",
        "run_id": "run-abc12345",
        "orders": [
            {"symbol": "NVDA", "side": "buy", "qty": 5},
            {"symbol": "AAPL", "action": "SELL", "qty": 10},
        ],
        "data_status": {"macro": "ok", "news": "ok", "tech": "ok", "earnings": "ok"},
    }
    msg = format_session_result("morning", result, 12.3)
    assert msg is not None
    assert "🟢 Morning session" in msg
    assert "Morning session — traded" in msg  # plain name + plain outcome
    # No run identifier anywhere (owner review 2026-09-18).
    assert "run_id" not in msg and "run-abc12345" not in msg
    assert "BUY 1 / SELL 1" in msg
    assert "NVDA" in msg
    assert "AAPL" in msg
    assert "took 12.3s" in msg
    assert "degraded" not in msg  # all data ok


def test_format_morning_shows_per_order_detail_with_price_and_stop():
    """Each order line should render symbol + qty + limit price + stop_loss
    (when present) so the operator doesn't need to ssh in to see what
    was actually traded. Drives the post-2026-05-12 enriched format."""
    result = {
        "status": "executed",
        "run_id": "run-rich",
        "orders": [
            # Rich shape from the post-fix broker.submit_order
            {
                "symbol": "BA",
                "side": "buy",
                "qty": 27,
                "limit_price": 238.63,
                "stop_loss_price": 230.00,
            },
            {
                "symbol": "MP",
                "side": "sell",
                "qty": 63,
                "limit_price": 66.62,
                "stop_loss_price": None,
            },
        ],
        "data_status": {"macro": "ok", "news": "ok", "tech": "ok", "earnings": "ok"},
    }
    msg = format_session_result("morning", result, 590.0)
    assert msg is not None
    # SELL listed first (closing context before opening), then BUY.
    assert "SELL  MP" in msg
    assert "BUY   BA" in msg
    assert "qty=27" in msg
    assert "qty=63" in msg
    assert "@$238.63" in msg
    assert "@$66.62" in msg
    assert "SL=$230.00" in msg
    # SELL has no SL (we don't attach stops on exits) — must not render
    # a misleading "SL=$0" or "SL=None".
    sell_line = [l for l in msg.split("\n") if "SELL  MP" in l][0]
    assert "SL=" not in sell_line


def test_format_morning_renders_all_orders_not_just_count():
    """All orders shown (10/side cap). Previous version capped at 5/side
    which dropped detail on heavy-volume days."""
    result = {
        "status": "executed",
        "run_id": "run-many",
        "orders": [
            {"symbol": f"SYM{i:02d}", "side": "buy", "qty": i, "limit_price": 100.0 + i, "stop_loss_price": 95.0 + i}
            for i in range(1, 8)
        ],
        "data_status": {"macro": "ok"},
    }
    msg = format_session_result("morning", result, 60.0)
    assert msg is not None
    for i in range(1, 8):
        assert f"SYM{i:02d}" in msg
    # 7 orders, all rendered. No "(+N more)" omission marker.
    assert "more" not in msg


def test_format_morning_caps_at_ten_per_side_with_omission_marker():
    """Edge case for unusual heavy session: 15 BUYs → 10 shown + omission."""
    result = {
        "status": "executed",
        "run_id": "run-mass",
        "orders": [
            {"symbol": f"S{i:02d}", "side": "buy", "qty": 1, "limit_price": 100, "stop_loss_price": 95}
            for i in range(15)
        ],
    }
    msg = format_session_result("morning", result, 60.0)
    assert msg is not None
    assert "(+5 more — see audit log)" in msg


def test_format_morning_no_trades_shows_zero_orders():
    result = {"status": "no_trades", "run_id": "run-x", "orders": []}
    msg = format_session_result("morning", result, 65.7)
    assert msg is not None
    assert "⚪ Morning session" in msg
    assert "orders: 0" in msg
    assert "took 1m 5s" in msg


def test_format_morning_degraded_data_flagged():
    result = {
        "status": "executed",
        "run_id": "run-x",
        "orders": [],
        "data_status": {"macro": "failed", "news": "ok", "tech": "ok", "earnings": "failed"},
    }
    msg = format_session_result("morning", result, 5.0)
    assert msg is not None
    assert "degraded" in msg
    assert "macro" in msg
    assert "earnings" in msg


def test_format_morning_truncated_smart_money_flagged_as_degraded():
    """A smart_money_analyst call that hit finish_reason=length must show up
    in the degraded banner distinctly from "ok"/"empty" — it must never be
    silently swallowed the way a genuinely quiet day (empty) or a clean run
    (ok) would be."""
    result = {
        "status": "executed",
        "run_id": "run-trunc",
        "orders": [],
        "data_status": {
            "macro": "ok",
            "news": "ok",
            "tech": "ok",
            "earnings": "ok",
            "smart_money": "truncated",
        },
    }
    msg = format_session_result("morning", result, 5.0)
    assert msg is not None
    assert "degraded" in msg
    assert "smart_money" in msg


def test_format_morning_empty_smart_money_not_flagged_as_degraded():
    """Sanity check on the other side of the distinction: a real "no
    signal today" (empty) must NOT be flagged degraded — only truncation
    (or another failure) should be."""
    result = {
        "status": "executed",
        "run_id": "run-empty",
        "orders": [],
        "data_status": {
            "macro": "ok",
            "news": "ok",
            "tech": "ok",
            "earnings": "ok",
            "smart_money": "empty",
        },
    }
    msg = format_session_result("morning", result, 5.0)
    assert msg is not None
    assert "degraded" not in msg


def test_format_morning_force_delever_triggers_autonomous_intervention_banner():
    """FORCE_DELEVER is the system's hardest safety net (auto-sells
    biggest-loser-first when cash < -$1, margin disabled). Without the
    banner, a force_delever SELL looks identical to a routine SELL on
    Telegram — the operator misses the most important "system intervened"
    signal. The banner must appear before the order list."""
    result = {
        "status": "executed",
        "run_id": "run-x",
        "orders": [
            {"symbol": "TSLA", "action": "FORCE_DELEVER", "qty": 5, "limit_price": 200.0, "side": "sell"},
            {"symbol": "NVDA", "action": "BUY", "qty": 10, "limit_price": 250.0, "side": "buy"},
        ],
    }
    msg = format_session_result("morning", result, 65.7)
    assert msg is not None
    assert "🚨 AUTONOMOUS INTERVENTION" in msg
    assert "FORCE_DELEVER" in msg
    assert "TSLA" in msg
    # Per-line tag also surfaces on the specific order line
    assert "🚨FORCE" in msg


def test_format_morning_emergency_sell_triggers_autonomous_intervention_banner():
    """EMERGENCY_SELL fires from intra_check's flash-crash protection.
    Same intervention signal class as FORCE_DELEVER; same banner."""
    result = {
        "status": "executed",
        "run_id": "run-x",
        "orders": [
            {"symbol": "AMZN", "action": "EMERGENCY_SELL", "qty": 3, "limit_price": 145.0, "side": "sell"},
        ],
    }
    msg = format_session_result("morning", result, 30.0)
    assert msg is not None
    assert "🚨 AUTONOMOUS INTERVENTION" in msg
    assert "EMERGENCY_SELL" in msg
    assert "🚨EMER" in msg


def test_format_morning_routine_sells_no_intervention_banner():
    """Routine SELL/REDUCE actions must NOT trigger the intervention
    banner — habituation kills the signal."""
    result = {
        "status": "executed",
        "run_id": "run-x",
        "orders": [
            {"symbol": "AAPL", "action": "SELL", "qty": 5, "limit_price": 180.0, "side": "sell"},
            {"symbol": "META", "action": "REDUCE", "qty": 2, "limit_price": 500.0, "side": "sell"},
        ],
    }
    msg = format_session_result("morning", result, 30.0)
    assert msg is not None
    assert "🚨 AUTONOMOUS INTERVENTION" not in msg, (
        "routine SELL/REDUCE must NOT trigger the banner; only "
        "FORCE_DELEVER / EMERGENCY_SELL count as autonomous intervention"
    )


def test_format_evening_shows_risk_and_outlook():
    analysis = {
        "risk_rating": "moderate",
        "tomorrow_bias": "bullish",
        "tomorrow_conviction": "high",
        "tomorrow_outlook": "Macro is risk-on; AI capex theme intact.",
    }
    result = {"status": "analyzed", "run_id": "run-e", "analysis": analysis}
    msg = format_session_result("evening", result, 30.0)
    assert msg is not None
    # Tomorrow line groups risk / bias / conv together.
    assert "🔮 Tomorrow" in msg
    assert "risk=moderate" in msg
    assert "bias=bullish" in msg
    assert "conv=high" in msg
    assert "AI capex" in msg


def test_format_evening_shows_daily_pnl_when_present():
    """Daily P&L is the headline of the evening summary — operator
    wants to know 'did I make money today' without grepping logs."""
    result = {
        "status": "analyzed",
        "run_id": "run-e",
        "daily_pnl": 1234.56,
        "total_value": 107278.55,
        "daily_return_pct": 1.15,
        "analysis": {
            "risk_rating": "moderate",
            "tomorrow_bias": "neutral",
            "tomorrow_conviction": "medium",
            "tomorrow_outlook": "Steady tape.",
        },
    }
    msg = format_session_result("evening", result, 45.0)
    assert msg is not None
    assert "💰 Daily P&L" in msg
    assert "+$1,234.56" in msg
    # Return % is computed from daily_pnl / (total_value - daily_pnl)
    # — i.e., over PRIOR-day equity, the canonical denominator. The
    # daily_return_pct field on the result dict is ignored (it had a
    # 100x scale bug historically; the formatter sidesteps that).
    assert "+1.15%" in msg or "+1.16%" in msg
    assert "$107,278.55" in msg


def test_format_evening_return_pct_na_when_prior_equity_nonpositive():
    """When prior-day equity (total_value − daily_pnl) is ≤ 0 the return %
    is mathematically undefined. Render 'n/a', not a misleading '0.00%'
    that reads as a real flat day. The P&L dollar figure is still shown."""
    result = {
        "status": "analyzed",
        "run_id": "run-zero",
        # prior_equity = total_value - daily_pnl = -1000 - (-500) = -500 ≤ 0
        "daily_pnl": -500.0,
        "total_value": -1000.0,
        "daily_return_pct": 0.0,
    }
    msg = format_session_result("evening", result, 10.0)
    assert msg is not None
    # Assert on the Daily P&L line specifically rather than bare "n/a"
    # (other message sections could legitimately contain that text).
    assert "💰 Daily P&L: −$500.00 (n/a)" in msg, msg


def test_format_evening_shows_negative_daily_pnl():
    result = {
        "status": "analyzed",
        "run_id": "run-e",
        "daily_pnl": -373.46,
        "total_value": 107278.55,
        "analysis": {"risk_rating": "elevated"},
    }
    msg = format_session_result("evening", result, 30.0)
    assert msg is not None
    assert "💰 Daily P&L" in msg
    assert "−$373.46" in msg  # true minus sign, not a hyphen
    assert "-0.35%" in msg


def test_format_evening_prepends_operator_attention_banner_on_elevated():
    """risk_rating=elevated is the evening agent's escalation channel
    for thesis-broken holdings / macro-warning-ignored losses. The
    notifier MUST prepend a visible banner so the operator notices in
    the Telegram push without reading prose."""
    result = {
        "status": "analyzed",
        "run_id": "run-e",
        "daily_pnl": -200.0,
        "total_value": 100_000.0,
        "analysis": {"risk_rating": "elevated", "tomorrow_bias": "bearish"},
    }
    msg = format_session_result("evening", result, 30.0)
    assert msg is not None
    assert "🚨 OPERATOR ATTENTION" in msg, (
        "elevated risk_rating must trigger the OPERATOR ATTENTION "
        "banner in the Telegram body — it's the only push-time signal "
        "the operator gets for system-flagged risk."
    )
    assert "risk_rating=elevated" in msg


def test_format_evening_prepends_operator_attention_banner_on_high():
    """risk_rating=high is the strongest escalation evening can emit
    (multiple broken theses or macro warning + daily loss). Banner
    must fire."""
    result = {
        "status": "analyzed",
        "run_id": "run-e",
        "analysis": {"risk_rating": "high"},
    }
    msg = format_session_result("evening", result, 30.0)
    assert msg is not None
    assert "🚨 OPERATOR ATTENTION" in msg
    assert "risk_rating=high" in msg


def test_format_evening_no_operator_banner_on_moderate():
    """risk_rating=moderate is the everyday baseline — the banner
    MUST NOT fire, or the operator habituates to it and the signal
    becomes noise."""
    result = {
        "status": "analyzed",
        "run_id": "run-e",
        "analysis": {"risk_rating": "moderate"},
    }
    msg = format_session_result("evening", result, 30.0)
    assert msg is not None
    assert "🚨 OPERATOR ATTENTION" not in msg, (
        "moderate risk_rating must NOT trigger the operator banner. "
        "If every evening has this banner, it stops being a signal."
    )


def test_format_evening_expands_suggested_actions_on_elevated():
    """When risk_rating is elevated, the specific suggested_actions
    list must be expanded inline so the operator can act without
    opening the DB."""
    result = {
        "status": "analyzed",
        "run_id": "run-e",
        "analysis": {
            "risk_rating": "elevated",
            "suggested_actions": [
                "Sell XOM tomorrow open — thesis broken on 4th EIA build",
                "Tighten IWM stop to $248",
                "Watch NVDA for entry below $280",
            ],
        },
    }
    msg = format_session_result("evening", result, 30.0)
    assert msg is not None
    assert "⚡ Suggested actions:" in msg
    assert "Sell XOM tomorrow open" in msg
    assert "Tighten IWM stop" in msg
    assert "Watch NVDA" in msg


def test_format_evening_does_not_expand_suggested_actions_on_moderate():
    """On moderate risk_rating, the existing tomorrow_outlook line is
    enough — keep suggested_actions out of the message body to control
    noise. Operators still see them via the DB / morning PM consumption."""
    result = {
        "status": "analyzed",
        "run_id": "run-e",
        "analysis": {
            "risk_rating": "moderate",
            "suggested_actions": [
                "Trim AAPL by 2%",
                "Tighten IWM stop to $248",
            ],
            "tomorrow_outlook": "Steady tape.",
        },
    }
    msg = format_session_result("evening", result, 30.0)
    assert msg is not None
    assert "⚡ Suggested actions:" not in msg
    # The Tomorrow line still surfaces the prose outlook as before.
    assert "Steady tape" in msg


def test_format_includes_session_cost_when_db_has_rows(tmp_path, monkeypatch):
    """When a run_id matches rows in agent_logs with cost_usd populated,
    the notifier should surface cost and the actual provider-request count."""
    # Redirect the notifier's DB lookup at the module-level constant.
    # (Pre-2026-05-13 the notifier used Path("data/..."), so chdir
    # alone worked; the fix anchored the path to project root, so we
    # now monkeypatch the constant directly.)
    db_path = tmp_path / "data" / "quant_agent.db"
    monkeypatch.setattr("src.notifier._DB_PATH", db_path)
    # Build a minimal DB matching the schema notifier reads from.
    import sqlite3

    db_dir = tmp_path / "data"
    db_dir.mkdir()
    conn = sqlite3.connect(str(db_dir / "quant_agent.db"))
    conn.execute("""
        CREATE TABLE agent_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_name TEXT, run_id TEXT, cost_usd REAL
        )
    """)
    conn.executemany(
        "INSERT INTO agent_logs (agent_name, run_id, cost_usd) VALUES (?, ?, ?)",
        [
            ("tech_analyst", "run-cost-demo", 3.45),
            ("portfolio_manager", "run-cost-demo", 0.90),
            ("risk_manager", "run-cost-demo", 0.18),
        ],
    )
    conn.commit()
    conn.close()

    result = {
        "status": "executed",
        "run_id": "run-cost-demo",
        "orders": [{"symbol": "NVDA", "side": "buy", "qty": 5, "limit_price": 420, "stop_loss_price": 400}],
    }
    msg = format_session_result("morning", result, 600.0)
    assert msg is not None
    assert "💵 AI cost for this run: $4.53" in msg  # 3.45 + 0.90 + 0.18
    # The provider-request count is gone: engineering detail he cannot act on.
    assert "provider request" not in msg


def test_format_omits_cost_line_when_no_db(tmp_path, monkeypatch):
    """No DB file → cost line is omitted (not '$?.??' noise)."""
    # Point the notifier at a non-existent DB path under tmp_path so
    # this test doesn't accidentally read the real project DB.
    monkeypatch.setattr(
        "src.notifier._DB_PATH",
        tmp_path / "data" / "quant_agent.db",
    )
    result = {"status": "executed", "run_id": "run-x", "orders": []}
    msg = format_session_result("morning", result, 60.0)
    assert msg is not None
    assert "💵 AI cost" not in msg


def test_format_flags_cost_unknown_when_any_row_has_null_cost(tmp_path, monkeypatch):
    """Mixed-pricing-coverage row set: at least one agent's model isn't
    in cost_table.PRICING and stored NULL. The session push should
    surface the gap rather than fake a partial sum."""
    db_path = tmp_path / "data" / "quant_agent.db"
    monkeypatch.setattr("src.notifier._DB_PATH", db_path)
    import sqlite3

    db_dir = tmp_path / "data"
    db_dir.mkdir()
    conn = sqlite3.connect(str(db_dir / "quant_agent.db"))
    conn.execute("""
        CREATE TABLE agent_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_name TEXT, run_id TEXT, cost_usd REAL
        )
    """)
    conn.executemany(
        "INSERT INTO agent_logs (agent_name, run_id, cost_usd) VALUES (?, ?, ?)",
        [
            ("tech_analyst", "run-mixed", 3.45),
            ("portfolio_manager", "run-mixed", None),  # unknown model
        ],
    )
    conn.commit()
    conn.close()

    result = {"status": "executed", "run_id": "run-mixed", "orders": []}
    msg = format_session_result("morning", result, 60.0)
    assert msg is not None
    assert "AI cost for this run: not available" in msg
    assert "has no price on file" in msg
    # No fabricated figure, and no provider-request count beside it.
    assert "$?.??" not in msg and "provider request" not in msg


def test_format_evening_position_snapshot_tolerates_null_unrealized_pnl(
    tmp_path,
    monkeypatch,
):
    """Defensive: a NULL `unrealized_pnl` row (schema migration / future
    direct manipulation) must not crash the winners/losers list comps
    with `None > 0` — the outer try/except can't catch a TypeError
    inside a list comprehension at the right granularity, so the entire
    evening snapshot would silently disappear. The current schema sets
    NOT NULL so this can't fire in production today, but the filter
    costs nothing and locks the contract. Audit 2026-05-27."""
    import sqlite3

    db_path = tmp_path / "positions.db"
    conn = sqlite3.connect(str(db_path))
    # Bypass the production NOT NULL constraint to simulate data drift.
    conn.execute(
        "CREATE TABLE positions ("
        "symbol TEXT PRIMARY KEY, qty REAL, avg_entry REAL,"
        " current_price REAL, market_value REAL,"
        " unrealized_pnl REAL, sector TEXT)"
    )
    # One clean row, one NULL-pnl row.
    conn.execute("INSERT INTO positions VALUES ('AAPL', 10, 100, 105, 1050, 50, 'Tech')")
    conn.execute("INSERT INTO positions VALUES ('NVDA', 5, 200, 200, 1000, NULL, 'Tech')")
    conn.commit()
    conn.close()

    monkeypatch.setattr("src.notifier._DB_PATH", db_path)
    result = {
        "status": "analyzed",
        "run_id": "run-e",
        "daily_pnl": 0.0,
        "total_value": 2050.0,
        "analysis": {"risk_rating": "moderate"},
    }
    # Must not raise — the whole point of the defensive None filter.
    msg = format_session_result("evening", result, 30.0)
    assert msg is not None
    # AAPL has +$50 unrealized → appears in winners. NVDA's NULL row is
    # filtered out of both winners and losers.
    assert "AAPL" in msg


def test_format_evening_position_snapshot_null_entry_or_price_says_not_available(
    tmp_path,
    monkeypatch,
):
    """`_row_line` used to compute `(curr / avg - 1) * 100` with `avg`
    falsy meaning "treat as 0%", which rendered a fabricated "(+0.0%)"
    on a NULL `avg_entry` instead of admitting the return isn't known.
    Worse, a NULL `current_price` with a real `avg_entry` raised
    TypeError on `curr / avg` — uncaught at this granularity, which
    would drop the entire winners/losers block for every row, not just
    the gapped one. Both must now render "not available" for that row's
    percentage and keep the rest of the block intact."""
    import sqlite3

    db_path = tmp_path / "positions.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE positions ("
        "symbol TEXT PRIMARY KEY, qty REAL, avg_entry REAL,"
        " current_price REAL, market_value REAL,"
        " unrealized_pnl REAL, sector TEXT)"
    )
    # NULL avg_entry, both winning (positive pnl).
    conn.execute("INSERT INTO positions VALUES ('AAPL', 10, NULL, 105, 1050, 50, 'Tech')")
    # NULL current_price with a real avg_entry — the unguarded-division case.
    conn.execute("INSERT INTO positions VALUES ('MSFT', 5, 300, NULL, 1500, 25, 'Tech')")
    # One clean row so the block has something normal to compare against.
    conn.execute("INSERT INTO positions VALUES ('NVDA', 5, 200, 220, 1100, 100, 'Tech')")
    conn.commit()
    conn.close()

    monkeypatch.setattr("src.notifier._DB_PATH", db_path)
    result = {
        "status": "analyzed",
        "run_id": "run-null-entry",
        "daily_pnl": 0.0,
        "total_value": 3650.0,
        "analysis": {"risk_rating": "moderate"},
    }
    # Must not raise, and must not drop the block.
    msg = format_session_result("evening", result, 30.0)
    assert msg is not None
    assert "Top winners" in msg
    assert "AAPL" in msg and "MSFT" in msg and "NVDA" in msg
    assert "(+0.0%)" not in msg
    assert "(not available)" in msg
    assert "+10.0%" in msg  # NVDA's real percentage still computes normally


def test_format_evening_daily_return_uses_prior_equity_denominator():
    """Daily return is P&L over PRIOR-day equity, not over current.
    Using current understates losses (denominator includes today's
    draw). Picked values where the two denominators differ at the
    formatter's 2-decimal resolution so the bug surfaces clearly.

    Old (current-equity denom):
       -5000 / 95000 * 100 = -5.26%
    New (prior-equity denom, prior = 100000):
       -5000 / 100000 * 100 = -5.00%  ← the canonical figure
    """
    result = {
        "status": "analyzed",
        "run_id": "run-down",
        "daily_pnl": -5000.0,
        "total_value": 95000.0,
        "analysis": {"risk_rating": "elevated"},
    }
    msg = format_session_result("evening", result, 30.0)
    assert msg is not None
    assert "-5.00%" in msg, (
        f"daily return should be -5.00% (loss / prior-equity); "
        f"-5.26% would mean the old current-equity denom regressed back in"
    )
    assert "-5.26%" not in msg


def test_format_evening_position_snapshot_skips_gracefully_without_db(tmp_path, monkeypatch):
    """No DB at the resolved path → position snapshot section is
    skipped silently rather than crashing the message."""
    monkeypatch.setattr(
        "src.notifier._DB_PATH",
        tmp_path / "data" / "quant_agent.db",  # does not exist
    )
    result = {
        "status": "analyzed",
        "run_id": "run-e",
        "daily_pnl": 100.0,
        "total_value": 100000.0,
        "analysis": {"risk_rating": "moderate", "tomorrow_bias": "neutral"},
    }
    msg = format_session_result("evening", result, 30.0)
    assert msg is not None
    # The other sections must still be present.
    assert "💰 Daily P&L" in msg
    assert "Tomorrow" in msg
    # Position snapshot absent (no DB).
    assert "Top winners" not in msg
    assert "Underwater" not in msg


def test_db_path_is_absolute_anchored_to_project_root():
    """_DB_PATH must be anchored to the project root, not CWD.

    Pre-fix bug: notifier used Path("data/quant_agent.db") which
    resolves relative to whatever directory the caller's CWD happened
    to be. launchd/systemd set WorkingDirectory so it worked there,
    but `python /abs/path/main.py --mode evening` from another dir
    would silently lose the cost line and position snapshot.

    Pin: _DB_PATH must be absolute and live under the project tree
    so the path is stable regardless of CWD.
    """
    from src import notifier

    assert notifier._DB_PATH.is_absolute(), f"_DB_PATH must be absolute, got {notifier._DB_PATH}"
    # The path should resolve under the project root — i.e. live in
    # the same tree as notifier.py.
    notifier_root = notifier._DB_PATH.parent.parent
    src_dir = (notifier_root / "src").resolve()
    assert src_dir.exists(), (
        f"_DB_PATH={notifier._DB_PATH} should resolve under a project tree with a src/ directory; checked {src_dir}"
    )


def test_format_earnings_preprocess_with_analysis_notifies():
    result = {
        "status": "preprocessed",
        "run_id": "run-ep",
        "analyzed": 2,
        "confirmed": 2,
        "failed": 0,
    }
    msg = format_session_result("earnings_preprocess", result, 18.5)
    assert msg is not None
    assert "analyzed: 2" in msg
    assert "confirmed: 2" in msg


def test_format_earnings_preprocess_nothing_new_is_silent():
    """Most pre-market days have no fresh 10-Q. Don't ping for that —
    would be a ton of useless 'nothing happened' messages."""
    result = {"status": "nothing_new", "run_id": "run-ep", "count": 0}
    msg = format_session_result("earnings_preprocess", result, 3.0)
    assert msg is None


def test_format_earnings_preprocess_market_holiday_is_silent():
    result = {"status": "market_holiday", "run_id": "run-ep"}
    msg = format_session_result("earnings_preprocess", result, 0.5)
    assert msg is None


def test_format_earnings_preprocess_fetch_error_is_silent():
    """SEC EDGAR has occasional transient outages; the retry layer
    handles them. Don't page operator on every transient miss."""
    result = {"status": "fetch_error", "run_id": "run-ep", "error": "SEC 503"}
    msg = format_session_result("earnings_preprocess", result, 5.0)
    assert msg is None


def test_format_earnings_preprocess_suspended_shows_backlog_not_nothing():
    """2026-09-24: a suspended run used to render 'analyzed:0 confirmed:0
    failed:0' — indistinguishable from a day with no filings at all — even
    when N filings were queued and waiting on the cost circuit. The
    suspended payload now carries `filings_waiting`, and the message must
    say so rather than the misleading zero counts."""
    result = {
        "status": "paid_analysis_suspended",
        "run_id": "run-ep-suspended",
        "paid_analysis_suspended": True,
        "error": "mandatory cost circuit is open",
        "orders": [],
        "filings_waiting": [
            {"symbol": "NVDA", "form_type": "10-Q", "filing_date": "2026-09-24", "outcome": "waiting"},
        ],
        "filings_waiting_count": 1,
    }
    msg = format_session_result("earnings_preprocess", result, 4.0)
    assert msg is not None
    assert "analyzed: 0" not in msg
    assert "1 filing(s) waiting" in msg
    assert "NVDA" in msg


def test_format_earnings_preprocess_suspended_with_no_backlog_says_so():
    result = {
        "status": "paid_analysis_suspended",
        "run_id": "run-ep-suspended-empty",
        "paid_analysis_suspended": True,
        "error": "mandatory cost circuit is open",
        "orders": [],
        "filings_waiting": [],
        "filings_waiting_count": 0,
    }
    msg = format_session_result("earnings_preprocess", result, 4.0)
    assert msg is not None
    assert "no filings waiting" in msg


def test_format_intra_check_ok_is_silent():
    """intra_check fires every 30 min. The 14 silent OK ticks per day
    must NOT generate notifications."""
    result = {"status": "ok", "run_id": "intra_check-x"}
    msg = format_session_result("intra_check", result, 4.0)
    assert msg is None


def test_format_intra_check_emergency_sold_notifies():
    """When the circuit breaker actually fires, we WANT loud
    notification — it means a -3% day or worse was breached."""
    result = {
        "status": "emergency_sold",
        "run_id": "intra_check-y",
        "orders": [
            {"symbol": "NVDA", "side": "sell", "qty": 5},
            {"symbol": "AAPL", "side": "sell", "qty": 10},
        ],
        "reason": "daily-loss circuit breaker breached",
    }
    msg = format_session_result("intra_check", result, 3.2)
    assert msg is not None
    assert "🟡 Half-hourly check" in msg
    assert "EMERGENCY orders: 2" in msg
    assert "NVDA" in msg
    assert "reason" in msg


def test_format_meta_skipped_is_silent():
    """Meta runs daily but only does work on the last trading day of
    the quarter. Don't ping for the 60+ silent days per quarter."""
    result = {"status": "skipped", "reason": "not_quarter_end"}
    msg = format_session_result("meta", result, 1.0)
    assert msg is None


def test_format_meta_reflected_notifies():
    # audit round 2 (#15/#19): use the REAL producer shape — counts live as
    # LISTS nested in editor_report (ApplicationReport.to_dict), never as
    # flat top-level ints. The old fixture pinned a shape the pipeline
    # never emits, so the test passed green against dead code.
    result = {
        "status": "reflected",
        "run_id": "meta-q1",
        "period": "2026-Q1",
        "proposed_learnings_count": 4,
        "editor_report": {
            "period": "2026-Q1",
            "applied": [
                {
                    "agent_name": "tech_analyst",
                    "operation": "append",
                    "learning_text": "a",
                    "content_hash": "h1",
                    "period": "2026-Q1",
                    "prompt_path": "x",
                },
                {
                    "agent_name": "news_analyst",
                    "operation": "append",
                    "learning_text": "b",
                    "content_hash": "h2",
                    "period": "2026-Q1",
                    "prompt_path": "y",
                },
                {
                    "agent_name": "macro_analyst",
                    "operation": "append",
                    "learning_text": "c",
                    "content_hash": "h3",
                    "period": "2026-Q1",
                    "prompt_path": "z",
                },
            ],
            "rejected": [
                {
                    "agent_name": "evening_analyst",
                    "operation": "append",
                    "learning_text": "d",
                    "reason": "jaccard_similarity=0.80",
                    "period": "2026-Q1",
                },
            ],
            "rolled_off": [],
            "agents_edited": 3,
            "git_commit": "abc",
        },
    }
    msg = format_session_result("meta", result, 90.0)
    assert msg is not None
    assert "period: 2026-Q1" in msg
    assert "applied=3" in msg
    assert "rejected=1" in msg


def test_format_meta_digest_only_uses_yellow_warning_emoji():
    """`digest_only` means quarterly meta-reflection wrote the digest
    file but the LLM reflection step itself failed (Anthropic timeout
    / parse error). The learning loop is half-broken until next quarter
    — operator must see a 🟡 warning, not a 🟢 success that they skim
    past in the Telegram feed.
    """
    result = {
        "status": "digest_only",
        "run_id": "meta-q1",
        "period": "2026-Q1",
    }
    msg = format_session_result("meta", result, 30.0)
    assert msg is not None
    assert "🟡" in msg, f"digest_only should render warning emoji, got: {msg}"
    assert "🟢" not in msg


def test_format_exception_path_includes_error_type_and_message():
    """Errors always notify — and 'always' means the per-mode noise
    policy is bypassed."""
    exc = ValueError("broker timeout after 3 retries")
    msg = format_session_result("morning", None, 17.0, error=exc)
    assert msg is not None
    assert "🛑 FAILED: Morning session" in msg
    assert "ValueError" in msg
    assert "broker timeout" in msg
    assert "took 17.0s" in msg


def test_format_exception_path_overrides_noise_policy():
    """Even modes that are normally silent must notify when they crash —
    operator wants to know about intra_check / meta crashes."""
    exc = RuntimeError("OOM")
    for mode in ("intra_check", "earnings_preprocess", "meta"):
        msg = format_session_result(mode, None, 1.0, error=exc)
        assert msg is not None, f"{mode} must notify on crash"
        assert "FAILED" in msg


def test_format_unknown_status_uses_neutral_emoji():
    """Defensive: an unfamiliar status string shouldn't crash the
    formatter; falls back to the neutral white-circle emoji."""
    result = {"status": "some_new_status_we_havent_seen", "run_id": "x"}
    msg = format_session_result("morning", result, 1.0)
    assert msg is not None
    # humanize_status() has no table entry for this status. It says so in
    # words and keeps the raw code LABELLED as the desk's own, rather than
    # passing a tidied-up code off as a sentence written for the owner.
    assert "no plain wording for" in msg
    assert "some_new_status_we_havent_seen" in msg


def test_format_non_dict_result_does_not_crash():
    """run_* methods are supposed to return dicts, but defensively
    handle other types without raising."""
    msg = format_session_result("morning", None, 1.0)
    assert msg is not None
    assert "reported nothing the desk could read" in msg
    msg = format_session_result("morning", "oops", 1.0)
    assert msg is not None
    assert "reported nothing the desk could read" in msg


def test_format_elapsed_formatting():
    """Under 60s shows sub-second precision; over 60s rolls to m+s."""
    r = {"status": "executed", "run_id": "x", "orders": []}
    short = format_session_result("morning", r, 3.7)
    assert "3.7s" in short
    long_msg = format_session_result("morning", r, 247.0)
    assert "4m 7s" in long_msg


# ===========================================================================
# Auto-meta dry-run hint in evening Telegram push (Round 6).
# Without surfacing this, the operator would miss that the quarterly
# meta-reflection ran and staged proposals for review.
# ===========================================================================


def test_format_evening_surfaces_meta_dry_run_hint():
    """When evening's auto_meta result indicates the dry-run staged N
    proposals (each surfaced as a 'rejected' editor_report entry whose
    reason carries 'dry_run'), the message must include a line pointing
    to proposed_edits.json so the operator can review.

    audit round 2 (#15/#19): fixture uses the REAL pipeline shape —
    run_quarterly_meta_reflection nests the counts as LISTS inside
    editor_report; there are no flat top-level applied/rejected ints.
    The old flat-int fixture made this test pass against dead code."""
    result = {
        "status": "analyzed",
        "run_id": "run-e",
        "analysis": {"risk_rating": "moderate"},
        "auto_meta": {
            "status": "reflected",
            "period": "2026-Q1",
            "proposed_learnings_count": 2,
            "editor_report": {
                "period": "2026-Q1",
                "applied": [],
                "rejected": [
                    {
                        "agent_name": "tech_analyst",
                        "operation": "append",
                        "learning_text": "l1",
                        "reason": (
                            "dry_run=True; proposal staged to "
                            "data/evolution/2026-Q1/proposed_edits.json "
                            "for operator review"
                        ),
                        "period": "2026-Q1",
                    },
                    {
                        "agent_name": "news_analyst",
                        "operation": "append",
                        "learning_text": "l2",
                        "reason": (
                            "dry_run=True; proposal staged to "
                            "data/evolution/2026-Q1/proposed_edits.json "
                            "for operator review"
                        ),
                        "period": "2026-Q1",
                    },
                ],
                "rolled_off": [],
                "agents_edited": 0,
                "git_commit": None,
            },
        },
    }
    msg = format_session_result("evening", result, 30.0)
    assert msg is not None
    assert "🧪 meta 2026-Q1" in msg
    assert "2 proposal(s) staged" in msg
    assert "proposed_edits.json" in msg


def test_format_evening_surfaces_meta_live_apply_when_dry_run_off():
    """When dry_run=False (operator flipped it) the live apply happens.
    The hint changes shape: 'applied N' instead of the staged-dry-run
    line. audit round 2 (#15/#19): real nested editor_report shape."""
    result = {
        "status": "analyzed",
        "run_id": "run-e",
        "analysis": {"risk_rating": "moderate"},
        "auto_meta": {
            "status": "reflected",
            "period": "2026-Q1",
            "proposed_learnings_count": 1,
            "editor_report": {
                "period": "2026-Q1",
                "applied": [
                    {
                        "agent_name": "tech_analyst",
                        "operation": "append",
                        "learning_text": "l1",
                        "content_hash": "h1",
                        "period": "2026-Q1",
                        "prompt_path": "p",
                    },
                ],
                "rejected": [],
                "rolled_off": [],
                "agents_edited": 1,
                "git_commit": "sha",
            },
        },
    }
    msg = format_session_result("evening", result, 30.0)
    assert msg is not None
    assert "🧪 meta 2026-Q1: applied 1" in msg


def test_format_evening_surfaces_meta_error():
    """If the meta piggyback raised (auto_meta_error status), surface
    the error so the operator can fix it before the next quarter end
    (3 months away)."""
    result = {
        "status": "analyzed",
        "run_id": "run-e",
        "analysis": {"risk_rating": "moderate"},
        "auto_meta": {
            "status": "auto_meta_error",
            "error": "broker unreachable",
        },
    }
    msg = format_session_result("evening", result, 30.0)
    assert msg is not None
    assert "🧪 meta" in msg and "ERROR" in msg
    assert "broker unreachable" in msg


def test_format_evening_no_meta_line_on_normal_day():
    """Non-quarter-end days have auto_meta=None (or missing key).
    The evening push must NOT have the meta hint line."""
    result = {
        "status": "analyzed",
        "run_id": "run-e",
        "analysis": {"risk_rating": "moderate"},
        "auto_meta": None,  # not quarter end
    }
    msg = format_session_result("evening", result, 30.0)
    assert msg is not None
    assert "🧪 meta" not in msg


# === dead-man's banner + action ordering ===
# The deterministic daily-loss escalation banner that was also covered in
# this section is gone: its four tests were deleted 2026-09-20 with the
# account-level loss alarms (retired board item 32). Two of them asserted
# the banner FIRED; the other two asserted it stayed quiet, and those two
# would have gone on passing against a feature that no longer exists.


def test_format_evening_missing_morning_session_is_red():
    result = {
        "status": "analyzed",
        "run_id": "r",
        "daily_pnl": 0.0,
        "total_value": 100_000.0,
        "missing_sessions": ["morning", "midday"],
        "analysis": {"risk_rating": "low"},
    }
    msg = format_session_result("evening", result, 10.0)
    assert "🛑 INCOMPLETE: MORNING SESSION TODAY — morning" in msg
    assert "midday" in msg  # soft warning for the non-morning miss


def test_format_evening_suggested_actions_render_high_in_message():
    """Suggested actions must appear high in the message (right after the
    headline P&L) so the tail-clip truncation can't eat them on high-risk
    days. (The P&L history table they used to precede was replaced by the
    daily CSV export — PR #99.)"""
    result = {
        "status": "analyzed",
        "run_id": "r",
        "daily_pnl": -100.0,
        "total_value": 100_000.0,
        "analysis": {
            "risk_rating": "high",
            "suggested_actions": ["Reduce NVDA exposure", "Raise cash to 30%"],
            "tomorrow_outlook": "cautious",
        },
    }
    msg = format_session_result("evening", result, 10.0)
    assert "⚡ Suggested actions:" in msg
    assert "Reduce NVDA exposure" in msg
    # Appears before the Tomorrow block (the tail of the message).
    assert msg.index("⚡ Suggested actions:") < msg.index("🔮 Tomorrow")


def test_format_morning_renders_stop_coverage_gap_banner():
    """Spec §11.1 guard 3: a stop that IS present but covers too few shares
    reads as MIS-SIZED, never as the harsher no-stop banner."""
    result = {
        "status": "executed",
        "run_id": "r",
        "orders": [],
        "stop_coverage_gaps": [{"symbol": "NVDA", "held_qty": 10.0, "covered_qty": 4.0}],
    }
    msg = format_session_result("morning", result, 5.0)
    assert "⚠️ STOP MIS-SIZED" in msg
    assert "NO STOP AT ALL" not in msg
    assert "NVDA" in msg


def test_format_evening_renders_stop_coverage_gap_banner():
    """...and zero coverage reads as NO STOP AT ALL. The two conditions were
    a single "STOP-COVERAGE GAP" line before §11.1, which hid the worse of
    them inside the milder one."""
    result = {
        "status": "analyzed",
        "run_id": "r",
        "daily_pnl": 10.0,
        "total_value": 100_000.0,
        "stop_coverage_gaps": [{"symbol": "AAPL", "held_qty": 5.0, "covered_qty": 0.0}],
    }
    msg = format_session_result("evening", result, 5.0)
    assert "🛑🛑🛑 NO STOP AT ALL" in msg
    assert "STOP MIS-SIZED" not in msg
    assert "AAPL" in msg


def test_format_evening_uses_true_4pm_pnl_not_offset_day():
    """The 4pm-to-4pm headline uses pnl_4pm/equity_close (today's OFFICIAL
    close, computed by the pipeline) directly. Regression against the off-by-
    one that differenced account.last_equity and showed the PRIOR day's P&L:
    here the real-time figures say +$1,200 (incl. after-hours) but today
    actually closed DOWN $500 — the headline must show -$500, not +$1,200."""
    result = {
        "status": "analyzed",
        "run_id": "r",
        "daily_pnl": 1200.0,
        "total_value": 101_200.0,  # real-time (incl AH) — ignored
        "pnl_4pm": -500.0,
        "equity_close": 100_500.0,  # today's true close-to-close
        "analysis": {"risk_rating": "low"},
    }
    msg = format_session_result("evening", result, 10.0)
    assert "💰 Daily P&L: −$500.00" in msg
    assert "4pm close" in msg
    assert "$100,500.00" in msg
    assert "+$1,200" not in msg  # must not leak the real-time figure
    assert "(-0.50%)" in msg  # -500 / (100500+500) = -0.495% → -0.50%


def test_format_evening_falls_back_to_realtime_when_no_4pm():
    """No pnl_4pm/equity_close (API gap / legacy) → real-time fallback, no
    '4pm close' tag."""
    result = {
        "status": "analyzed",
        "run_id": "r",
        "daily_pnl": 1234.56,
        "total_value": 107_278.55,
        "analysis": {"risk_rating": "low"},
    }
    msg = format_session_result("evening", result, 10.0)
    assert "💰 Daily P&L: +$1,234.56" in msg
    assert "4pm close" not in msg


def test_format_evening_4pm_path_when_equity_close_zero():
    """[A] equity_close == 0.0 (account liquidated at exactly $0) is a VALID
    value — the headline must still take the 4pm path, not fall back (the old
    truthy check treated 0.0 as 'unavailable')."""
    result = {
        "status": "analyzed",
        "run_id": "r",
        "daily_pnl": -50.0,
        "total_value": 10.0,  # real-time fallback values
        "pnl_4pm": -100.0,
        "equity_close": 0.0,  # liquidated at 4pm
        "analysis": {"risk_rating": "low"},
    }
    msg = format_session_result("evening", result, 10.0)
    assert "💰 Daily P&L: −$100.00" in msg
    assert "4pm close" in msg  # took the 4pm path
    assert "   Equity: $0.00" in msg


def test_format_evening_shows_pnl_vs_risk_capital():
    """Phase 6 (§6.3b): the same day's P&L expressed against capital
    actually at risk, not just equity — `risk_capital_dollars` is threaded
    through from `TradingPipeline.run_evening`'s reuse of
    `src.risk.metrics.portfolio_heat`'s `budget_risk_dollars`."""
    result = {
        "status": "analyzed",
        "run_id": "r",
        "pnl_4pm": 500.0,
        "equity_close": 100_500.0,
        "risk_capital_dollars": 2_000.0,
        "analysis": {"risk_rating": "low"},
    }
    msg = format_session_result("evening", result, 10.0)
    assert "vs risk capital" in msg
    assert "+25.00%" in msg  # 500 / 2000
    assert "$2,000.00 at risk" in msg


def test_format_evening_risk_capital_flat_book_shows_labelled_absence():
    """A flat book (or every stop trailed past entry, releasing all risk)
    reports risk_capital_dollars=0.0 — must render a labelled absence, never
    a ZeroDivisionError and never a fabricated 0%."""
    result = {
        "status": "analyzed",
        "run_id": "r",
        "pnl_4pm": 0.0,
        "equity_close": 100_000.0,
        "risk_capital_dollars": 0.0,
        "analysis": {"risk_rating": "low"},
    }
    msg = format_session_result("evening", result, 10.0)
    assert "vs risk capital: n/a — no capital currently at risk (flat book)" in msg
    assert "%" not in msg.split("vs risk capital")[1].split("\n")[0]


def test_format_evening_risk_capital_absent_key_renders_nothing():
    """No risk_capital_dollars key at all (heat build failed, or a result
    dict from before this field existed) — say nothing rather than guess."""
    result = {
        "status": "analyzed",
        "run_id": "r",
        "pnl_4pm": 500.0,
        "equity_close": 100_500.0,
        "analysis": {"risk_rating": "low"},
    }
    msg = format_session_result("evening", result, 10.0)
    assert "vs risk capital" not in msg


def test_format_evening_risk_capital_line_uses_realtime_fallback_pnl():
    """When the 4pm figures aren't available, the risk-capital line still
    appears, keyed off the SAME real-time daily_pnl the headline fell back
    to (not silently dropped just because pnl_4pm is missing)."""
    result = {
        "status": "analyzed",
        "run_id": "r",
        "daily_pnl": 300.0,
        "total_value": 100_300.0,
        "risk_capital_dollars": 1_500.0,
        "analysis": {"risk_rating": "low"},
    }
    msg = format_session_result("evening", result, 10.0)
    assert "4pm close" not in msg
    assert "vs risk capital: +20.00%" in msg  # 300 / 1500


def test_rehearsal_mode_suppresses_operator_alerts(monkeypatch):
    """A rehearsal replays a real session, so it raises real alerts.

    Delivered unmarked to the operator's normal chat they are
    indistinguishable from production — worse than useless, because it teaches
    him to distrust the channel that exists to tell him something is wrong.

    The owner chose suppression over a second Telegram bot (2026-08-28):
    "I don't wanna bother with another bot since we're gonna turn this off at
    some point."
    """
    import importlib
    import src.notifier as notifier_module

    monkeypatch.setenv("QAMC_REHEARSAL", "1")
    importlib.reload(notifier_module)
    try:
        notifier = notifier_module.TelegramNotifier(token="fake", chat_id="1")
        assert notifier.enabled is True, "the guard must not work by disabling the notifier"
        assert notifier.send("PAID ANALYSIS SUSPENDED") is False
    finally:
        monkeypatch.delenv("QAMC_REHEARSAL", raising=False)
        importlib.reload(notifier_module)


def test_alerts_are_delivered_when_not_rehearsing(monkeypatch):
    """The guard must be off by default — production must still alert."""
    import importlib
    import src.notifier as notifier_module

    monkeypatch.delenv("QAMC_REHEARSAL", raising=False)
    importlib.reload(notifier_module)
    assert notifier_module._REHEARSAL_MODE is False

    sent = {}

    class _Resp:
        def raise_for_status(self):
            return None

    def _fake_post(url, json=None, timeout=None):
        sent["text"] = json["text"]
        return _Resp()

    monkeypatch.setattr(notifier_module.requests, "post", _fake_post)
    notifier = notifier_module.TelegramNotifier(token="fake", chat_id="1")
    assert notifier.send("real alert") is True
    assert sent["text"] == "real alert"


def test_balance_appears_on_morning_but_not_on_every_session(monkeypatch):
    """Morning only; the line comes from the one shared calculator."""
    import src.notifier as n

    monkeypatch.setattr(n, "_REHEARSAL_MODE", False)
    monkeypatch.setattr("src.notifier.session.balance_line", lambda: "AI credit: $8.81 left")
    result = {"status": "executed", "orders": [], "run_id": "r1"}
    morning = n.format_session_result("morning", result, 12.0) or ""
    midday = n.format_session_result("midday", result, 12.0) or ""
    assert "AI credit:" in morning
    assert "AI credit:" not in midday


# ===========================================================================
# Day-to-date against the self-imposed daily brake
#
# The per-session line answers "what did this session cost". It never
# answered "how close am I to the brake" — that lived only on the dashboard.
# On 2026-08-31 the desk hit that brake twice and no Telegram message ever
# showed how near it was.
# ===========================================================================


def test_day_cost_line_shows_spend_against_the_limit(monkeypatch, tmp_path):
    import sqlite3
    import src.notifier as n

    db = tmp_path / "quant_agent.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE llm_budget_days (day TEXT, baseline_cost_usd REAL, incremental_cost_usd REAL)")
    from src.trading_calendar import et_now

    conn.execute("INSERT INTO llm_budget_days VALUES (?,0,1.1395)", (et_now().strftime("%Y-%m-%d"),))
    conn.commit()
    conn.close()
    monkeypatch.setattr(n, "_DB_PATH", db)
    monkeypatch.setattr(n, "_daily_cost_limit", lambda: 2.75)
    line = n._day_cost_line()
    assert "$1.14 of the $2.75 cap for the day" in line
    # The percentage is gone: it restated the two figures already on the
    # line, and board item 89 flags percentages the owner cannot anchor.
    assert "%" not in line


def test_day_cost_line_degrades_without_a_limit(monkeypatch, tmp_path):
    import sqlite3
    import src.notifier as n

    db = tmp_path / "quant_agent.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE llm_budget_days (day TEXT, baseline_cost_usd REAL, incremental_cost_usd REAL)")
    from src.trading_calendar import et_now

    conn.execute("INSERT INTO llm_budget_days VALUES (?,0,0.5)", (et_now().strftime("%Y-%m-%d"),))
    conn.commit()
    conn.close()
    monkeypatch.setattr(n, "_DB_PATH", db)
    monkeypatch.setattr(n, "_daily_cost_limit", lambda: None)
    assert n._day_cost_line() == "📅 Spent today: $0.50 so far"


def test_day_cost_line_never_breaks_the_alert(monkeypatch):
    import src.notifier as n
    from pathlib import Path

    monkeypatch.setattr(n, "_DB_PATH", Path("/nonexistent/quant_agent.db"))
    assert n._day_cost_line() is None


def test_daily_brake_and_prepaid_balance_are_labelled_differently(monkeypatch, tmp_path):
    """Conflating 'hit the self-imposed brake' (costs nothing, resets
    tomorrow) with 'ran out of money' (needs a payment) would be expensive."""
    import sqlite3
    import src.notifier as n

    db = tmp_path / "quant_agent.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE llm_budget_days (day TEXT, baseline_cost_usd REAL, incremental_cost_usd REAL)")
    from src.trading_calendar import et_now

    conn.execute("INSERT INTO llm_budget_days VALUES (?,0,1.0)", (et_now().strftime("%Y-%m-%d"),))
    conn.commit()
    conn.close()
    monkeypatch.setattr(n, "_DB_PATH", db)
    monkeypatch.setattr(n, "_daily_cost_limit", lambda: 2.75)
    day, bal = n._day_cost_line(), "AI credit: $8.81 left"
    assert "cap for the day" in day and "left of" not in day
    assert "cap for the day" not in bal


# ===========================================================================
# maybe_alert_data_quality — a bad analyst seat must get its OWN Telegram
# message, never a line buried inside the routine session summary. Owner's
# ratified rule, reiterated 2026-09-02: separate message, severity in text.
# ===========================================================================


def test_bad_data_status_fires_a_standalone_alert():
    from src.notifier import maybe_alert_data_quality

    result = {
        "run_id": "run-abc123",
        "data_status": {"tech": "ok", "earnings": "ok", "smart_money": "truncated"},
    }
    with patch("src.notifier.send_owner_alert", return_value=True) as alert:
        fired = maybe_alert_data_quality(result, mode="morning")

    assert fired is True
    alert.assert_called_once()
    body = alert.call_args.args[0]
    assert "DATA QUALITY ALERT" in body
    assert "smart_money=truncated" in body
    # 2026-09-18: board item 89's run-identifier removal, finished for this
    # alert. A run id is a database key with nothing the owner can do about
    # it; it stays in the log line and every stored row.
    assert "run-abc123" not in body
    assert "morning" in body


def test_market_wide_blind_pages_the_owner_in_plain_words():
    """2026-09-23. The market-wide Form 4 pass read nothing for five
    sessions and nothing said so, because the seat's word for it was
    `partial` — the same word it uses on an ordinary residue. The new word
    has to reach the owner through the SAME standing alert, in words, with
    no seat names or state tokens in the sentence he reads."""
    from src.notifier import maybe_alert_data_quality

    result = {"data_status": {"tech": "ok", "smart_money": "market_wide_blind"}}
    with patch("src.notifier.send_owner_alert", return_value=True) as alert:
        fired = maybe_alert_data_quality(result, mode="morning")

    assert fired is True
    body = alert.call_args.args[0]
    assert "DATA QUALITY ALERT" in body
    assert "read none of the wider market's insider filings" in body
    # The raw pair is kept for the log line only, beneath the plain words.
    assert "smart_money=market_wide_blind" in body
    assert "no plain wording" not in body


def test_same_session_reuse_does_not_fire_data_quality_alert():
    """Intra reuse words are usable, not a paging event. Intra results
    currently omit data_status except on evidence_gate_skip, but the
    helper must not page if that dict later carries the reuse labels."""
    from src.notifier import maybe_alert_data_quality

    result = {
        "run_id": "intra_check-f0f27e08",
        "data_status": {
            "tech": "ok",
            "macro": "carried_from_morning",
            "news": "carried_from_morning",
            "earnings": "not_run_intraday",
        },
    }
    with patch("src.notifier.send_owner_alert") as alert:
        fired = maybe_alert_data_quality(result, mode="intra_check")

    assert fired is False
    alert.assert_not_called()


def test_news_low_confidence_status_fires_a_standalone_alert():
    """`low_confidence` (news's self-reported-confidence override, see
    pipeline_stages.py) is a status value like any other non-ok/empty
    value — this alert is value-agnostic by design, so a new status
    should reach it with no changes here. Asserted explicitly so a
    future refactor of `evidence_gate.counts_as_degraded` can't silently
    special-case it back out."""
    from src.notifier import maybe_alert_data_quality

    result = {
        "run_id": "run-lowconf",
        "data_status": {"tech": "ok", "news": "low_confidence", "macro": "ok"},
    }
    with patch("src.notifier.send_owner_alert", return_value=True) as alert:
        fired = maybe_alert_data_quality(result, mode="morning")

    assert fired is True
    body = alert.call_args.args[0]
    assert "news=low_confidence" in body


def test_clean_data_status_does_not_alert():
    from src.notifier import maybe_alert_data_quality

    result = {
        "run_id": "run-clean",
        "data_status": {"tech": "ok", "news": "ok", "macro": "ok", "earnings": "ok", "smart_money": "empty"},
    }
    with patch("src.notifier.send_owner_alert") as alert:
        fired = maybe_alert_data_quality(result, mode="morning")

    assert fired is False
    alert.assert_not_called()


def test_low_confidence_tech_does_not_fire_standalone_alert():
    """2026-09-04: a fully-resolved tech batch with a self-reported
    low-conviction read is a real, distinct signal (see pipeline_stages.py's
    'ok' -> 'low_confidence' downgrade) but deliberately does NOT page the
    owner the same way a failed/silent seat does — it still reaches the
    bundled session-summary 'degraded:' line and the RiskStage data_degraded
    advisory, just not this standalone alert."""
    from src.notifier import maybe_alert_data_quality

    result = {
        "run_id": "run-lowconf",
        "data_status": {"tech": "low_confidence", "news": "ok", "macro": "ok"},
    }
    with patch("src.notifier.send_owner_alert") as alert:
        fired = maybe_alert_data_quality(result, mode="morning")

    assert fired is False
    alert.assert_not_called()


def test_low_confidence_tech_does_not_mask_a_real_failure_alert():
    """low_confidence is excluded specifically, not the whole 'bad' set —
    a genuinely failed seat alongside a low-confidence one still pages."""
    from src.notifier import maybe_alert_data_quality

    result = {
        "run_id": "run-mixed",
        "data_status": {"tech": "low_confidence", "news": "failed"},
    }
    with patch("src.notifier.send_owner_alert", return_value=True) as alert:
        fired = maybe_alert_data_quality(result, mode="morning")

    assert fired is True
    body = alert.call_args.args[0]
    assert "news=failed" in body
    assert "tech=low_confidence" not in body


def test_low_confidence_pages_normally_for_seats_other_than_tech():
    """The exemption is per-seat, not a bare string match: news/macro's
    `low_confidence` means the WHOLE report is low-confidence (not one
    symbol out of many resolved, tech's case) and must still page — proves
    the seat-specific exemption dict, not a global `"low_confidence"`
    exclusion, is what's actually in effect."""
    from src.notifier import maybe_alert_data_quality

    result = {
        "run_id": "run-news-lowconf",
        "data_status": {"tech": "ok", "news": "low_confidence", "macro": "ok"},
    }
    with patch("src.notifier.send_owner_alert", return_value=True) as alert:
        fired = maybe_alert_data_quality(result, mode="morning")

    assert fired is True
    body = alert.call_args.args[0]
    assert "news=low_confidence" in body


def test_multiple_bad_seats_are_all_named_in_one_alert():
    from src.notifier import maybe_alert_data_quality

    result = {
        "run_id": "run-multi",
        "data_status": {"tech": "partial", "news": "parse_error", "macro": "ok"},
    }
    with patch("src.notifier.send_owner_alert", return_value=True) as alert:
        fired = maybe_alert_data_quality(result, mode="close")

    assert fired is True
    body = alert.call_args.args[0]
    assert "news=parse_error" in body
    assert "tech=partial" in body


def test_low_confidence_macro_status_fires_the_same_standalone_alert():
    """2026-09-04: `data_status["macro"]` can now be "low_confidence" (a
    technically clean call the model itself rates low-confidence — see
    pipeline_stages.py). Deliberately routed through this SAME existing
    alert path rather than a new one: `maybe_alert_data_quality` already
    treats anything but "ok"/"empty" as bad, so no new plumbing was needed,
    and this condition is expected to be rare in production (the model's
    daily-indicator staleness gate for `confidence` sits at 3 days, comfortably
    above FRED's real ~2-day lag — unlike the unrelated, already-tracked
    `regime_shift` 1-day gate that fires on 52% of runs) so it should not
    compound that existing alert volume."""
    from src.notifier import maybe_alert_data_quality

    result = {
        "run_id": "run-lowconf",
        "data_status": {
            "tech": "ok",
            "news": "ok",
            "macro": "low_confidence",
            "earnings": "ok",
            "smart_money": "empty",
        },
    }
    with patch("src.notifier.send_owner_alert", return_value=True) as alert:
        fired = maybe_alert_data_quality(result, mode="morning")

    assert fired is True
    body = alert.call_args.args[0]
    assert "macro=low_confidence" in body


def test_missing_or_malformed_result_never_raises():
    from src.notifier import maybe_alert_data_quality

    with patch("src.notifier.send_owner_alert") as alert:
        assert maybe_alert_data_quality(None, mode="morning") is False
        assert maybe_alert_data_quality({}, mode="morning") is False
        assert maybe_alert_data_quality({"data_status": "not-a-dict"}, mode="morning") is False

    alert.assert_not_called()


def test_macro_release_overdue_status_fires_a_standalone_alert():
    """`release_overdue` is the macro seat's new status for an expected FRED
    print that did not arrive (a publication or fetch failure, not normal
    release timing — see src/data/macro.py::SeriesFreshness). It replaced a
    calendar-day staleness gate that could never be satisfied, and unlike
    that gate it describes a real problem, so it must page like any other
    non-ok/empty value rather than being exempted as expected noise."""
    from src.notifier import maybe_alert_data_quality

    result = {
        "run_id": "run-overdue1",
        "data_status": {"tech": "ok", "macro": "release_overdue"},
    }
    with patch("src.notifier.send_owner_alert", return_value=True) as alert:
        fired = maybe_alert_data_quality(result, mode="morning")

    assert fired is True
    body = alert.call_args.args[0]
    assert "macro=release_overdue" in body


# === 2026-09-17 scan-first redesign: structural markup survives escaping ===
#
# src/trader_feed.py's formatters embed a fixed, small set of literal HTML
# tags (`<b>`, `<blockquote expandable>`) in otherwise plain text. `send()`
# still must `html.escape()` everything else — PM/risk free text is full of
# '&', tickers can carry punctuation, and an unescaped '<'/'>' from that
# free text must not corrupt the message or get it silently rendered as
# real markup. `preserve_structural_markup=True` is the opt-in that lets
# ONLY those four fixed strings survive; everything else, including a
# stray '<'/'>'/'&' the caller never intended as a tag, is still escaped.


def test_preserved_markup_survives_while_surrounding_text_is_escaped():
    notifier = TelegramNotifier(token="t", chat_id="c")
    text = (
        "<b>✅ DONE</b>\n"
        "   • SHORT FLNC (Fluence Energy) — Blocked by desk safety check "
        "(not the broker): stop $9.66 is 24% from price $7.79\n"
        "<b>DETAILS</b>\n"
        "<blockquote expandable>AT&T's <thesis> was 'strong' & 24% > 20%</blockquote>"
    )
    payload = notifier._build_payload(text, preserve_structural_markup=True)
    body = payload["text"]

    # The four fixed structural tags survive verbatim — real Telegram
    # markup, not escaped text.
    assert "<b>✅ DONE</b>" in body
    assert "<b>DETAILS</b>" in body
    assert body.count("<blockquote expandable>") == 1
    assert body.count("</blockquote>") == 1

    # Everything else inside/around them is still escaped — a stray '<',
    # '>', or '&' in free text can never inject markup of its own.
    assert "AT&amp;T&#x27;s &lt;thesis&gt;" in body
    assert "&#x27;strong&#x27;" in body
    assert "& 24" not in body  # the bare '&' before "24%" must be escaped
    assert "&amp; 24" in body
    assert "24% &gt; 20%" in body
    assert "<thesis>" not in body


def test_preserve_structural_markup_defaults_to_full_escaping():
    """Every OTHER `send()`/`_build_payload()` caller (cost-circuit alerts,
    `send_owner_alert`, scripts/*) must keep the historical "always fully
    escape" contract — a coincidental literal '<b>' in free text renders as
    visible text, never as markup, unless the caller opts in."""
    notifier = TelegramNotifier(token="t", chat_id="c")
    payload = notifier._build_payload("hello <b>world</b> & co")
    assert payload["text"] == "hello &lt;b&gt;world&lt;/b&gt; &amp; co"


def test_send_with_preserve_structural_markup_ships_the_tag_on_the_wire():
    """End-to-end through `send()` (not `_build_payload` directly) — the
    real path src/trader_feed.py's callers use (main.py, src/scheduler.py,
    both pass `preserve_structural_markup=True`)."""
    notifier = TelegramNotifier(token="t", chat_id="c")
    with patch("src.notifier.requests.post") as mock_post:
        mock_post.return_value = MagicMock(status_code=200, raise_for_status=lambda: None)
        assert (
            notifier.send(
                "<b>hi</b> & bye",
                preserve_structural_markup=True,
            )
            is True
        )
    body = mock_post.call_args.kwargs["json"]["text"]
    assert body == "<b>hi</b> &amp; bye"


def test_send_without_the_flag_still_fully_escapes_a_literal_tag():
    notifier = TelegramNotifier(token="t", chat_id="c")
    with patch("src.notifier.requests.post") as mock_post:
        mock_post.return_value = MagicMock(status_code=200, raise_for_status=lambda: None)
        assert notifier.send("<b>hi</b> & bye") is True
    body = mock_post.call_args.kwargs["json"]["text"]
    assert body == "&lt;b&gt;hi&lt;/b&gt; &amp; bye"


def test_data_quality_alert_speaks_in_words_and_keeps_the_raw_pair_labelled():
    """Board item 89 clarity defect: the alert named internal components
    ("macro=failed"). The seat and its state are now said in words, what
    it means for the owner is stated, and the raw pair is kept beneath,
    labelled as a machine record."""
    from src.notifier import maybe_alert_data_quality

    result = {"run_id": "run-dq", "data_status": {"macro": "failed", "tech": "partial", "sector": "brand_new_state"}}
    with patch("src.notifier.send_owner_alert") as alert:
        assert maybe_alert_data_quality(result, mode="morning")
    body = alert.call_args[0][0]
    assert "DATA QUALITY ALERT" in body
    assert "the market-backdrop research did not return an answer" in body
    assert "the chart research returned only part of an answer" in body
    assert "no plain wording for (kept for the record: brand_new_state)" in body
    assert "WHAT THIS MEANS FOR YOU" in body
    assert "Machine record, kept for the log" in body
    assert "macro=failed, sector=brand_new_state, tech=partial" in body


def test_coverage_gap_banner_says_the_quantities_in_words():
    """ "NVDA(4/10)" was a bare fraction; it now reads as what it is."""
    result = {
        "status": "ok",
        "run_id": "r",
        "stop_coverage_gaps": [{"symbol": "NVDA", "held_qty": 10.0, "covered_qty": 4.0}],
    }
    msg = format_session_result("intra_check", result, 1.0)
    assert msg is not None
    assert "NVDA holding 10, stop covers 4" in msg
    assert "(4/10)" not in msg


# ===========================================================================
# notifier_sends: recording every outgoing message (2026-09-18)
#
# Before this, a successful send left no record at all — only failures
# (`logger.warning`) and rehearsal-suppressions (`logger.info`) were ever
# recorded, so "what did the desk actually tell the owner, and did it
# arrive" had no single answer. See `TelegramNotifier._record_send`.
# ===========================================================================


def _notifier_sends_rows(db_path, **where):
    import sqlite3

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        clause = ""
        args: list = []
        if where:
            clause = "WHERE " + " AND ".join(f"{k} = ?" for k in where)
            args = list(where.values())
        return [
            dict(r)
            for r in conn.execute(
                f"SELECT * FROM notifier_sends {clause} ORDER BY id",
                args,
            ).fetchall()
        ]
    finally:
        conn.close()


def test_successful_send_is_recorded(tmp_path, monkeypatch):
    """A send that reaches Telegram now leaves a durable row — previously
    nothing at all was recorded on the success path."""
    db_path = tmp_path / "data" / "quant_agent.db"
    monkeypatch.setattr("src.notifier._DB_PATH", db_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat")
    monkeypatch.delenv("TELEGRAM_DISABLED", raising=False)
    n = TelegramNotifier()

    with patch("src.notifier.requests.post") as mock_post:
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_post.return_value = mock_response
        assert n.send("morning report body", kind="morning", run_id="run-1") is True

    rows = _notifier_sends_rows(db_path)
    assert len(rows) == 1
    assert rows[0]["kind"] == "morning"
    assert rows[0]["status"] == "sent"
    assert rows[0]["run_id"] == "run-1"
    assert rows[0]["text"] == "morning report body"
    assert rows[0]["detail"] is None


def test_failed_send_is_recorded_and_distinguishable(tmp_path, monkeypatch):
    """A failed send is recorded as 'failed', with a reason, distinct from
    a successful 'sent' row — same query, different status."""
    db_path = tmp_path / "data" / "quant_agent.db"
    monkeypatch.setattr("src.notifier._DB_PATH", db_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat")
    monkeypatch.delenv("TELEGRAM_DISABLED", raising=False)
    n = TelegramNotifier()

    with patch("src.notifier.requests.post") as mock_post:
        mock_post.side_effect = requests.ConnectionError("DNS hiccup")
        assert n.send("close report body", kind="close") is False

    rows = _notifier_sends_rows(db_path)
    assert len(rows) == 4
    assert rows[0]["kind"] == "close"
    assert rows[0]["status"] == "failed"
    assert rows[0]["text"] == "close report body"
    assert "DNS hiccup" in rows[0]["detail"]


def test_rehearsal_suppressed_send_is_recorded_as_suppressed(tmp_path, monkeypatch):
    """A rehearsal-suppressed send is recorded too, as its own status — not
    conflated with a real send or a real failure."""
    db_path = tmp_path / "data" / "quant_agent.db"
    monkeypatch.setattr("src.notifier._DB_PATH", db_path)
    monkeypatch.setattr("src.notifier._REHEARSAL_MODE", True)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat")
    monkeypatch.delenv("TELEGRAM_DISABLED", raising=False)
    n = TelegramNotifier()

    with patch("src.notifier.requests.post") as mock_post:
        assert not n.send("PAID ANALYSIS SUSPENDED", kind="owner_alert")
        mock_post.assert_not_called()

    rows = _notifier_sends_rows(db_path)
    assert len(rows) == 1
    assert rows[0]["status"] == "suppressed"
    assert rows[0]["kind"] == "owner_alert"
    assert rows[0]["text"] == "PAID ANALYSIS SUSPENDED"


def test_recorded_output_never_contains_token_or_chat_id(tmp_path, monkeypatch):
    """The token and chat id must never land in the durable record — same
    guarantee `_redact` already gives the failure log, extended to cover
    a case where a caller's free-text message happens to embed either
    value (e.g. a rationale that quotes a URL fragment)."""
    db_path = tmp_path / "data" / "quant_agent.db"
    monkeypatch.setattr("src.notifier._DB_PATH", db_path)
    token = "9999999:SENTINELTOKEN"
    chat_id = "424242424242"
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", token)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", chat_id)
    monkeypatch.delenv("TELEGRAM_DISABLED", raising=False)
    n = TelegramNotifier()

    # Successful send: message body itself happens to mention both values.
    with patch("src.notifier.requests.post") as mock_post:
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_post.return_value = mock_response
        assert (
            n.send(
                f"debug: token={token} chat={chat_id}",
                kind="generic",
            )
            is True
        )

    # Failed send: the exception text (as `requests` produces it) embeds
    # the token in the request URL — the exact leak `_redact` exists for.
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    with patch("src.notifier.requests.post") as mock_post:
        bad = MagicMock()
        bad.raise_for_status.side_effect = requests.HTTPError(f"401 Client Error: Unauthorized for url: {url}")
        mock_post.return_value = bad
        assert n.send("second message", kind="generic") is False

    rows = _notifier_sends_rows(db_path)
    assert len(rows) == 5
    dump = str(rows)
    assert token not in dump
    assert chat_id not in dump
    assert "<redacted>" in dump


def test_truncated_send_records_the_delivered_text_not_the_original(tmp_path, monkeypatch):
    """2026-09-24: `send()` truncates oversized text inside `_build_payload`
    before it goes on the wire, but used to record the ORIGINAL `text` in
    `notifier_sends` regardless — so the durable record differed from what
    Telegram actually delivered, with no marker a truncation happened at
    all. The recorded row must now match what was actually sent."""
    db_path = tmp_path / "data" / "quant_agent.db"
    monkeypatch.setattr("src.notifier._DB_PATH", db_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat")
    monkeypatch.delenv("TELEGRAM_DISABLED", raising=False)
    n = TelegramNotifier()
    oversized = "x" * (n.MAX_MESSAGE_CHARS + 500)

    with patch("src.notifier.requests.post") as mock_post:
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_post.return_value = mock_response
        assert n.send(oversized, kind="generic") is True

    delivered = mock_post.call_args.kwargs["json"]["text"]
    rows = _notifier_sends_rows(db_path)
    assert len(rows) == 1
    # The record must match what actually went on the wire...
    assert rows[0]["text"] == delivered
    # ...which is shorter than (not equal to) the original oversized text.
    assert len(rows[0]["text"]) < len(oversized)
    assert rows[0]["text"] != oversized
    assert "[...truncated]" in rows[0]["text"]


def test_recording_failure_does_not_prevent_the_send(tmp_path, monkeypatch):
    """A recording bug (bad DB path, locked file, whatever) must never
    stop the message from sending — the notifier is a best-effort side
    channel and recording is strictly additive to it."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat")
    monkeypatch.delenv("TELEGRAM_DISABLED", raising=False)
    n = TelegramNotifier()

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(n, "_record_send", boom)

    with patch("src.notifier.requests.post") as mock_post:
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_post.return_value = mock_response
        # _record_send itself would raise if called directly; send() must
        # not call it in a way that can propagate that.
        assert n.send("still goes out") is True


def test_recording_failure_inside_record_send_is_swallowed(tmp_path, monkeypatch):
    """Even without mocking `_record_send` away: if the DB write itself
    raises (unreadable path, corrupt file, permissions), `_record_send`
    swallows it and the caller never sees an exception."""
    # Point at a path that cannot be opened as a SQLite DB (a directory).
    bad_path = tmp_path / "not_a_db"
    bad_path.mkdir()
    monkeypatch.setattr("src.notifier._DB_PATH", bad_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat")
    monkeypatch.delenv("TELEGRAM_DISABLED", raising=False)
    n = TelegramNotifier()

    with patch("src.notifier.requests.post") as mock_post:
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_post.return_value = mock_response
        assert n.send("still goes out despite bad db") is True


def test_send_document_success_and_failure_are_recorded(tmp_path, monkeypatch):
    """send_document (the daily P&L CSV path) gets the same treatment as
    send(): success and failure both recorded, distinguishable, and the
    file bytes themselves are never stored — only the caption/filename."""
    db_path = tmp_path / "data" / "quant_agent.db"
    monkeypatch.setattr("src.notifier._DB_PATH", db_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat")
    monkeypatch.delenv("TELEGRAM_DISABLED", raising=False)
    n = TelegramNotifier()

    with patch("src.notifier.requests.post") as mock_post:
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_post.return_value = mock_response
        assert n.send_document(b"a,b\n1,2\n", "pnl.csv", caption="daily P&L") is True

    with patch("src.notifier.requests.post") as mock_post:
        mock_post.side_effect = requests.ConnectionError("timeout")
        assert n.send_document(b"a,b\n1,2\n", "pnl.csv", caption="daily P&L") is False

    rows = _notifier_sends_rows(db_path)
    assert len(rows) == 2
    assert rows[0]["status"] == "sent"
    assert rows[1]["status"] == "failed"
    for row in rows:
        assert row["kind"] == "document"
        assert "pnl.csv" in row["text"]
        assert "daily P&L" in row["text"]
        # CSV bytes must never be stored, only the caption/filename.
        assert "a,b" not in row["text"]


def test_owner_alert_is_recorded_with_its_own_kind(monkeypatch, tmp_path):
    """send_owner_alert routes through send(); its record should say
    'owner_alert', not the generic default, so it's distinguishable from
    routine session-result messages in the same table."""
    from src.notifier import send_owner_alert

    db_path = tmp_path / "data" / "quant_agent.db"
    monkeypatch.setattr("src.notifier._DB_PATH", db_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat")
    monkeypatch.delenv("TELEGRAM_DISABLED", raising=False)

    with patch("src.notifier.requests.post") as mock_post:
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_post.return_value = mock_response
        assert send_owner_alert("naked position: NVDA") is True

    rows = _notifier_sends_rows(db_path)
    assert len(rows) == 1
    assert rows[0]["kind"] == "owner_alert"
    assert rows[0]["status"] == "sent"


def test_last_sends_of_each_kind_query_matches_notifier_last_sends_script(tmp_path, monkeypatch):
    """Sanity check on the exact query `scripts/notifier_last_sends.py`
    runs: for two kinds with several rows each, it must return only the
    single latest row per kind."""
    import sqlite3

    db_path = tmp_path / "data" / "quant_agent.db"
    monkeypatch.setattr("src.notifier._DB_PATH", db_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat")
    monkeypatch.delenv("TELEGRAM_DISABLED", raising=False)
    n = TelegramNotifier()

    with patch("src.notifier.requests.post") as mock_post:
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_post.return_value = mock_response
        n.send("morning v1", kind="morning")
        n.send("evening v1", kind="evening")
        n.send("morning v2 (latest)", kind="morning")

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM notifier_sends WHERE id IN (SELECT MAX(id) FROM notifier_sends GROUP BY kind) ORDER BY kind"
        ).fetchall()
    finally:
        conn.close()

    by_kind = {r["kind"]: r["text"] for r in rows}
    assert by_kind == {"morning": "morning v2 (latest)", "evening": "evening v1"}


# === Smart-money seat label must track the real congress_enabled switch ===
# `config.smart_money.congress_enabled` (src/config.py) was switched on
# 2026-09-20 per owner ruling. Telegram text must never claim the desk
# reads congressional trading disclosures while that switch is off, and
# must name it once it is genuinely on.


def test_smart_money_seat_label_off():
    from src.notifier import _smart_money_seat_label

    label = _smart_money_seat_label(False)
    assert "congressional" not in label
    assert label == "the insider-trading feed"


def test_smart_money_seat_label_on():
    from src.notifier import _smart_money_seat_label

    label = _smart_money_seat_label(True)
    assert label == "the insider-and-congressional-trading feed"


def test_seat_words_smart_money_reflects_live_switch(monkeypatch):
    """seat_words() must derive its wording from the actual runtime switch,
    not a fixed string — this is what src.notifier._congress_enabled_now
    is for."""
    import src.notifier as notifier

    monkeypatch.setattr(notifier, "_congress_enabled_now", lambda: False)
    assert notifier.seat_words("smart_money") == "the insider-trading feed"

    monkeypatch.setattr(notifier, "_congress_enabled_now", lambda: True)
    assert notifier.seat_words("smart_money") == "the insider-and-congressional-trading feed"


def test_seat_words_smart_money_matches_repo_default_config():
    """Revert-and-fail: without any mocking, seat_words("smart_money") must
    match the repo's OWN config default (congress_enabled=True since the
    2026-09-20 owner ruling), proving the wording is actually derived from
    the switch and not hard-coded back to omitting congress unconditionally."""
    from src.config import SmartMoneyConfig
    from src.notifier import seat_words

    assert SmartMoneyConfig().congress_enabled is True
    assert seat_words("smart_money") == "the insider-and-congressional-trading feed"


def test_congress_enabled_now_defaults_false_on_load_failure(monkeypatch):
    """A wording helper must never raise or break an alert just because
    settings.yaml couldn't be read -- and must conservatively assume the
    switch is OFF on that failure (never claim a feed is running when the
    real state couldn't be confirmed), regardless of the field's own live
    default (`True` since the 2026-09-20 owner ruling).

    `_congress_enabled_now` reads `config/settings.yaml` directly, not
    through `src.config.load_config`, so the failure has to be induced on
    the actual read path it uses (`yaml.safe_load`), not on the unrelated
    loader.
    """
    import src.notifier as notifier

    def _boom(*_args, **_kwargs):
        raise RuntimeError("no config here")

    monkeypatch.setattr("yaml.safe_load", _boom)
    assert notifier._congress_enabled_now() is False


# === malformed-number guard (garbled figures in LLM narrative prose) ===
#
# The bug this catches: a deterministic number is validated before it ever
# reaches an owner message, but free-text PM/risk/analyst prose is never
# reworded — so a garbled figure the LLM wrote inside its own reasoning
# (the real incident: "$10,21.36") went straight through. These tests prove
# the guard catches that shape and its close siblings, and — just as
# importantly — never touches a real desk figure.


def test_finds_the_actual_reported_defect():
    from src.notifier import _find_malformed_numeric_tokens

    found = _find_malformed_numeric_tokens("total exposure came to $10,21.36 today")
    assert ("$10,21.36", "malformed thousands separator") in found


def test_finds_malformed_thousands_separator_without_dollar_sign():
    from src.notifier import _find_malformed_numeric_tokens

    found = _find_malformed_numeric_tokens("that's roughly 10,21.36 shares")
    assert any(tok == "10,21.36" for tok, _reason in found)


def test_finds_multiple_decimal_points():
    from src.notifier import _find_malformed_numeric_tokens

    found = _find_malformed_numeric_tokens("price ticked between 12.34.56 all day")
    assert ("12.34.56", "multiple decimal points") in found


def test_finds_dollar_amount_with_wrong_decimal_places():
    from src.notifier import _find_malformed_numeric_tokens

    found = _find_malformed_numeric_tokens("cost basis was $10.567 per share")
    assert ("$10.567", "dollar amount without exactly 2 decimal places") in found


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, "unavailable"),
        (0.0, "$0.00"),
        (0.0049, "under a cent"),
        (0.0102, "$0.01"),
        (0.9, "$0.90"),
        (2.75, "$2.75"),
        (10.567, "$10.57"),
    ],
)
def test_format_settled_money_matches_expected_words(value, expected):
    """`format_settled_money` is `src/cost_circuit.py`'s replacement for the
    `f"${x:.4f}"` formatting that produced the exact malformed tokens
    ("$0.0049", "$0.0000") the 2026-09-29 log shows the notifier redacting
    out of five straight owner-facing alerts."""
    from src.notifier import format_settled_money

    assert format_settled_money(value) == expected


@pytest.mark.parametrize("value", [0.0, 0.0049, 0.0102, 0.9, 2.75, 10.567, 123456.789])
def test_format_settled_money_never_produces_a_malformed_token(value):
    """None of `format_settled_money`'s outputs may ever be something
    `_find_malformed_numeric_tokens` would flag -- that guard is the
    backstop, not the fix; the generator itself must stop producing the
    bad shape."""
    from src.notifier import _find_malformed_numeric_tokens, format_settled_money

    rendered = format_settled_money(value)
    assert _find_malformed_numeric_tokens(f"cost: {rendered} today") == []


@pytest.mark.parametrize(
    "sentence",
    [
        "closed the position for $1,021.36 net of fees",
        "up 14.6% on the day, mostly momentum",
        "sized at 2.0x the usual allocation",
        "expects a pullback in the 5-15 day window",
        "raised roughly $10M from the sweep account",
        "as of 2026-09-24 the book is fully hedged",
        "filled at 12:51 after the open",
        "added 0.3155 shares to round out the lot",
        "trimmed CRM, now $492.30 in realized P&L",
        "core position is 14,600 shares, unchanged",
    ],
)
def test_never_flags_a_real_desk_figure(sentence):
    """False-positive battery: every one of these is a genuine, correctly
    formatted figure the desk writes routinely. None may be touched."""
    from src.notifier import _find_malformed_numeric_tokens

    assert _find_malformed_numeric_tokens(sentence) == []


def test_redact_replaces_only_the_malformed_token_and_leaves_the_rest():
    from src.notifier import _redact_malformed_numbers

    text = "PM view: sized at 2.0x, but total came to $10,21.36 net of fees."
    redacted = _redact_malformed_numbers(text)
    assert "$10,21.36" not in redacted
    assert "[number garbled" in redacted
    assert "2.0x" in redacted
    assert "net of fees" in redacted


def test_redact_never_touches_clean_text():
    from src.notifier import _redact_malformed_numbers

    text = "closed CRM for $1,021.36, up 14.6% on the day"
    assert _redact_malformed_numbers(text) == text


def test_redact_logs_the_original_garbled_token_for_diagnosis(caplog):
    from src.notifier import _redact_malformed_numbers

    with caplog.at_level("WARNING"):
        _redact_malformed_numbers("total exposure came to $10,21.36 today")
    assert any("10,21.36" in rec.message for rec in caplog.records)


def test_send_does_not_judge_numbers_the_guard_runs_at_model_prose(monkeypatch):
    """`send()` cannot tell desk-formatted text from model prose, and running
    the guard there garbled real desk figures ("stop covers 3, $164.61").
    The guard now runs where model prose enters a message (trader feed
    `_clip`) — see tests/test_number_guard_scope.py — so `send()` itself
    passes the text through unjudged."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "BOT_TOK")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "CHAT_ID")
    monkeypatch.delenv("TELEGRAM_DISABLED", raising=False)
    n = TelegramNotifier()

    with patch("src.notifier.requests.post") as mock_post:
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_post.return_value = mock_response

        ok = n.send("PM view: total exposure came to $10,21.36 today")

    assert ok is True
    sent = mock_post.call_args.kwargs["json"]["text"]
    assert "10,21.36" in sent
    assert "number garbled" not in sent


def test_redact_raw_exception_text_replaces_only_the_exception_token():
    """Board item 89 defect 5: a `reason=f"... raised: {exc}"` style string
    built upstream (e.g. `src/coverage_watchdog.py`) must not reach the
    owner with the raw exception text intact."""
    from src.notifier import _redact_raw_exception_text

    text = (
        "  AAPL: holding 10.0000 — snapshot_protective_stops raised: ConnectionError('Connection timed out after 5s')"
    )
    redacted = _redact_raw_exception_text(text)
    assert "ConnectionError" not in redacted
    assert "Connection timed out" not in redacted
    assert "internal error" in redacted
    assert "AAPL: holding 10.0000" in redacted


def test_redact_raw_exception_text_strips_a_traceback():
    from src.notifier import _redact_raw_exception_text

    text = (
        "risk manager raised:\n"
        "Traceback (most recent call last):\n"
        '  File "src/pipeline.py", line 1234, in _run\n'
        "    raise ValueError('bad state')\n"
        "ValueError: bad state"
    )
    redacted = _redact_raw_exception_text(text)
    assert "Traceback" not in redacted
    assert "bad state" not in redacted
    assert "src/pipeline.py" not in redacted
    assert "internal error" in redacted


def test_redact_raw_exception_text_never_touches_clean_owner_text():
    """False-positive guard: ordinary owner prose — prices, symbols, plain-
    English reasons — must pass through unchanged."""
    from src.notifier import _redact_raw_exception_text

    text = (
        "closed CRM for $1,021.36, up 14.6% on the day — the risk check "
        "turned the plan down because the position was already at its cap."
    )
    assert _redact_raw_exception_text(text) == text


def test_redact_raw_exception_text_logs_the_original_for_diagnosis(caplog):
    from src.notifier import _redact_raw_exception_text

    with caplog.at_level("WARNING"):
        _redact_raw_exception_text("placement raised (sqlite3.OperationalError: no such table: agent_logs)")
    assert any("OperationalError" in rec.message for rec in caplog.records)


def test_send_redacts_raw_exception_text_before_it_reaches_telegram(monkeypatch):
    """End-to-end: `send()` is the single chokepoint every owner-facing
    message passes through — a raw exception string must be sanitized here
    regardless of which upstream reason-string builder produced it."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "BOT_TOK")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "CHAT_ID")
    monkeypatch.delenv("TELEGRAM_DISABLED", raising=False)
    n = TelegramNotifier()

    with patch("src.notifier.requests.post") as mock_post:
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_post.return_value = mock_response

        ok = n.send(
            "PROTECTIVE STOP UNREADABLE\n"
            "  AAPL: holding 10.0000 — snapshot_protective_stops raised: "
            "ConnectionError('timed out')"
        )

    assert ok is True
    sent = mock_post.call_args.kwargs["json"]["text"]
    assert "ConnectionError" not in sent
    assert "timed out" not in sent
    assert "internal error" in sent


def test_send_does_not_alter_a_normal_owner_message(monkeypatch):
    """Sanity check for the same chokepoint: a normal message with no
    malformed numbers and no raw exception text passes through unchanged."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "BOT_TOK")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "CHAT_ID")
    monkeypatch.delenv("TELEGRAM_DISABLED", raising=False)
    n = TelegramNotifier()

    plain = "Closing review: bought CRM at $271.36, up 1.2% on the day."
    with patch("src.notifier.requests.post") as mock_post:
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_post.return_value = mock_response

        ok = n.send(plain)

    assert ok is True
    sent = mock_post.call_args.kwargs["json"]["text"]
    assert plain in sent


def test_the_global_mute_records_every_message_it_drops(tmp_path, monkeypatch):
    """Item 211 defect 3. `TELEGRAM_DISABLED` used to drop the message before
    anything was written down, so nothing that would have fired while the
    desk was muted exists anywhere — the owner cannot see what he is missing
    and neither can the desk. Nothing is un-muted and nothing is sent; a
    muted message must simply leave a durable trace.
    """
    import sqlite3

    from src import notifier as notifier_mod

    db_path = tmp_path / "quant_agent.db"
    monkeypatch.setattr(notifier_mod, "_DB_PATH", db_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "y")
    monkeypatch.setenv("TELEGRAM_DISABLED", "1")

    n = notifier_mod.TelegramNotifier()
    assert n.muted is True
    assert n.enabled is False

    assert was_suppressed(out := n.send("AAA has no protective stop", kind="owner_alert", symbols=["AAA"])) and not out

    conn = sqlite3.connect(str(db_path))
    try:
        rows = list(conn.execute("SELECT kind, status, text, detail, timestamp FROM notifier_sends"))
    finally:
        conn.close()
    assert len(rows) == 1, "the muted message left no trace"
    kind, status, text, detail, timestamp = rows[0]
    assert kind == "owner_alert"
    assert status == "muted"
    assert "AAA" in text
    assert "AAA" in (detail or "")
    assert timestamp
