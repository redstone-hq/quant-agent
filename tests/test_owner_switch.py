"""The owner switch records Stop / Freeze / Start only for the owner, with his token, from its own page."""

import http.client
import json
import os
import sqlite3
import threading

import pytest

from src.owner_flags import read_flags
from src.owner_intents import intake
from src.owner_switch.__main__ import serve
from src.owner_switch.app import ConfigError, load_settings

LOGIN = "owner@example.test"
ORIGIN = "https://switch.example.test"
TOKEN = "t" * 40


def _intent_count(db):
    if not os.path.exists(db):
        return 0
    conn = sqlite3.connect(db)
    try:
        return conn.execute("SELECT COUNT(*) FROM owner_intents").fetchone()[0]
    except sqlite3.OperationalError:
        return 0
    finally:
        conn.close()


@pytest.fixture
def switch(tmp_path):
    token_file = tmp_path / "token"
    token_file.write_text(TOKEN + "\n")
    token_file.chmod(0o600)
    db = str(tmp_path / "desk.db")
    env = {
        "OWNER_SWITCH_OWNER_LOGIN": LOGIN,
        "OWNER_SWITCH_ORIGIN": ORIGIN,
        "OWNER_SWITCH_TOKEN_FILE": str(token_file),
        "OWNER_SWITCH_DB_PATH": db,
    }
    server = serve(load_settings(env), 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]

    def call(method="POST", path="/action", body=None, **overrides):
        headers = {
            "Origin": ORIGIN,
            "Tailscale-User-Login": LOGIN,
            "X-Owner-Token": TOKEN,
            "Content-Type": "application/json",
        }
        for k, v in overrides.items():
            name = k.replace("_", "-")
            if v is None:
                headers.pop(name, None)
            else:
                headers[name] = v
        payload = json.dumps(body).encode() if body is not None else b""
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp.status, data

    yield call, db
    server.shutdown()
    server.server_close()


@pytest.mark.parametrize("login", [None, "", "someone-else@example.test", LOGIN + "x"])
def test_missing_or_wrong_identity_is_refused(switch, login):
    call, db = switch
    status, _ = call(body={"action": "stop"}, **{"Tailscale_User_Login": login})
    assert status == 403
    assert _intent_count(db) == 0


@pytest.mark.parametrize("token", [None, "", "wrong", TOKEN[:-1], TOKEN + "x"])
def test_missing_or_wrong_token_is_refused(switch, token):
    call, db = switch
    status, _ = call(body={"action": "stop"}, **{"X_Owner_Token": token})
    assert status == 403
    assert _intent_count(db) == 0


@pytest.mark.parametrize(
    "origin,referer",
    [(None, None), ("https://evil.example.test", None), (ORIGIN + ".evil.test", None), (ORIGIN, "https://evil.test/")],
)
def test_foreign_or_missing_origin_is_refused(switch, origin, referer):
    call, db = switch
    status, _ = call(body={"action": "stop"}, Origin=origin, Referer=referer)
    assert status == 403
    assert _intent_count(db) == 0


@pytest.mark.parametrize("path", ["/", "/switch.js", "/switch.css", "/action", "/action?action=stop", "/nope"])
def test_get_cannot_change_state(switch, path):
    call, db = switch
    status, _ = call("GET", path)
    assert status in (200, 404)
    assert _intent_count(db) == 0


def test_page_is_served_with_a_strict_policy(switch):
    call, _ = switch
    status, body = call("GET", "/")
    assert status == 200 and b"Owner switch" in body and b"/switch.js" in body


@pytest.mark.parametrize("body", [{"action": "buy"}, {"action": "STOP"}, {"action": ""}, {}, ["stop"], "stop"])
def test_any_other_action_is_a_bad_request(switch, body):
    call, db = switch
    status, _ = call(body=body)
    assert status == 400
    assert _intent_count(db) == 0


def test_other_methods_are_not_allowed(switch):
    call, db = switch
    for method in ("PUT", "DELETE", "PATCH"):
        assert call(method, body={"action": "stop"})[0] == 405
    assert _intent_count(db) == 0


@pytest.mark.parametrize(
    "steps,stopped,frozen",
    [
        (["stop"], True, True),
        (["freeze"], False, True),
        (["stop", "start"], False, False),
        (["freeze", "start"], False, False),
    ],
)
def test_valid_post_records_the_intent_and_the_desk_reader_sees_it(switch, steps, stopped, frozen):
    call, db = switch
    for step in steps:
        status, data = call(body={"action": step})
        assert status == 200, data
        assert json.loads(data)["ok"] is True
    # The switch only records; the desk's own pickup acts on it.
    assert read_flags(db).stopped is False and read_flags(db).frozen is False
    intake(db)
    flags = read_flags(db)
    assert (flags.stopped, flags.frozen) == (stopped, frozen)


def test_refuses_to_start_with_a_token_file_others_can_read(tmp_path):
    token_file = tmp_path / "token"
    token_file.write_text(TOKEN)
    token_file.chmod(0o644)
    env = {"OWNER_SWITCH_OWNER_LOGIN": LOGIN, "OWNER_SWITCH_ORIGIN": ORIGIN, "OWNER_SWITCH_TOKEN_FILE": str(token_file)}
    with pytest.raises(ConfigError):
        load_settings(env)


@pytest.mark.parametrize("missing", ["OWNER_SWITCH_OWNER_LOGIN", "OWNER_SWITCH_ORIGIN"])
def test_refuses_to_start_half_configured(tmp_path, missing):
    token_file = tmp_path / "token"
    token_file.write_text(TOKEN)
    token_file.chmod(0o600)
    env = {"OWNER_SWITCH_OWNER_LOGIN": LOGIN, "OWNER_SWITCH_ORIGIN": ORIGIN, "OWNER_SWITCH_TOKEN_FILE": str(token_file)}
    env.pop(missing)
    with pytest.raises(ConfigError):
        load_settings(env)


def test_token_value_is_never_logged(switch, caplog):
    call, _ = switch
    caplog.set_level("INFO", logger="owner_switch")
    call(body={"action": "stop"}, X_Owner_Token="wrong-guess-value")
    call(body={"action": "freeze"})
    text = caplog.text
    assert "REFUSED" in text and "ACCEPTED action=freeze" in text
    assert TOKEN not in text and "wrong-guess-value" not in text


def test_refuses_to_start_without_the_token_file(tmp_path):
    env = {
        "OWNER_SWITCH_OWNER_LOGIN": LOGIN,
        "OWNER_SWITCH_ORIGIN": ORIGIN,
        "OWNER_SWITCH_TOKEN_FILE": str(tmp_path / "absent"),
    }
    with pytest.raises(OSError):
        load_settings(env)
