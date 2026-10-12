"""Request handling for the owner's switch. Standard library only.

A POST is accepted only when ALL of these hold, checked in this order:
  1. Origin equals the configured public origin (CSRF; a missing Origin is refused,
     and a Referer, if sent, must be on the same origin). No cookies are used.
  2. `Tailscale-User-Login` equals the configured owner login (set by Tailscale
     Serve for the signed-in tailnet user).
  3. `X-Owner-Token` matches the secret held only by the service user.
  4. The body is JSON `{"action": "stop" | "freeze" | "start"}` (else 400).
Every request that reaches a decision is logged with time, action and outcome;
token values are never logged.
"""

import hmac
import json
import logging
import os
import sqlite3
import stat
from dataclasses import dataclass
from pathlib import Path

from src.owner_flags import FREEZE, STOP, UNFREEZE
from src.owner_intents import raise_intent
from src.storage.schema.owner_intent_tables import apply

logger = logging.getLogger("owner_switch")

# Owner-facing word -> stored action string (src/owner_flags.py).
ACTIONS = {"stop": STOP, "freeze": FREEZE, "start": UNFREEZE}
MAX_BODY = 1024
MIN_TOKEN_LEN = 32
TOKEN_CREDENTIAL = "owner_switch_token"
STATIC = Path(__file__).resolve().parent / "static"
_PAGES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/switch.js": ("switch.js", "text/javascript; charset=utf-8"),
    "/switch.css": ("switch.css", "text/css; charset=utf-8"),
}

_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
        "form-action 'none'; frame-ancestors 'none'; base-uri 'none'"
    ),
}


class ConfigError(RuntimeError):
    """The service refuses to start rather than run half-configured."""


@dataclass(frozen=True)
class Settings:
    owner_login: str
    token: str
    origin: str
    db_path: str


@dataclass(frozen=True)
class Response:
    status: int
    body: bytes
    content_type: str = "application/json"

    def headers(self) -> dict:
        return {"Content-Type": self.content_type, "Content-Length": str(len(self.body)), **_SECURITY_HEADERS}


def _read_token(path: Path) -> str:
    st = path.stat()
    if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise ConfigError(f"token file {path} is readable by others: chmod 0600 it")
    token = path.read_text().strip()
    if len(token) < MIN_TOKEN_LEN:
        raise ConfigError(f"token in {path} is shorter than {MIN_TOKEN_LEN} characters")
    return token


def _default_db_path(root: Path) -> str:
    import yaml

    cfg = yaml.safe_load((root / "config" / "settings.yaml").read_text())
    return str(cfg["storage"]["db_path"])


def load_settings(env=None, root: Path | None = None) -> Settings:
    """Everything from the environment; nothing secret or personal is in the repo."""
    env = os.environ if env is None else env
    login = env.get("OWNER_SWITCH_OWNER_LOGIN", "").strip()
    origin = env.get("OWNER_SWITCH_ORIGIN", "").strip().rstrip("/")
    if not login:
        raise ConfigError("OWNER_SWITCH_OWNER_LOGIN is not set")
    if not origin.startswith("https://"):
        raise ConfigError("OWNER_SWITCH_ORIGIN must be the https:// address the phone opens")
    token_file = env.get("OWNER_SWITCH_TOKEN_FILE")
    if not token_file:
        token_file = str(Path.home() / "credentials" / TOKEN_CREDENTIAL)
    token = _read_token(Path(token_file))
    db_path = env.get("OWNER_SWITCH_DB_PATH") or _default_db_path(root or Path.cwd())
    return Settings(owner_login=login, token=token, origin=origin, db_path=db_path)


def _json(status: int, **payload) -> Response:
    return Response(status, json.dumps(payload).encode())


def _header(headers, name):
    value = headers.get(name)
    return value.strip() if isinstance(value, str) else None


def _refuse(status: int, action: str, reason: str) -> Response:
    logger.warning("owner switch REFUSED action=%s status=%s reason=%s", action, status, reason)
    return _json(status, ok=False, error=reason)


def _same_origin(origin, referer, expected) -> str | None:
    """None when the request comes from the switch's own page, else the refusal reason."""
    if not origin:
        return "no Origin header"
    if origin.rstrip("/") != expected:
        return "foreign Origin"
    if referer and not (referer == expected or referer.startswith(expected + "/")):
        return "foreign Referer"
    return None


def _parse_action(body: bytes) -> str | None:
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    word = data.get("action") if isinstance(data, dict) else None
    return word if isinstance(word, str) and word in ACTIONS else None


def _record(db_path: str, stored_action: str) -> int:
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        apply(conn)
        return raise_intent(conn, stored_action, reason="owner switch (phone)")
    finally:
        conn.close()


def handle_get(path: str) -> Response:
    """GET serves the page and its script; it never records anything."""
    if path not in _PAGES:
        return _json(404, ok=False, error="not found")
    name, ctype = _PAGES[path]
    return Response(200, (STATIC / name).read_bytes(), ctype)


def handle_post(settings: Settings, path: str, headers, body: bytes) -> Response:
    """Record one owner intent when, and only when, every check passes."""
    if path != "/action":
        return _refuse(404, "-", "not found")
    why = _same_origin(_header(headers, "Origin"), _header(headers, "Referer"), settings.origin)
    if why:
        return _refuse(403, "-", why)
    login = _header(headers, "Tailscale-User-Login")
    if not login or not hmac.compare_digest(login.encode(), settings.owner_login.encode()):
        return _refuse(403, "-", "identity missing or not the owner")
    token = _header(headers, "X-Owner-Token")
    if not token or not hmac.compare_digest(token.encode(), settings.token.encode()):
        return _refuse(403, "-", "token missing or wrong")
    ctype = (_header(headers, "Content-Type") or "").split(";")[0].strip().lower()
    if ctype != "application/json":
        return _refuse(400, "-", "body must be application/json")
    word = _parse_action(body)
    if word is None:
        return _refuse(400, "-", "action must be one of stop, freeze, start")
    try:
        intent_id = _record(settings.db_path, ACTIONS[word])
    except sqlite3.Error as exc:
        logger.error("owner switch FAILED action=%s reason=database error: %s", word, exc)
        return _json(503, ok=False, error="could not record; nothing changed, try again")
    logger.warning("owner switch ACCEPTED action=%s intent=%s", word, intent_id)
    return _json(200, ok=True, action=word, intent=intent_id, note="recorded; the desk acts on it at its next pickup")
