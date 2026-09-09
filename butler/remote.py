"""Remote access (feature 19).

A lightweight, token-authenticated HTTP API (stdlib only) that exposes
Butler's core operations so a client (phone/script) can query the filesystem
without Telegram. Endpoints mirror the decider intents.

    GET  /status
    GET  /list?path=...
    GET  /find?q=...
    GET  /search?q=...
    GET  /resume
    GET  /dupes?root=...
    GET  /trash
    POST /organize   {path}        -> returns Plan (not applied)
    POST /trash      {paths:[...]}
    POST /recover    {ids:[]}
    POST /mkdir      {path}

Responses are JSON. A simple ``Bearer <token>`` header (or ``?token=``) is
required.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

try:
    from .core import build_container
except Exception:  # pragma: no cover - import-time fallback
    build_container = None


def make_handler(container: Any):
    cfg = container.cfg
    token = cfg.remote_token

    class Handler(BaseHTTPRequestHandler):
        def _check(self) -> bool:
            supplied = self.headers.get("Authorization", "")
            if supplied.startswith("Bearer "):
                supplied = supplied[7:]
            else:
                from urllib.parse import parse_qs, urlparse
                supplied = parse_qs(urlparse(self.path).query).get("token", [""])[0]
            return bool(token) and supplied == token

        def _send(self, obj: Any, code: int = 200) -> None:
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _path_args(self) -> dict[str, str]:
            from urllib.parse import parse_qs, urlparse
            return {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}

        def do_GET(self):  # noqa: N802
            if not self._check():
                return self._send({"error": "unauthorized"}, 401)
            from urllib.parse import urlparse
            route = urlparse(self.path).path.rstrip("/")
            args = self._path_args()
            result = container.remote_route("GET", route, args)
            self._send(result)

        def do_POST(self):  # noqa: N802
            if not self._check():
                return self._send({"error": "unauthorized"}, 401)
            from urllib.parse import urlparse
            route = urlparse(self.path).path.rstrip("/")
            try:
                length = int(self.headers.get("Content-Length", 0) or 0)
                payload = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                payload = {}
            result = container.remote_route("POST", route, payload)
            self._send(result)

        def log_message(self, *a):  # quiet
            return

    return Handler


class RemoteServer:
    def __init__(self, container: Any):
        self.container = container
        cfg = container.cfg
        self.httpd = ThreadingHTTPServer(
            ("0.0.0.0", cfg.remote_port), make_handler(container))
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.httpd.shutdown()
