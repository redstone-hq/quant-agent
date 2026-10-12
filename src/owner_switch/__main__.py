"""Run the owner's switch: `python -m src.owner_switch` (loopback only; see docs/OWNER_SWITCH.md)."""

import logging
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from src.owner_switch.app import MAX_BODY, ConfigError, Response, Settings, handle_get, handle_post, load_settings

logger = logging.getLogger("owner_switch")
HOST = "127.0.0.1"  # never configurable: the only way in is Tailscale Serve
DEFAULT_PORT = 8801


def make_handler(settings: Settings):
    class Handler(BaseHTTPRequestHandler):
        server_version = "owner-switch"
        sys_version = ""

        def _send(self, resp: Response) -> None:
            self.send_response(resp.status)
            for name, value in resp.headers().items():
                self.send_header(name, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(resp.body)

        def do_GET(self):  # noqa: N802 - http.server naming
            self._send(handle_get(urlsplit(self.path).path))

        def do_POST(self):  # noqa: N802
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = -1
            if not 0 <= length <= MAX_BODY:
                logger.warning("owner switch REFUSED action=- status=413 reason=body length %s", length)
                self._send(Response(413, b'{"ok": false, "error": "body too large"}'))
                return
            body = self.rfile.read(length)
            self._send(handle_post(settings, urlsplit(self.path).path, self.headers, body))

        def _not_allowed(self):
            logger.warning("owner switch REFUSED action=- status=405 reason=method %s", self.command)
            self._send(Response(405, b'{"ok": false, "error": "method not allowed"}'))

        do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _not_allowed  # noqa: N815

        def log_message(self, fmt, *args):  # request line only; headers (token) never logged
            logger.info("%s %s", self.address_string(), fmt % args)

    return Handler


def serve(settings: Settings, port: int) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((HOST, port), make_handler(settings))


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        settings = load_settings()
    except (ConfigError, OSError, KeyError) as exc:
        logger.error("owner switch not started: %s", exc)
        return 2
    port = int(os.environ.get("OWNER_SWITCH_PORT", DEFAULT_PORT))
    server = serve(settings, port)
    logger.warning("owner switch listening on %s:%s for %s", HOST, port, settings.origin)
    try:
        server.serve_forever()
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
