"""Over-long owner message keeps its cost/time footer and a Mission Control line (19 Oct 2026)."""

from src.notifier.transport import TelegramNotifier

FOOTER = "\U0001f9fe AI cost $0.10 · took 2m"
LINK = "https://mc.example/zzz"


def _notifier():
    return TelegramNotifier(token="t", chat_id="1", mission_control_url=LINK)


def _text(passed_count):
    passed = ", ".join(f"ZZZ{i}" for i in range(passed_count))
    details = "<blockquote expandable>" + "ZZZ reasoning line\n" * 300 + "</blockquote>"
    return f"<b>ZZZ summary</b>\n\nPassed on: {passed}\n\n{details}\n\n{FOOTER}"


def _payload(text):
    return _notifier()._build_payload(text, preserve_structural_markup=True)["text"]


def test_long_message_keeps_footer_and_mission_control_line():
    out = _payload(_text(900))
    assert len(out) <= TelegramNotifier.MAX_MESSAGE_CHARS
    assert out.count("<blockquote") == 0
    assert FOOTER in out
    assert "Mission Control" in out
    assert LINK in out


def test_short_message_is_unchanged():
    text = f"<b>ZZZ summary</b>\n\nPassed on: ZZZ1\n\n{FOOTER}"
    assert _payload(text) == text + f'\n\n<a href="{LINK}">{TelegramNotifier.DEFAULT_LINK_LABEL}</a>'


def test_real_tail_shape_keeps_cost_line_and_credit_line():
    cost = "\U0001f9fe AI cost: $0.15 · took 2m 27s"
    credit = "AI credit: $19.12 left"
    passed = ", ".join(f"ZZZ{i}" for i in range(900))
    details = "<blockquote expandable>" + "ZZZ reasoning line\n" * 300 + "</blockquote>"
    text = f"<b>ZZZ summary</b>\n\nPassed on: {passed}\n\n{details}\n\n{cost}\n\n{credit}"
    out = _payload(text)
    assert len(out) <= TelegramNotifier.MAX_MESSAGE_CHARS
    assert cost in out
    assert credit in out
    assert "Mission Control" in out
